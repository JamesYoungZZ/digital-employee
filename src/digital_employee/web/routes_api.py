"""JSON REST API routes for the Digital Employee Control Plane."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException

from digital_employee.context.decision_ledger import DecisionLedger
from digital_employee.integrations.feishu_cards import (
    FeishuNotifier,
    build_accepted_receipt_card,
    build_clarification_card,
    build_evidence_report_card,
    build_handoff_card,
    build_test_connection_card,
    parse_feishu_command,
)
from digital_employee.onboarding import onboard_repository
from digital_employee.orchestrator import DelegationOrchestrator
from digital_employee.settings import PROJECT_ROOT, Settings
from digital_employee.web.schemas import (
    ClarifyPayload,
    ControlPayload,
    CreateTaskPayload,
    FeishuConfigPayload,
    FeishuSimulatePayload,
    FollowupPayload,
    OnboardRepoPayload,
    PromoteDecisionPayload,
)


def _read_artifact_uri(uri: str | None, max_chars: int = 40000) -> str:
    if not uri or not uri.startswith("file://"):
        return ""
    fpath = Path(uri.removeprefix("file://"))
    if not fpath.is_file():
        return ""
    text = fpath.read_text(encoding="utf-8", errors="replace")
    return text[-max_chars:] if len(text) > max_chars else text


def _upsert_dotenv_keys(env_path: Path, updates: dict[str, str]) -> None:
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.is_file() else []
    remaining = dict(updates)
    out_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, _ = stripped.split("=", 1)
            k = key.strip()
            if k in remaining:
                out_lines.append(f"{k}={remaining.pop(k)}")
                continue
        out_lines.append(line)
    for k, v in remaining.items():
        out_lines.append(f"{k}={v}")
    env_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    try:
        env_path.chmod(0o600)
    except OSError:
        pass


def build_api_router(
    cfg: Settings,
    orch: DelegationOrchestrator,
    *,
    poller_enabled_fn,
    feishu_ws_fn,
) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/health")
    def api_health() -> dict[str, Any]:
        repos = list(cfg.load_repositories().keys())
        return {
            "status": "ok",
            "inference_mode": cfg.inference_mode,
            "fast_model": cfg.fast_model,
            "reasoning_model": cfg.reasoning_model,
            "registered_repositories": repos,
            "github_configured": orch.github.is_configured,
            "github_poller_enabled": bool(poller_enabled_fn()),
            "feishu_configured": orch.feishu.is_configured or bool(feishu_ws_fn()),
            "feishu_webhook_configured": bool(orch.feishu.webhook_url),
            "feishu_app_configured": bool(orch.feishu.app_id and orch.feishu.app_secret),
            "feishu_ws_active": bool(feishu_ws_fn()),
        }

    @router.get("/metrics")
    def api_metrics() -> dict[str, Any]:
        return orch.store.compute_funnel_metrics()

    @router.get("/repositories")
    def api_list_repositories() -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for p in cfg.load_repositories().values():
            ledger = DecisionLedger.from_repo(Path(p.local_path), p.decision_ledger_path)
            item = p.model_dump()
            item["active_policies"] = [e.model_dump() for e in ledger.active_policies()]
            result.append(item)
        return result

    @router.post("/repositories/onboard")
    def api_onboard_repository(payload: OnboardRepoPayload) -> dict[str, Any]:
        try:
            profile = onboard_repository(
                cfg,
                payload.source,
                repo_id=payload.repo_id.strip() if payload.repo_id else None,
                base_branch=payload.base_branch.strip() if payload.base_branch else None,
                bootstrap_venv=payload.bootstrap_venv,
            )
            return profile.model_dump()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/tasks")
    def api_list_tasks(status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for t in orch.store.list_tasks(status=status, limit=limit):
            d = t.model_dump()
            d["estimated_net_minutes_saved"] = (
                t.estimated_net_minutes_saved
                if t.lifecycle_status
                in ("PR_OPEN_FOLLOWING_UP", "DELIVERED_LOCAL_READY", "ACCEPTED_AND_CLOSED")
                else None
            )
            items.append(d)
        return items

    @router.get("/tasks/{task_id}")
    def api_get_task(task_id: str) -> dict[str, Any]:
        state = orch.store.get_task(task_id)
        if state is None:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

        diff_text = _read_artifact_uri(state.patch_artifact_uri, max_chars=50000)
        log_text = _read_artifact_uri(
            state.verification_evidence.artifact_log_uri
            if state.verification_evidence
            else None,
            max_chars=25000,
        )

        # Build live Feishu card preview matching current lifecycle stage
        if state.lifecycle_status == "WAITING_CLARIFICATION":
            active_card = build_clarification_card(state, orch.web_base_url)
        elif state.lifecycle_status == "HANDED_OFF_INCOMPLETE":
            active_card = build_handoff_card(state, orch.web_base_url)
        elif state.verification_evidence is not None:
            active_card = build_evidence_report_card(state, orch.web_base_url)
        else:
            active_card = build_accepted_receipt_card(state, orch.web_base_url)

        dumped = state.model_dump()
        dumped["estimated_net_minutes_saved"] = state.estimated_net_minutes_saved
        return {
            "task": dumped,
            "checkpoints": orch.store.list_checkpoints(task_id),
            "diff": diff_text,
            "verification_log": log_text,
            "feishu_card_preview": active_card,
        }

    @router.post("/tasks")
    def api_create_task(
        payload: CreateTaskPayload,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        try:
            state = orch.submit_task(
                raw_request=payload.raw_request,
                target_repo=payload.target_repo,
                base_branch=payload.base_branch,
                accept_defaults=payload.accept_defaults,
                auto_execute=False,
                publish_pr=payload.publish_pr,
                source_channel="web",
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if payload.auto_execute and state.lifecycle_status == "QUEUED":
            background_tasks.add_task(
                orch.execute_task, state.task_id, publish_pr=payload.publish_pr
            )
        return state.model_dump()

    @router.post("/tasks/{task_id}/clarify")
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
                orch.execute_task,
                task_id,
                publish_pr=payload.publish_pr or state.auto_publish_pr,
            )
        return state.model_dump()

    @router.post("/tasks/{task_id}/control")
    def api_control_task(task_id: str, payload: ControlPayload) -> dict[str, Any]:
        try:
            state = orch.update_scope_or_cancel(
                task_id,
                action=payload.action,
                permitted_scopes=payload.permitted_scopes,
                note=payload.note,
            )
            return state.model_dump()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/tasks/{task_id}/followup")
    def api_followup_task(
        task_id: str,
        payload: FollowupPayload,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        state = orch.store.get_task(task_id)
        if state is None:
            raise HTTPException(status_code=404, detail="Task not found")
        state.lifecycle_status = "QUEUED"
        orch.store.save_task(
            state, step_name="followup_queued", note=payload.comment_body[:160]
        )
        background_tasks.add_task(
            orch.handle_pr_review_comment,
            task_id,
            comment_body=payload.comment_body,
            publish_pr=payload.publish_pr or orch.github.is_configured,
        )
        return {"status": "queued_followup", "task_id": task_id}

    @router.post("/tasks/{task_id}/promote-decision")
    def api_promote_decision(
        task_id: str, payload: PromoteDecisionPayload
    ) -> dict[str, Any]:
        entry = orch.promote_decision_rule(
            task_id,
            scope=payload.scope,
            rule=payload.rule,
            reason=payload.reason,
        )
        return entry.model_dump()

    # =========================================================================
    # Feishu Integration Configuration, Test Push & Message Simulator
    # =========================================================================

    @router.get("/integrations/feishu")
    def api_get_feishu_config() -> dict[str, Any]:
        wh = orch.feishu.webhook_url
        masked_wh = (wh[:38] + "..." + wh[-6:]) if len(wh) > 48 else ("已配置" if wh else "")
        return {
            "webhook_configured": bool(wh),
            "webhook_masked": masked_wh,
            "app_id": orch.feishu.app_id,
            "app_secret_configured": bool(orch.feishu.app_secret),
            "ws_active": bool(feishu_ws_fn()),
            "callback_webhook_path": f"{orch.web_base_url}/webhooks/feishu",
        }

    @router.post("/integrations/feishu/config")
    def api_save_feishu_config(payload: FeishuConfigPayload) -> dict[str, Any]:
        updates: dict[str, str] = {}
        if payload.webhook_url.strip():
            updates["DE_FEISHU_WEBHOOK_URL"] = payload.webhook_url.strip()
            cfg.feishu_webhook_url = payload.webhook_url.strip()
        if payload.app_id.strip():
            updates["DE_FEISHU_APP_ID"] = payload.app_id.strip()
            cfg.feishu_app_id = payload.app_id.strip()
        if payload.app_secret.strip():
            updates["DE_FEISHU_APP_SECRET"] = payload.app_secret.strip()
            cfg.feishu_app_secret = payload.app_secret.strip()

        if updates:
            _upsert_dotenv_keys(PROJECT_ROOT / ".env", updates)

        orch.feishu = FeishuNotifier(
            cfg.feishu_webhook_url,
            app_id=cfg.feishu_app_id,
            app_secret=cfg.feishu_app_secret,
        )
        return {"ok": True, "feishu_configured": orch.feishu.is_configured}

    @router.post("/integrations/feishu/test")
    def api_test_feishu_push() -> dict[str, Any]:
        card = build_test_connection_card(orch.web_base_url)
        if not orch.feishu.webhook_url:
            return {
                "ok": False,
                "message": "尚未配置 DE_FEISHU_WEBHOOK_URL，已在下方返回卡片仿真预览。",
                "card_preview": card,
            }
        try:
            delivery = orch.feishu.send_card(card)
            return {
                "ok": True,
                "message": f"测试卡片已成功推送至飞书群 ({delivery})！",
                "card_preview": card,
            }
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"飞书推送失败: {exc}") from exc

    @router.post("/integrations/feishu/simulate")
    def api_simulate_feishu_message(
        payload: FeishuSimulatePayload,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        repos = list(cfg.load_repositories().keys())
        parsed = parse_feishu_command(payload.raw_text, repos)
        if not payload.dispatch_real_task:
            return {
                "dispatched": False,
                "parsed_intent": parsed,
                "sample_test_card": build_test_connection_card(orch.web_base_url),
            }

        # Dispatch synchronously up to contract draft, then background sandbox execution
        state = orch.handle_feishu_text_message(
            raw_text=payload.raw_text,
            source_channel="feishu_sim",
            auto_execute=False,
        )
        if state is None:
            return {"dispatched": False, "parsed_intent": parsed}

        if state.lifecycle_status == "QUEUED":
            background_tasks.add_task(
                orch.execute_task,
                state.task_id,
                publish_pr=orch.github.is_configured,
            )
            card = build_accepted_receipt_card(state, orch.web_base_url)
        elif state.lifecycle_status == "WAITING_CLARIFICATION":
            card = build_clarification_card(state, orch.web_base_url)
        else:
            card = build_evidence_report_card(state, orch.web_base_url)

        return {
            "dispatched": True,
            "parsed_intent": parsed,
            "task": state.model_dump(),
            "feishu_reply_card": card,
        }

    return router
