"""Feishu Interactive Cards & Webhook Notifier (Spec v2.0 Section 1.3 & 3.6)."""

from __future__ import annotations

from typing import Any

import httpx

from digital_employee.models.state import DurableTaskState


def build_clarification_card(state: DurableTaskState, web_base_url: str = "http://127.0.0.1:8020") -> dict[str, Any]:
    """Build 5-second micro-clarification Feishu interactive card."""
    c = state.contract
    decision_lines: list[str] = []
    for idx, d in enumerate(c.unresolved_decisions, start=1):
        opts = " / ".join(f"`{o}`" for o in d.options)
        decision_lines.append(
            f"**Q{idx} ({d.impact_scope})**: {d.question}\n"
            f"- 候选选项：{opts}\n"
            f"- 默认假设：`{d.default_assumption}`"
        )

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"⏸️ 需你 5 秒拍板 | 任务契约 #{state.task_id}",
                },
                "template": "orange",
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        f"**推断目标**：{c.objective}\n"
                        f"**目标仓库**：`{c.target_repo}` (`{c.base_branch}` @ `{c.base_commit_sha[:8]}`)\n"
                        f"**允许范围**：`{', '.join(c.permitted_scopes)}`\n"
                        f"**明确排除**：`{', '.join(c.out_of_scope)}`\n\n"
                        + "\n\n".join(decision_lines)
                    ),
                },
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "打开控制面板一键确认"},
                            "type": "primary",
                            "url": f"{web_base_url}/tasks/{state.task_id}",
                        }
                    ],
                },
            ],
        },
    }


def build_evidence_report_card(state: DurableTaskState, web_base_url: str = "http://127.0.0.1:8020") -> dict[str, Any]:
    """Build final Evidence Report Feishu card (strictly links to human OAuth Git UI for merge)."""
    ev = state.verification_evidence
    risks = (
        ev.reviewer_report.unverified_risks
        if (ev and ev.reviewer_report and ev.reviewer_report.unverified_risks)
        else (ev.unverified_risks if ev else [])
    )
    risk_md = "\n".join(f"- {r}" for r in risks) if risks else "- 无额外未覆盖风险"

    actions: list[dict[str, Any]] = [
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "查看任务证据详情"},
            "type": "default",
            "url": f"{web_base_url}/tasks/{state.task_id}",
        }
    ]
    if state.deliverable_pr_url:
        actions.insert(
            0,
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "前往 GitHub 审阅并合入 (人类身份授权)"},
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
                        f"**回归测试 (Fail -> Pass)**：`{ev.repro_test_target if ev else ''}` "
                        f"(基线复现失败=`{ev.repro_test_failed_on_base if ev else False}`, "
                        f"修复后通过=`{ev.repro_test_passed_on_head if ev else False}`)\n"
                        f"**反作弊 & 原有套件**：`通过`\n"
                        f"**预计净省人工耗时**：`{state.estimated_net_minutes_saved} 分钟` (成本 `${state.total_cost_usd:.4f}`)\n\n"
                        f"**⚠️ 未验证边界显式披露 (Unverified Risks)**：\n{risk_md}"
                    ),
                },
                {"tag": "action", "actions": actions},
            ],
        },
    }


def build_handoff_card(state: DurableTaskState, web_base_url: str = "http://127.0.0.1:8020") -> dict[str, Any]:
    """Build structured Handoff Bundle card when task cannot complete autonomously."""
    hb = state.handoff_bundle
    clues = "\n".join(f"- {c}" for c in (hb.root_cause_clues if hb else [])) or "- 见执行轨迹日志"
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
                        f"**保留现场分支**：`{state.branch_name}`\n"
                        f"**已排查线索**：\n{clues}\n"
                        f"**建议下一步**：{hb.recommended_next_step if hb else '人工接手 worktree 继续验证'}"
                    ),
                },
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "查看交接现场与日志"},
                            "type": "primary",
                            "url": f"{web_base_url}/tasks/{state.task_id}",
                        }
                    ],
                },
            ],
        },
    }


class FeishuNotifier:
    """Dispatches interactive cards to Feishu Webhook when configured."""

    def __init__(self, webhook_url: str = "") -> None:
        self.webhook_url = webhook_url.strip()

    @property
    def is_configured(self) -> bool:
        return bool(self.webhook_url)

    def send_card(self, card_payload: dict[str, Any]) -> str:
        if not self.is_configured:
            return "local_artifact_only"
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(self.webhook_url, json=card_payload)
            resp.raise_for_status()
            return f"sent:{resp.status_code}"
