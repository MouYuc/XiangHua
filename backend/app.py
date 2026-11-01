"""FastAPI application factory — the composition root.

Route handlers live in :mod:`backend.api.routers`, one module per product area.
This module owns only what must happen exactly once per process:

* middleware order (request guard outermost, then CORS);
* lifespan work (job-snapshot persistence + background model warm-up);
* exception handlers that give every error a stable machine-readable ``code``;
* the double ``/api`` + ``/v1`` mount;
* the SPA fallback for the bundled WebUI.

Before this split the whole application was one 1085-line ``create_app()``
closure holding 76 routes and every shared helper; nothing could be imported,
tested or reviewed in isolation.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.api import deps
from backend.api.routers import (
    dialogue,
    media,
    platform,
    synthesis,
    system,
    timbre,
    training,
    voices,
)
from backend.infra.logging import configure_logging, get_logger
from backend.services import core, media_ingest
from backend.services.gpu_arbiter import GpuBusyTimeout
from backend.services.job_store import JobStore
from backend.services.request_guard import RequestGuardMiddleware
from backend.services.synthesis_jobs import SYNTHESIS_JOBS

logger = get_logger("xianghua.app")

#: Product routers, in mount order. Each is mounted under both ``/api`` and ``/v1``.
ROUTERS = (system, synthesis, voices, timbre, dialogue, training, media, platform)

# Re-exported so existing tests and scripts can keep importing them from
# ``backend.app``; the implementations live in ``backend.api.deps``.
__all__ = [
    "SYNTHESIS_JOBS",
    "_error_code",
    "_merge_config",
    "_upload_filename",
    "app",
    "create_app",
]
_merge_config = deps._merge_config
_upload_filename = deps._upload_filename
_error_code = deps._error_code


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Start-up work that must not delay the first request.

    Both steps are best-effort by design: a broken job store or a GPU that is
    busy with a training run must not stop the API from serving ``/api/health``
    and the WebUI shell.
    """
    configure_logging()
    store = JobStore(core.CONFIG_DIR)
    try:
        SYNTHESIS_JOBS.enable_persistence(store)
    except Exception as exc:  # pragma: no cover - disk-dependent
        logger.warning("任务快照持久化启用失败，重启后历史任务不可见：%s", exc)
    # Bound the on-disk job history at start-up. Before this, snapshots were
    # never removed and the directory grew for the life of the install.
    try:
        purged = store.purge(keep=200)
        if purged:
            logger.info("启动清理：删除 %s 个过期任务快照。", purged)
    except Exception as exc:  # pragma: no cover - disk-dependent
        logger.warning("任务快照清理失败：%s", exc)
    core.SYNTHESIS_BACKEND.start_warmup(core.load_config())
    logger.info("像话 API 已启动。")
    yield
    logger.info("像话 API 已停止。")


def _install_exception_handlers(app: FastAPI) -> None:
    """One error shape for the whole API: ``{"detail": <中文>, "code": <STABLE>}``."""

    @app.exception_handler(media_ingest.MediaIngestError)
    async def media_ingest_error(_request: Any, exc: media_ingest.MediaIngestError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={"detail": str(exc), "code": "MEDIA_INGEST_FAILED"},
        )

    @app.exception_handler(GpuBusyTimeout)
    async def gpu_busy_error(_request: Any, exc: GpuBusyTimeout) -> JSONResponse:
        """Another job owns the card: 409 with the holder named, not a 500."""
        return JSONResponse(
            status_code=409,
            content={"detail": str(exc), "code": "GPU_BUSY", "holder": exc.holder},
        )

    @app.exception_handler(StarletteHTTPException)
    async def api_error(_request: Any, exc: StarletteHTTPException) -> JSONResponse:
        """Give every error a stable ``code`` next to the human ``detail``.

        The WebUI reads ``detail`` and keeps working; API clients can branch on
        ``code`` without parsing Chinese prose.
        """
        detail = exc.detail
        if isinstance(detail, dict) and "code" in detail:
            return JSONResponse(status_code=exc.status_code, content=detail)
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "detail": detail if isinstance(detail, str) else str(detail),
                "code": deps._error_code(exc.status_code),
            },
        )


def _mount_frontend(app: FastAPI) -> None:
    """Serve the built SPA, if a build exists.

    Mounted last so a real API route always wins. The catch-all refuses to
    answer for ``api/``/``v1/`` paths (a mistyped API URL must be a JSON 404, not
    the HTML shell) and refuses to resolve outside ``dist``.
    """
    frontend_dist = Path(os.environ.get("XIANGHUA_FRONTEND_DIST", core.REPO_ROOT / "frontend" / "dist"))
    if not frontend_dist.is_dir():
        return
    assets = frontend_dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="frontend-assets")

    @app.get("/{frontend_path:path}", include_in_schema=False)
    def frontend(frontend_path: str) -> FileResponse:
        if frontend_path.startswith(("api/", "v1/")):
            raise HTTPException(status_code=404, detail="接口不存在。")
        # 路径解析后必须仍落在 dist 内：百分号编码的 ../ 或绝对盘符
        # （例如 /%2e%2e/%2e%2e/backend/app.py）不允许读到 dist 外的文件。
        dist_root = frontend_dist.resolve()
        candidate = (frontend_dist / frontend_path).resolve()
        if frontend_path and candidate.is_relative_to(dist_root) and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(frontend_dist / "index.html")


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(title="XiangHua API", version="1.1.0", lifespan=_lifespan)
    # Order matters: middleware added later sits *outside*, so the guard — which
    # validates Host/Origin and rejects /v1 calls without a key before the body is
    # read — is registered after CORS on purpose.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(RequestGuardMiddleware)

    _install_exception_handlers(app)

    # One handler set, two prefixes: ``/api`` for the bundled WebUI, ``/v1`` as the
    # public contract, so an internal UI change can never silently shift the API.
    for prefix in ("/api", "/v1"):
        for module in ROUTERS:
            app.include_router(module.router, prefix=prefix)

    _mount_frontend(app)
    return app


app = create_app()
