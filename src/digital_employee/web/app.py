"""Modular FastAPI Control Plane application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from digital_employee.integrations.feishu_ws import start_feishu_long_connection_if_configured
from digital_employee.integrations.github_poller import GitHubFollowThroughPoller
from digital_employee.orchestrator import DelegationOrchestrator
from digital_employee.settings import Settings, load_settings
from digital_employee.web.routes_api import build_api_router
from digital_employee.web.routes_webhooks import build_webhook_router

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(
    settings: Settings | None = None,
    orchestrator: DelegationOrchestrator | None = None,
    *,
    enable_background_daemons: bool = True,
) -> FastAPI:
    cfg = settings or load_settings()
    orch = orchestrator or DelegationOrchestrator(cfg)
    poller = GitHubFollowThroughPoller(orch, interval_seconds=15.0)
    feishu_ws_active = False

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        nonlocal feishu_ws_active
        if enable_background_daemons:
            poller.start()
            feishu_ws_active = start_feishu_long_connection_if_configured(orch)
        try:
            yield
        finally:
            poller.stop()

    app = FastAPI(
        title="AI Digital Employee v2.0 Control Plane",
        version="2.0.0a1",
        lifespan=lifespan,
    )

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(
        build_api_router(
            cfg,
            orch,
            poller_enabled_fn=lambda: enable_background_daemons and orch.github.is_configured,
            feishu_ws_fn=lambda: feishu_ws_active,
        )
    )
    app.include_router(build_webhook_router(orch))

    @app.get("/", include_in_schema=False)
    def serve_console() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/tasks/{task_id}", include_in_schema=False)
    def redirect_legacy_task_url(task_id: str) -> RedirectResponse:
        return RedirectResponse(url=f"/#task={task_id}", status_code=302)

    return app
