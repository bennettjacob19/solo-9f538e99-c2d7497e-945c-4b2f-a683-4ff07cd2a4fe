"""基于 4 KiB 块的链式磁盘镜像存储。

存储布局（DATA_DIR）::

    baseline.raw            预置 raw 基线镜像（版本 1 的内容）
    current.json            指向最后一次*完整提交*版本的指针（原子替换）
    v000001/meta.json       版本 1（基线）元数据
    v000002/meta.json       版本 N 元数据（仅记录该批变动块）
    blocks/b<idx>_v<ver>    每个变动块单独落盘（块号左补零到 10 位）
    tmp/<ver>/...           提交过程中的暂存目录，崩溃后启动即清理

版本链规则：
* 每版 meta 只记录本批「覆盖」或「丢弃」的块；
* 读取某块时沿版本链向上找最近一次改动，找不到则读基线；
* 丢弃块读零（按该块在镜像中的真实长度补零，兼容非整块镜像）；
* 提交全程先写 tmp 暂存、校验通过后才原子发布，任何失败都保留原 current。
"""

from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import json
import os
import shutil
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

BLOCK_SIZE = 4096
BASELINE_VERSION = 1
META_FILENAME = "meta.json"
CURRENT_FILENAME = "current.json"
LOCK_FILENAME = ".commit.lock"
TMP_DIRNAME = "tmp"
BLOCKS_DIRNAME = "blocks"


def pad64(data: bytes) -> bytes:
    """base64 解码（自动补齐 padding），非法输入抛 ValueError。"""
    if isinstance(data, str):
        data = data.encode("ascii")
    padding = (-len(data)) % 4
    try:
        return base64.b64decode(data + b"=" * padding, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"非法的 base64 块数据: {exc}") from exc


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """同目录临时文件 + fsync + 原子 rename。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def atomic_write_json(path: Path, payload: dict) -> None:
    atomic_write_bytes(path, (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"))


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _version_dir(root: Path, version: int) -> Path:
    return root / f"v{version:06d}"


def _block_file(root: Path, index: int, version: int) -> Path:
    return root / BLOCKS_DIRNAME / f"b{index:010d}_v{version:06d}"


def _block_len(image_size: int, index: int) -> int:
    start = index * BLOCK_SIZE
    if start >= image_size:
        return 0
    return min(BLOCK_SIZE, image_size - start)


@dataclass(frozen=True)
class ImageState:
    """某个已发布版本的不可变快照。"""

    version: int
    image_size: int
    sha256: str
    block_count: int


class StorageError(Exception):
    """存储层业务错误，API 层映射为 400。"""


class ConflictError(StorageError):
    """父版本与当前版本不一致，API 层映射为 409。"""


class BlockStore:
    def __init__(self, data_dir: str | os.PathLike[str]):
        self.root = Path(data_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / BLOCKS_DIRNAME).mkdir(exist_ok=True)
        (self.root / TMP_DIRNAME).mkdir(exist_ok=True)

        self.baseline_path = self.root / "baseline.raw"
        self._commit_lock = threading.Lock()
        self._handle_lock = threading.RLock()
        self._lock_fh = open(self.root / LOCK_FILENAME, "a+")
        # 读路径上的文件句柄 LRU（块文件随版本累积，避免同时打开过多 fd）
        self._handle_cache: OrderedDict[tuple[int, int], object] = OrderedDict()
        self._handle_cache_cap = 64

        self.current: ImageState | None = None
        # 不可变元组：发布新版本时整体替换，读线程持引用遍历不会与提交线程竞争
        self._chain: tuple[dict, ...] = ()

    # ------------------------------------------------------------------ #
    # 初始化与启动对账
    # ------------------------------------------------------------------ #
    def init_baseline(self, image_size: int, force: bool = False) -> ImageState:
        """创建预置 raw 基线镜像（确定性内容）并发布版本 1。

        幂等：基线已存在且大小一致时直接返回当前状态；force=True 可重建
        （会清空全部版本，仅限空存储/初始化场景使用）。
        """
        if self.baseline_path.exists() and not force:
            if self.baseline_path.stat().st_size != image_size:
                raise StorageError(
                    f"基线镜像已存在且大小为 {self.baseline_path.stat().st_size}，"
                    f"与请求的 {image_size} 不一致"
                )
            return self.open()

        if force and self.current is not None and self.current.version > BASELINE_VERSION:
            raise StorageError("已存在恢复版本，禁止强制重建基线")

        with self._commit_lock:
            fcntl.flock(self._lock_fh.fileno(), fcntl.LOCK_EX)
            try:
                # 清空旧数据
                for child in self.root.iterdir():
                    if child.name in {TMP_DIRNAME, BLOCKS_DIRNAME, LOCK_FILENAME}:
                        continue
                    if child.is_dir():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
                (self.root / BLOCKS_DIRNAME).mkdir(exist_ok=True)
                tmp_dir = self.root / TMP_DIRNAME
                shutil.rmtree(tmp_dir, ignore_errors=True)
                tmp_dir.mkdir(exist_ok=True)

                digest = hashlib.sha256()
                tmp_baseline = self.baseline_path.with_name("baseline.raw.tmp")
                pattern = bytes((i * 7 + 3) & 0xFF for i in range(BLOCK_SIZE))
                full_blocks, tail = divmod(image_size, BLOCK_SIZE)
                with open(tmp_baseline, "wb") as fh:
                    for _ in range(full_blocks):
                        fh.write(pattern)
                        digest.update(pattern)
                    if tail:
                        fh.write(pattern[:tail])
                        digest.update(pattern[:tail])
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp_baseline, self.baseline_path)
                _fsync_dir(self.root)

                block_count = full_blocks + (1 if tail else 0)
                meta = {
                    "version": BASELINE_VERSION,
                    "parent_version": None,
                    "image_size": image_size,
                    "block_count": block_count,
                    "changes": [],
                    "sha256": digest.hexdigest(),
                }
                atomic_write_json(_version_dir(self.root, BASELINE_VERSION) / META_FILENAME, meta)
                state = ImageState(
                    version=BASELINE_VERSION,
                    image_size=image_size,
                    sha256=digest.hexdigest(),
                    block_count=block_count,
                )
                self._publish_current(state)
            finally:
                fcntl.flock(self._lock_fh.fileno(), fcntl.LOCK_UN)
        return self.open()

    def open(self) -> ImageState:
        """启动/复用：清理崩溃暂存，校验版本链，只认最后完整提交的版本。"""
        tmp_root = self.root / TMP_DIRNAME
        if tmp_root.exists():
            for child in tmp_root.iterdir():
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink()

        if not self.baseline_path.exists():
            raise StorageError(f"基线镜像不存在: {self.baseline_path}，请先运行 init_baseline")

        current_path = self.root / CURRENT_FILENAME
        if not current_path.exists():
            raise StorageError(f"版本指针 {CURRENT_FILENAME} 缺失，存储未正确初始化")
        try:
            pointer = json.loads(current_path.read_text("utf-8"))
            current_version = int(pointer["version"])
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            raise StorageError(f"版本指针损坏: {exc}") from exc

        # 从 v1 顺序校验整条已发布链；任何一版 meta 损坏/缺块都在该版截断
        chain: list[dict] = []
        latest_good = BASELINE_VERSION - 1
        for ver in range(BASELINE_VERSION, current_version + 1):
            meta_path = _version_dir(self.root, ver) / META_FILENAME
            try:
                meta = json.loads(meta_path.read_text("utf-8"))
                self._validate_meta(meta, expected_version=ver, prev=chain[-1] if chain else None)
            except (StorageError, FileNotFoundError, json.JSONDecodeError, KeyError, TypeError):
                break
            missing = [
                c["index"]
                for c in meta["changes"]
                if c["op"] == "write"
                and not _block_file(self.root, c["index"], ver).is_file()
            ]
            if missing:
                break
            chain.append(meta)
            latest_good = ver

        if latest_good < BASELINE_VERSION:
            raise StorageError("基线版本元数据缺失或损坏，无法恢复服务")

        good_meta = chain[-1]
        state = ImageState(
            version=latest_good,
            image_size=int(good_meta["image_size"]),
            sha256=str(good_meta["sha256"]),
            block_count=int(good_meta["block_count"]),
        )
        # 若 current 指向了未完整落盘的版本，回退指针到最后完整版本
        if latest_good != current_version:
            self._publish_current(state)

        self._chain = tuple(chain)
        self.current = state
        return state

    def _validate_meta(self, meta: dict, expected_version: int, prev: dict | None) -> None:
        for key in ("version", "parent_version", "image_size", "block_count", "changes", "sha256"):
            if key not in meta:
                raise StorageError(f"版本 {expected_version} 元数据缺少字段 {key}")
        if int(meta["version"]) != expected_version:
            raise StorageError(f"版本目录与元数据版本号不一致: {expected_version}")
        if expected_version == BASELINE_VERSION:
            if meta["parent_version"] is not None or meta["changes"]:
                raise StorageError("基线版本元数据不合法")
        else:
            if prev is None or int(meta["parent_version"]) != int(prev["version"]):
                raise StorageError(f"版本 {expected_version} 父链断裂")
            if int(meta["image_size"]) != int(prev["image_size"]):
                raise StorageError(f"版本 {expected_version} 镜像大小与父版不一致")
        for change in meta["changes"]:
            if change["op"] not in {"write", "discard"} or not isinstance(change["index"], int):
                raise StorageError(f"版本 {expected_version} 元数据包含非法变动项")

    # ------------------------------------------------------------------ #
    # 提交
    # ------------------------------------------------------------------ #
    def commit(
        self,
        parent_version: int,
        changes: list[dict],
        expected_sha256: str,
    ) -> ImageState:
        """提交一批块变动，成功返回新版本快照；失败不影响当前版本。

        changes: [{"index": int, "op": "write", "data_base64": str} |
                  {"index": int, "op": "discard"}]
        """
        state = self.current
        if state is None:
            raise StorageError("存储尚未初始化")
        expected_sha256 = (expected_sha256 or "").strip().lower()
        if len(expected_sha256) != 64:
            raise StorageError("目标镜像 SHA-256 必须是 64 位十六进制字符串")
        try:
            int(expected_sha256, 16)
        except ValueError:
            raise StorageError("目标镜像 SHA-256 含非十六进制字符")

        with self._commit_lock:
            fcntl.flock(self._lock_fh.fileno(), fcntl.LOCK_EX)
            try:
                if parent_version != self.current.version:
                    raise ConflictError(
                        f"父版本 {parent_version} 不是当前版本 {self.current.version}，"
                        "请基于最新版本重新生成增量包"
                    )

                new_version = self.current.version + 1
                block_count = self.current.block_count
                image_size = self.current.image_size

                # 1. 校验并解码变动项
                seen: set[int] = set()
                decoded: list[dict] = []
                for raw in changes:
                    idx = raw.get("index")
                    op = raw.get("op")
                    if not isinstance(idx, int) or isinstance(idx, bool):
                        raise StorageError(f"非法块号: {raw.get('index')!r}")
                    if idx < 0 or idx >= block_count:
                        raise StorageError(f"块号 {idx} 越界，合法范围 0..{block_count - 1}")
                    if idx in seen:
                        raise StorageError(f"块号 {idx} 在同一批次中重复出现")
                    seen.add(idx)
                    if op == "discard":
                        decoded.append({"index": idx, "op": "discard"})
                    elif op == "write":
                        try:
                            payload = pad64(raw.get("data_base64", ""))
                        except ValueError as exc:
                            raise StorageError(str(exc)) from exc
                        real_len = _block_len(image_size, idx)
                        if len(payload) != real_len:
                            raise StorageError(
                                f"块 {idx} 数据长度 {len(payload)} 与期望 {real_len} 字节不符"
                            )
                        decoded.append({"index": idx, "op": "write", "data": payload})
                    else:
                        raise StorageError(f"块 {idx} 的操作类型非法: {op!r}")

                # 2. 暂存：变动块逐个落盘到 tmp，再原子挪入 blocks/
                tmp_dir = self.root / TMP_DIRNAME / f"v{new_version:06d}"
                if tmp_dir.exists():
                    shutil.rmtree(tmp_dir)
                tmp_dir.mkdir(parents=True)
                staged: list[dict] = []
                published = False
                try:
                    for item in decoded:
                        idx = item["index"]
                        if item["op"] == "discard":
                            staged.append({"index": idx, "op": "discard"})
                            continue
                        final_block = _block_file(self.root, idx, new_version)
                        tmp_block = tmp_dir / f"b{idx:010d}"
                        with open(tmp_block, "wb") as fh:
                            fh.write(item["data"])
                            fh.flush()
                            os.fsync(fh.fileno())
                        final_block.parent.mkdir(exist_ok=True)
                        os.replace(tmp_block, final_block)
                        staged.append({"index": idx, "op": "write"})

                    # 3. 用暂存结果流式计算整盘摘要（此时 current 尚未改变）
                    overlay = {c["index"]: c for c in staged}
                    actual_sha = self._hash_image(overlay, new_version)

                    if actual_sha != expected_sha256:
                        raise StorageError(
                            f"还原镜像摘要不吻合: 期望 {expected_sha256}，实际 {actual_sha}"
                        )

                    # 4. 写新版本 meta 并原子发布 current 指针
                    meta = {
                        "version": new_version,
                        "parent_version": parent_version,
                        "image_size": image_size,
                        "block_count": block_count,
                        "changes": staged,
                        "sha256": actual_sha,
                    }
                    atomic_write_json(_version_dir(self.root, new_version) / META_FILENAME, meta)
                    new_state = ImageState(
                        version=new_version,
                        image_size=image_size,
                        sha256=actual_sha,
                        block_count=block_count,
                    )
                    # 先让内存版本链可见，再原子发布 current 指针，
                    # 保证任何看到新版号的读取都能在链中解析到该版
                    self._chain = self._chain + (meta,)
                    self._publish_current(new_state)
                    self.current = new_state
                    published = True
                    return new_state
                finally:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                    if not published:
                        # 提交失败：撤销本批已落盘的块，current 从未改动，原版完整保留
                        for item in decoded:
                            if item["op"] == "write":
                                block_path = _block_file(self.root, item["index"], new_version)
                                self._invalidate_handle(item["index"], new_version)
                                block_path.unlink(missing_ok=True)
            finally:
                fcntl.flock(self._lock_fh.fileno(), fcntl.LOCK_UN)

    def _publish_current(self, state: ImageState) -> None:
        atomic_write_json(
            self.root / CURRENT_FILENAME,
            {
                "version": state.version,
                "image_size": state.image_size,
                "block_count": state.block_count,
                "sha256": state.sha256,
            },
        )

    # ------------------------------------------------------------------ #
    # 读取
    # ------------------------------------------------------------------ #
    def _resolve(
        self,
        index: int,
        overlay: dict[int, dict] | None,
        overlay_version: int,
        max_version: int,
    ):
        """返回 (数据来源, 版本号或 None)。

        来源为 ("zero", None) / ("block", version) / ("baseline", None)。
        max_version 固定读取视图：下载开始后发布的新版本不会进入本次读取，
        从而保证「已开始的下载仍返回原版」。
        """
        if overlay is not None and index in overlay:
            change = overlay[index]
            if change["op"] == "discard":
                return ("zero", None)
            return ("block", overlay_version)

        for meta in reversed(self._chain):
            if meta["version"] > max_version:
                continue
            for change in meta["changes"]:
                if change["index"] == index:
                    if change["op"] == "discard":
                        return ("zero", None)
                    return ("block", meta["version"])
        return ("baseline", None)

    def _invalidate_handle(self, index: int, version: int) -> None:
        """回滚时关闭并移除指向已删除块文件的缓存句柄。"""
        key = (index, version)
        with self._handle_lock:
            handle = self._handle_cache.pop(key, None)
        if handle is not None:
            handle.close()

    def _open_block(self, index: int, version: int):
        key = (index, version)
        with self._handle_lock:
            handle = self._handle_cache.get(key)
            if handle is not None:
                self._handle_cache.move_to_end(key)
                return handle
            # 锁外打开文件可能阻塞，先释放再重新入缓存（同 key 幂等）
        path = _block_file(self.root, index, version)
        new_handle = open(path, "rb")
        with self._handle_lock:
            handle = self._handle_cache.get(key)
            if handle is None:
                self._handle_cache[key] = new_handle
                handle = new_handle
                if len(self._handle_cache) > self._handle_cache_cap:
                    _, old = self._handle_cache.popitem(last=False)
                    old.close()
            else:
                new_handle.close()
        return handle

    def read_block(
        self,
        index: int,
        overlay: dict[int, dict] | None = None,
        overlay_version: int = 0,
        max_version: int | None = None,
    ) -> bytes:
        """读取单个块（按镜像真实长度返回，末尾块可能小于 4 KiB）。"""
        if self.current is None:
            raise StorageError("存储尚未初始化")
        if index < 0 or index >= self.current.block_count:
            raise StorageError(f"块号 {index} 越界")
        if max_version is None:
            max_version = self.current.version
        length = _block_len(self.current.image_size, index)
        source, version = self._resolve(index, overlay, overlay_version, max_version)
        if source == "zero":
            return b"\x00" * length
        if source == "baseline":
            with open(self.baseline_path, "rb") as fh:
                fh.seek(index * BLOCK_SIZE)
                return fh.read(length)
        fh = self._open_block(index, version)
        fh.seek(0)
        return fh.read(length)

    def _hash_image(self, overlay: dict[int, dict] | None, overlay_version: int) -> str:
        digest = hashlib.sha256()
        # 提交校验时 current 尚未发布，读取视图固定在父版本
        max_version = overlay_version - 1 if overlay is not None else self.current.version
        for chunk in self.iter_image(
            overlay=overlay,
            overlay_version=overlay_version,
            max_version=max_version,
        ):
            digest.update(chunk)
        return digest.hexdigest()

    def hash_current(self) -> str:
        return self._hash_image(None, 0)

    def iter_image(
        self,
        overlay: dict[int, dict] | None = None,
        overlay_version: int = 0,
        max_version: int | None = None,
    ) -> Iterator[bytes]:
        """按块流式产出整盘 raw 镜像（固定 4 KiB 输出块，末尾按真实长度）。

        显式传入 max_version 可冻结读取视图：迭代过程中即使有新版本发布，
        产出内容仍保持为该版本（用于下载快照隔离）。
        """
        if self.current is None:
            raise StorageError("存储尚未初始化")
        if max_version is None:
            max_version = self.current.version
        for index in range(self.current.block_count):
            yield self.read_block(
                index,
                overlay=overlay,
                overlay_version=overlay_version,
                max_version=max_version,
            )

    def snapshot_state(self) -> ImageState:
        """下载开始时固定的版本快照（发布新版本不影响本次下载）。"""
        if self.current is None:
            raise StorageError("存储尚未初始化")
        return self.current

    def close(self) -> None:
        for handle in self._handle_cache.values():
            handle.close()
        self._handle_cache.clear()
        self._lock_fh.close()
