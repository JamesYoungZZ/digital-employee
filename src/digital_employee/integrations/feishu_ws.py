"""Feishu WebSocket Long-Connection Receiver (`lark-oapi`, zero public IP needed).

Handles BOTH:
1. Incoming chat messages (`im.message.receive_v1`) — DMs & group `@bot` mentions
2. Interactive card button clicks (`card.action.trigger`) — 1-click Merge Approval & Clarification!
"""

from __future__ import annotations

import json
import logging
import threading
from collections import deque
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from digital_employee.orchestrator import DelegationOrchestrator

logger = logging.getLogger("digital_employee.feishu_ws")

_ACTIVE_WS_LOCK = threading.Lock()
_ACTIVE_WS_APP_ID: str | None = None
_SEEN_MESSAGE_IDS: deque[str] = deque(maxlen=512)


def is_feishu_ws_running() -> bool:
    return _ACTIVE_WS_APP_ID is not None


def _extract_feishu_message_text(msg_type: str, content_json: str) -> str:
    try:
        payload = json.loads(content_json or "{}")
    except json.JSONDecodeError:
        return ""
    if msg_type == "text":
        return str(payload.get("text") or "").strip()
    if msg_type == "post":
        # Extract text nodes from rich-text Feishu post
        parts: list[str] = []
        title = str(payload.get("title") or "").strip()
        if title:
            parts.append(title)
        for line in payload.get("content") or []:
            if isinstance(line, list):
                for seg in line:
                    if isinstance(seg, dict) and seg.get("tag") == "text":
                        parts.append(str(seg.get("text") or ""))
        return " ".join(parts).strip()
    return ""


def start_feishu_long_connection_if_configured(orchestrator: DelegationOrchestrator) -> bool:
    """Start Feishu WebSocket client in a daemon thread when DE_FEISHU_APP_ID & SECRET are set."""
    global _ACTIVE_WS_APP_ID

    s = orchestrator.settings
    app_id = s.feishu_app_id.strip()
    app_secret = s.feishu_app_secret.strip()
    if not (app_id and app_secret):
        return False

    with _ACTIVE_WS_LOCK:
        if _ACTIVE_WS_APP_ID == app_id:
            return True

    try:
        import lark_oapi as lark
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            CallBackToast,
            P2CardActionTrigger,
            P2CardActionTriggerResponse,
        )
    except ImportError:
        logger.warning(
            "DE_FEISHU_APP_ID is set, but `lark-oapi` is not installed. Run `uv pip install lark-oapi`."
        )
        return False

    def _on_message(data: Any) -> None:
        try:
            msg = data.event.message
            msg_id = str(getattr(msg, "message_id", "") or "")
            if msg_id:
                if msg_id in _SEEN_MESSAGE_IDS:
                    return
                _SEEN_MESSAGE_IDS.append(msg_id)

            chat_id = str(getattr(msg, "chat_id", "") or "")
            if chat_id:
                orchestrator.bind_feishu_default_chat_id(chat_id)

            msg_type = str(getattr(msg, "message_type", "text") or "text")
            raw = _extract_feishu_message_text(msg_type, str(getattr(msg, "content", "") or "{}"))
            if not raw:
                return

            threading.Thread(
                target=orchestrator.handle_feishu_text_message,
                kwargs={
                    "raw_text": raw,
                    "chat_id": chat_id,
                    "source_channel": "feishu_ws",
                },
                daemon=True,
            ).start()
        except Exception as exc:
            logger.warning("Feishu WS message handler error: %s", exc)

    def _on_card_action(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
        resp = P2CardActionTriggerResponse()
        toast = CallBackToast()
        try:
            ev = data.event
            chat_id = str(getattr(getattr(ev, "context", None), "open_chat_id", "") or "")
            if chat_id:
                orchestrator.bind_feishu_default_chat_id(chat_id)

            val = getattr(getattr(ev, "action", None), "value", None) or {}
            act = str(val.get("action") or "")
            tid = str(val.get("task_id") or "")

            if tid and act == "approve_merge":
                threading.Thread(
                    target=orchestrator.approve_and_merge,
                    args=(tid,),
                    kwargs={"approved_by": "feishu_ws_card"},
                    daemon=True,
                ).start()
                toast.type = "success"
                toast.content = f"已批准！正在自动 Squash 合入任务 {tid} 的 GitHub PR..."
                resp.toast = toast
                return resp

            if tid and act == "autofix_review":
                extra_cmt = str(val.get("comment") or "")
                threading.Thread(
                    target=orchestrator.autofix_review_issues,
                    args=(tid,),
                    kwargs={"extra_comment": extra_cmt, "triggered_by": "feishu_ws_card"},
                    daemon=True,
                ).start()
                toast.type = "success"
                toast.content = f"🔧 已唤醒数字员工！正在按 Review 意见自动修复 {tid} 并更新 PR…"
                resp.toast = toast
                return resp

            if tid and act == "ignore_review_comment":
                toast.type = "info"
                toast.content = "好的，暂不自动修改，保留当前 PR 状态。"
                resp.toast = toast
                return resp

            if tid and act == "reject_merge":
                threading.Thread(
                    target=orchestrator.update_scope_or_cancel,
                    args=(tid,),
                    kwargs={"action": "abandon", "note": "Rejected via Feishu WS card"},
                    daemon=True,
                ).start()
                toast.type = "info"
                toast.content = f"已暂缓/拒绝合入任务 {tid}。"
                resp.toast = toast
                return resp

            if tid and act == "clarify_default":
                threading.Thread(
                    target=orchestrator.resolve_clarification,
                    args=(tid,),
                    kwargs={
                        "use_defaults": True,
                        "auto_execute": True,
                        "publish_pr": orchestrator.github.is_configured,
                    },
                    daemon=True,
                ).start()
                toast.type = "success"
                toast.content = f"已按默认假设唤醒任务 {tid} 进入沙箱执行！"
                resp.toast = toast
                return resp

            if tid and act == "clarify_answer":
                q_idx = str(val.get("question_index", "0"))
                ans = str(val.get("answer", ""))
                threading.Thread(
                    target=orchestrator.resolve_clarification,
                    args=(tid,),
                    kwargs={
                        "answers": {q_idx: ans},
                        "use_defaults": True,
                        "auto_execute": True,
                        "publish_pr": orchestrator.github.is_configured,
                    },
                    daemon=True,
                ).start()
                toast.type = "success"
                toast.content = f"已确认选项「{ans[:18]}」，沙箱开始执行！"
                resp.toast = toast
                return resp

            toast.type = "info"
            toast.content = "操作已收到"
            resp.toast = toast
            return resp
        except Exception as exc:
            logger.warning("Feishu WS card callback error: %s", exc)
            toast.type = "error"
            toast.content = f"处理失败: {exc}"
            resp.toast = toast
            return resp

    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(_on_message)
        .register_p2_card_action_trigger(_on_card_action)
        .build()
    )
    ws_client = lark.ws.Client(
        app_id,
        app_secret,
        event_handler=event_handler,
        log_level=lark.LogLevel.INFO,
    )

    def _run_ws() -> None:
        import asyncio

        import lark_oapi.ws.client as lark_ws_mod

        # Give lark-oapi WS its own dedicated asyncio event loop isolated from uvicorn's loop
        dedicated_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(dedicated_loop)
        lark_ws_mod.loop = dedicated_loop
        try:
            ws_client.start()
        except Exception as exc:
            logger.warning("Feishu WS connection stopped: %s", exc)
            global _ACTIVE_WS_APP_ID
            with _ACTIVE_WS_LOCK:
                if _ACTIVE_WS_APP_ID == app_id:
                    _ACTIVE_WS_APP_ID = None

    with _ACTIVE_WS_LOCK:
        _ACTIVE_WS_APP_ID = app_id
    threading.Thread(target=_run_ws, name=f"de-feishu-ws-{app_id[-6:]}", daemon=True).start()
    return True
