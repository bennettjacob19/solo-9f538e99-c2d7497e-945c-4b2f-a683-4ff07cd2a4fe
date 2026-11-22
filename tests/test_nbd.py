"""NBD 只读导出：握手、按版号选版、跨块读、丢弃块读零、末块长度、
越界/写错误、版本钉住、旧版可连、慢连接隔离（含慢连接期间换版）。

qemu-io / qemu-img 可用时（PATH 中）额外跑真实 QEMU 客户端集成测试。
"""

import base64
import shutil
import socket
import struct
import subprocess
import threading
import time

import pytest

from app.nbd_server import (
    CMD_FLUSH,
    CMD_READ,
    CMD_TRIM,
    CMD_WRITE,
    EOVERFLOW,
    EROFS,
    IHAVEOPT,
    INFO_BLOCK_SIZE,
    INFO_EXPORT,
    NBDMAGIC,
    OPTION_REPLY_MAGIC,
    OPT_ABORT,
    OPT_EXPORT_NAME,
    OPT_GO,
    OPT_LIST,
    OPT_STRUCTURED_REPLY,
    REP_ACK,
    REP_ERR_UNKNOWN,
    REP_ERR_UNSUP,
    REP_INFO,
    REP_SERVER,
    REQUEST_MAGIC,
    SIMPLE_REPLY_MAGIC,
    NBDServer,
)
from app.storage import BLOCK_SIZE, BlockStore

from .conftest import expected_image, sha256_hex


def write_change(index: int, data: bytes) -> dict:
    return {"index": index, "op": "write", "data_base64": base64.b64encode(data).decode("ascii")}


# ------------------------------------------------------------------ #
# 最小 fixed-newstyle NBD 客户端（测试用）
# ------------------------------------------------------------------ #
def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("对端关闭连接")
        buf.extend(chunk)
    return bytes(buf)


class OptionError(Exception):
    def __init__(self, reply_type: int):
        super().__init__(f"选项被拒绝: {reply_type:#x}")
        self.reply_type = reply_type


class WireClient:
    """最小 NBD 客户端：fixed newstyle 握手 + 简单应答传输。"""

    def __init__(self, host: str, port: int):
        self.sock = socket.create_connection((host, port), timeout=10)
        magic, ihaveopt, flags = struct.unpack(">QQH", _recv_exact(self.sock, 18))
        assert magic == NBDMAGIC
        assert ihaveopt == IHAVEOPT
        assert flags & 0x1  # FIXED_NEWSTYLE
        # FIXED_NEWSTYLE | NO_ZEROES
        self.sock.sendall(struct.pack(">I", 0x3))

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def _option(self, option: int, data: bytes = b""):
        self.sock.sendall(struct.pack(">QII", IHAVEOPT, option, len(data)) + data)

    def _read_option_reply(self):
        magic, option, rtype, length = struct.unpack(">QIII", _recv_exact(self.sock, 20))
        assert magic == OPTION_REPLY_MAGIC
        payload = _recv_exact(self.sock, length) if length else b""
        return option, rtype, payload

    def go(self, name: bytes, infos: tuple[int, ...] = ()) -> list[bytes]:
        """NBD_OPT_GO；成功返回 INFO 应答列表，失败抛 OptionError。"""
        payload = struct.pack(">I", len(name)) + name + struct.pack(">H", len(infos))
        payload += b"".join(struct.pack(">H", i) for i in infos)
        self._option(OPT_GO, payload)
        info_replies = []
        while True:
            option, rtype, data = self._read_option_reply()
            assert option == OPT_GO
            if rtype == REP_ACK:
                return info_replies
            if rtype & 0x80000000:
                raise OptionError(rtype)
            assert rtype == REP_INFO
            info_replies.append(data)

    def export_name(self, name: bytes):
        """NBD_OPT_EXPORT_NAME：成功返回 (size, flags)；失败时服务端硬断开。"""
        self._option(OPT_EXPORT_NAME, name)
        size, flags = struct.unpack(">QH", _recv_exact(self.sock, 10))
        return size, flags

    def structured_reply(self):
        self._option(OPT_STRUCTURED_REPLY)
        option, rtype, _ = self._read_option_reply()
        assert option == OPT_STRUCTURED_REPLY
        return rtype

    def abort(self):
        self._option(OPT_ABORT)
        option, rtype, _ = self._read_option_reply()
        assert option == OPT_ABORT
        return rtype

    def list_exports(self) -> list[str]:
        self._option(OPT_LIST, struct.pack(">IH", 0, 0))
        names = []
        while True:
            option, rtype, data = self._read_option_reply()
            assert option == OPT_LIST
            if rtype == REP_ACK:
                return names
            assert rtype == REP_SERVER
            (nlen,) = struct.unpack(">I", data[:4])
            names.append(data[4 : 4 + nlen].decode("ascii"))

    def read(self, offset: int, length: int, handle: int = 0xA5):
        """返回 (error, data)；error 非 0 时 data 为 None。"""
        self.sock.sendall(
            struct.pack(">IHHQQI", REQUEST_MAGIC, 0, CMD_READ, handle, offset, length)
        )
        magic, error, rhandle = struct.unpack(">IIQ", _recv_exact(self.sock, 16))
        assert magic == SIMPLE_REPLY_MAGIC
        assert rhandle == handle
        if error:
            return error, None
        return 0, _recv_exact(self.sock, length)

    def write(self, offset: int, data: bytes, handle: int = 0xB6) -> int:
        self.sock.sendall(
            struct.pack(">IHHQQI", REQUEST_MAGIC, 0, CMD_WRITE, handle, offset, len(data)) + data
        )
        magic, error, rhandle = struct.unpack(">IIQ", _recv_exact(self.sock, 16))
        assert magic == SIMPLE_REPLY_MAGIC
        assert rhandle == handle
        return error

    def command(self, cmd: int, offset: int = 0, length: int = 0, handle: int = 0xC7) -> int:
        self.sock.sendall(
            struct.pack(">IHHQQI", REQUEST_MAGIC, 0, cmd, handle, offset, length)
        )
        magic, error, rhandle = struct.unpack(">IIQ", _recv_exact(self.sock, 16))
        assert magic == SIMPLE_REPLY_MAGIC
        assert rhandle == handle
        return error


# ------------------------------------------------------------------ #
# 夹具：v1 基线；v2 覆盖块2 + 丢弃块0；v3 覆盖块5
# ------------------------------------------------------------------ #
@pytest.fixture
def nbd_env(tmp_path, image_size):
    store = BlockStore(tmp_path / "data")
    store.init_baseline(image_size)
    base = expected_image(store)

    block2 = bytes((i * 5 + 1) & 0xFF for i in range(BLOCK_SIZE))
    img_v2 = bytearray(base)
    img_v2[2 * BLOCK_SIZE : 3 * BLOCK_SIZE] = block2
    img_v2[0:BLOCK_SIZE] = b"\x00" * BLOCK_SIZE
    store.commit(
        1,
        [write_change(2, block2), {"index": 0, "op": "discard"}],
        sha256_hex(bytes(img_v2)),
    )

    block5 = b"\x7e" * BLOCK_SIZE
    img_v3 = bytearray(img_v2)
    img_v3[5 * BLOCK_SIZE : 6 * BLOCK_SIZE] = block5
    store.commit(2, [write_change(5, block5)], sha256_hex(bytes(img_v3)))

    server = NBDServer(store, "127.0.0.1", 0)
    _, port = server.start()
    try:
        yield {
            "store": store,
            "port": port,
            "base": bytes(base),
            "v2": bytes(img_v2),
            "v3": bytes(img_v3),
            "block2": block2,
            "block5": block5,
        }
    finally:
        server.stop()
        store.close()


def connect(port: int, version: bytes) -> WireClient:
    client = WireClient("127.0.0.1", port)
    client.go(version)
    return client


# ------------------------------------------------------------------ #
# 握手与导出选择
# ------------------------------------------------------------------ #
def test_go_reports_export_and_block_size(nbd_env, image_size):
    client = WireClient("127.0.0.1", nbd_env["port"])
    infos = client.go(b"2", infos=(INFO_BLOCK_SIZE,))
    client.close()
    # 第一个 INFO 必须是 NBD_INFO_EXPORT：大小 + 只读等传输标志
    info_type, payload = struct.unpack(">H", infos[0][:2])[0], infos[0][2:]
    assert info_type == INFO_EXPORT
    size, flags = struct.unpack(">QH", payload)
    assert size == image_size
    assert flags & 0x2  # READ_ONLY
    # 第二个是 NBD_INFO_BLOCK_SIZE：min=1, preferred=4096, max=32MiB
    info_type, payload = struct.unpack(">H", infos[1][:2])[0], infos[1][2:]
    assert info_type == INFO_BLOCK_SIZE
    assert struct.unpack(">III", payload) == (1, BLOCK_SIZE, 32 * 1024 * 1024)


def test_export_name_option_still_supported(nbd_env, image_size):
    client = WireClient("127.0.0.1", nbd_env["port"])
    size, flags = client.export_name(b"1")
    assert size == image_size
    assert flags & 0x2
    # 旧式选项进入传输阶段后可直接读
    error, data = client.read(0, BLOCK_SIZE)
    assert error == 0
    assert data == nbd_env["base"][:BLOCK_SIZE]
    client.close()


def test_only_committed_versions_accepted(nbd_env):
    # 当前已提交到 v3：0、4、999、非数字都应拒绝
    for bad in (b"0", b"4", b"999", b"abc", b" 1", b"1x"):
        client = WireClient("127.0.0.1", nbd_env["port"])
        with pytest.raises(OptionError) as excinfo:
            client.go(bad)
        assert excinfo.value.reply_type == REP_ERR_UNKNOWN
        client.close()
    # 旧版（已提交）可以连接
    for good in (b"1", b"2", b"3"):
        client = connect(nbd_env["port"], good)
        client.close()
    # NBD_OPT_EXPORT_NAME 无应答通道：未知导出名 → 服务端硬断开
    client = WireClient("127.0.0.1", nbd_env["port"])
    with pytest.raises(EOFError):
        client.export_name(b"999")
    client.close()


def test_empty_export_name_defaults_to_current(nbd_env):
    client = connect(nbd_env["port"], b"")
    error, data = client.read(5 * BLOCK_SIZE, BLOCK_SIZE)
    assert error == 0
    assert data == nbd_env["block5"]  # 当前版 = v3
    client.close()


def test_opt_list_shows_all_committed_versions(nbd_env):
    client = WireClient("127.0.0.1", nbd_env["port"])
    assert client.list_exports() == ["1", "2", "3"]
    client.close()


def test_structured_reply_declined_then_normal_go(nbd_env):
    client = WireClient("127.0.0.1", nbd_env["port"])
    # 不支持 structured reply：回 ERR_UNSUP 后连接继续可用（qemu 的回退路径）
    assert client.structured_reply() == REP_ERR_UNSUP
    infos = client.go(b"1")
    assert infos  # 至少包含 NBD_INFO_EXPORT
    error, data = client.read(0, 16)
    assert error == 0
    assert data == nbd_env["base"][:16]
    client.close()


def test_opt_abort_closes_cleanly(nbd_env):
    client = WireClient("127.0.0.1", nbd_env["port"])
    assert client.abort() == REP_ACK
    with pytest.raises(EOFError):
        _recv_exact(client.sock, 1)
    client.close()


# ------------------------------------------------------------------ #
# 读取语义
# ------------------------------------------------------------------ #
def test_read_selected_versions(nbd_env):
    # v1：基线原样
    c1 = connect(nbd_env["port"], b"1")
    err, data = c1.read(2 * BLOCK_SIZE, BLOCK_SIZE)
    assert err == 0
    assert data == nbd_env["base"][2 * BLOCK_SIZE : 3 * BLOCK_SIZE]
    # v2：块2被覆盖
    c2 = connect(nbd_env["port"], b"2")
    err, data = c2.read(2 * BLOCK_SIZE, BLOCK_SIZE)
    assert err == 0
    assert data == nbd_env["block2"]
    # v3：块2沿用 v2，块5是 v3 新覆盖
    c3 = connect(nbd_env["port"], b"3")
    err, data = c3.read(2 * BLOCK_SIZE, BLOCK_SIZE)
    assert err == 0 and data == nbd_env["block2"]
    err, data = c3.read(5 * BLOCK_SIZE, BLOCK_SIZE)
    assert err == 0 and data == nbd_env["block5"]
    for c in (c1, c2, c3):
        c.close()


def test_cross_block_read_unaligned(nbd_env, image_size):
    """跨块读取：非对齐偏移 + 跨 4 个块边界，逐字节比对。"""
    client = connect(nbd_env["port"], b"3")
    offset = BLOCK_SIZE - 7
    length = 3 * BLOCK_SIZE + 13
    err, data = client.read(offset, length)
    assert err == 0
    assert data == nbd_env["v3"][offset : offset + length]
    # 整盘顺序读（含非整块末尾），与期望镜像逐字节一致
    err, full = client.read(0, image_size)
    assert err == 0
    assert full == nbd_env["v3"]
    client.close()


def test_discard_block_reads_zero(nbd_env):
    c2 = connect(nbd_env["port"], b"2")
    err, data = c2.read(0, BLOCK_SIZE)
    assert err == 0
    assert data == b"\x00" * BLOCK_SIZE
    # 同一区域在 v1 中是基线内容（旧版不受丢弃影响）
    c1 = connect(nbd_env["port"], b"1")
    err, data = c1.read(0, BLOCK_SIZE)
    assert err == 0
    assert data == nbd_env["base"][:BLOCK_SIZE]
    c1.close()
    c2.close()


def test_tail_block_actual_length(nbd_env, image_size):
    tail_len = image_size % BLOCK_SIZE  # 777
    client = connect(nbd_env["port"], b"1")
    # 只读末块真实长度
    err, data = client.read(image_size - tail_len, tail_len)
    assert err == 0
    assert len(data) == tail_len
    assert data == nbd_env["base"][-tail_len:]
    # 跨末块的非对齐读
    offset = image_size - tail_len - 5
    err, data = client.read(offset, tail_len + 5)
    assert err == 0
    assert data == nbd_env["base"][offset:]
    client.close()


def test_out_of_range_read_returns_eoverflow(nbd_env, image_size):
    client = connect(nbd_env["port"], b"1")
    # 部分越界
    err, _ = client.read(image_size - 10, 20)
    assert err == EOVERFLOW
    # 起点即越界
    err, _ = client.read(image_size, 1)
    assert err == EOVERFLOW
    # 远超末尾
    err, _ = client.read(image_size * 4, BLOCK_SIZE)
    assert err == EOVERFLOW
    # 出错后连接仍可用
    err, data = client.read(0, 64)
    assert err == 0
    assert data == nbd_env["base"][:64]
    client.close()


def test_write_and_trim_rejected_readonly(nbd_env):
    client = connect(nbd_env["port"], b"1")
    assert client.write(0, b"\x99" * 512) == EROFS
    assert client.write(BLOCK_SIZE, b"\x99" * BLOCK_SIZE) == EROFS
    assert client.command(CMD_TRIM, 0, BLOCK_SIZE) == EROFS
    # FLUSH 成功；读仍正常（写未生效）
    assert client.command(CMD_FLUSH) == 0
    err, data = client.read(0, 512)
    assert err == 0
    assert data == nbd_env["base"][:512]
    client.close()


def test_pipelined_requests_replied_in_order(nbd_env):
    """客户端流水线连发多个请求，服务端按序应答（qemu 的工作方式）。"""
    client = connect(nbd_env["port"], b"3")
    for i in range(8):
        client.sock.sendall(
            struct.pack(
                ">IHHQQI", REQUEST_MAGIC, 0, CMD_READ, 0x100 + i, i * BLOCK_SIZE, BLOCK_SIZE
            )
        )
    for i in range(8):
        magic, error, handle = struct.unpack(">IIQ", _recv_exact(client.sock, 16))
        assert magic == SIMPLE_REPLY_MAGIC
        assert error == 0
        assert handle == 0x100 + i
        data = _recv_exact(client.sock, BLOCK_SIZE)
        assert data == nbd_env["v3"][i * BLOCK_SIZE : (i + 1) * BLOCK_SIZE]
    client.close()


# ------------------------------------------------------------------ #
# 版本钉住与并发
# ------------------------------------------------------------------ #
def test_connected_client_keeps_reading_selected_version(nbd_env):
    """增量提交新版后，已连接的客户端继续读所选版；新连接才能看到新版。"""
    store = nbd_env["store"]
    old = connect(nbd_env["port"], b"2")
    err, before = old.read(7 * BLOCK_SIZE, BLOCK_SIZE)
    assert err == 0

    # 提交 v4：覆盖块7
    block7 = b"\x44" * BLOCK_SIZE
    img_v4 = bytearray(nbd_env["v3"])
    img_v4[7 * BLOCK_SIZE : 8 * BLOCK_SIZE] = block7
    store.commit(3, [write_change(7, block7)], sha256_hex(bytes(img_v4)))
    assert store.current.version == 4

    # 已连接客户端（选的是 v2）：块7 仍是 v2 内容，块2 仍是 v2 覆盖
    err, after = old.read(7 * BLOCK_SIZE, BLOCK_SIZE)
    assert err == 0
    assert after == before == nbd_env["v2"][7 * BLOCK_SIZE : 8 * BLOCK_SIZE]
    err, data = old.read(2 * BLOCK_SIZE, BLOCK_SIZE)
    assert err == 0 and data == nbd_env["block2"]
    old.close()

    # 新连接选 v4 才能读到块7 新内容；空名（当前版）同样看到 v4
    new = connect(nbd_env["port"], b"4")
    err, data = new.read(7 * BLOCK_SIZE, BLOCK_SIZE)
    assert err == 0 and data == block7
    new.close()
    cur = connect(nbd_env["port"], b"")
    err, data = cur.read(7 * BLOCK_SIZE, BLOCK_SIZE)
    assert err == 0 and data == block7
    cur.close()


def test_parallel_connections_read_independently(nbd_env, image_size):
    """多连接并发读：各自选对版本，结果互不干扰。"""
    errors: list[Exception] = []

    def worker(version: bytes, expected: bytes):
        try:
            client = connect(nbd_env["port"], version)
            for _ in range(5):
                for index in range(0, 11, 3):
                    err, data = client.read(index * BLOCK_SIZE, BLOCK_SIZE)
                    assert err == 0
                    start = index * BLOCK_SIZE
                    assert data == expected[start : start + BLOCK_SIZE]
            client.close()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(v, e))
        for v, e in ((b"1", nbd_env["base"]), (b"2", nbd_env["v2"]), (b"3", nbd_env["v3"]))
        for _ in range(3)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert errors == []


def test_slow_connection_does_not_block_others_or_commits(tmp_path, monkeypatch):
    """慢连接隔离 + 慢连接期间换版（任务点名的自测场景）。

    客户端 A 钉住 v1 请求整盘后停止收包；期间：
    * 另一个 NBD 客户端 B 仍能正常读盘；
    * 原 HTTP 接口仍能提交增量（发布 v2）；
    * A 恢复收包后拿到的仍是 v1 内容；
    * 新连接选 v2 读到新内容。
    """
    import httpx
    import uvicorn

    from app.main import create_app

    image_size = 32 * 1024 * 1024
    data_dir = tmp_path / "data"
    boot = BlockStore(data_dir)
    boot.init_baseline(image_size)
    baseline = b"".join(boot.iter_image())
    boot.close()

    monkeypatch.setenv("NBD_HOST", "127.0.0.1")
    monkeypatch.setenv("NBD_PORT", "0")  # ephemeral 端口
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
        http_port = server.servers[0].sockets[0].getsockname()[1]
        nbd_port = app.state.nbd_server.address[1]

        # —— 慢客户端 A：钉住 v1，请求整盘 32MiB，读一点就停 ——
        # 默认缓冲区即可：32MiB 超过内核收发缓冲总和，服务端线程会堵在 send，
        # 直到 A 恢复收包（不要用超小 SO_RCVBUF：loopback MSS ~64KiB，
        # 窗口小于 1 个 MSS 会触发 SWS 规避 + 延迟 ACK，吞吐崩塌拖慢测试）
        a = socket.create_connection(("127.0.0.1", nbd_port), timeout=10)
        _recv_exact(a, 18)  # 服务端问候
        a.sendall(struct.pack(">I", 0x3))
        name = b"1"
        payload = struct.pack(">I", len(name)) + name + struct.pack(">H", 0)
        a.sendall(struct.pack(">QII", IHAVEOPT, OPT_GO, len(payload)) + payload)
        while True:  # 收掉 GO 的 INFO + ACK 应答
            _, _, rtype, length = struct.unpack(">QIII", _recv_exact(a, 20))
            if length:
                _recv_exact(a, length)
            if rtype == REP_ACK:
                break
        a.sendall(struct.pack(">IHHQQI", REQUEST_MAGIC, 0, CMD_READ, 0x11, 0, image_size))
        magic, error, _ = struct.unpack(">IIQ", _recv_exact(a, 16))
        assert magic == SIMPLE_REPLY_MAGIC and error == 0
        first = a.recv(4096)
        assert first
        time.sleep(0.5)  # A 停在这里：服务端发送缓冲区逐渐填满，线程堵在 send

        with httpx.Client(base_url=f"http://127.0.0.1:{http_port}", timeout=10) as http:
            # 1) 慢连接期间，另一个 NBD 客户端 B 仍能正常读盘
            b = connect(nbd_port, b"1")
            err, data = b.read(BLOCK_SIZE, BLOCK_SIZE)
            assert err == 0
            assert data == baseline[BLOCK_SIZE : 2 * BLOCK_SIZE]
            b.close()

            # 2) 慢连接期间，原 HTTP 接口仍能提交增量
            new_block = b"\x5c" * BLOCK_SIZE
            target = new_block + baseline[BLOCK_SIZE:]
            r = http.post(
                "/recover/commit",
                json={
                    "parent_version": 1,
                    "target_sha256": sha256_hex(target),
                    "changes": [write_change(0, new_block)],
                },
            )
            assert r.status_code == 201, r.text
            assert http.get("/version").json()["version"] == 2

            # 3) A 恢复收包：必须仍是钉住的 v1 内容
            received = bytearray(first)
            while len(received) < image_size:
                chunk = a.recv(1 << 20)
                assert chunk
                received.extend(chunk)
            assert bytes(received) == baseline
            a.close()

            # 4) 新连接选 v2 读到新内容
            c = connect(nbd_port, b"2")
            err, data = c.read(0, BLOCK_SIZE)
            assert err == 0 and data == new_block
            c.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)


# ------------------------------------------------------------------ #
# 真实 QEMU 客户端（qemu-io / qemu-img 在 PATH 中时启用）
#
# 注意：qemu 块层以 512 字节扇区为粒度记录设备大小，会把非对齐的导出
# 大小向上取整并尝试读取填充区（服务端按设计对越界读回 EOVERFLOW）。
# 真实磁盘镜像天然扇区对齐，故本组用例使用 512 对齐的镜像大小。
# ------------------------------------------------------------------ #
QEMU_IO = shutil.which("qemu-io")
QEMU_IMG = shutil.which("qemu-img")


@pytest.fixture
def qemu_env(tmp_path):
    """512 对齐镜像（10 个整块 + 512 字节末块）；v2 丢块0+写块2；v3 写块5。"""
    image_size = 10 * BLOCK_SIZE + 512
    store = BlockStore(tmp_path / "data")
    store.init_baseline(image_size)
    base = expected_image(store)

    img_v2 = bytearray(base)
    img_v2[0:BLOCK_SIZE] = b"\x00" * BLOCK_SIZE
    img_v2[2 * BLOCK_SIZE : 3 * BLOCK_SIZE] = b"\x77" * BLOCK_SIZE
    store.commit(
        1,
        [{"index": 0, "op": "discard"}, write_change(2, b"\x77" * BLOCK_SIZE)],
        sha256_hex(bytes(img_v2)),
    )
    img_v3 = bytearray(img_v2)
    img_v3[5 * BLOCK_SIZE : 6 * BLOCK_SIZE] = b"\x33" * BLOCK_SIZE
    store.commit(2, [write_change(5, b"\x33" * BLOCK_SIZE)], sha256_hex(bytes(img_v3)))

    server = NBDServer(store, "127.0.0.1", 0)
    _, port = server.start()
    try:
        yield {"port": port, "images": (bytes(base), bytes(img_v2), bytes(img_v3))}
    finally:
        server.stop()
        store.close()


@pytest.mark.skipif(QEMU_IMG is None, reason="qemu-img 不在 PATH")
def test_qemu_img_convert_matches_each_committed_version(qemu_env, tmp_path):
    """qemu-img 把每个已提交版本整盘导出，逐字节比对（含 512 字节末块）。"""
    port = qemu_env["port"]
    for idx, expected in enumerate(qemu_env["images"], start=1):
        out = tmp_path / f"qemu-v{idx}.raw"
        subprocess.run(
            [QEMU_IMG, "convert", "-O", "raw", f"nbd://127.0.0.1:{port}/{idx}", str(out)],
            check=True,
            capture_output=True,
            timeout=60,
        )
        assert out.read_bytes() == expected


@pytest.mark.skipif(QEMU_IO is None, reason="qemu-io 不在 PATH")
def test_qemu_io_cross_block_read_and_write_rejected(qemu_env):
    port = qemu_env["port"]
    # 跨块读（v2）：块0 尾部=丢弃读零、块1=基线、块2=0x77，一次非对齐请求取回
    proc = subprocess.run(
        [QEMU_IO, "-r", "-c", "read -v 4000 8192", f"nbd://127.0.0.1:{port}/2"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "00000fa0:  00 00 00 00 00 00 00 00" in proc.stdout  # 丢弃块读零
    assert " 77 77 77 77 77 77 77 77" in proc.stdout  # v2 覆盖块
    # 写请求：默认读写方式打开即在握手期被拒
    proc = subprocess.run(
        [QEMU_IO, "-c", "write 0 512", f"nbd://127.0.0.1:{port}/2"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode != 0
    assert "permission denied" in proc.stderr.lower()
    # 只读打开后写命令同样被拒
    proc = subprocess.run(
        [QEMU_IO, "-r", "-c", "write 0 512", f"nbd://127.0.0.1:{port}/2"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "read-only" in proc.stderr.lower()
