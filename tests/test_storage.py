"""存储核心：链式版本、丢弃块、失败回滚、崩溃对账、只落盘变动块。"""

import base64
import json
import os

import pytest

from app.storage import BLOCK_SIZE, BlockStore, ConflictError, StorageError

from .conftest import expected_image, sha256_hex


def write_change(index: int, data: bytes) -> dict:
    return {"index": index, "op": "write", "data_base64": base64.b64encode(data).decode("ascii")}


def test_baseline_initialized(store, image_size):
    assert store.current.version == 1
    assert store.current.image_size == image_size
    assert store.current.block_count == 11

    raw = b"".join(store.iter_image())
    assert len(raw) == image_size
    assert sha256_hex(raw) == store.current.sha256 == store.hash_current()
    # 基线文件就是版本 1 的完整内容
    assert raw == open(store.baseline_path, "rb").read()


def test_commit_write_and_inherit(store, image_size):
    payload_v2 = {2: bytes((i * 3) & 0xFF for i in range(BLOCK_SIZE))}
    image_v2 = expected_image(store, payload_v2)

    state_v2 = store.commit(
        parent_version=1,
        changes=[write_change(2, payload_v2[2])],
        expected_sha256=sha256_hex(image_v2),
    )
    assert state_v2.version == 2

    raw = b"".join(store.iter_image())
    assert raw == image_v2
    # 未列出的块沿用父版（抽查块 0、5）
    assert store.read_block(0) == expected_image(store)[0:BLOCK_SIZE]
    assert store.read_block(5) == expected_image(store)[5 * BLOCK_SIZE : 6 * BLOCK_SIZE]
    # 覆盖块生效
    assert store.read_block(2) == payload_v2[2]

    # 第二批只改块 7，块 2 保持 v2 内容
    payload_v3 = {7: b"\xab" * BLOCK_SIZE}
    image_v3 = bytearray(image_v2)
    image_v3[7 * BLOCK_SIZE : 8 * BLOCK_SIZE] = payload_v3[7]
    state_v3 = store.commit(
        parent_version=2,
        changes=[write_change(7, payload_v3[7])],
        expected_sha256=sha256_hex(bytes(image_v3)),
    )
    assert state_v3.version == 3
    assert b"".join(store.iter_image()) == bytes(image_v3)
    assert store.read_block(2) == payload_v2[2]
    assert store.read_block(7) == payload_v3[7]


def test_discard_reads_zero(store, image_size):
    tail_index = store.current.block_count - 1
    tail_len = image_size - tail_index * BLOCK_SIZE  # 777
    state = store.commit(
        parent_version=1,
        changes=[
            {"index": 0, "op": "discard"},
            {"index": tail_index, "op": "discard"},
        ],
        expected_sha256=sha256_hex(
            b"\x00" * BLOCK_SIZE
            + expected_image(store)[BLOCK_SIZE : tail_index * BLOCK_SIZE]
            + b"\x00" * tail_len
        ),
    )
    assert state.version == 2
    assert store.read_block(0) == b"\x00" * BLOCK_SIZE
    # 丢弃的末块按真实长度补零，而不是整块 4 KiB
    assert store.read_block(tail_index) == b"\x00" * tail_len
    full = b"".join(store.iter_image())
    assert len(full) == image_size
    assert full[0:BLOCK_SIZE] == b"\x00" * BLOCK_SIZE
    assert full[-tail_len:] == b"\x00" * tail_len


def test_discard_then_rewrite(store):
    store.commit(
        parent_version=1,
        changes=[{"index": 1, "op": "discard"}],
        expected_sha256=sha256_hex(
            expected_image(store)[:BLOCK_SIZE]
            + b"\x00" * BLOCK_SIZE
            + expected_image(store)[2 * BLOCK_SIZE :]
        ),
    )
    new_data = b"\xcd" * BLOCK_SIZE
    image_v3 = bytearray(expected_image(store))
    image_v3[BLOCK_SIZE : 2 * BLOCK_SIZE] = new_data
    store.commit(
        parent_version=2,
        changes=[write_change(1, new_data)],
        expected_sha256=sha256_hex(bytes(image_v3)),
    )
    assert store.read_block(1) == new_data


def test_stale_parent_rejected(store):
    good = expected_image(store, {0: b"\x11" * BLOCK_SIZE})
    store.commit(
        parent_version=1,
        changes=[write_change(0, b"\x11" * BLOCK_SIZE)],
        expected_sha256=sha256_hex(good),
    )
    with pytest.raises(ConflictError, match="不是当前版本"):
        store.commit(
            parent_version=1,
            changes=[write_change(0, b"\x22" * BLOCK_SIZE)],
            expected_sha256="0" * 64,
        )
    # 当前版本仍是 v2，内容未变
    assert store.current.version == 2
    assert b"".join(store.iter_image()) == good


def test_hash_mismatch_keeps_original(store, image_size):
    before = b"".join(store.iter_image())
    current_version = store.current.version
    with pytest.raises(StorageError, match="摘要不吻合"):
        store.commit(
            parent_version=1,
            changes=[write_change(3, b"\x55" * BLOCK_SIZE)],
            expected_sha256="f" * 64,
        )

    # 版本指针、镜像内容均保持原样
    assert store.current.version == current_version
    assert b"".join(store.iter_image()) == before
    # 失败批次的块文件不得残留
    assert os.listdir(store.root / "blocks") == []
    # 暂存目录被清理
    assert not (store.root / "tmp" / "v000002").exists()


def test_validation_errors(store):
    # 块号越界
    with pytest.raises(StorageError, match="越界"):
        store.commit(1, [{"index": 99, "op": "discard"}], "0" * 64)
    # 重复块号
    with pytest.raises(StorageError, match="重复"):
        store.commit(
            1,
            [
                {"index": 0, "op": "discard"},
                write_change(0, b"\x00" * BLOCK_SIZE),
            ],
            "0" * 64,
        )
    # 非法 base64
    with pytest.raises(StorageError, match="base64"):
        store.commit(1, [{"index": 0, "op": "write", "data_base64": "@@not-base64@@"}], "0" * 64)
    # 块长度不符
    with pytest.raises(StorageError, match="数据长度"):
        store.commit(
            1,
            [write_change(0, b"short")],
            "0" * 64,
        )
    # 非法摘要格式
    with pytest.raises(StorageError, match="SHA-256"):
        store.commit(1, [{"index": 0, "op": "discard"}], "xyz")
    # 非法操作类型
    with pytest.raises(StorageError, match="操作类型非法"):
        store.commit(1, [{"index": 0, "op": "resize"}], "0" * 64)


def test_only_changed_blocks_land_on_disk(store, image_size):
    data = b"\x99" * BLOCK_SIZE
    image = expected_image(store, {4: data})
    store.commit(
        parent_version=1,
        changes=[write_change(4, data)],
        expected_sha256=sha256_hex(image),
    )
    blocks_dir = store.root / "blocks"
    files = sorted(os.listdir(blocks_dir))
    # 只落盘 1 个变动块文件（4 KiB），没有整盘副本
    assert files == ["b0000000004_v000002"]
    assert os.path.getsize(blocks_dir / files[0]) == BLOCK_SIZE
    meta = json.loads((store.root / "v000002" / "meta.json").read_text())
    assert meta["changes"] == [{"index": 4, "op": "write"}]
    assert meta["parent_version"] == 1
    # 基线文件大小不变
    assert os.path.getsize(store.baseline_path) == image_size


def test_restart_serves_last_complete_commit(tmp_path, image_size):
    data_dir = tmp_path / "data"
    s1 = BlockStore(data_dir)
    s1.init_baseline(image_size)
    image_v2 = expected_image(s1, {1: b"\x77" * BLOCK_SIZE})
    s1.commit(
        parent_version=1,
        changes=[write_change(1, b"\x77" * BLOCK_SIZE)],
        expected_sha256=sha256_hex(image_v2),
    )
    s1.close()

    # 模拟崩溃残留：tmp 暂存目录与一个“已写 meta 但 current 未切换”的半成品 v3
    (data_dir / "tmp" / "v000003").mkdir(parents=True)
    (data_dir / "tmp" / "v000003" / "junk").write_bytes(b"x")
    os.makedirs(data_dir / "v000003", exist_ok=True)
    (data_dir / "v000003" / "meta.json").write_text("{broken")

    s2 = BlockStore(data_dir)
    state = s2.open()
    # 重启后只提供最后完整提交的版本 v2
    assert state.version == 2
    assert state.sha256 == sha256_hex(image_v2)
    assert b"".join(s2.iter_image()) == image_v2
    # 崩溃暂存被清理；current 指针保持/回退到 v2
    assert json.loads((data_dir / "current.json").read_text())["version"] == 2
    assert not (data_dir / "tmp" / "v000003").exists()
    s2.close()

    # 再次重启依旧稳定
    s3 = BlockStore(data_dir)
    assert s3.open().version == 2
    s3.close()


def test_restart_with_missing_block_file(tmp_path, image_size):
    data_dir = tmp_path / "data"
    s1 = BlockStore(data_dir)
    s1.init_baseline(image_size)
    image_v2 = expected_image(s1, {0: b"\x33" * BLOCK_SIZE})
    s1.commit(
        parent_version=1,
        changes=[write_change(0, b"\x33" * BLOCK_SIZE)],
        expected_sha256=sha256_hex(image_v2),
    )
    # 人为损坏 v2：删掉它的块文件
    os.remove(data_dir / "blocks" / "b0000000000_v000002")
    s1.close()

    s2 = BlockStore(data_dir)
    state = s2.open()
    # v2 不完整 → 回退到完整的基线 v1
    assert state.version == 1
    assert b"".join(s2.iter_image()) == open(data_dir / "baseline.raw", "rb").read()
    s2.close()


def test_init_baseline_is_idempotent(store, image_size):
    again = store.init_baseline(image_size)
    assert again.version == 1
    assert again.sha256 == store.current.sha256


def test_failed_commit_then_retry_same_version_succeeds(store):
    """失败回滚后重试同一新版本号，必须读到新数据而非缓存的旧句柄。"""
    data = b"\x66" * BLOCK_SIZE
    target = expected_image(store, {0: data})

    # 第一次：错误摘要失败（哈希过程会把块句柄放进缓存）
    with pytest.raises(StorageError, match="摘要不吻合"):
        store.commit(
            parent_version=1,
            changes=[write_change(0, data)],
            expected_sha256="0" * 64,
        )
    assert store.current.version == 1
    assert os.listdir(store.root / "blocks") == []

    # 第二次：正确摘要，版本号仍是 2，内容与摘要必须正确
    state = store.commit(
        parent_version=1,
        changes=[write_change(0, data)],
        expected_sha256=sha256_hex(target),
    )
    assert state.version == 2
    assert store.read_block(0) == data
    assert b"".join(store.iter_image()) == target


def test_concurrent_reads_during_commits(store):
    """提交线程持续发布新版本的同时，读线程反复整盘读取，不得抛并发异常。"""
    import threading

    errors: list[Exception] = []
    stop = threading.Event()

    def reader():
        try:
            while not stop.is_set():
                # 固定读取视图与对应版本，避免与提交线程的发布动作竞争
                snapshot = store.current
                raw = b"".join(store.iter_image(max_version=snapshot.version))
                assert len(raw) == snapshot.image_size
                assert sha256_hex(raw) == snapshot.sha256
        except Exception as exc:  # noqa: BLE001 - 测试需要捕获任何并发异常
            errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()

    for ver in range(2, 12):
        data = bytes([ver & 0xFF]) * BLOCK_SIZE
        target = bytearray(b"".join(store.iter_image()))
        target[0:BLOCK_SIZE] = data
        store.commit(
            parent_version=ver - 1,
            changes=[write_change(0, data)],
            expected_sha256=sha256_hex(bytes(target)),
        )

    stop.set()
    for t in threads:
        t.join(timeout=10)
    assert errors == []
    assert store.current.version == 11
