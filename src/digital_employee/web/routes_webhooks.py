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

    return router
