"""NBD 只读导出：让 QEMU 把指定版本的灾备盘当作只读块设备按需读取。

* 导出名 = 十进制版号（空名 = 当前版）；只接受已提交版本（1..当前版），旧版也可连接；
* fixed newstyle 握手：支持 NBD_OPT_GO / NBD_OPT_EXPORT_NAME / NBD_OPT_INFO /
  NBD_OPT_LIST / NBD_OPT_ABORT，其余选项（structured reply、TLS、meta context 等）
  一律回 NBD_REP_ERR_UNSUP / NBD_REP_ERR_POLICY，qemu 会自动回退到简单应答；
* 连接在握手时钉住所选版本：之后提交新版，已连接客户端仍读所选旧版内容；
* 每连接独立线程收发：单个连接收包变慢只阻塞它自己的线程，
  不影响其他连接读盘，也不影响 HTTP 接口提交增量；
* 读取只访问请求范围涉及的块（跨块自动拼接），丢弃块读零，末块按真实长度返回；
* 越界读返回 NBD_EOVERFLOW；写/修剪类请求返回 NBD_EROFS（导出恒为只读）。
"""

from __future__ import annotations

import logging
import socket
import struct
import threading

from .storage import BLOCK_SIZE, BlockStore, StorageError

logger = logging.getLogger("disk_recovery.nbd")

# ------------------------------------------------------------------ #
# 协议常量（NBD protocol: fixed newstyle）
# ------------------------------------------------------------------ #
NBDMAGIC = 0x4E42444D41474943          # "NBDMAGIC"
IHAVEOPT = 0x49484156454F5054          # "IHAVEOPT"
REQUEST_MAGIC = 0x25609513
SIMPLE_REPLY_MAGIC = 0x67446698
OPTION_REPLY_MAGIC = 0x3E889045565A9

FLAG_FIXED_NEWSTYLE = 0x0001           # 握手阶段 server/client 通用
FLAG_NO_ZEROES = 0x0002

# 传输阶段 export flags
TRANSMISSION_FLAGS = (
    0x0001        # NBD_FLAG_HAS_FLAGS
    | 0x0002      # NBD_FLAG_READ_ONLY
    | 0x0004      # NBD_FLAG_SEND_FLUSH
    | 0x0100      # NBD_FLAG_CAN_MULTI_CONN
)

# 协商阶段选项
OPT_EXPORT_NAME = 1
OPT_ABORT = 2
OPT_LIST = 3
OPT_STARTTLS = 5
OPT_INFO = 6
OPT_GO = 7
OPT_STRUCTURED_REPLY = 8
OPT_LIST_META_CONTEXT = 9
OPT_SET_META_CONTEXT = 10

# 选项应答类型
REP_ACK = 1
REP_SERVER = 2
REP_INFO = 3
REP_ERR_UNSUP = 0x80000001
REP_ERR_POLICY = 0x80000002
REP_ERR_INVALID = 0x80000003
REP_ERR_UNKNOWN = 0x80000006

# NBD_INFO_* 信息类型
INFO_EXPORT = 0
INFO_NAME = 1
INFO_DESCRIPTION = 2
INFO_BLOCK_SIZE = 3

# 传输阶段命令
CMD_READ = 0
CMD_WRITE = 1
CMD_DISC = 2
CMD_FLUSH = 3
CMD_TRIM = 4
CMD_CACHE = 5
CMD_WRITE_ZEROES = 6

# 传输阶段错误码（Linux errno）
EPERM = 1
EIO = 5
EINVAL = 22
EROFS = 30
EOVERFLOW = 75

# 块尺寸约束（通过 NBD_INFO_BLOCK_SIZE 告知客户端）
MIN_BLOCK = 1                          # 无对齐约束，允许任意偏移/长度
PREFERRED_BLOCK = BLOCK_SIZE
MAX_REQUEST = 32 * 1024 * 1024         # 单请求上限 32 MiB，超出回 EINVAL

EXPORT_DESCRIPTION = "disk-recovery read-only snapshot"

_MAX_OPTION_DATA = 1 << 20             # 协商选项数据上限 1 MiB（防内存滥用）


class NBDProtocolError(Exception):
    """客户端协议违规：直接关闭连接。"""


class NBDServer:
    """多线程 NBD 服务：每连接一个线程，共享同一 BlockStore 只读视图。"""

    def __init__(self, store: BlockStore, host: str = "0.0.0.0", port: int = 10809):
        self.store = store
        self.host = host
        self.port = port
        self._listener: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._connections: set[socket.socket] = set()
        self._connections_lock = threading.Lock()

    @property
    def address(self) -> tuple[str, int] | None:
        """实际绑定地址（port=0 时用于取回 ephemeral 端口）。"""
        if self._listener is None:
            return None
        return self._listener.getsockname()[:2]

    def start(self) -> tuple[str, int]:
        """绑定监听并启动后台 accept 线程，返回实际 (host, port)。"""
        if self._listener is not None:
            raise RuntimeError("NBD 服务已启动")
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.host, self.port))
        listener.listen(128)
        listener.settimeout(0.5)  # accept 循环可及时响应 stop()
        self._listener = listener
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="nbd-accept", daemon=True
        )
        self._accept_thread.start()
        return self.address

    def serve_forever(self) -> None:
        """前台运行（阻塞），供独立进程模式使用。"""
        self.start()
        try:
            while not self._stopping.is_set():
                self._stopping.wait(0.5)
        finally:
            self.stop()

    def stop(self) -> None:
        """停止监听并断开所有活动连接（幂等）。"""
        self._stopping.set()
        listener, self._listener = self._listener, None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        thread, self._accept_thread = self._accept_thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)
        with self._connections_lock:
            connections = list(self._connections)
        for conn in connections:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass

    # ------------------------------------------------------------------ #
    # 连接管理
    # ------------------------------------------------------------------ #
    def _accept_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                conn, addr = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break  # 监听 socket 已关闭
            try:
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            with self._connections_lock:
                self._connections.add(conn)
            threading.Thread(
                target=self._run_connection,
                args=(conn, addr),
                name=f"nbd-{addr[0]}:{addr[1]}",
                daemon=True,
            ).start()

    def _run_connection(self, conn: socket.socket, addr) -> None:
        # 单连接独立线程：该连接上的任何阻塞（如对端收包慢）都只影响本线程
        try:
            NBDSession(self.store, conn).run()
        except (NBDProtocolError, EOFError, ConnectionError, OSError) as exc:
            logger.debug("NBD 连接 %s 结束: %r", addr, exc)
        except Exception:
            logger.exception("NBD 连接 %s 处理异常", addr)
        finally:
            with self._connections_lock:
                self._connections.discard(conn)
            try:
                conn.close()
            except OSError:
                pass


class NBDSession:
    """单个客户端连接：fixed newstyle 握手 + 传输阶段服务。"""

    def __init__(self, store: BlockStore, conn: socket.socket):
        self.store = store
        self.conn = conn

    def run(self) -> None:
        version = self._negotiate()
        self._serve(version)

    # ------------------------------------------------------------------ #
    # 握手与选项协商
    # ------------------------------------------------------------------ #
    def _negotiate(self) -> int:
        """完成握手与选项协商，返回客户端选定的版本号。"""
        self._send(
            struct.pack(">QQH", NBDMAGIC, IHAVEOPT, FLAG_FIXED_NEWSTYLE | FLAG_NO_ZEROES)
        )
        (client_flags,) = struct.unpack(">I", self._recv_exact(4))
        if not client_flags & FLAG_FIXED_NEWSTYLE:
            raise NBDProtocolError("客户端未声明 fixed newstyle，拒绝继续")
        no_zeroes = bool(client_flags & FLAG_NO_ZEROES)

        while True:
            magic, option, length = struct.unpack(">QII", self._recv_exact(16))
            if magic != IHAVEOPT:
                raise NBDProtocolError(f"选项魔数错误: {magic:#018x}")
            if length > _MAX_OPTION_DATA:
                raise NBDProtocolError(f"选项数据过长: {length}")
            data = self._recv_exact(length) if length else b""

            if option == OPT_EXPORT_NAME:
                # 该选项无应答通道：未知导出名只能硬断开
                version = self._resolve_export(data)
                if version is None:
                    raise NBDProtocolError("未知导出名，按协议硬断开")
                payload = struct.pack(">QH", self._image_size(), TRANSMISSION_FLAGS)
                if not no_zeroes:
                    payload += b"\x00" * 124
                self._send(payload)
                return version
            if option == OPT_GO:
                version = self._handle_info_or_go(data, go=True)
                if version is not None:
                    return version
                continue
            if option == OPT_INFO:
                self._handle_info_or_go(data, go=False)
                continue
            if option == OPT_LIST:
                self._handle_list(data)
                continue
            if option == OPT_ABORT:
                self._send_option_reply(OPT_ABORT, REP_ACK)
                raise EOFError("客户端主动中止协商")
            if option == OPT_STARTTLS:
                self._send_option_reply(OPT_STARTTLS, REP_ERR_POLICY)
                continue
            # STRUCTURED_REPLY / META_CONTEXT / 其他未知选项：不支持，客户端应回退
            self._send_option_reply(option, REP_ERR_UNSUP)

    def _resolve_export(self, raw_name: bytes) -> int | None:
        """把导出名解析为已提交版本号；空名 = 当前版。非法返回 None。"""
        try:
            name = raw_name.decode("ascii")
        except UnicodeDecodeError:
            return None
        current = self.store.snapshot_state().version
        if name == "":
            return current
        if not name.isdigit() or len(name) > 10:
            return None
        version = int(name)
        if version < 1 or version > current:
            return None
        return version

    def _image_size(self) -> int:
        # 镜像大小跨版本恒定（提交校验强制），取当前状态即可
        return self.store.snapshot_state().image_size

    def _handle_info_or_go(self, data: bytes, go: bool) -> int | None:
        """处理 NBD_OPT_GO / NBD_OPT_INFO；GO 成功时返回版本号并进入传输阶段。"""
        option = OPT_GO if go else OPT_INFO
        if len(data) < 6:
            self._send_option_reply(option, REP_ERR_INVALID)
            return None
        (name_len,) = struct.unpack(">I", data[:4])
        if 4 + name_len + 2 > len(data):
            self._send_option_reply(option, REP_ERR_INVALID)
            return None
        raw_name = data[4 : 4 + name_len]
        (num_infos,) = struct.unpack(">H", data[4 + name_len : 6 + name_len])
        infos = data[6 + name_len : 6 + name_len + 2 * num_infos]
        if len(infos) != 2 * num_infos:
            self._send_option_reply(option, REP_ERR_INVALID)
            return None
        requested = set(struct.unpack(f">{num_infos}H", infos)) if num_infos else set()

        version = self._resolve_export(raw_name)
        if version is None:
            self._send_option_reply(option, REP_ERR_UNKNOWN)
            return None

        # NBD_INFO_EXPORT 必须首先返回
        self._send_info_reply(
            option, INFO_EXPORT, struct.pack(">QH", self._image_size(), TRANSMISSION_FLAGS)
        )
        if INFO_NAME in requested:
            canonical = raw_name or str(version).encode("ascii")
            self._send_info_reply(option, INFO_NAME, canonical)
        if INFO_DESCRIPTION in requested:
            self._send_info_reply(option, INFO_DESCRIPTION, EXPORT_DESCRIPTION.encode())
        if INFO_BLOCK_SIZE in requested:
            self._send_info_reply(
                option,
                INFO_BLOCK_SIZE,
                struct.pack(">III", MIN_BLOCK, PREFERRED_BLOCK, MAX_REQUEST),
            )
        self._send_option_reply(option, REP_ACK)
        return version if go else None

    def _handle_list(self, data: bytes) -> None:
        """NBD_OPT_LIST：列出全部已提交版本（或指定名字的单个导出）。"""
        if len(data) < 4:
            self._send_option_reply(OPT_LIST, REP_ERR_INVALID)
            return
        (name_len,) = struct.unpack(">I", data[:4])
        raw_name = data[4 : 4 + name_len]
        rest = data[4 + name_len :]
        num_infos = struct.unpack(">H", rest[:2])[0] if len(rest) >= 2 else 0
        infos = rest[2 : 2 + 2 * num_infos]
        requested = set(struct.unpack(f">{num_infos}H", infos)) if num_infos else set()
        want_desc = INFO_DESCRIPTION in requested

        if raw_name:
            version = self._resolve_export(raw_name)
            versions = [version] if version is not None else []
        else:
            versions = list(range(1, self.store.snapshot_state().version + 1))
        for version in versions:
            name = str(version).encode("ascii")
            payload = struct.pack(">I", len(name)) + name
            if want_desc:
                desc = EXPORT_DESCRIPTION.encode()
                payload += struct.pack(">I", len(desc)) + desc
            self._send_option_reply(OPT_LIST, REP_SERVER, payload)
        self._send_option_reply(OPT_LIST, REP_ACK)

    # ------------------------------------------------------------------ #
    # 传输阶段
    # ------------------------------------------------------------------ #
    def _serve(self, version: int) -> None:
        """请求处理循环：version 在握手时钉住，不受后续提交影响。"""
        image_size = self._image_size()
        while True:
            magic, flags, cmd, handle, offset, length = struct.unpack(
                ">IHHQQI", self._recv_exact(28)
            )
            if magic != REQUEST_MAGIC:
                raise NBDProtocolError(f"请求魔数错误: {magic:#010x}")

            if cmd == CMD_READ:
                if length == 0:
                    self._send_simple_reply(handle, 0)
                    continue
                if length > MAX_REQUEST:
                    self._send_simple_reply(handle, EINVAL)
                    continue
                if offset > image_size or length > image_size - offset:
                    self._send_simple_reply(handle, EOVERFLOW)
                    continue
                try:
                    data = self.store.read_range(offset, length, max_version=version)
                except StorageError:
                    logger.exception("NBD 读取失败: version=%s offset=%s length=%s", version, offset, length)
                    self._send_simple_reply(handle, EIO)
                    continue
                self._send_simple_reply(handle, 0)
                self._send(data)
            elif cmd == CMD_WRITE:
                # 必须先消费写载荷再回错，否则后续报文错位
                self._discard(length)
                self._send_simple_reply(handle, EROFS)
            elif cmd == CMD_FLUSH:
                self._send_simple_reply(handle, 0)
            elif cmd == CMD_DISC:
                return
            elif cmd in (CMD_TRIM, CMD_WRITE_ZEROES):
                self._send_simple_reply(handle, EROFS)
            else:  # CMD_CACHE 及未知命令
                self._send_simple_reply(handle, EINVAL)

    # ------------------------------------------------------------------ #
    # 报文工具
    # ------------------------------------------------------------------ #
    def _send_option_reply(self, option: int, reply_type: int, data: bytes = b"") -> None:
        self._send(
            struct.pack(">QIII", OPTION_REPLY_MAGIC, option, reply_type, len(data)) + data
        )

    def _send_info_reply(self, option: int, info_type: int, data: bytes) -> None:
        self._send_option_reply(option, REP_INFO, struct.pack(">H", info_type) + data)

    def _send_simple_reply(self, handle: int, error: int) -> None:
        self._send(struct.pack(">IIQ", SIMPLE_REPLY_MAGIC, error, handle))

    def _send(self, data: bytes) -> None:
        self.conn.sendall(data)

    def _recv_exact(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = self.conn.recv(n - len(buf))
            if not chunk:
                raise EOFError("对端关闭连接")
            buf.extend(chunk)
        return bytes(buf)

    def _discard(self, n: int) -> None:
        """消费并丢弃 n 字节（写请求载荷）。"""
        while n > 0:
            chunk = self.conn.recv(min(n, 65536))
            if not chunk:
                raise EOFError("对端关闭连接")
            n -= len(chunk)
