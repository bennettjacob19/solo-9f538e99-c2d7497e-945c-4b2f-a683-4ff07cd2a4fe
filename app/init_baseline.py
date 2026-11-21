"""预置 raw 基线镜像的初始化 CLI。

用法：
    python -m app.init_baseline --size 16MiB
    python -m app.init_baseline --size 1048576 --data-dir /data
"""

from __future__ import annotations

import argparse
import sys

from .storage import BlockStore, StorageError

_SUFFIX = {
    "": 1,
    "b": 1,
    "k": 1024,
    "kb": 1024,
    "kib": 1024,
    "m": 1024**2,
    "mb": 1024**2,
    "mib": 1024**2,
    "g": 1024**3,
    "gb": 1024**3,
    "gib": 1024**3,
}


def parse_size(text: str) -> int:
    raw = text.strip().lower()
    digits = ""
    for ch in raw:
        if ch.isdigit():
            digits += ch
        else:
            break
    if not digits:
        raise argparse.ArgumentTypeError(f"无法解析镜像大小: {text!r}")
    suffix = raw[len(digits):].strip()
    if suffix not in _SUFFIX:
        raise argparse.ArgumentTypeError(f"不支持的大小后缀: {suffix!r}")
    size = int(digits) * _SUFFIX[suffix]
    if size <= 0:
        raise argparse.ArgumentTypeError("镜像大小必须为正数")
    return size


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="初始化 raw 基线镜像（版本 1）")
    parser.add_argument(
        "--data-dir",
        default="/data",
        help="存储目录（默认 /data，或环境变量 RECOVERY_DATA_DIR）",
    )
    parser.add_argument(
        "--size",
        type=parse_size,
        required=True,
        help="镜像大小，如 4096、64KiB、16MiB、1GiB",
    )
    parser.add_argument("--force", action="store_true", help="强制重建（仅允许在无恢复版本时）")
    args = parser.parse_args(argv)

    store = BlockStore(args.data_dir)
    try:
        state = store.init_baseline(args.size, force=args.force)
    except StorageError as exc:
        print(f"[init_baseline] 失败: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()

    print(
        "[init_baseline] 就绪: "
        f"version={state.version} size={state.image_size} "
        f"blocks={state.block_count} sha256={state.sha256}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
