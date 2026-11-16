"""Optional Feishu WebSocket Long-Connection Receiver (`lark-oapi`, zero public IP needed)."""

from __future__ import annotations

import json
import logging
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from digital_employee.orchestrator import DelegationOrchestrator

logger = logging.getLogger("digital_employee.feishu_ws")


def start_feishu_long_connection_if_configured(orchestrator: DelegationOrchestrator) -> bool:
    """Start Feishu WebSocket client in a daemon thread when DE_FEISHU_APP_ID & SECRET are set."""
    s = orchestrator.settings
    if not (s.feishu_app_id.strip() and s.feishu_app_secret.strip()):
        return False

    try:
        import lark_oapi as lark  # type: ignore[import-not-found]
    except ImportError:
        logger.warning(
            "DE_FEISHU_APP_ID is set, but `lark-oapi` is not installed. Run `uv pip install lark-oapi` to enable Feishu WS."
        )
        return False

    def _on_message(data) -> None:  # type: ignore[no-untyped-def]
        try:
            msg = data.event.message
            chat_id = str(getattr(msg, "chat_id", "") or "")
            raw = json.loads(msg.content or "{}").get("text", "").strip()
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

    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(_on_message)
        .build()
    )
    ws_client = lark.ws.Client(
        s.feishu_app_id,
        s.feishu_app_secret,
        event_handler=event_handler,
        log_level=lark.LogLevel.WARNING,
    )
    threading.Thread(target=ws_client.start, name="de-feishu-ws", daemon=True).start()
    return True
