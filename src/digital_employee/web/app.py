"""Lightweight Web Task Queue, Control Surface & Feishu/GitHub Webhooks (Spec v2.0 Section 2 & 3.6)."""

from __future__ import annotations

import html
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from digital_employee.integrations.feishu_ws import start_feishu_long_connection_if_configured
from digital_employee.integrations.github_poller import GitHubFollowThroughPoller
from digital_employee.onboarding import onboard_repository
from digital_employee.orchestrator import DelegationOrchestrator
from digital_employee.settings import Settings, load_settings


class CreateTaskPayload(BaseModel):
    raw_request: str
    target_repo: str = "modular-rag"
    base_branch: str | None = None
    accept_defaults: bool = False
    auto_execute: bool = True
    publish_pr: bool = False


class ClarifyPayload(BaseModel):
    answers: dict[str, str] = Field(default_factory=dict)
    use_defaults: bool = False
    interaction_seconds: int = 5
    auto_execute: bool = True
    publish_pr: bool = False


class ControlPayload(BaseModel):
    action: str  # cancel | update_scope | accept | abandon
    permitted_scopes: list[str] | None = None
    note: str = ""


class FollowupPayload(BaseModel):
    comment_body: str
    publish_pr: bool = False


class OnboardRepoPayload(BaseModel):
    source: str
    repo_id: str | None = None
    base_branch: str | None = None
    bootstrap_venv: bool = False


def _read_artifact_uri(uri: str | None) -> str:
    if not uri or not uri.startswith("file://"):
        return ""
    fpath = Path(uri.removeprefix("file://"))
    if not fpath.is_file():
        return ""
    return fpath.read_text(encoding="utf-8", errors="replace")


def _status_badge(status: str) -> str:
    colors = {
        "WAITING_CLARIFICATION": "#d97706",
        "RUNNING_IN_SANDBOX": "#2563eb",
        "VERIFYING_EVIDENCE": "#7c3aed",
        "DELIVERED_LOCAL_READY": "#059669",
        "PR_OPEN_FOLLOWING_UP": "#059669",
        "ACCEPTED_AND_CLOSED": "#047857",
        "HANDED_OFF_INCOMPLETE": "#dc2626",
        "ABANDONED_OR_REVERTED": "#6b7280",
    }
    bg = colors.get(status, "#374151")
    return f'<span style="background:{bg};color:#fff;padding:3px 9px;border-radius:999px;font-size:12px;font-weight:600;">{html.escape(status)}</span>'


def create_app(
    settings: Settings | None = None,
    orchestrator: DelegationOrchestrator | None = None,
    *,
    enable_background_daemons: bool = True,
) -> FastAPI:
    cfg = settings or load_settings()
    orch = orchestrator or DelegationOrchestrator(cfg)
    poller = GitHubFollowThroughPoller(orch, interval_seconds=30.0)
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

    @app.get("/api/health")
    def api_health() -> dict[str, Any]:
        repos = list(cfg.load_repositories().keys())
        return {
            "status": "ok",
            "inference_mode": cfg.inference_mode,
            "registered_repositories": repos,
            "github_configured": orch.github.is_configured,
            "github_poller_enabled": enable_background_daemons and orch.github.is_configured,
            "feishu_configured": orch.feishu.is_configured or feishu_ws_active,
        }

    @app.get("/api/metrics")
    def api_metrics() -> dict[str, Any]:
        return orch.store.compute_funnel_metrics()

    @app.get("/api/repositories")
    def api_list_repositories() -> list[dict[str, Any]]:
        return [p.model_dump() for p in cfg.load_repositories().values()]

    @app.post("/api/repositories/onboard")
    def api_onboard_repository(payload: OnboardRepoPayload) -> dict[str, Any]:
        profile = onboard_repository(
            cfg,
            payload.source,
            repo_id=payload.repo_id,
            base_branch=payload.base_branch,
            bootstrap_venv=payload.bootstrap_venv,
        )
        return profile.model_dump()

    @app.get("/api/tasks")
    def api_list_tasks(status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return [t.model_dump() for t in orch.store.list_tasks(status=status, limit=limit)]

    @app.get("/api/tasks/{task_id}")
    def api_get_task(task_id: str) -> dict[str, Any]:
        state = orch.store.get_task(task_id)
        if state is None:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
        return {
            "task": state.model_dump(),
            "checkpoints": orch.store.list_checkpoints(task_id),
        }

    @app.post("/api/tasks")
    def api_create_task(
        payload: CreateTaskPayload,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        # Draft contract synchronously (fast model ~2-4s) so caller immediately sees contract / clarification questions;
        # run heavy sandbox coding loop in background if auto_execute=True!
        state = orch.submit_task(
            raw_request=payload.raw_request,
            target_repo=payload.target_repo,
            base_branch=payload.base_branch,
            accept_defaults=payload.accept_defaults,
            auto_execute=False,
            publish_pr=payload.publish_pr,
            source_channel="web",
        )
        if payload.auto_execute and state.lifecycle_status == "QUEUED":
            background_tasks.add_task(
                orch.execute_task, state.task_id, publish_pr=payload.publish_pr
            )
        return state.model_dump()

    @app.post("/api/tasks/{task_id}/clarify")
    def api_clarify_task(
        task_id: str,
        payload: ClarifyPayload,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        state = orch.resolve_clarification(
            task_id,
            answers=payload.answers,
            use_defaults=payload.use_defaults,
            interaction_seconds=payload.interaction_seconds,
            auto_execute=False,
            publish_pr=payload.publish_pr,
        )
        if payload.auto_execute and state.lifecycle_status == "QUEUED":
            background_tasks.add_task(
                orch.execute_task, task_id, publish_pr=payload.publish_pr
            )
        return state.model_dump()

    @app.post("/api/tasks/{task_id}/control")
    def api_control_task(task_id: str, payload: ControlPayload) -> dict[str, Any]:
        state = orch.update_scope_or_cancel(
            task_id,
            action=payload.action,
            permitted_scopes=payload.permitted_scopes,
            note=payload.note,
        )
        return state.model_dump()

    @app.post("/api/tasks/{task_id}/followup")
    def api_followup_task(
        task_id: str,
        payload: FollowupPayload,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        state = orch.store.get_task(task_id)
        if state is None:
            raise HTTPException(status_code=404, detail="Task not found")
        background_tasks.add_task(
            orch.handle_pr_review_comment,
            task_id,
            comment_body=payload.comment_body,
            publish_pr=payload.publish_pr,
        )
        return {"status": "queued_followup", "task_id": task_id}

    # =========================================================================
    # Feishu & GitHub Webhook Entrypoints
    # =========================================================================

    @app.post("/webhooks/feishu")
    async def feishu_webhook(
        request: Request, background_tasks: BackgroundTasks
    ) -> dict[str, Any]:
        body = await request.json()
        # Feishu URL verification handshake
        if "challenge" in body:
            return {"challenge": body["challenge"]}

        header = body.get("header") or {}
        if header.get("event_type") == "im.message.receive_v1":
            event = body.get("event") or {}
            msg = event.get("message") or {}
            content_raw = msg.get("content") or "{}"
            try:
                text = json.loads(content_raw).get("text", "").strip()
            except json.JSONDecodeError:
                text = str(content_raw).strip()

            if text:
                repos = list(cfg.load_repositories().keys())
                chosen_repo = repos[0] if repos else "modular-rag"
                for r in repos:
                    if r.lower() in text.lower():
                        chosen_repo = r
                        break
                background_tasks.add_task(
                    orch.submit_task,
                    raw_request=text,
                    target_repo=chosen_repo,
                    auto_execute=True,
                    source_channel="feishu",
                )
                return {"ok": True, "accepted_repo": chosen_repo}
        return {"ok": True}

    @app.post("/webhooks/github")
    async def github_webhook(
        request: Request, background_tasks: BackgroundTasks
    ) -> dict[str, Any]:
        event_name = request.headers.get("X-GitHub-Event", "")
        body = await request.json()
        if event_name in ("pull_request_review_comment", "issue_comment"):
            comment = (body.get("comment") or {}).get("body", "").strip()
            pr = body.get("pull_request") or body.get("issue") or {}
            pr_num = pr.get("number")
            if comment and pr_num:
                for task in orch.store.list_tasks(limit=100):
                    if task.deliverable_pr_number == int(pr_num):
                        background_tasks.add_task(
                            orch.handle_pr_review_comment,
                            task.task_id,
                            comment_body=comment,
                            publish_pr=True,
                        )
                        return {"ok": True, "resumed_task_id": task.task_id}
        return {"ok": True}

    # =========================================================================
    # Lightweight HTML Control Dashboard
    # =========================================================================

    @app.get("/", response_class=HTMLResponse)
    def web_dashboard() -> str:
        metrics = orch.store.compute_funnel_metrics()
        tasks = orch.store.list_tasks(limit=30)
        repos = cfg.load_repositories()

        repo_options = "".join(
            f'<option value="{html.escape(r.repo_id)}">{html.escape(r.repo_id)} ({html.escape(r.github_repo)})</option>'
            for r in repos.values()
        )
        rows = ""
        for t in tasks:
            ev = t.verification_evidence
            f2p = (
                "✅ Fail->Pass"
                if (ev and ev.repro_test_failed_on_base and ev.repro_test_passed_on_head)
                else ("—" if not ev else "❌")
            )
            pr_label = (
                f"PR #{t.deliverable_pr_number} &rarr;"
                if t.deliverable_pr_number
                else "一键提 PR &rarr;"
            )
            pr_cell = (
                f'<a href="{html.escape(t.deliverable_pr_url)}" target="_blank" style="color:#2563eb;font-weight:600;">{pr_label}</a>'
                if t.deliverable_pr_url
                else "—"
            )
            saved_cell = (
                f"{t.estimated_net_minutes_saved}m"
                if t.lifecycle_status
                in ("PR_OPEN_FOLLOWING_UP", "DELIVERED_LOCAL_READY", "ACCEPTED_AND_CLOSED")
                else "—"
            )
            rows += f"""
            <tr>
                <td><a href="/tasks/{html.escape(t.task_id)}" style="font-weight:600;color:#2563eb;">{html.escape(t.task_id)}</a></td>
                <td><code>{html.escape(t.contract.target_repo)}</code></td>
                <td>{html.escape(t.contract.objective[:75])}</td>
                <td>{_status_badge(t.lifecycle_status)}</td>
                <td>{f2p}</td>
                <td>{pr_cell}</td>
                <td>${t.total_cost_usd:.4f}</td>
                <td>{saved_cell}</td>
            </tr>
            """
        if not rows:
            rows = '<tr><td colspan="8" style="text-align:center;color:#6b7280;padding:24px;">暂无委托任务，可在上方直接发起第一个真实任务。</td></tr>'

        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<title>AI Digital Employee v2.0 — Task Queue</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background:#f8fafc; color:#0f172a; margin:0; padding:28px; }}
.container {{ max-width: 1120px; margin: 0 auto; }}
.card {{ background:#fff; border:1px solid #e2e8f0; border-radius:12px; padding:20px 24px; margin-bottom:20px; box-shadow:0 1px 2px rgba(0,0,0,0.03); }}
.kpi-grid {{ display:grid; grid-template-columns: repeat(5, 1fr); gap:12px; margin-bottom:20px; }}
.kpi {{ background:#fff; border:1px solid #e2e8f0; border-radius:10px; padding:14px 16px; }}
.kpi .label {{ font-size:12px; color:#64748b; }}
.kpi .val {{ font-size:22px; font-weight:700; margin-top:4px; }}
table {{ width:100%; border-collapse:collapse; font-size:14px; }}
th, td {{ text-align:left; padding:11px 10px; border-bottom:1px solid #f1f5f9; }}
th {{ color:#475569; font-weight:600; background:#f8fafc; }}
input, select, textarea {{ width:100%; box-sizing:border-box; padding:9px 12px; border:1px solid #cbd5e1; border-radius:8px; font-size:14px; margin-top:6px; }}
button {{ background:#0f172a; color:#fff; border:none; padding:10px 18px; border-radius:8px; font-weight:600; cursor:pointer; }}
button:hover {{ background:#1e293b; }}
</style>
</head>
<body>
<div class="container">
  <h1 style="margin:0 0 6px 0;font-size:24px;">AI 研发数字员工控制台 (v2.0)</h1>
  <p style="margin:0 0 20px 0;color:#475569;font-size:14px;">契约先行 · 隔离沙箱循环 · Fail-to-Pass 回归证据门禁 · 组织决策账本</p>

  <div class="kpi-grid">
    <div class="kpi"><div class="label">总委托任务数</div><div class="val">{metrics["total_delegated"]}</div></div>
    <div class="kpi"><div class="label">证据门禁通过</div><div class="val">{metrics["evidence_gate_passed"]}</div></div>
    <div class="kpi"><div class="label">已采纳闭环</div><div class="val">{metrics["accepted_without_rework"]}</div></div>
    <div class="kpi"><div class="label">净节省人工时间</div><div class="val">{metrics["estimated_net_human_minutes_saved"]} min</div></div>
    <div class="kpi"><div class="label">累计模型成本</div><div class="val">${metrics["total_cost_usd"]:.4f}</div></div>
  </div>

  <div class="card">
    <h3 style="margin-top:0;">发起新研发委托</h3>
    <form method="post" action="/ui/delegate">
      <div style="display:grid;grid-template-columns:1fr 2fr;gap:14px;">
        <div>
          <label style="font-size:13px;font-weight:600;">目标仓库</label>
          <select name="target_repo">{repo_options}</select>
        </div>
        <div>
          <label style="font-size:13px;font-weight:600;">任务描述 / 报错线索</label>
          <input name="raw_request" placeholder="例如：修复 TraceService.get_stage_timings 在处理 stage 字段时的 KeyError Bug" required />
        </div>
      </div>
      <div style="margin-top:12px;display:flex;justify-content:space-between;align-items:center;">
        <div style="display:flex;gap:18px;font-size:13px;color:#475569;">
          <label><input type="checkbox" name="accept_defaults" value="1" style="width:auto;margin-right:6px;"/>遇轻微歧义按默认假设执行</label>
          <label><input type="checkbox" name="publish_pr" value="1" {"checked" if orch.github.is_configured else ""} style="width:auto;margin-right:6px;"/>验证通过后自动提 GitHub Draft PR</label>
        </div>
        <button type="submit">起草契约并执行委托</button>
      </div>
    </form>
    <details style="margin-top:16px;padding-top:12px;border-top:1px dashed #e2e8f0;">
      <summary style="cursor:pointer;font-size:13px;font-weight:600;color:#2563eb;">➕ 一键接入新代码仓库 (Auto-Onboarding：填本地路径或 GitHub 地址自动识别技术栈)</summary>
      <form method="post" action="/ui/onboard" style="margin-top:10px;display:grid;grid-template-columns:2fr 1fr 1fr auto;gap:10px;align-items:end;">
        <div>
          <label style="font-size:12px;color:#475569;">Git 地址 / owner/repo / 本地目录</label>
          <input name="source" placeholder="如 git@github.com:org/service.git 或 /path/to/repo" required />
        </div>
        <div>
          <label style="font-size:12px;color:#475569;">别名 ID (可选)</label>
          <input name="repo_id" placeholder="自动推断" />
        </div>
        <div>
          <label style="font-size:12px;color:#475569;">基线分支 (可选)</label>
          <input name="base_branch" placeholder="默认自动探测" />
        </div>
        <button type="submit" style="background:#2563eb;height:38px;">接入仓库</button>
      </form>
    </details>
  </div>

  <div class="card">
    <h3 style="margin-top:0;">我的任务队列 (Work Queue)</h3>
    <table>
      <thead>
        <tr><th>Task ID</th><th>仓库</th><th>契约目标</th><th>当前状态</th><th>回归证据</th><th>Draft PR</th><th>成本</th><th>净省时间</th></tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
  </div>
</div>
</body>
</html>"""

    @app.post("/ui/delegate")
    def ui_delegate(
        background_tasks: BackgroundTasks,
        target_repo: str = Form(...),
        raw_request: str = Form(...),
        accept_defaults: str | None = Form(default=None),
        publish_pr: str | None = Form(default=None),
    ) -> RedirectResponse:
        state = orch.submit_task(
            raw_request=raw_request,
            target_repo=target_repo,
            accept_defaults=bool(accept_defaults),
            auto_execute=False,
            publish_pr=bool(publish_pr),
            source_channel="web",
        )
        if state.lifecycle_status == "QUEUED":
            background_tasks.add_task(
                orch.execute_task, state.task_id, publish_pr=bool(publish_pr)
            )
        return RedirectResponse(url=f"/tasks/{state.task_id}", status_code=303)

    @app.post("/ui/onboard")
    def ui_onboard(
        source: str = Form(...),
        repo_id: str = Form(default=""),
        base_branch: str = Form(default=""),
    ) -> RedirectResponse:
        onboard_repository(
            cfg,
            source,
            repo_id=repo_id.strip() or None,
            base_branch=base_branch.strip() or None,
            bootstrap_venv=False,
        )
        return RedirectResponse(url="/", status_code=303)

    @app.post("/ui/tasks/{task_id}/clarify")
    def ui_clarify(
        task_id: str,
        background_tasks: BackgroundTasks,
    ) -> RedirectResponse:
        state = orch.resolve_clarification(
            task_id,
            use_defaults=True,
            interaction_seconds=5,
            auto_execute=False,
        )
        if state.lifecycle_status == "QUEUED":
            background_tasks.add_task(
                orch.execute_task,
                task_id,
                publish_pr=state.auto_publish_pr or orch.github.is_configured,
            )
        return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)

    @app.post("/ui/tasks/{task_id}/followup")
    def ui_followup(
        task_id: str,
        background_tasks: BackgroundTasks,
        comment_body: str = Form(...),
    ) -> RedirectResponse:
        state = orch.store.get_task(task_id)
        if state is None:
            raise HTTPException(status_code=404, detail="Task not found")
        state.lifecycle_status = "QUEUED"
        orch.store.save_task(
            state,
            step_name="ui_followup_queued",
            note=comment_body[:160],
        )
        background_tasks.add_task(
            orch.handle_pr_review_comment,
            task_id,
            comment_body=comment_body,
            publish_pr=state.auto_publish_pr or orch.github.is_configured,
        )
        return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)

    @app.post("/ui/tasks/{task_id}/promote-decision")
    def ui_promote_decision(
        task_id: str,
        scope: str = Form(...),
        rule: str = Form(...),
        reason: str = Form(default=""),
    ) -> RedirectResponse:
        orch.promote_decision_rule(task_id, scope=scope, rule=rule, reason=reason)
        return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)

    @app.post("/ui/tasks/{task_id}/action")
    def ui_action(task_id: str, action: str = Form(...)) -> RedirectResponse:
        orch.update_scope_or_cancel(task_id, action=action)
        return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)

    @app.get("/tasks/{task_id}", response_class=HTMLResponse)
    def web_task_detail(task_id: str) -> str:
        t = orch.store.get_task(task_id)
        if t is None:
            raise HTTPException(status_code=404, detail="Task not found")
        c = t.contract
        ev = t.verification_evidence
        diff_text = _read_artifact_uri(t.patch_artifact_uri)
        checkpoints = orch.store.list_checkpoints(task_id)

        clarify_html = ""
        if t.lifecycle_status == "WAITING_CLARIFICATION" and c.unresolved_decisions:
            q_items = "".join(
                f"<li><b>{html.escape(d.question)}</b> (默认假设: <code>{html.escape(d.default_assumption)}</code>)</li>"
                for d in c.unresolved_decisions
            )
            clarify_html = f"""
            <div class="card" style="border-color:#f59e0b;background:#fffbeb;">
              <h3 style="margin-top:0;color:#b45309;">⏸️ 待你 5 秒微确认</h3>
              <ul>{q_items}</ul>
              <form method="post" action="/ui/tasks/{html.escape(task_id)}/clarify">
                <button type="submit">按默认假设继续执行</button>
              </form>
            </div>
            """

        ev_html = "<p style='color:#64748b;'>尚未生成验证证据。</p>"
        if ev:
            risks = (
                ev.reviewer_report.unverified_risks
                if ev.reviewer_report
                else ev.unverified_risks
            )
            risk_li = "".join(f"<li>{html.escape(r)}</li>" for r in risks)
            rev_summary = (
                html.escape(ev.reviewer_report.summary)
                if (ev.reviewer_report and ev.reviewer_report.summary)
                else ""
            )
            ev_html = f"""
            <p><b>验证 Commit</b>: <code>{html.escape(ev.verified_commit_sha)}</code> | <b>目标回归单测</b>: <code>{html.escape(ev.repro_test_target)}</code></p>
            <ul>
              <li>基线复现失败 (Fail on Base): <b>{'✅ YES' if ev.repro_test_failed_on_base else '❌ NO'}</b></li>
              <li>修复后验证通过 (Pass on Head): <b>{'✅ YES' if ev.repro_test_passed_on_head else '❌ NO'}</b></li>
              <li>原有关联测试套件通过: <b>{'✅ YES' if ev.existing_test_suite_passed else '❌ NO'}</b></li>
              <li>反作弊扫描 (未删断言/未加 skip): <b>{'✅ CLEAN' if ev.tests_weakened_check else '❌ VIOLATION'}</b></li>
              <li>作用域与静态检查合规: <b>{'✅ CLEAN' if (ev.scope_compliance_check and ev.static_analysis_passed) else '❌ VIOLATION'}</b></li>
            </ul>
            <p><b>单审查器结论</b>: {rev_summary}</p>
            <p><b>⚠️ 未验证边界披露 (Unverified Risks)</b>:</p>
            <ul>{risk_li}</ul>
            """

        cp_rows = "".join(
            f"<li><code>{html.escape(cp['created_at'][:19])}</code> [{html.escape(cp['step_name'])}] -> <b>{html.escape(cp['lifecycle_status'])}</b> {html.escape(cp['note'][:140])}</li>"
            for cp in checkpoints
        )

        pr_action_html = ""
        if t.deliverable_pr_url:
            pr_action_html = f'<a href="{html.escape(t.deliverable_pr_url)}" target="_blank" style="background:#2563eb;color:#fff;padding:8px 14px;border-radius:6px;text-decoration:none;margin-right:8px;font-size:13.5px;font-weight:600;">打开 GitHub Draft PR &rarr;</a>'
        elif (
            orch.github.is_configured
            and t.verification_evidence
            and t.verification_evidence.passed_all_hard_gates
        ):
            pr_action_html = f"""<form method="post" action="/ui/tasks/{html.escape(t.task_id)}/action" style="display:inline;">
        <input type="hidden" name="action" value="publish_pr"/>
        <button type="submit" style="background:#2563eb;">推送并创建 GitHub Draft PR</button>
      </form>"""

        is_active = t.lifecycle_status in (
            "DRAFTING_CONTRACT",
            "QUEUED",
            "RUNNING_IN_SANDBOX",
            "VERIFYING_EVIDENCE",
        )
        refresh_meta = '<meta http-equiv="refresh" content="4"/>' if is_active else ""
        running_html = ""
        if is_active:
            running_html = f"""
            <div class="card" style="border-color:#3b82f6;background:#eff6ff;">
              <b style="color:#1d4ed8;">⏳ 正在隔离沙箱中异步执行 ({html.escape(t.lifecycle_status)}，第 {t.sandbox_attempt_count or 1} 轮尝试)...</b>
              <span style="font-size:13px;color:#334155;margin-left:8px;">页面每 4 秒自动刷新进度；跑过 Fail-to-Pass 门禁后将{'自动推送并创建 GitHub Draft PR' if (t.auto_publish_pr or orch.github.is_configured) else '交付本地分支'}。</span>
            </div>
            """

        handoff_html = ""
        if t.handoff_bundle:
            hb = t.handoff_bundle
            clue_items = "".join(f"<li><code>{html.escape(cl[:300])}</code></li>" for cl in hb.root_cause_clues)
            handoff_html = f"""
            <div class="card" style="border-color:#ef4444;background:#fef2f2;">
              <h3 style="margin-top:0;color:#b91c1c;">🤝 结构化交接包 (Handoff: {html.escape(hb.failure_class)})</h3>
              <p><b>停止原因</b>：{html.escape(hb.reason)}</p>
              <p><b>已排查线索</b>：</p>
              <ul>{clue_items}</ul>
              <p><b>建议下一步</b>：{html.escape(hb.recommended_next_step)}（建议将大包干任务拆分为单个明确失败单测模块逐个委托）</p>
            </div>
            """

        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
{refresh_meta}
<title>Task {html.escape(t.task_id)} — Digital Employee</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; background:#f8fafc; color:#0f172a; padding:28px; }}
.container {{ max-width: 1040px; margin: 0 auto; }}
.card {{ background:#fff; border:1px solid #e2e8f0; border-radius:12px; padding:20px 24px; margin-bottom:18px; }}
pre {{ background:#0f172a; color:#f8fafc; padding:14px; border-radius:8px; overflow-x:auto; font-size:12.5px; }}
button {{ background:#0f172a; color:#fff; border:none; padding:8px 14px; border-radius:6px; cursor:pointer; margin-right:8px; }}
</style>
</head>
<body>
<div class="container">
  <p><a href="/" style="color:#2563eb;text-decoration:none;">&larr; 返回任务队列</a></p>
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;">
    <h2 style="margin:0;">任务 #{html.escape(t.task_id)} {_status_badge(t.lifecycle_status)}</h2>
    <div>
      {pr_action_html}
      <form method="post" action="/ui/tasks/{html.escape(t.task_id)}/action" style="display:inline;">
        <input type="hidden" name="action" value="accept"/>
        <button type="submit" style="background:#059669;">标记已采纳合入</button>
      </form>
      <form method="post" action="/ui/tasks/{html.escape(t.task_id)}/action" style="display:inline;">
        <input type="hidden" name="action" value="cancel"/>
        <button type="submit" style="background:#dc2626;">中断并转交人工 (Handoff)</button>
      </form>
    </div>
  </div>

  {running_html}
  {handoff_html}
  {clarify_html}

  <div class="card">
    <h3 style="margin-top:0;">1. 任务委托契约 (TaskContract v{c.contract_version})</h3>
    <p><b>目标</b>：{html.escape(c.objective)}</p>
    <p><b>仓库 & 基线</b>：<code>{html.escape(c.target_repo)}</code> (<code>{html.escape(c.base_branch)}</code> @ <code>{html.escape(c.base_commit_sha[:10])}</code>) &rarr; 工作树分支 <code>{html.escape(t.branch_name)}</code></p>
    <p><b>允许修改范围</b>：<code>{html.escape(', '.join(c.permitted_scopes))}</code></p>
    <p><b>禁止触碰边界</b>：<code>{html.escape(', '.join(c.out_of_scope))}</code></p>
    <p><b>验收标准 (AC)</b>：</p>
    <ul>{''.join(f'<li>{html.escape(ac)}</li>' for ac in c.acceptance_criteria)}</ul>
  </div>

  <div class="card">
    <h3 style="margin-top:0;">2. 证据门禁报告 (Evidence-Based Gate)</h3>
    {ev_html}
  </div>

  <div class="card">
    <h3 style="margin-top:0;">3. 代码变更补丁 (Git Diff)</h3>
    <pre>{html.escape(diff_text or '(暂无代码变更)')}</pre>
  </div>

  <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;">
    <div class="card">
      <h3 style="margin-top:0;">💬 追加修改指令 (Post-PR Follow-Through)</h3>
      <p style="font-size:12.5px;color:#64748b;margin-top:0;">可直接在此输入修改意见，或直接在 GitHub PR 页面留评论（本机后台每 30 秒自动轮询感应并推送新 Commit）。</p>
      <form method="post" action="/ui/tasks/{html.escape(t.task_id)}/followup">
        <input name="comment_body" placeholder="例如：补充空字符串和全数字边界单测断言" required style="width:100%;box-sizing:border-box;padding:8px 10px;border:1px solid #cbd5e1;border-radius:6px;margin-bottom:10px;"/>
        <button type="submit">唤醒沙箱增量修改并更新 PR</button>
      </form>
    </div>

    <div class="card">
      <h3 style="margin-top:0;">🧠 沉淀长期团队规则 (.ai/DECISIONS.yaml)</h3>
      <p style="font-size:12.5px;color:#64748b;margin-top:0;">将本次 Review 确认的架构/业务约束写入仓库决策账本（<code>APPROVED_POLICY</code>）。</p>
      <form method="post" action="/ui/tasks/{html.escape(t.task_id)}/promote-decision">
        <input name="scope" value="{html.escape(c.permitted_scopes[0] if c.permitted_scopes else '*')}" placeholder="作用域，如 src/ingestion/*" required style="width:100%;box-sizing:border-box;padding:7px 10px;border:1px solid #cbd5e1;border-radius:6px;margin-bottom:8px;"/>
        <input name="rule" placeholder="规则内容：例如分词器必须保护 ASCII 连字符术语不被切碎" required style="width:100%;box-sizing:border-box;padding:7px 10px;border:1px solid #cbd5e1;border-radius:6px;margin-bottom:8px;"/>
        <input name="reason" placeholder="原因背景（可选）" style="width:100%;box-sizing:border-box;padding:7px 10px;border:1px solid #cbd5e1;border-radius:6px;margin-bottom:10px;"/>
        <button type="submit" style="background:#0f766e;">写入 .ai/DECISIONS.yaml</button>
      </form>
    </div>
  </div>

  <div class="card">
    <h3 style="margin-top:0;">4. 持久化状态机快照 (Durable Checkpoints)</h3>
    <ul style="font-size:13px;line-height:1.7;">{cp_rows}</ul>
  </div>
</div>
</body>
</html>"""

    return app
