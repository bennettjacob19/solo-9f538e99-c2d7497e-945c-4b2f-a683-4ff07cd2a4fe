"""API 端到端测试：提交、流式下载、错误语义、下载快照隔离、重启。"""

import base64
import hashlib
import json

from app.storage import BLOCK_SIZE, BlockStore

from .conftest import sha256_hex


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _current_image(store) -> bytes:
    return b"".join(store.iter_image())


def _with_block(image: bytes, index: int, data: bytes) -> bytes:
    out = bytearray(image)
    start = index * BLOCK_SIZE
    out[start : start + len(data)] = data
    return bytes(out)


def test_health_and_version(client, image_size):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "version": 1}

    r = client.get("/version")
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == 1
    assert body["image_size"] == image_size
    assert body["block_count"] == 11
    assert len(body["sha256"]) == 64


def test_commit_endpoint_full_flow(client, image_size):
    store = client.app.state.store
    new_block = bytes((i ^ 0x5A) & 0xFF for i in range(BLOCK_SIZE))
    target_v2 = _with_block(_current_image(store), 3, new_block)

    r = client.post(
        "/recover/commit",
        json={
            "parent_version": 1,
            "target_sha256": sha256_hex(target_v2),
            "changes": [{"index": 3, "op": "write", "data_base64": _b64(new_block)}],
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["version"] == 2
    assert body["parent_version"] == 1
    assert body["sha256"] == sha256_hex(target_v2)
    assert _current_image(store) == target_v2

    # 第二批：丢弃块 0、覆盖块 5、丢弃末块（777 字节）
    tail_index = 10
    tail_len = image_size - tail_index * BLOCK_SIZE
    image_v3 = bytearray(target_v2)
    image_v3[0:BLOCK_SIZE] = b"\x00" * BLOCK_SIZE
    image_v3[5 * BLOCK_SIZE : 6 * BLOCK_SIZE] = b"\xEE" * BLOCK_SIZE
    image_v3[tail_index * BLOCK_SIZE :] = b"\x00" * tail_len
    image_v3 = bytes(image_v3)

    r = client.post(
        "/recover/commit",
        json={
            "parent_version": 2,
            "target_sha256": sha256_hex(image_v3),
            "changes": [
                {"index": 0, "op": "discard"},
                {"index": 5, "op": "write", "data_base64": _b64(b"\xEE" * BLOCK_SIZE)},
                {"index": tail_index, "op": "discard"},
            ],
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["version"] == 3
    assert _current_image(store) == image_v3

    r = client.get("/version")
    assert r.json()["version"] == 3
    assert r.json()["sha256"] == sha256_hex(image_v3)


def test_commit_hash_mismatch_returns_400_and_preserves(client):
    before = client.get("/version").json()
    r = client.post(
        "/recover/commit",
        json={
            "parent_version": 1,
            "target_sha256": "0" * 64,
            "changes": [{"index": 0, "op": "discard"}],
        },
    )
    assert r.status_code == 400
    assert "摘要不吻合" in r.json()["detail"]
    # 原版完整保留
    assert client.get("/version").json() == before


def test_commit_stale_parent_returns_409(client):
    store = client.app.state.store
    target = _with_block(_current_image(store), 0, b"\x01" * BLOCK_SIZE)
    ok = client.post(
        "/recover/commit",
        json={
            "parent_version": 1,
            "target_sha256": sha256_hex(target),
            "changes": [{"index": 0, "op": "write", "data_base64": _b64(b"\x01" * BLOCK_SIZE)}],
        },
    )
    assert ok.status_code == 201

    stale = client.post(
        "/recover/commit",
        json={
            "parent_version": 1,
            "target_sha256": "0" * 64,
            "changes": [{"index": 1, "op": "discard"}],
        },
    )
    assert stale.status_code == 409
    assert "不是当前版本" in stale.json()["detail"]


def test_commit_validation_errors(client):
    # 请求体不符合 schema（缺字段）→ 422
    r = client.post("/recover/commit", json={"parent_version": 1})
    assert r.status_code == 422

    # 块号越界 → 400
    r = client.post(
        "/recover/commit",
        json={
            "parent_version": 1,
            "target_sha256": "0" * 64,
            "changes": [{"index": 999, "op": "discard"}],
        },
    )
    assert r.status_code == 400

    # 同批重复块号 → 400
    r = client.post(
        "/recover/commit",
        json={
            "parent_version": 1,
            "target_sha256": "0" * 64,
            "changes": [
                {"index": 0, "op": "discard"},
                {"index": 0, "op": "write", "data_base64": _b64(b"\x00" * BLOCK_SIZE)},
            ],
        },
    )
    assert r.status_code == 400
    assert "重复" in r.json()["detail"]

    # 非法 base64 → 400
    r = client.post(
        "/recover/commit",
        json={
            "parent_version": 1,
            "target_sha256": "0" * 64,
            "changes": [{"index": 0, "op": "write", "data_base64": "@@bad@@"}],
        },
    )
    assert r.status_code == 400
    assert "base64" in r.json()["detail"]


def test_download_stream_matches_hash_and_headers(client, image_size):
    r = client.get("/image")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/octet-stream"
    assert int(r.headers["content-length"]) == image_size
    assert r.headers["x-image-version"] == "1"

    data = b"".join(r.iter_bytes())
    assert len(data) == image_size
    assert hashlib.sha256(data).hexdigest() == r.headers["x-image-sha256"]

    # 与基线文件逐字节一致
    assert data == open(client.app.state.store.baseline_path, "rb").read()


def test_download_snapshot_isolated_from_new_commit(tmp_path, image_size):
    """下载开始后提交新版本，进行中的下载仍返回旧版本内容。

    用真实 uvicorn 服务 + 独立线程并发：下载流读到一半时通过另一个连接
    提交新版本，再读完下载，验证其内容与摘要仍停留在下载开始时的版本。
    """
    import socket
    import threading
    import time

    import httpx
    import uvicorn

    from app.main import create_app

    data_dir = tmp_path / "data"
    boot = BlockStore(data_dir)
    boot.init_baseline(image_size)
    old_image = b"".join(boot.iter_image())
    old_version = boot.current.version
    old_sha = sha256_hex(old_image)
    boot.close()

    app = create_app(data_dir=str(data_dir))
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 10
        while not server.started and time.time() < deadline:
            time.sleep(0.02)
        assert server.started
        sock: socket.socket = server.servers[0].sockets[0]
        base_url = f"http://127.0.0.1:{sock.getsockname()[1]}"

        new_block = b"\xC3" * BLOCK_SIZE
        target = _with_block(old_image, 0, new_block)

        with httpx.Client(base_url=base_url, timeout=10) as http:
            with http.stream("GET", "/image") as resp:
                assert resp.status_code == 200
                assert resp.headers["x-image-version"] == str(old_version)
                stream = resp.iter_bytes()
                first = next(stream)
                assert first and old_image.startswith(first)

                # 下载进行中，从另一连接提交新版本
                commit = http.post(
                    "/recover/commit",
                    json={
                        "parent_version": old_version,
                        "target_sha256": sha256_hex(target),
                        "changes": [
                            {"index": 0, "op": "write", "data_base64": _b64(new_block)}
                        ],
                    },
                )
                assert commit.status_code == 201
                assert http.get("/version").json()["version"] == old_version + 1

                # 继续读完旧下载（复用同一迭代器）：必须仍是旧版本
                rest = b"".join(stream)

            downloaded = first + rest
            assert downloaded == old_image
            assert hashlib.sha256(downloaded).hexdigest() == old_sha

            # 之后的新下载拿到新版本
            fresh = http.get("/image")
            assert fresh.headers["x-image-version"] == str(old_version + 1)
            assert fresh.content == target
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_service_restart_serves_last_complete_version(client, image_size):
    data_dir = client.app.state.data_dir
    store = client.app.state.store
    target = _with_block(_current_image(store), 2, b"\x42" * BLOCK_SIZE)
    r = client.post(
        "/recover/commit",
        json={
            "parent_version": 1,
            "target_sha256": sha256_hex(target),
            "changes": [{"index": 2, "op": "write", "data_base64": _b64(b"\x42" * BLOCK_SIZE)}],
        },
    )
    assert r.status_code == 201

    # 重启进程：重新打开同一数据目录
    reboot = BlockStore(data_dir)
    state = reboot.open()
    assert state.version == 2
    assert state.sha256 == sha256_hex(target)
    assert b"".join(reboot.iter_image()) == target
    assert json.loads((data_dir / "current.json").read_text())["version"] == 2
    reboot.close()
