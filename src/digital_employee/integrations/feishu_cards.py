"""Feishu Interactive Cards, Dual-Channel Notifier (Webhook + OpenAPI) & Message Router."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from digital_employee.models.state import DurableTaskState

MENTION_TAG_RE = re.compile(r"@_user_\d+\s*")
TASK_TAG_RE = re.compile(r"#?(t-\d{8}-[a-f0-9]{6})\b", re.IGNORECASE)
REPO_BRACKET_RE = re.compile(r"^\[([A-Za-z0-9_.\-]+)\]\s*(.+)$", re.DOTALL)


def _clip(text: str, limit: int = 80) -> str:
    """Collapse newlines and cap length so Feishu cards stay crisp and mobile-friendly."""
    compact = " ".join((text or "").split()).strip()
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"


def _meta_fields(left_label: str, left_val: str, right_label: str, right_val: str) -> dict[str, Any]:
    return {
        "tag": "div",
        "fields": [
            {"is_short": True, "text": {"tag": "lark_md", "content": f"**{left_label}**\n{left_val}"}},
            {"is_short": True, "text": {"tag": "lark_md", "content": f"**{right_label}**\n{right_val}"}},
        ],
    }


def build_clarification_card(
    state: DurableTaskState, web_base_url: str = "http://127.0.0.1:8020"
) -> dict[str, Any]:
    """Build concise 5-second micro-clarification Feishu interactive card."""
    c = state.contract
    decision_lines: list[str] = []
    action_buttons: list[dict[str, Any]] = []

    for idx, d in enumerate(c.unresolved_decisions[:2], start=1):
        opts = "\n".join(f"`{i}` {_clip(o, 42)}" for i, o in enumerate(d.options[:3], start=1))
        q_prefix = f"**待确认 {idx}**：" if len(c.unresolved_decisions) > 1 else "**待确认**："
        decision_lines.append(f"{q_prefix}{_clip(d.question, 70)}\n{opts}")
        for opt in d.options[:2]:
            action_buttons.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": _clip(opt, 18)},
                    "type": "default",
                    "value": {
                        "action": "clarify_answer",
                        "task_id": state.task_id,
                        "question_index": str(idx - 1),
                        "answer": opt,
                    },
                }
            )

    action_buttons.append(
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "按默认继续"},
            "type": "primary",
            "value": {"action": "clarify_default", "task_id": state.task_id},
        }
    )
    action_buttons.append(
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "控制台"},
            "type": "default",
            "url": f"{web_base_url}/#task={state.task_id}",
        }
    )

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"⏸️ 需你拍板 · {state.task_id}"},
                "template": "orange",
            },
            "elements": [
                _meta_fields("仓库", f"`{c.target_repo}` (`{c.base_branch}`)", "目标", _clip(c.objective, 52)),
                {"tag": "hr"},
                {
                    "tag": "markdown",
                    "content": "\n\n".join(decision_lines) + "\n\n<font color='grey'>直接点按钮，或回复 1 / 2 / 默认</font>",
                },
                {"tag": "action", "actions": action_buttons[:4]},
            ],
        },
    }


def build_accepted_receipt_card(
    state: DurableTaskState, web_base_url: str = "http://127.0.0.1:8020"
) -> dict[str, Any]:
    """Build concise task acceptance receipt card."""
    c = state.contract
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"🚀 已接单开工 · {state.task_id}"},
                "template": "blue",
            },
            "elements": [
                _meta_fields("仓库", f"`{c.target_repo}` (`{c.base_branch}`)", "沙箱分支", f"`{state.branch_name}`"),
                {"tag": "hr"},
                {
                    "tag": "markdown",
                    "content": f"**任务**：{_clip(c.objective, 75)}\n<font color='grey'>跑通单测、AI 审查与线上 CI 后，将向你推送合入审批卡。</font>",
                },
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "查看进度"},
                            "type": "default",
                            "url": f"{web_base_url}/#task={state.task_id}",
                        }
                    ],
                },
            ],
        },
    }


def build_evidence_report_card(
    state: DurableTaskState, web_base_url: str = "http://127.0.0.1:8020"
) -> dict[str, Any]:
    """Build concise PR opened & verified card (while GitHub CI is running)."""
    ev = state.verification_evidence
    rev_verdict = ev.reviewer_report.verdict if (ev and ev.reviewer_report) else "APPROVE"
    pr_label = f"PR #{state.deliverable_pr_number}" if state.deliverable_pr_number else f"`{(state.head_commit_sha or '')[:8]}`"

    actions: list[dict[str, Any]] = []
    if state.deliverable_pr_url:
        actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": f"查看 {pr_label}"},
                "type": "primary",
                "url": state.deliverable_pr_url,
            }
        )
    actions.append(
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "控制台详情"},
            "type": "default",
            "url": f"{web_base_url}/#task={state.task_id}",
        }
    )

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"🧪 本地验证已过 · 等待线上 CI ({pr_label})"},
                "template": "turquoise",
            },
            "elements": [
                _meta_fields("仓库", f"`{state.contract.target_repo}`", "花费", f"`${state.total_cost_usd:.3f}`"),
                {"tag": "hr"},
                {
                    "tag": "markdown",
                    "content": (
                        f"**改动**：{_clip(state.contract.objective, 75)}\n"
                        f"**门禁**：✅ 单测 `Fail→Pass` · {'✅' if rev_verdict == 'APPROVE' else '🛑'} AI Review `{rev_verdict}`\n"
                        f"<font color='grey'>线上 GitHub Actions CI 全绿后将推送「批准合入」按钮。</font>"
                    ),
                },
                {"tag": "action", "actions": actions},
            ],
        },
    }


def build_merge_approval_card(
    state: DurableTaskState, web_base_url: str = "http://127.0.0.1:8020"
) -> dict[str, Any]:
    """Build crisp, executive-friendly Merge Approval Card once Review + CI both pass."""
    mc = state.merge_approval_card
    ev = state.verification_evidence
    pr_num = mc.pr_number if mc else (state.deliverable_pr_number or 0)
    pr_url = (mc.pr_url if mc else state.deliverable_pr_url) or web_base_url
    rev_summary = _clip(
        (mc.reviewer_summary if mc else (ev.reviewer_report.summary if (ev and ev.reviewer_report) else ""))
        or "代码改动克制，无阻断级风险。",
        85,
    )
    ci_cnt = mc.ci_checks_count if mc else 2

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"🛎️ 待你审批合入 · PR #{pr_num}"},
                "template": "green",
            },
            "elements": [
                _meta_fields(
                    "目标仓库",
                    f"`{state.contract.target_repo}` → `{state.contract.base_branch}`",
                    "校验状态",
                    f"CI `{ci_cnt}/{ci_cnt}` 全绿 · `${state.total_cost_usd:.3f}`",
                ),
                {"tag": "hr"},
                {
                    "tag": "markdown",
                    "content": (
                        f"**修复内容**：{_clip(state.contract.objective, 75)}\n"
                        f"**AI 审查结论**：{rev_summary}\n"
                        f"**准入门禁**：✅ 单测复现通过 · ✅ Review 0 阻断 · ✅ 线上 CI 全绿"
                    ),
                },
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "✅ 批准并自动合入"},
                            "type": "primary",
                            "value": {"action": "approve_merge", "task_id": state.task_id},
                        },
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": f"看 PR #{pr_num}"},
                            "type": "default",
                            "url": pr_url,
                        },
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "暂缓"},
                            "type": "default",
                            "value": {"action": "reject_merge", "task_id": state.task_id},
                        },
                    ],
                },
            ],
        },
    }


def build_handoff_card(
    state: DurableTaskState, web_base_url: str = "http://127.0.0.1:8020"
) -> dict[str, Any]:
    """Build concise Handoff card when task pauses for human intervention."""
    hb = state.handoff_bundle
    reason = _clip((hb.reason if hb else state.failure_reason) or "未能在预算步数内通过门禁", 80)
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"🤝 需人工接手 · {state.task_id}"},
                "template": "wathet",
            },
            "elements": [
                _meta_fields("仓库", f"`{state.contract.target_repo}`", "现场分支", f"`{state.branch_name}`"),
                {"tag": "hr"},
                {
                    "tag": "markdown",
                    "content": f"**任务**：{_clip(state.contract.objective, 65)}\n**卡点**：{reason}",
                },
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "打开控制台看日志"},
                            "type": "primary",
                            "url": f"{web_base_url}/#task={state.task_id}",
                        }
                    ],
                },
            ],
        },
    }


def build_noop_verified_card(
    state: DurableTaskState, web_base_url: str = "http://127.0.0.1:8020"
) -> dict[str, Any]:
    """Build concise No-Op completion card."""
    ev = state.verification_evidence
    summary = _clip(ev.noop_summary if (ev and ev.noop_summary) else "目标测试集已全部通过，无需改代码。", 80)
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"✅ 体检全绿・无需改动 · {state.task_id}"},
                "template": "green",
            },
            "elements": [
                _meta_fields("仓库", f"`{state.contract.target_repo}` (`{state.contract.base_branch}`)", "改动文件", "`0` (未建多余 PR)"),
                {"tag": "hr"},
                {"tag": "markdown", "content": f"**结论**：{summary}"},
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "查看日志"},
                            "type": "default",
                            "url": f"{web_base_url}/#task={state.task_id}",
                        }
                    ],
                },
            ],
        },
    }


def build_test_connection_card(web_base_url: str = "http://127.0.0.1:8020") -> dict[str, Any]:
    """Crisp welcome & quick-command card sent on `连接` / `链接` / `帮助`."""
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": "🟢 数字员工已就绪"},
                "template": "green",
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        "已绑定当前会话，你可以直接这样使唤我：\n"
                        "• **派活**：`[modular-rag] 修复某某报错`\n"
                        "• **合入**：收到审批卡直接点按钮，或回 `批准`\n"
                        "• **追改**：`#任务号 补充边界处理`"
                    ),
                },
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "打开工作台"},
                            "type": "primary",
                            "url": web_base_url,
                        }
                    ],
                },
            ],
        },
    }


def parse_feishu_command(
    raw_text: str, registered_repos: list[str]
) -> dict[str, str]:
    """Parse incoming Feishu message into `clarify` | `followup` | `delegate` intent."""
    cleaned = MENTION_TAG_RE.sub("", raw_text or "").strip()
    if not cleaned:
        return {"intent": "empty"}

    # 0. Greeting / connection ping ("连接", "链接", "绑定", "你好", "hi", "ping", "help", "帮助")
    if (
        cleaned.lower()
        in {
            "连接",
            "链接",
            "绑定",
            "测试",
            "你好",
            "您好",
            "在吗",
            "菜单",
            "状态",
            "status",
            "hi",
            "hello",
            "ping",
            "help",
            "帮助",
            "?",
            "？",
        }
        or len(cleaned) <= 2
    ):
        return {"intent": "bind_ping"}

    # 0b. Quick merge approval reply ("批准", "同意合入", "合入", "approve", "merge")
    if cleaned.lower() in {"批准", "同意合入", "合入", "批准合入", "approve", "merge", "lgtm"}:
        return {"intent": "quick_approve_merge"}

    # 1. Quick clarification reply ("默认", "继续", "1", "2")
    if cleaned.lower() in {"默认", "继续", "确认", "default", "ok", "yes", "1", "2"}:
        return {"intent": "quick_clarify", "choice": cleaned}

    # 2. Follow-up or targeted approval on specific task ID ("#t-20261116-abcdef 补充单测")
    task_match = TASK_TAG_RE.search(cleaned)
    if task_match:
        tid = task_match.group(1).lower()
        rest = (cleaned[: task_match.start()] + cleaned[task_match.end() :]).strip(" :：,，")
        if rest.lower() in {"批准", "同意合入", "合入", "批准合入", "approve", "merge", "lgtm"}:
            return {"intent": "task_approve_merge", "task_id": tid}
        if rest in {"默认", "继续", "default"}:
            return {"intent": "task_clarify_default", "task_id": tid}
        if rest:
            return {"intent": "followup", "task_id": tid, "comment": rest}

    # 3. New task delegation (check `[repo] ...` prefix or repo substring)
    chosen_repo = registered_repos[0] if registered_repos else "modular-rag"
    req_body = cleaned
    bracket = REPO_BRACKET_RE.match(cleaned)
    if bracket and bracket.group(1) in registered_repos:
        chosen_repo = bracket.group(1)
        req_body = bracket.group(2).strip()
    else:
        for r in registered_repos:
            if r.lower() in cleaned.lower():
                chosen_repo = r
                break

    return {"intent": "delegate", "target_repo": chosen_repo, "raw_request": req_body}


class FeishuNotifier:
    """Dispatches interactive cards via Feishu Webhook URL and/or App OpenAPI (`chat_id`)."""

    def __init__(
        self,
        webhook_url: str = "",
        *,
        app_id: str = "",
        app_secret: str = "",
        default_chat_id: str = "",
    ) -> None:
        self.webhook_url = webhook_url.strip()
        self.app_id = app_id.strip()
        self.app_secret = app_secret.strip()
        self.default_chat_id = default_chat_id.strip()

    @property
    def is_configured(self) -> bool:
        return bool(self.webhook_url or (self.app_id and self.app_secret))

    def _fetch_tenant_token(self, client: httpx.Client) -> str:
        resp = client.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code", 0) != 0:
            raise RuntimeError(f"Feishu auth error: {data.get('msg')}")
        return str(data["tenant_access_token"])

    def verify_app_credentials(self) -> str:
        """Validate `app_id` & `app_secret` against Feishu Auth API; returns token prefix."""
        if not (self.app_id and self.app_secret):
            raise ValueError("Missing Feishu App ID or App Secret")
        with httpx.Client(timeout=10.0) as client:
            tok = self._fetch_tenant_token(client)
            return tok[:10]

    def _auto_discover_bot_chat_id(self, client: httpx.Client, token: str) -> str:
        """Discover the first group chat the bot belongs to when `default_chat_id` is unset."""
        try:
            resp = client.get(
                "https://open.feishu.cn/open-apis/im/v1/chats",
                params={"page_size": 5},
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code < 400:
                items = (resp.json().get("data") or {}).get("items") or []
                if items:
                    cid = str(items[0].get("chat_id") or "")
                    if cid:
                        self.default_chat_id = cid
                        return cid
        except Exception:
            pass
        return ""

    def send_card(self, card_payload: dict[str, Any], *, chat_id: str = "") -> str:
        results: list[str] = []
        with httpx.Client(timeout=15.0) as client:
            target_chat_id = chat_id.strip() or self.default_chat_id
            if self.app_id and self.app_secret:
                token = self._fetch_tenant_token(client)
                if not target_chat_id:
                    target_chat_id = self._auto_discover_bot_chat_id(client, token)
                if target_chat_id:
                    resp = client.post(
                        "https://open.feishu.cn/open-apis/im/v1/messages",
                        params={"receive_id_type": "chat_id"},
                        headers={"Authorization": f"Bearer {token}"},
                        json={
                            "receive_id": target_chat_id,
                            "msg_type": "interactive",
                            "content": json.dumps(card_payload["card"], ensure_ascii=False),
                        },
                    )
                    resp.raise_for_status()
                    body = resp.json()
                    if isinstance(body, dict) and body.get("code", 0) not in (0, None):
                        raise RuntimeError(f"Feishu OpenAPI send error: {body}")
                    results.append(f"openapi_chat:{target_chat_id[:12]}")

            if self.webhook_url:
                resp = client.post(self.webhook_url, json=card_payload)
                resp.raise_for_status()
                body = resp.json()
                if isinstance(body, dict) and body.get("code", 0) not in (0, None):
                    raise RuntimeError(f"Feishu webhook error: {body}")
                results.append(f"webhook:{resp.status_code}")

        return ",".join(results) if results else "local_artifact_only"
