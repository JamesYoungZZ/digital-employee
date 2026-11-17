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
    build_merge_approval_card,
    build_noop_verified_card,
    build_review_fix_card,
    build_test_connection_card,
    parse_feishu_command,
)
from digital_employee.integrations.feishu_ws import start_feishu_long_connection_if_configured
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
    ReviewerConfigPayload,
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
            "reviewer_model": cfg.reviewer_model,
            "reviewer_provider": cfg.reviewer_provider,
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
            d["worker_alive"] = orch.is_task_actively_running(t.task_id)
            d["estimated_net_minutes_saved"] = (
                t.estimated_net_minutes_saved
                if t.lifecycle_status
                in (
                    "PR_OPEN_FOLLOWING_UP",
                    "WAITING_MERGE_APPROVAL",
                    "DELIVERED_LOCAL_READY",
                    "ACCEPTED_AND_CLOSED",
                )
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
        has_review_block = bool(
            state.verification_evidence
            and state.verification_evidence.reviewer_report
            and state.verification_evidence.reviewer_report.verdict == "BLOCK"
        )
        if state.lifecycle_status == "WAITING_CLARIFICATION":
            active_card = build_clarification_card(state, orch.web_base_url)
        elif state.lifecycle_status == "WAITING_MERGE_APPROVAL":
            active_card = build_merge_approval_card(state, orch.web_base_url)
        elif has_review_block:
            active_card = build_review_fix_card(state, orch.web_base_url)
        elif state.lifecycle_status == "HANDED_OFF_INCOMPLETE":
            active_card = build_handoff_card(state, orch.web_base_url)
        elif state.verification_evidence is not None and state.verification_evidence.noop_verified:
            active_card = build_noop_verified_card(state, orch.web_base_url)
        elif state.verification_evidence is not None:
            active_card = build_evidence_report_card(state, orch.web_base_url)
        else:
            active_card = build_accepted_receipt_card(state, orch.web_base_url)

        dumped = state.model_dump()
        dumped["worker_alive"] = orch.is_task_actively_running(task_id)
        dumped["estimated_net_minutes_saved"] = state.estimated_net_minutes_saved
        return {
            "task": dumped,
            "checkpoints": orch.store.list_checkpoints(task_id),
            "live_events": orch.read_live_events(task_id, limit=120),
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
                auto_merge=payload.auto_merge,
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
    def api_control_task(
        task_id: str,
        payload: ControlPayload,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        if payload.action in ("retry", "autofix_review"):
            state = orch.store.get_task(task_id)
            if state is None:
                raise HTTPException(status_code=404, detail="Task not found")
            if orch.is_task_actively_running(task_id):
                return state.model_dump()

            ev = state.verification_evidence
            has_block = bool(ev and ev.reviewer_report and ev.reviewer_report.blocking_issues)
            if has_block or payload.action == "autofix_review":
                background_tasks.add_task(
                    orch.autofix_review_issues,
                    task_id,
                    extra_comment=payload.note.strip(),
                    triggered_by="console_user",
                )
                state.lifecycle_status = "QUEUED"
                return state.model_dump()

            followup = payload.note.strip()
            ev = state.verification_evidence
            if not followup and ev and ev.reviewer_report and ev.reviewer_report.blocking_issues:
                issues_txt = "\n".join(
                    f"- [{b.severity}] {b.path}:{b.line or ''} {b.message}"
                    for b in ev.reviewer_report.blocking_issues
                )
                followup = f"Resolve all blocking Code Review issues:\n{issues_txt}"

            state.lifecycle_status = "QUEUED"
            state.failure_class = "NONE"
            state.failure_reason = None
            state.handoff_bundle = None
            orch.append_live_event(
                state,
                attempt=state.sandbox_attempt_count + 1,
                kind="retry",
                icon="🔄",
                title="已手动唤醒沙箱继续执行…",
                detail=followup[:240],
            )
            orch.store.save_task(state, step_name="manual_retry_queued", note=followup[:200] or "Manual retry")
            background_tasks.add_task(
                orch.execute_task,
                task_id,
                publish_pr=state.auto_publish_pr or orch.github.is_configured,
                followup_comment=followup,
            )
            return state.model_dump()

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
            "default_chat_id": orch.feishu.default_chat_id,
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

        candidate = FeishuNotifier(
            cfg.feishu_webhook_url,
            app_id=cfg.feishu_app_id,
            app_secret=cfg.feishu_app_secret,
            default_chat_id=cfg.feishu_default_chat_id,
        )
        # Validate App ID & Secret immediately against Feishu Auth API if both are present
        if candidate.app_id and candidate.app_secret:
            try:
                candidate.verify_app_credentials()
            except Exception as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"飞书 App ID / App Secret 校验失败: {exc}",
                ) from exc

        if updates:
            _upsert_dotenv_keys(PROJECT_ROOT / ".env", updates)

        orch.feishu = candidate
        ws_started = start_feishu_long_connection_if_configured(orch)
        return {
            "ok": True,
            "feishu_configured": orch.feishu.is_configured,
            "ws_active": ws_started,
        }

    @router.post("/integrations/feishu/test")
    def api_test_feishu_push() -> dict[str, Any]:
        card = build_test_connection_card(orch.web_base_url)
        if not orch.feishu.is_configured:
            return {
                "ok": False,
                "message": "尚未配置飞书 Webhook 或自建应用凭证，已在下方返回卡片仿真预览。",
                "card_preview": card,
            }
        try:
            delivery = orch.feishu.send_card(card)
            if delivery == "local_artifact_only":
                return {
                    "ok": False,
                    "message": "飞书应用凭证已连通，但尚未绑定会话！请在手机/电脑飞书里找到你的机器人发一句「连接」（或将机器人拉入群聊），即可自动绑定并接收卡片。",
                    "card_preview": card,
                }
            if orch.feishu.default_chat_id:
                orch.bind_feishu_default_chat_id(orch.feishu.default_chat_id)
            return {
                "ok": True,
                "message": f"测试卡片已成功推送至飞书 ({delivery})！",
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

    @router.get("/integrations/reviewer")
    def api_get_reviewer_config() -> dict[str, Any]:
        return {
            "provider": cfg.reviewer_provider,
            "model": cfg.reviewer_model,
            "base_url": cfg.reviewer_base_url,
            "api_key_configured": bool(cfg.reviewer_api_key),
        }

    @router.post("/integrations/reviewer/config")
    def api_save_reviewer_config(payload: ReviewerConfigPayload) -> dict[str, Any]:
        from digital_employee.agents.reviewer import FocusedReviewer
        from digital_employee.llm.client import LLMClient

        prov = payload.provider.strip().lower()
        if prov not in ("gemini_cli", "gemini", "anthropic", "openai_compatible"):
            raise HTTPException(status_code=400, detail=f"Unsupported reviewer provider: {prov}")

        model_name = payload.model.strip() or (
            "gemini-3.8-flash" if prov in ("gemini_cli", "gemini") else "deepseek-flash"
        )
        base_url = payload.base_url.strip()
        if not base_url:
            if prov == "gemini_cli":
                base_url = "gemini-cli://local"
            elif prov == "gemini":
                base_url = "https://generativelanguage.googleapis.com/v1beta/openai"
            elif prov == "anthropic":
                base_url = "https://api.anthropic.com"
            else:
                base_url = cfg.llm_base_url

        updates: dict[str, str] = {
            "DE_REVIEWER_PROVIDER": prov,
            "DE_REVIEWER_MODEL": model_name,
            "DE_REVIEWER_BASE_URL": base_url,
        }
        if payload.api_key.strip():
            updates["DE_REVIEWER_API_KEY"] = payload.api_key.strip()
            cfg.reviewer_api_key = payload.api_key.strip()

        cfg.reviewer_provider = prov  # type: ignore[assignment]
        cfg.reviewer_model = model_name
        cfg.reviewer_base_url = base_url
        _upsert_dotenv_keys(PROJECT_ROOT / ".env", updates)

        orch.reviewer_llm = LLMClient(
            cfg,
            provider=cfg.reviewer_provider,
            base_url=cfg.reviewer_base_url,
            api_key=cfg.reviewer_api_key,
            default_model=cfg.reviewer_model,
        )
        orch.reviewer = FocusedReviewer(orch.reviewer_llm, skill_path=cfg.pr_review_skill_path)
        return {
            "ok": True,
            "provider": cfg.reviewer_provider,
            "model": cfg.reviewer_model,
        }

    return router
