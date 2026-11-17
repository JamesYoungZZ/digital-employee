"""Modular FastAPI Control Plane application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from digital_employee.agents.reviewer import render_full_review_report_html
from digital_employee.integrations.feishu_ws import (
    is_feishu_ws_running,
    start_feishu_long_connection_if_configured,
)
from digital_employee.integrations.github_poller import GitHubFollowThroughPoller
from digital_employee.integrations.jira_poller import JiraIssuePoller
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
    jira_poller = JiraIssuePoller(orch, interval_seconds=30.0)
    feishu_ws_active = False

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        nonlocal feishu_ws_active
        if enable_background_daemons:
            poller.start()
            jira_poller.start()
            feishu_ws_active = start_feishu_long_connection_if_configured(orch)
            orch.recover_orphaned_tasks()
        try:
            yield
        finally:
            poller.stop()
            jira_poller.stop()

    app = FastAPI(
        title="AI Digital Employee v3.0 Control Plane",
        version="3.0.0",
        lifespan=lifespan,
    )

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(
        build_api_router(
            cfg,
            orch,
            poller_enabled_fn=lambda: enable_background_daemons and orch.github.is_configured,
            feishu_ws_fn=is_feishu_ws_running,
            jira_poller=jira_poller,
        )
    )
    app.include_router(build_webhook_router(orch))

    @app.get("/", include_in_schema=False)
    def serve_console() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/tasks/{task_id}", include_in_schema=False)
    def redirect_legacy_task_url(task_id: str) -> RedirectResponse:
        return RedirectResponse(url=f"/#task={task_id}", status_code=302)

    @app.get("/reports/{task_id}/review", response_class=HTMLResponse, include_in_schema=False)
    def serve_full_review_report(task_id: str) -> HTMLResponse:
        state = orch.store.get_task(task_id)
        if state is None:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
        diff_text = ""
        if state.patch_artifact_uri:
            fp = Path(state.patch_artifact_uri)
            if fp.is_file():
                diff_text = fp.read_text(encoding="utf-8")[:80000]
        return HTMLResponse(render_full_review_report_html(state, diff_text))

    return app
