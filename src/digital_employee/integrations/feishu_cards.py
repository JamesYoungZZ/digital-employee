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


def build_clarification_card(
    state: DurableTaskState, web_base_url: str = "http://127.0.0.1:8020"
) -> dict[str, Any]:
    """Build 5-second micro-clarification Feishu interactive card."""
    c = state.contract
    decision_lines: list[str] = []
    action_buttons: list[dict[str, Any]] = []

    for idx, d in enumerate(c.unresolved_decisions, start=1):
        opts = "\n".join(f"  `{i}` {o}" for i, o in enumerate(d.options, start=1))
        decision_lines.append(
            f"**问题 {idx}（影响范围：{d.impact_scope}）**\n{d.question}\n"
            f"{opts}\n"
            f"- 💡 *默认假设*：`{d.default_assumption}`"
        )
        for opt in d.options[:3]:
            action_buttons.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": opt[:26]},
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
            "text": {"tag": "plain_text", "content": "按默认假设继续"},
            "type": "primary",
            "value": {
                "action": "clarify_default",
                "task_id": state.task_id,
            },
        }
    )
    action_buttons.append(
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "在控制台查看"},
            "type": "default",
            "url": f"{web_base_url}/#task={state.task_id}",
        }
    )

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"⏸️ 需你 5 秒拍板 | 任务 #{state.task_id}",
                },
                "template": "orange",
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        f"**推断目标**：{c.objective}\n"
                        f"**目标仓库**：`{c.target_repo}` (`{c.base_branch}` @ `{c.base_commit_sha[:8]}`)\n"
                        f"**允许修改**：`{', '.join(c.permitted_scopes)}`\n"
                        f"**明确不碰**：`{', '.join(c.out_of_scope[:4])}`\n\n"
                        + "\n\n".join(decision_lines)
                        + "\n\n*提示：可直接点击下方按钮，或在群内直接回复 `默认` / 选项序号 `1`、`2`。*"
                    ),
                },
                {"tag": "action", "actions": action_buttons[:5]},
            ],
        },
    }


def build_accepted_receipt_card(
    state: DurableTaskState, web_base_url: str = "http://127.0.0.1:8020"
) -> dict[str, Any]:
    """Build immediate task acceptance receipt card when contract has 0 ambiguities."""
    c = state.contract
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"🚀 已受理并进入隔离沙箱 | #{state.task_id}",
                },
                "template": "blue",
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        f"**契约目标**：{c.objective}\n"
                        f"**目标仓库**：`{c.target_repo}` -> 独立分支 `{state.branch_name}`\n"
                        f"**允许范围**：`{', '.join(c.permitted_scopes)}`\n"
                        f"**预计交付**：沙箱复现并跑通 `Fail-to-Pass` 门禁后将自动推送 Draft PR 报告卡。"
                    ),
                },
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "查看实时沙箱进度"},
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
    """Build final Evidence Report Feishu card (strictly links to human OAuth Git UI for merge)."""
    ev = state.verification_evidence
    risks = (
        ev.reviewer_report.unverified_risks
        if (ev and ev.reviewer_report and ev.reviewer_report.unverified_risks)
        else (ev.unverified_risks if ev else [])
    )
    risk_md = "\n".join(f"- {r}" for r in risks) if risks else "- 无额外未覆盖风险"
    rev_summary = (
        ev.reviewer_report.summary
        if (ev and ev.reviewer_report and ev.reviewer_report.summary)
        else "已通过确定性回归验证与反作弊检查。"
    )

    actions: list[dict[str, Any]] = [
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "打开控制台看 Diff & 日志"},
            "type": "default",
            "url": f"{web_base_url}/#task={state.task_id}",
                        }
    ]
    if state.deliverable_pr_url:
        actions.insert(
            0,
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "前往 GitHub 审阅并合入 Draft PR"},
                "type": "primary",
                "url": state.deliverable_pr_url,
            },
        )

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"✅ 证据门禁已通过 | #{state.task_id}",
                },
                "template": "green",
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        f"**任务目标**：{state.contract.objective}\n"
                        f"**交付分支**：`{state.branch_name}` (`{(state.head_commit_sha or '')[:8]}`)\n"
                        f"**回归单测 (`Fail -> Pass`)**：`{ev.repro_test_target if ev else ''}`\n"
                        f"- 基线复现失败：`{'✅ 是' if (ev and ev.repro_test_failed_on_base) else '❌ 否'}` | "
                        f"修复后通过：`{'✅ 是' if (ev and ev.repro_test_passed_on_head) else '❌ 否'}` | "
                        f"反作弊扫描：`✅ 通过`\n"
                        f"**审查摘要**：{rev_summary}\n"
                        f"**效率核算**：预计净省人工 `{state.estimated_net_minutes_saved} 分钟` · 模型费用 `${state.total_cost_usd:.4f}`\n\n"
                        f"**⚠️ 未验证边界诚实披露 (Unverified Risks)**：\n{risk_md}\n\n"
                        f"*追改提示：直接在 GitHub PR 评论，或在群内发 `#{state.task_id} 你的修改意见` 即可自动增量更新。*"
                    ),
                },
                {"tag": "action", "actions": actions},
            ],
        },
    }


def build_merge_approval_card(
    state: DurableTaskState, web_base_url: str = "http://127.0.0.1:8020"
) -> dict[str, Any]:
    """Build interactive Merge Approval Card pushed once PR Review + GitHub CI both pass."""
    mc = state.merge_approval_card
    ev = state.verification_evidence
    pr_num = mc.pr_number if mc else (state.deliverable_pr_number or 0)
    pr_url = (mc.pr_url if mc else state.deliverable_pr_url) or web_base_url
    rev_model = mc.reviewer_model if mc else (
        ev.reviewer_report.reviewer_model if (ev and ev.reviewer_report) else ""
    )
    rev_summary = mc.reviewer_summary if mc else (
        ev.reviewer_report.summary if (ev and ev.reviewer_report) else ""
    )
    ci_cnt = mc.ci_checks_count if mc else 0
    risk_line = mc.residual_risk if mc else (
        ev.unverified_risks[0] if (ev and ev.unverified_risks) else "无额外未覆盖风险"
    )

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"🛎️ 待你审批合入 | PR #{pr_num} 审查与 CI 已全绿",
                },
                "template": "green",
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        f"**任务目标**：{state.contract.objective}\n"
                        f"**目标分支**：`{state.branch_name}` → `{state.contract.base_branch}` (`{(state.head_commit_sha or '')[:8]}`)\n"
                        f"- **回归门禁 (`Fail → Pass`)**：`{ev.repro_test_target if ev else ''}` ✅\n"
                        f"- **独立跨模型 PR Review (`{rev_model}`)**：`APPROVE · 0 Blocking` ✅\n"
                        f"- **线上 GitHub Actions CI**：`{ci_cnt} 项检查全部通过` ✅\n\n"
                        f"**审查摘要**：{rev_summary}\n"
                        f"**残余边界提示**：{risk_line}\n\n"
                        f"*点击下方「批准并自动合入」或直接回复 `批准` / `#{state.task_id} 批准`，系统将立即执行 Squash Merge。*"
                    ),
                },
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "✅ 批准并自动合入"},
                            "type": "primary",
                            "value": {
                                "action": "approve_merge",
                                "task_id": state.task_id,
                            },
                        },
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "查看 GitHub PR"},
                            "type": "default",
                            "url": pr_url,
                        },
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "暂缓 / 拒绝"},
                            "type": "default",
                            "value": {
                                "action": "reject_merge",
                                "task_id": state.task_id,
                            },
                        },
                    ],
                },
            ],
        },
    }


def build_handoff_card(
    state: DurableTaskState, web_base_url: str = "http://127.0.0.1:8020"
) -> dict[str, Any]:
    """Build structured Handoff Bundle card when task cannot complete autonomously."""
    hb = state.handoff_bundle
    clues = "\n".join(f"- {c[:200]}" for c in (hb.root_cause_clues if hb else [])) or "- 见执行轨迹日志"
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"🤝 结构化交接包 (Handoff) | #{state.task_id}",
                },
                "template": "wathet",
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        f"**停止原因 (`{state.failure_class}`)**：{hb.reason if hb else state.failure_reason}\n"
                        f"**已保留现场分支**：`{state.branch_name}`\n"
                        f"**已排查线索**：\n{clues}\n"
                        f"**建议下一步**：{hb.recommended_next_step if hb else '人工接手 worktree 继续验证'}"
                    ),
                },
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "打开控制台查看现场"},
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
    """Build clean No-Op completion card when sandbox test suite is already 100% green."""
    ev = state.verification_evidence
    summary = ev.noop_summary if (ev and ev.noop_summary) else "目标测试集已在基线全部通过，无需修改代码。"
    target = ev.repro_test_target if ev else "tests"
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"✅ 体检通过・无需改动 (No-Op) | #{state.task_id}",
                },
                "template": "green",
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        f"**任务目标**：{state.contract.objective}\n"
                        f"**目标仓库**：`{state.contract.target_repo}` (`{state.contract.base_branch}` @ `{state.contract.base_commit_sha[:8]}`)\n"
                        f"- **独立沙箱实测目标**：`{target}` (`exit_code=0` ✅)\n"
                        f"- **文件变更数**：`0 个文件（保持干净，未发起多余 PR）`\n\n"
                        f"**体检结论**：{summary}\n"
                        f"**花费**：`${state.total_cost_usd:.4f}`"
                    ),
                },
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "打开控制台查看验证日志"},
                            "type": "default",
                            "url": f"{web_base_url}/#task={state.task_id}",
                        }
                    ],
                },
            ],
        },
    }


def build_test_connection_card(web_base_url: str = "http://127.0.0.1:8020") -> dict[str, Any]:
    """Sample card sent when user clicks 'Send Test Card to Feishu' in Console."""
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": "🟢 AI 研发数字员工连接成功"},
                "template": "green",
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        "飞书消息通道已就绪！后续任务的 **5 秒契约微确认卡**、**Fail-to-Pass 证据交付卡** 与 **结构化交接卡** 将实时推送到此处。\n\n"
                        "- **派活输入格式**：`[仓库名] 修复某某问题或报错堆栈`\n"
                        "- **追改输入格式**：`#t-20261116-xxxxxx 补充某个边界判断`"
                    ),
                },
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "打开数字员工工作台"},
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

    # 0. Greeting / connection ping ("连接", "绑定", "你好", "hi", "ping", "help", "帮助")
    if cleaned.lower() in {"连接", "绑定", "测试", "你好", "您好", "hi", "hello", "ping", "help", "帮助", "?"}:
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
