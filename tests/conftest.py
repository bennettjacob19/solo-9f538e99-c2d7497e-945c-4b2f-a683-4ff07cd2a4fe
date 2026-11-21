"""共享测试夹具。"""

import hashlib
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import create_app  # noqa: E402
from app.storage import BLOCK_SIZE, BlockStore  # noqa: E402


def expected_image(store: BlockStore, overlay: dict[int, bytes] | None = None) -> bytes:
    """按存储规则在内存中拼出期望整盘镜像。"""
    overlay = overlay or {}
    parts: list[bytes] = []
    with open(store.baseline_path, "rb") as fh:
        baseline = fh.read()
    for idx in range(store.current.block_count):
        start = idx * BLOCK_SIZE
        length = min(BLOCK_SIZE, store.current.image_size - start)
        if idx in overlay:
            parts.append(overlay[idx])
        else:
            parts.append(baseline[start : start + length])
    return b"".join(parts)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def image_size() -> int:
    # 10 个整块 + 1 个 777 字节末块，共 41737 字节
    return 10 * BLOCK_SIZE + 777


@pytest.fixture
def store(tmp_path, image_size):
    s = BlockStore(tmp_path / "data")
    s.init_baseline(image_size)
    yield s
    s.close()


@pytest.fixture
def client(tmp_path, image_size):
    data_dir = tmp_path / "data"
    # 预置基线
    boot = BlockStore(data_dir)
    boot.init_baseline(image_size)
    boot.close()

    app = create_app(data_dir=str(data_dir))
    with TestClient(app) as c:
        c.app.state.data_dir = data_dir
        yield c
