"""单盘恢复 FastAPI 服务。

接口：
* GET  /health           健康检查
* GET  /version          当前版本号与摘要
* POST /recover/commit   提交一批 4 KiB 块变动（覆盖/丢弃），发布新版本
* GET  /image            流式下载当前 raw 镜像（下载开始时固定版本快照）

交互式文档由 FastAPI 内置提供在 /docs（Swagger UI）与 /redoc。
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .storage import BlockStore, ConflictError, StorageError

logger = logging.getLogger("disk_recovery")


def _default_data_dir() -> str:
    return os.environ.get("RECOVERY_DATA_DIR", "/data")


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    data_dir = getattr(app.state, "data_dir", None) or _default_data_dir()
    store = BlockStore(data_dir)
    store.open()
    app.state.store = store
    try:
        yield
    finally:
        store.close()


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
