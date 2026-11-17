"""Webhook routes for Feishu Event/Card Action Callbacks & GitHub PR Events."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Request

from digital_employee.orchestrator import DelegationOrchestrator


def build_webhook_router(orch: DelegationOrchestrator) -> APIRouter:
    router = APIRouter(prefix="/webhooks")

    @router.post("/feishu")
    async def feishu_webhook(
        request: Request, background_tasks: BackgroundTasks
    ) -> dict[str, Any]:
        body = await request.json()

        # 1. Feishu URL verification handshake
        if "challenge" in body:
            return {"challenge": body["challenge"]}

        header = body.get("header") or {}
        event_type = header.get("event_type") or body.get("type", "")

        # 2. Feishu Interactive Card Button Click Callback (`card.action.trigger`)
        action_obj = (body.get("event") or {}).get("action") or body.get("action")
        if isinstance(action_obj, dict) and isinstance(action_obj.get("value"), dict):
            val = action_obj["value"]
            act = val.get("action")
            tid = str(val.get("task_id") or "")
            if tid and act == "clarify_default":
                background_tasks.add_task(
                    orch.resolve_clarification,
                    tid,
                    use_defaults=True,
                    auto_execute=True,
                    publish_pr=orch.github.is_configured,
                )
                return {
                    "toast": {
                        "type": "success",
                        "content": f"已按默认假设唤醒任务 {tid} 进入沙箱执行！",
                    }
                }
            if tid and act == "clarify_answer":
                q_idx = str(val.get("question_index", "0"))
                ans = str(val.get("answer", ""))
                background_tasks.add_task(
                    orch.resolve_clarification,
                    tid,
                    answers={q_idx: ans},
                    use_defaults=True,
                    auto_execute=True,
                    publish_pr=orch.github.is_configured,
                )
                return {
                    "toast": {
                        "type": "success",
                        "content": f"已确认选项「{ans[:18]}」，沙箱开始执行！",
                    }
                }
            if tid and act == "approve_merge":
                background_tasks.add_task(
                    orch.approve_and_merge,
                    tid,
                    approved_by="feishu_card",
                )
                return {
                    "toast": {
                        "type": "success",
                        "content": f"已批准合入！正在自动 Squash Merge 任务 {tid} 的 PR...",
                    }
                }
            if tid and act == "autofix_review":
                extra_cmt = str(val.get("comment") or "")
                background_tasks.add_task(
                    orch.autofix_review_issues,
                    tid,
                    extra_comment=extra_cmt,
                    triggered_by="feishu_webhook_card",
                )
                return {
                    "toast": {
                        "type": "success",
                        "content": f"🔧 已唤醒数字员工！正在按 Review 意见自动修复 {tid}…",
                    }
                }
            if tid and act == "ignore_review_comment":
                return {"toast": {"type": "info", "content": "已暂缓修改，保留当前 PR。"}}
            if tid and act == "reject_merge":
                background_tasks.add_task(
                    orch.update_scope_or_cancel,
                    tid,
                    action="abandon",
                    note="Rejected via Feishu merge approval card",
                )
                return {
                    "toast": {
                        "type": "info",
                        "content": f"已拒绝合入任务 {tid}。",
                    }
                }

        # 3. Feishu Incoming Chat Message (`im.message.receive_v1`)
        if event_type == "im.message.receive_v1":
            event = body.get("event") or {}
            msg = event.get("message") or {}
            chat_id = str(msg.get("chat_id") or "")
            content_raw = msg.get("content") or "{}"
            try:
                text = json.loads(content_raw).get("text", "").strip()
            except json.JSONDecodeError:
                text = str(content_raw).strip()

            if text:
                background_tasks.add_task(
                    orch.handle_feishu_text_message,
                    raw_text=text,
                    chat_id=chat_id,
                    source_channel="feishu",
                    auto_execute=True,
                )
                return {"ok": True, "routed": True}

        return {"ok": True}

    @router.post("/github")
    async def github_webhook(
        request: Request, background_tasks: BackgroundTasks
    ) -> dict[str, Any]:
        event_name = request.headers.get("X-GitHub-Event", "")
        body = await request.json()
        if event_name in ("pull_request_review_comment", "issue_comment"):
            comment = (body.get("comment") or {}).get("body", "").strip()
            if "<!-- ai-digital-employee-bot -->" in comment:
                return {"ok": True, "ignored": "self_bot_comment"}
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

    @router.post("/alert")
    async def alert_webhook(
        request: Request, background_tasks: BackgroundTasks
    ) -> dict[str, Any]:
        """7x24 Ops Alert Ingestion (Alertmanager / Sentry / Generic Incident Webhook)."""
        body = await request.json()
        target_repo = str(body.get("target_repo") or body.get("service") or "").strip()
        if not target_repo:
            repos = orch.settings.load_repositories()
            if repos:
                target_repo = next(iter(repos.keys()))
            else:
                return {"ok": False, "error": "No target_repo provided and no repositories registered."}

        alert_name = str(body.get("alert_name") or body.get("title") or "RuntimeIncidentAlert").strip()
        severity = str(body.get("severity") or "critical").strip()
        error_message = str(body.get("error_message") or body.get("message") or "").strip()
        stack_trace = str(body.get("stack_trace") or body.get("culprit") or "").strip()
        auto_execute = bool(body.get("auto_execute", True))
        publish_pr = bool(body.get("publish_pr", orch.github.is_configured))

        raw_request = (
            f"[7x24 Ops Alert · {severity.upper()}] {alert_name}\n"
            f"Error: {error_message}\n"
        )
        if stack_trace:
            raw_request += f"Stack Trace / Diagnostic Context:\n{stack_trace[:3000]}\n"
        raw_request += (
            "Please triage root cause using Ops diagnostics, write a Fail-to-Pass regression fix if caused by a code bug, "
            "or exit via finish_noop with a root-cause report if repository tests are healthy."
        )

        if auto_execute:
            state = orch.submit_task(
                raw_request=raw_request,
                target_repo=target_repo,
                accept_defaults=True,
                auto_execute=False,
                publish_pr=publish_pr,
                source_channel="alert_webhook",
            )
            background_tasks.add_task(
                orch.execute_task,
                state.task_id,
                publish_pr=publish_pr,
            )
        else:
            state = orch.submit_task(
                raw_request=raw_request,
                target_repo=target_repo,
                accept_defaults=True,
                auto_execute=False,
                publish_pr=publish_pr,
                source_channel="alert_webhook",
            )
        return {
            "ok": True,
            "task_id": state.task_id,
            "lifecycle_status": state.lifecycle_status,
            "target_repo": state.contract.target_repo,
        }

    return router
