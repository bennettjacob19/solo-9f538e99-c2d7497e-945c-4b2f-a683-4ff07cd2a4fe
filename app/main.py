"""单盘恢复 FastAPI 服务。

接口：
* GET  /health           健康检查
* GET  /version          当前版本号与摘要
* POST /recover/commit   提交一批 4 KiB 块变动（覆盖/丢弃），发布新版本
* GET  /image            流式下载当前 raw 镜像（下载开始时固定版本快照）

交互式文档由 FastAPI 内置提供在 /docs（Swagger UI）与 /redoc。

另可选启动 NBD 只读导出（环境变量 NBD_PORT 非空即启用，NBD_HOST 默认 0.0.0.0）：
QEMU 以导出名 = 十进制版号连接（如 nbd://host:10809/2），把该版灾备盘当作
只读块设备按需读取；连接建立时钉住所选版本，之后提交新版不影响已连接客户端。
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .nbd_server import NBDServer
from .storage import BlockStore, ConflictError, StorageError

logger = logging.getLogger("disk_recovery")


def _default_data_dir() -> str:
    return os.environ.get("RECOVERY_DATA_DIR", "/data")


@asynccontextmanager
async def lifespan(app: FastAPI):
    data_dir = getattr(app.state, "data_dir", None) or _default_data_dir()
    store = BlockStore(data_dir)
    store.open()
    app.state.store = store
    # NBD 只读导出：NBD_PORT 非空即启用（与 HTTP 同进程，共享同一 BlockStore）
    nbd_server: NBDServer | None = None
    nbd_port = os.environ.get("NBD_PORT", "").strip()
    if nbd_port:
        nbd_host = os.environ.get("NBD_HOST", "0.0.0.0").strip() or "0.0.0.0"
        nbd_server = NBDServer(store, host=nbd_host, port=int(nbd_port))
        bound_host, bound_port = nbd_server.start()
        app.state.nbd_server = nbd_server
        # 业务日志默认无输出渠道：接入 uvicorn 的 handler，容器日志可见
        uvicorn_logger = logging.getLogger("uvicorn")
        if uvicorn_logger.handlers and not logger.handlers:
            logger.handlers = list(uvicorn_logger.handlers)
            logger.setLevel(logging.INFO)
            logger.propagate = False
        logger.info(
            "NBD 只读导出已监听 %s:%s（导出名 = 十进制版号，如 nbd://host:%s/1）",
            bound_host,
            bound_port,
            bound_port,
        )
    try:
        yield
    finally:
        if nbd_server is not None:
            nbd_server.stop()
        store.close()


class BlockChange(BaseModel):
    index: int = Field(..., ge=0, description="块号（从 0 开始，每块 4 KiB）")
    op: Literal["write", "discard"] = Field(
        ..., description="write=覆盖该块；discard=丢弃该块（读零）"
    )
    data_base64: str | None = Field(
        None, description="write 操作的块内容（base64，解码后须为 4 KiB，末块按实际长度）"
    )


class CommitRequest(BaseModel):
    parent_version: int = Field(..., ge=1, description="客户端所基于的父版号")
    target_sha256: str = Field(..., description="还原后整盘镜像期望的 SHA-256（十六进制）")
    changes: list[BlockChange] = Field(
        default_factory=list, description="本批要覆盖或丢弃的块；未列出的块沿用父版"
    )


class VersionResponse(BaseModel):
    version: int
    image_size: int
    block_count: int
    sha256: str


class CommitResponse(BaseModel):
    version: int
    parent_version: int
    sha256: str
    image_size: int
    block_count: int


def create_app(data_dir: str | None = None) -> FastAPI:
    app = FastAPI(
        title="单盘灾备恢复 API",
        version="1.0.0",
        description=(
            "把灾备增量包还原为虚拟机 raw 磁盘镜像。块大小固定 4 KiB；"
            "丢弃块读零，未列出块沿用父版；仅当摘要吻合且父版仍为当前版时才发布新版本。"
        ),
        lifespan=lifespan,
    )
    if data_dir is not None:
        app.state.data_dir = data_dir

    @app.exception_handler(ConflictError)
    async def conflict_handler(request: Request, exc: ConflictError):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(StorageError)
    async def storage_handler(request: Request, exc: StorageError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.get("/health", tags=["运维"])
    async def health(request: Request) -> dict:
        state = request.app.state.store.snapshot_state()
        return {"status": "ok", "version": state.version}

    @app.get("/version", response_model=VersionResponse, tags=["版本"])
    async def get_version(request: Request) -> VersionResponse:
        state = request.app.state.store.snapshot_state()
        return VersionResponse(
            version=state.version,
            image_size=state.image_size,
            block_count=state.block_count,
            sha256=state.sha256,
        )

    @app.post(
        "/recover/commit",
        response_model=CommitResponse,
        status_code=201,
        tags=["恢复"],
        summary="提交增量块并发布新版本",
    )
    async def commit(request: Request, payload: CommitRequest) -> CommitResponse:
        store: BlockStore = request.app.state.store
        changes = [change.model_dump() for change in payload.changes]
        # 存储层在线程池中执行（含落盘与整盘摘要计算，避免阻塞事件循环）
        state = await _run_sync(
            store.commit,
            payload.parent_version,
            changes,
            payload.target_sha256,
        )
        return CommitResponse(
            version=state.version,
            parent_version=payload.parent_version,
            sha256=state.sha256,
            image_size=state.image_size,
            block_count=state.block_count,
        )

    @app.get("/image", tags=["镜像"], summary="流式下载当前 raw 镜像")
    async def download_image(request: Request) -> StreamingResponse:
        store: BlockStore = request.app.state.store
        # 下载开始即固定版本：期间提交新版本不影响本次下载内容
        snapshot = store.snapshot_state()

        def generate():
            try:
                for chunk in store.iter_image(max_version=snapshot.version):
                    yield chunk
            except Exception:
                # 流中途出错：记录并中断连接（Content-Length 不足，客户端可识别为不完整）
                # 绝不补零或返回半盘冒充完整镜像
                logger.exception("镜像下载中断: version=%s", snapshot.version)
                raise

        headers = {
            "Content-Length": str(snapshot.image_size),
            "X-Image-Version": str(snapshot.version),
            "X-Image-Sha256": snapshot.sha256,
            "Content-Disposition": f'attachment; filename="disk-v{snapshot.version}.raw"',
        }
        return StreamingResponse(
            generate(),
            media_type="application/octet-stream",
            headers=headers,
        )

    return app


async def _run_sync(func, *args):
    import anyio

    return await anyio.to_thread.run_sync(lambda: func(*args))


app = create_app()
