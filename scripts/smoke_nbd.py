"""真实 NBD 冒烟：fixed newstyle 握手、按版号选版、跨块读、写/越界错误、与 HTTP 侧比对。

用法：
    RECOVERY_DATA_DIR=/tmp/demo NBD_PORT=10809 uvicorn app.main:app --port 8000 &
    python scripts/smoke_nbd.py            # 默认连 127.0.0.1:10809，HTTP 取 SMOKE_BASE

环境变量：
    NBD_ADDR    NBD 地址（默认 127.0.0.1:10809）
    SMOKE_BASE  HTTP 基址（默认 http://127.0.0.1:8000）
"""

import hashlib
import json
import os
import socket
import struct
import urllib.request

NBD_HOST, _, NBD_PORT = os.environ.get("NBD_ADDR", "127.0.0.1:10809").rpartition(":")
NBD_PORT = int(NBD_PORT or "10809")
SMOKE_BASE = os.environ.get("SMOKE_BASE", "http://127.0.0.1:8000")

NBDMAGIC = 0x4E42444D41474943
IHAVEOPT = 0x49484156454F5054
REQUEST_MAGIC = 0x25609513
SIMPLE_REPLY_MAGIC = 0x67446698
OPTION_REPLY_MAGIC = 0x3E889045565A9
OPT_GO = 7
REP_ACK = 1
CMD_READ, CMD_WRITE = 0, 1
EROFS, EOVERFLOW = 30, 75


def recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("对端关闭连接")
        buf.extend(chunk)
    return bytes(buf)


def nbd_connect(export: str):
    """fixed newstyle 握手 + NBD_OPT_GO，返回 (sock, size, flags)。"""
    sock = socket.create_connection((NBD_HOST, NBD_PORT), timeout=10)
    magic, ihaveopt, flags = struct.unpack(">QQH", recv_exact(sock, 18))
    assert magic == NBDMAGIC and ihaveopt == IHAVEOPT and flags & 0x1
    sock.sendall(struct.pack(">I", 0x3))  # FIXED_NEWSTYLE | NO_ZEROES
    name = export.encode()
    payload = struct.pack(">I", len(name)) + name + struct.pack(">H", 0)
    sock.sendall(struct.pack(">QII", IHAVEOPT, OPT_GO, len(payload)) + payload)
    size = trans_flags = None
    while True:
        magic, option, rtype, length = struct.unpack(">QIII", recv_exact(sock, 20))
        assert magic == OPTION_REPLY_MAGIC
        data = recv_exact(sock, length) if length else b""
        if rtype == REP_ACK:
            break
        assert not rtype & 0x80000000, f"导出 {export!r} 被拒绝: {rtype:#x}"
        if data[:2] == b"\x00\x00":  # NBD_INFO_EXPORT
            size, trans_flags = struct.unpack(">QH", data[2:12])
    return sock, size, trans_flags


def nbd_request(sock, cmd, offset, length, handle=0xA5, payload=b""):
    sock.sendall(struct.pack(">IHHQQI", REQUEST_MAGIC, 0, cmd, handle, offset, length) + payload)
    magic, error, rhandle = struct.unpack(">IIQ", recv_exact(sock, 16))
    assert magic == SIMPLE_REPLY_MAGIC and rhandle == handle
    return error


def nbd_read(sock, offset, length):
    error = nbd_request(sock, CMD_READ, offset, length)
    assert error == 0, f"读 [{offset}, +{length}) 失败: errno={error}"
    return recv_exact(sock, length)


# 1. HTTP 侧查版号（/docs 里可交互操作 /version）
version = json.loads(urllib.request.urlopen(SMOKE_BASE + "/version").read())
print("1) 当前版本:", version)
current, image_size = version["version"], version["image_size"]

# 2. NBD 按十进制版号连接当前版
sock, size, flags = nbd_connect(str(current))
assert size == image_size, (size, image_size)
assert flags & 0x2, "导出必须是只读"
print(f"2) NBD 连接导出 {current!r} 成功: size={size} flags={flags:#x}（只读）")

# 3. 分块（跨块、非对齐）读整盘，与 /version 摘要比对
received = bytearray()
chunk = 1024 * 1024 + 7  # 故意非 4 KiB 对齐，强制跨块
for offset in range(0, image_size, chunk):
    received += nbd_read(sock, offset, min(chunk, image_size - offset))
assert len(received) == image_size
assert hashlib.sha256(bytes(received)).hexdigest() == version["sha256"]
print("3) 跨块整盘读取完成，sha256 与 /version 一致:", version["sha256"][:16], "...")

# 4. 写请求 → EROFS；越界读 → EOVERFLOW
assert nbd_request(sock, CMD_WRITE, 0, 512, payload=b"\x00" * 512) == EROFS
assert nbd_request(sock, CMD_READ, image_size - 10, 20) == EOVERFLOW
assert nbd_request(sock, CMD_READ, image_size, 1) == EOVERFLOW
print("4) 写请求返回 EROFS，越界读返回 EOVERFLOW")
sock.close()

# 5. 旧版也可连接；未提交版被拒绝
if current >= 1:
    old, old_size, _ = nbd_connect("1")
    assert old_size == image_size
    old.close()
    print("5) 旧版 '1' 可连接")
try:
    nbd_connect(str(current + 1))
except AssertionError as exc:
    print(f"6) 未提交版 {current + 1} 被正确拒绝: {exc}")
else:
    raise SystemExit("未提交版未被拒绝！")

print("\nNBD 冒烟断言全部通过")
