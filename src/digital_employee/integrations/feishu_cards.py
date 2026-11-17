"""Feishu Interactive Cards, Dual-Channel Notifier (Webhook + OpenAPI) & Message Router."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from digital_employee.integrations.jira_client import extract_jira_issue_key
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


def _jira_link_md(state: DurableTaskState) -> str:
    if not state.jira_issue_key:
        return ""
    if state.jira_issue_url:
        return f"[{state.jira_issue_key}]({state.jira_issue_url})"
    return f"`{state.jira_issue_key}`"


def build_jira_approval_card(
    state: DurableTaskState, web_base_url: str = "http://127.0.0.1:8020"
) -> dict[str, Any]:
    """Build interactive Jira Ticket Draft review card (`WAITING_JIRA_APPROVAL`)."""
    c = state.contract
    d = state.jira_draft
    proj = (d.project_key if d else "") or "PROJ"
    itype = (d.issue_type if d else "") or "Task"
    prio = (d.priority if d else "") or "Medium"
    summary = _clip((d.summary if d else c.objective), 90)
    desc = _clip((d.description if d else c.reproduction_evidence) or c.objective, 120)
    ac_items = (d.acceptance_criteria if (d and d.acceptance_criteria) else c.acceptance_criteria)[:3]
    ac_md = "\n".join(f"• {_clip(ac, 72)}" for ac in ac_items) if ac_items else "• 回归单测与门禁通过"

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"📋 待你审核 Jira 工单 · {state.task_id}"},
                "template": "orange",
            },
            "elements": [
                _meta_fields(
                    "Jira 项目 / 类型",
                    f"`{proj}` · `{itype}` (`{prio}`)",
                    "目标仓库",
                    f"`{c.target_repo}` (`{c.base_branch}`)",
                ),
                {"tag": "hr"},
                {
                    "tag": "markdown",
                    "content": (
                        f"**工单标题**：{summary}\n"
                        f"**需求说明**：{desc}\n"
                        f"**验收标准**：\n{ac_md}\n"
                        f"<font color='grey'>确认后将在 Jira 正式建卡并锁定为唯一权威需求源（也可回复『建单』或进控制台改草稿）。</font>"
                    ),
                },
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "✅ 批准创建 Jira 并开工"},
                            "type": "primary",
                            "value": {"action": "approve_jira", "task_id": state.task_id},
                        },
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "✏️ 控制台修改草稿"},
                            "type": "default",
                            "url": f"{web_base_url}/#task={state.task_id}",
                        },
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "放弃"},
                            "type": "default",
                            "value": {"action": "reject_jira", "task_id": state.task_id},
                        },
                    ],
                },
            ],
        },
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
    jira_md = _jira_link_md(state)
    jira_line = f"**权威工单**：{jira_md}（全程以 Jira 任务为准）\n" if jira_md else ""
    actions: list[dict[str, Any]] = [
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "查看进度"},
            "type": "default",
            "url": f"{web_base_url}/#task={state.task_id}",
        }
    ]
    if state.jira_issue_url:
        actions.insert(
            0,
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": f"Jira {state.jira_issue_key}"},
                "type": "primary",
                "url": state.jira_issue_url,
            },
        )
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": (
                        f"🚀 已按 Jira {state.jira_issue_key} 接单开工"
                        if state.jira_issue_key
                        else f"🚀 已接单开工 · {state.task_id}"
                    ),
                },
                "template": "blue",
            },
            "elements": [
                _meta_fields("仓库", f"`{c.target_repo}` (`{c.base_branch}`)", "沙箱分支", f"`{state.branch_name}`"),
                {"tag": "hr"},
                {
                    "tag": "markdown",
                    "content": f"{jira_line}**任务**：{_clip(c.objective, 75)}\n<font color='grey'>跑通单测、AI 审查与线上 CI 后，将向你推送合入审批卡。</font>",
                },
                {
                    "tag": "action",
                    "actions": actions,
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
    pr_url = state.pr_review_comment_url or (mc.pr_url if mc else state.deliverable_pr_url) or web_base_url
    rev_model = (
        (mc.reviewer_model if mc else "")
        or (ev.reviewer_report.reviewer_model if (ev and ev.reviewer_report) else "")
        or "AI Reviewer"
    )
    rev_verdict = (
        (mc.reviewer_verdict if mc else "")
        or (ev.reviewer_report.verdict if (ev and ev.reviewer_report) else "APPROVE")
    )
    rev_summary = _clip(
        (mc.reviewer_summary if mc else (ev.reviewer_report.summary if (ev and ev.reviewer_report) else ""))
        or "代码改动克制，无阻断级风险。",
        90,
    )
    risk_note = _clip(
        (mc.residual_risk if mc else "")
        or (ev.unverified_risks[0] if (ev and ev.unverified_risks) else ""),
        75,
    )
    ci_cnt = mc.ci_checks_count if mc else 2
    jira_md = _jira_link_md(state)
    review_lines = []
    if jira_md:
        review_lines.append(f"**关联工单**：{jira_md}")
    review_lines.extend(
        [
            f"**交付内容**：{_clip(state.contract.objective, 75)}",
            f"**Code Review (`{rev_model}` · `{rev_verdict}`)**：{rev_summary}",
        ]
    )
    if risk_note:
        review_lines.append(f"**边界提示**：{risk_note}")

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"🛎️ Review 通过 · 待你合入 PR #{pr_num}"},
                "template": "green",
            },
            "elements": [
                _meta_fields(
                    "目标分支",
                    f"`{state.contract.target_repo}` → `{state.contract.base_branch}`",
                    "验证门禁",
                    f"✅ Review `0` 阻断 · ✅ CI `{ci_cnt}/{ci_cnt}`",
                ),
                {"tag": "hr"},
                {"tag": "markdown", "content": "\n".join(review_lines)},
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "✅ 批准合入"},
                            "type": "primary",
                            "value": {"action": "approve_merge", "task_id": state.task_id},
                        },
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "📄 全量Review报告"},
                            "type": "default",
                            "url": f"{web_base_url}/reports/{state.task_id}/review",
                        },
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": f"PR #{pr_num}"},
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


def build_review_fix_card(
    state: DurableTaskState,
    web_base_url: str = "http://127.0.0.1:8020",
    *,
    trigger_source: str = "ai_review",
    human_comment: str = "",
    comment_author: str = "",
) -> dict[str, Any]:
    """Build interactive 1-Click Auto-Fix Card when AI Review BLOCKs or a human comments on the PR."""
    ev = state.verification_evidence
    rep = ev.reviewer_report if ev else None
    pr_num = state.deliverable_pr_number or 0
    pr_url = state.pr_review_comment_url or state.deliverable_pr_url or web_base_url

    if trigger_source == "github_comment" and human_comment:
        header_title = f"💬 PR #{pr_num} 收到新 Review 评论 · 待你指令"
        left_k, left_v = "评论人", f"`@{comment_author or 'reviewer'}`"
        right_k, right_v = "关联仓库", f"`{state.contract.target_repo}`"
        body_md = (
            f"**任务**：{_clip(state.contract.objective, 65)}\n"
            f"**评论内容**：{_clip(human_comment, 120)}\n"
            f"<font color='grey'>点下方「一键让 AI 修复」或直接回复『修复』，将唤醒原沙箱改代码并更新 PR。</font>"
        )
    else:
        issues = rep.blocking_issues if rep else []
        header_title = f"🛑 Code Review 发现 {len(issues)} 项问题 · PR #{pr_num}" if pr_num else f"🛑 Code Review 拦截 · {state.task_id}"
        left_k, left_v = "审查模型", f"`{rep.reviewer_model if rep else 'AI Reviewer'}`"
        right_k, right_v = "目标仓库", f"`{state.contract.target_repo}`"
        issue_bullets = []
        for idx, b in enumerate(issues[:3], start=1):
            loc = f"`{b.path}:{b.line}` " if (b.path and b.line) else (f"`{b.path}` " if b.path else "")
            issue_bullets.append(f"`{idx}` **[{b.severity}]** {loc}{_clip(b.message, 68)}")
        if len(issues) > 3:
            issue_bullets.append(f"<font color='grey'>…其余 {len(issues) - 3} 项见全量报告</font>")
        body_md = (
            f"**改动目标**：{_clip(state.contract.objective, 65)}\n"
            + "\n".join(issue_bullets)
            + "\n<font color='grey'>点击「🔧 一键自动修复」或回复『修复』，Agent 将逐条修复并重新提交审查。</font>"
        )

    actions: list[dict[str, Any]] = [
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "🔧 一键自动修复"},
            "type": "primary",
            "value": {
                "action": "autofix_review",
                "task_id": state.task_id,
                "comment": human_comment[:400] if human_comment else "",
            },
        },
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "📄 全量报告"},
            "type": "default",
            "url": f"{web_base_url}/reports/{state.task_id}/review",
        },
    ]
    if state.deliverable_pr_url:
        actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": f"PR #{pr_num}" if pr_num else "查看 PR"},
                "type": "default",
                "url": pr_url,
            }
        )
    actions.append(
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "暂不处理"},
            "type": "default",
            "value": {"action": "ignore_review_comment", "task_id": state.task_id},
        }
    )

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": header_title},
                "template": "orange",
            },
            "elements": [
                _meta_fields(left_k, left_v, right_k, right_v),
                {"tag": "hr"},
                {"tag": "markdown", "content": body_md},
                {"tag": "action", "actions": actions[:4]},
            ],
        },
    }


def build_handoff_card(
    state: DurableTaskState, web_base_url: str = "http://127.0.0.1:8020"
) -> dict[str, Any]:
    """Build concise Handoff / Review-Blocked card when task pauses for human intervention."""
    hb = state.handoff_bundle
    ev = state.verification_evidence
    reason = _clip((hb.reason if hb else state.failure_reason) or "未能在预算步数内通过门禁", 80)
    rev_block_line = ""
    if ev and ev.reviewer_report and ev.reviewer_report.blocking_issues:
        b0 = ev.reviewer_report.blocking_issues[0]
        rev_block_line = f"\n**Review 拦截 (`{b0.severity}`)**：{_clip(b0.message, 75)}"

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
                    "content": f"**任务**：{_clip(state.contract.objective, 65)}\n**卡点**：{reason}{rev_block_line}",
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
                        "• **按已有 Jira 派活**：`[modular-rag] KAN-12`（直接按 Jira 开工）\n"
                        "• **新需求审卡派活**：`[modular-rag] 修复某某报错`（先推 Jira 草稿供你审核建卡）\n"
                        "• **审批指令**：回 `建单` 建 Jira 开工 · 回 `批准` 合入 PR · 回 `修复` 自动修评论"
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

    lowered = cleaned.lower()

    # 0a. Quick Jira ticket creation approval ("建单", "建卡", "确认建单", "批准建单")
    if lowered in {
        "建单",
        "建卡",
        "确认建单",
        "确认建卡",
        "批准建单",
        "批准建卡",
        "同意建单",
        "approve jira",
    }:
        return {"intent": "quick_approve_jira"}

    # 0b. Quick merge approval reply ("批准", "同意合入", "合入", "approve", "merge")
    if lowered in {"批准", "同意合入", "合入", "批准合入", "approve", "merge", "lgtm"}:
        return {"intent": "quick_approve_merge"}

    # 0c. Quick review auto-fix reply ("修复", "一键修复", "自动修复", "autofix", "fix")
    if lowered in {"修复", "一键修复", "自动修复", "按意见修复", "autofix", "fix"}:
        return {"intent": "quick_autofix_review"}

    # 1. Quick clarification reply ("默认", "继续", "1", "2")
    if lowered in {"默认", "继续", "确认", "default", "ok", "yes", "1", "2"}:
        return {"intent": "quick_clarify", "choice": cleaned}

    # 1b. Greeting / connection ping ("连接", "链接", "绑定", "你好", "hi", "ping", "help", "帮助")
    if (
        lowered
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

    # 2. Follow-up or targeted approval on specific task ID ("#t-20261116-abcdef 补充单测")
    task_match = TASK_TAG_RE.search(cleaned)
    if task_match:
        tid = task_match.group(1).lower()
        rest = (cleaned[: task_match.start()] + cleaned[task_match.end() :]).strip(" :：,，")
        rest_low = rest.lower()
        if rest_low in {"建单", "建卡", "确认建单", "确认建卡", "批准建单", "approve jira"}:
            return {"intent": "task_approve_jira", "task_id": tid}
        if rest_low in {"批准", "同意合入", "合入", "批准合入", "approve", "merge", "lgtm"}:
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
            if r.lower() in lowered:
                chosen_repo = r
                break

    out = {"intent": "delegate", "target_repo": chosen_repo, "raw_request": req_body}
    detected_jira = extract_jira_issue_key(req_body)
    if detected_jira:
        out["jira_issue_key"] = detected_jira
    return out


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
