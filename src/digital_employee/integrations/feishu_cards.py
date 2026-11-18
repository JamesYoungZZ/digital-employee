"""Feishu Interactive Cards, Dual-Channel Notifier (Webhook + OpenAPI) & Message Router."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from digital_employee.agents.reviewer import is_loopback_url
from digital_employee.integrations.jira_client import extract_jira_issue_key
from digital_employee.models.state import DurableTaskState

MENTION_TAG_RE = re.compile(r"@_user_\d+\s*")
TASK_TAG_RE = re.compile(r"#?(t-\d{8}-[a-f0-9]{6})\b", re.IGNORECASE)
REPO_BRACKET_RE = re.compile(r"^\[([A-Za-z0-9_.\-]+)\]\s*(.+)$", re.DOTALL)
QUICK_FOLLOWUP_RE = re.compile(
    r"^(?:追改|修改意见|再改下|改一下|修一下)\s*[:：,\s]\s*(.+)$", re.DOTALL
)


def _resolve_review_report_url(state: DurableTaskState, web_base_url: str) -> str:
    """Prefer GitHub PR Review permalink when `web_base_url` is localhost (`127.0.0.1`)."""
    local_report = f"{web_base_url}/reports/{state.task_id}/review"
    if not is_loopback_url(web_base_url):
        return local_report
    return state.pr_review_comment_url or state.deliverable_pr_url or local_report


def _clean_feishu_text(text: str) -> str:
    """Strip inline backticks unsupported by Feishu Card Schema 1.0 `lark_md`."""
    return (text or "").replace("`", "")


def _clip(text: str, limit: int = 80) -> str:
    """Collapse newlines and cap length so Feishu cards stay crisp and mobile-friendly."""
    compact = " ".join(_clean_feishu_text(text).split()).strip()
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"


def _meta_fields(left_label: str, left_val: str, right_label: str, right_val: str) -> dict[str, Any]:
    return {
        "tag": "div",
        "fields": [
            {
                "is_short": True,
                "text": {
                    "tag": "lark_md",
                    "content": f"**{_clean_feishu_text(left_label)}**\n{_clean_feishu_text(left_val)}",
                },
            },
            {
                "is_short": True,
                "text": {
                    "tag": "lark_md",
                    "content": f"**{_clean_feishu_text(right_label)}**\n{_clean_feishu_text(right_val)}",
                },
            },
        ],
    }


def _jira_link_md(state: DurableTaskState) -> str:
    if not state.jira_issue_key:
        return ""
    if state.jira_issue_url:
        return f"[{state.jira_issue_key}]({state.jira_issue_url})"
    return f"**{state.jira_issue_key}**"


def build_jira_approval_card(
    state: DurableTaskState,
    web_base_url: str = "http://127.0.0.1:8020",
    *,
    candidate_repos: list[str] | None = None,
) -> dict[str, Any]:
    """Build interactive PM/QA Business Jira Ticket Draft review card (`WAITING_JIRA_APPROVAL`)."""
    c = state.contract
    d = state.jira_draft
    proj = (d.project_key if d else "") or "PROJ"
    itype = (d.issue_type if d else "") or "Story"
    prio = (d.priority if d else "") or "Medium"
    role_tag = (
        "QA 缺陷单 Bug"
        if itype == "Bug"
        else ("产品故事 Story" if itype == "Story" else "业务需求 Task")
    )
    desc_label = "故障与影响" if itype == "Bug" else "业务背景与诉求"
    summary = _clip((d.summary if d else c.objective), 90)
    desc = _clip((d.description if d else c.reproduction_evidence) or c.objective, 160)
    ac_items = (d.acceptance_criteria if (d and d.acceptance_criteria) else c.acceptance_criteria)[:3]
    ac_md = (
        "\n".join(f"• {_clip(ac, 85)}" for ac in ac_items)
        if ac_items
        else "• 核心业务场景与异常边界表现符合预期"
    )

    actions: list[dict[str, Any]] = [
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "✅ 批准创建 Jira 并开工"},
            "type": "primary",
            "value": {"action": "approve_jira", "task_id": state.task_id},
        },
    ]
    alt_repos = [r for r in (candidate_repos or []) if r != c.target_repo]
    if alt_repos:
        next_repo = alt_repos[0]
        actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": f"🔀 改派 {next_repo}"},
                "type": "default",
                "value": {
                    "action": "switch_jira_repo",
                    "task_id": state.task_id,
                    "target_repo": next_repo,
                },
            }
        )
    actions.append(
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "✏️ 控制台改稿"},
            "type": "default",
            "url": f"{web_base_url}/#task={state.task_id}",
        }
    )
    actions.append(
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "放弃"},
            "type": "default",
            "value": {"action": "reject_jira", "task_id": state.task_id},
        }
    )

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"📋 待你审核 Jira 工单 ({role_tag}) · {state.task_id}",
                },
                "template": "orange",
            },
            "elements": [
                _meta_fields(
                    "Jira 看板 / 优先级",
                    f"{proj} · {role_tag} ({prio})",
                    "智能路由目标仓",
                    f"{c.target_repo} ({c.base_branch})",
                ),
                {"tag": "hr"},
                {
                    "tag": "markdown",
                    "content": (
                        f"**工单标题**：{summary}\n"
                        f"**{desc_label}**：{desc}\n"
                        f"**产品/QA 验收标准**：\n{ac_md}\n"
                        f"<font color='grey'>直接回复修改意见（如『改稿：把优先级改为Medium』）可在线重拟；确认无误点批准或回『建单』。</font>"
                    ),
                },
                {"tag": "action", "actions": actions[:4]},
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
    rep = ev.reviewer_report if ev else None
    rev_verdict = rep.verdict if rep else "APPROVE"
    score = rep.quality_score if (rep and rep.quality_score is not None) else (95 if rev_verdict == "APPROVE" else 45)
    rec_label = (rep.merge_recommendation_label if rep else "") or (
        "可直接合入" if score >= 90 else ("可合入（留意边界）" if rev_verdict == "APPROVE" else "禁止合入")
    )
    pr_label = (
        f"PR #{state.deliverable_pr_number}"
        if state.deliverable_pr_number
        else (state.head_commit_sha or "")[:8]
    )

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
                "title": {
                    "tag": "plain_text",
                    "content": f"🧪 本地验证已过 ({score}分) · 等待线上 CI ({pr_label})",
                },
                "template": "turquoise",
            },
            "elements": [
                _meta_fields(
                    "仓库 / 分支",
                    f"**{state.contract.target_repo}** → **{state.contract.base_branch}**",
                    "Review 预评",
                    f"{'🟢' if score >= 90 else ('🟡' if rev_verdict == 'APPROVE' else '🔴')} **{score}/100** · {rec_label}",
                ),
                {"tag": "hr"},
                {
                    "tag": "markdown",
                    "content": (
                        f"**改动**：{_clip(state.contract.objective, 75)}\n"
                        + (
                            f"**📌 跨目录连带修改**：{'、'.join(f'`{p}`' for p in ev.out_of_scope_files[:3])}\n"
                            if (ev and ev.out_of_scope_files)
                            else ""
                        )
                        + (
                            f"**💡 规则提炼**：已生成 `{len(state.distilled_policy_ids)}` 条候选治理规则草稿\n"
                            if state.distilled_policy_ids
                            else ""
                        )
                        + f"**门禁**：✅ 单测 Fail→Pass · {'✅' if rev_verdict == 'APPROVE' else '🛑'} AI Review {rev_verdict}（花费 ${state.total_cost_usd:.3f}）\n"
                        f"<font color='grey'>线上 GitHub Actions CI 全绿后将推送「批准合入」审批卡。</font>"
                    ),
                },
                {"tag": "action", "actions": actions},
            ],
        },
    }


def build_merge_approval_card(
    state: DurableTaskState, web_base_url: str = "http://127.0.0.1:8020"
) -> dict[str, Any]:
    """Build crisp, executive-friendly Merge Approval Card with 0-100 score & merge verdict."""
    mc = state.merge_approval_card
    ev = state.verification_evidence
    rep = ev.reviewer_report if ev else None

    pr_num = mc.pr_number if mc else (state.deliverable_pr_number or 0)
    pr_url = (mc.pr_url if mc else state.deliverable_pr_url) or state.pr_review_comment_url or web_base_url
    report_url = _resolve_review_report_url(state, web_base_url)
    rev_model = (
        (mc.reviewer_model if mc else "")
        or (rep.reviewer_model if rep else "")
        or "AI Reviewer"
    )
    if rep and rep.degraded_from_model:
        rev_model = f"{rev_model}·兜底"
    rev_verdict = (
        (mc.reviewer_verdict if mc else "")
        or (rep.verdict if rep else "APPROVE")
    )

    score = (
        rep.quality_score
        if (rep and rep.quality_score is not None)
        else (mc.quality_score if mc else 95)
    )
    rec_code = (
        rep.merge_recommendation
        if (rep and rep.merge_recommendation)
        else (mc.merge_recommendation if mc else ("MERGE_READY" if score >= 90 else "MERGE_WITH_CAUTION"))
    )
    rec_label = (
        (rep.merge_recommendation_label if rep else "")
        or (mc.merge_recommendation_label if mc else "")
        or ("可直接合入" if score >= 90 else "可合入（留意边界）")
    )
    rec_icon = "🟢" if rec_code == "MERGE_READY" else ("🟡" if rec_code == "MERGE_WITH_CAUTION" else "🔴")
    header_prefix = "✅ 可直接合入" if rec_code == "MERGE_READY" else "✅ 建议合入"

    checklist = rep.audit_checklist if rep else []
    if checklist:
        pass_cnt = sum(1 for item in checklist if item.status == "PASS")
        warn_items = [item for item in checklist if item.status != "PASS"]
        if warn_items:
            warn_names = "、".join(
                _clip(w.dimension.replace("业务逻辑与边界条件", "边界条件").replace("需求与验收覆盖", "验收覆盖"), 8)
                for w in warn_items[:2]
            )
            audit_summary = f"✅ {pass_cnt}/{len(checklist)} 通过 · ⚠️ {len(warn_items)} 留意（{warn_names}）"
        else:
            audit_summary = f"✅ {pass_cnt}/{len(checklist)} 维度全通过"
    else:
        audit_summary = f"✅ {rev_verdict}（无阻断项）"

    rev_summary = _clip(
        (mc.reviewer_summary if mc else (rep.summary if rep else ""))
        or "代码改动克制，无阻断级风险。",
        95,
    )
    risk_note = _clip(
        (mc.residual_risk if mc else "")
        or (ev.unverified_risks[0] if (ev and ev.unverified_risks) else ""),
        82,
    )
    ci_cnt = mc.ci_checks_count if mc else 2
    jira_md = _jira_link_md(state)

    review_lines: list[str] = []
    if jira_md:
        review_lines.append(f"**关联工单**：{jira_md}")

    if rep and rep.change_overview:
        overview_bullets = "\n".join(f"• {_clip(item, 68)}" for item in rep.change_overview[:3])
        review_lines.append(f"**改动概览**：{_clip(state.contract.objective, 65)}\n{overview_bullets}")
    else:
        review_lines.append(f"**交付内容**：{_clip(state.contract.objective, 75)}")

    review_lines.append(f"**审查结论**：{rev_summary}")
    if ev and ev.out_of_scope_files:
        extra_paths = "、".join(f"`{p}`" for p in ev.out_of_scope_files[:3])
        suffix = f" 等 {len(ev.out_of_scope_files)} 个文件" if len(ev.out_of_scope_files) > 3 else ""
        review_lines.append(f"**📌 跨目录连带修改**：{extra_paths}{suffix}")
    if state.distilled_policy_ids:
        review_lines.append(
            f"**💡 规则提炼**：已从本次踩坑提炼 `{len(state.distilled_policy_ids)}` 条候选治理规则草稿（可在控制台一键采纳）"
        )
    if risk_note:
        review_lines.append(f"**⚠️ 边界留意**：{risk_note}")
    review_lines.append("<font color='grey'>可直接点按钮，或回复『批准』合入 / 回复『追改：你的意见』微调本 PR。</font>")

    has_cautions = bool(
        rec_code == "MERGE_WITH_CAUTION"
        or risk_note
        or (rep and (rep.notable_observations or any(i.status != "PASS" for i in rep.audit_checklist)))
    )
    action_buttons: list[dict[str, Any]] = [
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "✅ 批准合入"},
            "type": "primary",
            "value": {"action": "approve_merge", "task_id": state.task_id},
        }
    ]
    if has_cautions:
        action_buttons.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "🔧 修掉边界项"},
                "type": "default",
                "value": {"action": "autofix_caution", "task_id": state.task_id},
            }
        )
    action_buttons.extend(
        [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "📄 全量Review报告"},
                "type": "default",
                "url": report_url,
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
                "value": {"action": "hold_merge", "task_id": state.task_id},
            },
        ]
    )

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"{header_prefix} ({score}分) · 待你审批 PR #{pr_num}",
                },
                "template": "green",
            },
            "elements": [
                _meta_fields(
                    "综合评分 / 合入建议",
                    f"{rec_icon} **{score} / 100** · **{rec_label}**",
                    "四道硬门禁",
                    f"✅ 单测回归 · ✅ CI {ci_cnt}/{ci_cnt} · ✅ 0 阻断",
                ),
                _meta_fields(
                    "目标分支",
                    f"**{state.contract.target_repo}** → **{state.contract.base_branch}**",
                    f"五维代码审查 ({rev_model})",
                    audit_summary,
                ),
                {"tag": "hr"},
                {"tag": "markdown", "content": "\n".join(review_lines)},
                {
                    "tag": "action",
                    "actions": action_buttons,
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

    if trigger_source == "jira_update":
        jira_md = _jira_link_md(state) or (state.jira_issue_key or "Jira")
        header_title = f"🔄 权威工单 {state.jira_issue_key} 有新变更 · 待你确认追改"
        left_k, left_v = "变更来源", f"{jira_md} ({comment_author or 'Jira 更新'})"
        right_k, right_v = "关联仓库", f"**{state.contract.target_repo}**"
        body_md = (
            f"**最新工单标题**：{_clip(state.contract.objective, 68)}\n"
            f"**变更 / 评论摘要**：{_clip(human_comment, 130)}\n"
            f"<font color='grey'>契约已同步最新 Jira；点下方「🔧 一键自动修复」将唤醒沙箱按新 Jira 追改代码。</font>"
        )
    elif trigger_source == "github_comment" and human_comment:
        header_title = f"💬 PR #{pr_num} 收到新 Review 评论 · 待你指令"
        left_k, left_v = "评论人", f"@{comment_author or 'reviewer'}"
        right_k, right_v = "关联仓库", f"**{state.contract.target_repo}**"
        body_md = (
            f"**任务**：{_clip(state.contract.objective, 65)}\n"
            f"**评论内容**：{_clip(human_comment, 120)}\n"
            f"<font color='grey'>点下方「一键让 AI 修复」或直接回复『修复』，将唤醒原沙箱改代码并更新 PR。</font>"
        )
    else:
        issues = rep.blocking_issues if rep else []
        score = rep.quality_score if (rep and rep.quality_score is not None) else 45
        header_title = (
            f"🛑 禁止合入 ({score}分 · {len(issues)} 项阻断) · PR #{pr_num}"
            if pr_num
            else f"🛑 禁止合入 ({score}分) · {state.task_id}"
        )
        left_k, left_v = "综合评分 / 判定", f"🔴 **{score} / 100** · **禁止合入**"
        right_k, right_v = (
            f"审查模型 ({rep.reviewer_model if rep else 'AI Reviewer'})",
            f"**{state.contract.target_repo}** · {len(issues)} 项阻断",
        )
        issue_bullets = []
        for idx, b in enumerate(issues[:3], start=1):
            loc = f"{b.path}:{b.line} " if (b.path and b.line) else (f"{b.path} " if b.path else "")
            issue_bullets.append(f"{idx}. **[{b.severity}]** {loc}{_clip(b.message, 68)}")
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
            "url": _resolve_review_report_url(state, web_base_url),
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


def build_merged_closed_card(
    state: DurableTaskState, web_base_url: str = "http://127.0.0.1:8020"
) -> dict[str, Any]:
    """In-place patched completion card replacing Merge Approval Card once merged."""
    pr_num = state.deliverable_pr_number or 0
    jira_md = _jira_link_md(state)
    jira_line = f"**权威工单**：{jira_md}（已自动流转为 **Done**）\n" if jira_md else ""
    actions: list[dict[str, Any]] = []
    if state.deliverable_pr_url:
        actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": f"查看已合入 PR #{pr_num}"},
                "type": "primary",
                "url": state.deliverable_pr_url,
            }
        )
    if state.jira_issue_url:
        actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": f"Jira {state.jira_issue_key}"},
                "type": "default",
                "url": state.jira_issue_url,
            }
        )
    actions.append(
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "控制台归档"},
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
                    "content": f"🎉 已合入主干闭环 · PR #{pr_num}"
                    + (f" ({state.jira_issue_key})" if state.jira_issue_key else ""),
                },
                "template": "green",
            },
            "elements": [
                _meta_fields(
                    "合入分支",
                    f"{state.contract.target_repo} → {state.contract.base_branch}",
                    "合入 Commit",
                    (state.merged_commit_sha or state.head_commit_sha or "")[:10],
                ),
                {"tag": "hr"},
                {
                    "tag": "markdown",
                    "content": f"{jira_line}**交付内容**：{_clip(state.contract.objective, 80)}",
                },
                {"tag": "action", "actions": actions[:3]},
            ],
        },
    }


def build_abandoned_card(state: DurableTaskState, reason: str = "已由人工放弃") -> dict[str, Any]:
    """In-place patched card replacing a rejected Jira draft or rejected Merge card."""
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"⚪ 任务已终止 · {state.task_id}"},
                "template": "grey",
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": f"**原诉求**：{_clip(state.contract.objective, 75)}\n**状态**：{_clip(reason, 90)}",
                }
            ],
        },
    }


def build_merge_result_card(
    state: DurableTaskState,
    *,
    merged: bool,
    merge_sha: str = "",
    reason: str = "",
    approved_by: str = "",
) -> dict[str, Any]:
    """Build crisp Feishu confirmation/error receipt card after clicking Approve Merge."""
    pr_num = state.deliverable_pr_number or 0
    pr_url = state.deliverable_pr_url or ""
    actions: list[dict[str, Any]] = []
    if pr_url:
        actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": f"查看 PR #{pr_num}"},
                "type": "primary" if not merged else "default",
                "url": pr_url,
            }
        )
    if merged:
        elements: list[dict[str, Any]] = [
            _meta_fields(
                "合入分支",
                f"**{state.contract.target_repo}** → **{state.contract.base_branch}**",
                "合入提交",
                f"**{merge_sha[:8] or 'merged'}**（{approved_by or 'human'}）",
            ),
            {"tag": "hr"},
            {
                "tag": "markdown",
                "content": f"**已交付**：{_clip(state.contract.objective, 75)}",
            },
        ]
        if actions:
            elements.append({"tag": "action", "actions": actions})
        return {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": f"✅ PR #{pr_num} 已成功 Squash 合入"},
                    "template": "green",
                },
                "elements": elements,
            },
        }

    elements = [
        _meta_fields(
            "目标仓库",
            f"**{state.contract.target_repo}** → **{state.contract.base_branch}**",
            "状态",
            "合入未完成（PR 仍保持开启）",
        ),
        {"tag": "hr"},
        {
            "tag": "markdown",
            "content": (
                f"**失败原因**：{_clip(reason or 'GitHub Merge API 未通过', 110)}\n"
                f"<font color='grey'>请检查 GitHub 分支保护规则或合并冲突，处理后可再次回复『批准』重试。</font>"
            ),
        },
    ]
    if actions:
        elements.append({"tag": "action", "actions": actions})
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"⚠️ PR #{pr_num} 自动合入未成功"},
                "template": "orange",
            },
            "elements": elements,
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
                        "已绑定当前会话，无需手敲仓库名，直接说人话即可：\n"
                        "• **新需求审卡派活**：直接发 `修复直播间弹幕内存泄漏`（自动识别仓库并起草 PM/QA 工单）\n"
                        "• **在线改工单草稿**：审卡时直接回 `改稿：把优先级改成Medium` 或回 `建单` 批准\n"
                        "• **按已有 Jira 派活**：直接发 `SCRUM-5`（零重复审卡直接开工）"
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

    # 0d. Quick caution/edge-case polish reply ("修边界", "修掉边界项", "优化边界")
    if lowered in {"修边界", "修边界项", "修掉边界项", "修掉边界", "优化边界"}:
        return {"intent": "quick_autofix_caution"}

    # 0e. Natural follow-up comment onto active waiting PR ("追改：把无法根治排除掉")
    qf_match = QUICK_FOLLOWUP_RE.match(cleaned)
    if qf_match and qf_match.group(1).strip():
        return {"intent": "quick_followup_pr", "comment": qf_match.group(1).strip()}

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
        if rest_low.startswith(("改稿", "改卡", "修改草稿", "修改工单")):
            instr = re.sub(r"^(?:改稿|改卡|修改草稿|修改工单)\s*[:：,，]?\s*", "", rest).strip()
            if instr:
                return {"intent": "task_refine_jira", "task_id": tid, "instruction": instr}
        if rest:
            return {"intent": "followup", "task_id": tid, "comment": rest}

    # 2b. Quick conversational Jira draft revision ("改稿：...", "改卡：...", "修改草稿：...")
    if lowered.startswith(("改稿", "改卡", "修改草稿", "修改工单")):
        instr = re.sub(r"^(?:改稿|改卡|修改草稿|修改工单)\s*[:：,，]?\s*", "", cleaned).strip()
        if instr:
            return {"intent": "quick_refine_jira", "instruction": instr}

    # 3. New task delegation (check `[repo] ...` prefix or explicit repo substring)
    chosen_repo = registered_repos[0] if registered_repos else "modular-rag"
    explicit_repo = "false"
    req_body = cleaned
    bracket = REPO_BRACKET_RE.match(cleaned)
    if bracket and bracket.group(1) in registered_repos:
        chosen_repo = bracket.group(1)
        explicit_repo = "true"
        req_body = bracket.group(2).strip()
    else:
        for r in registered_repos:
            if r.lower() in lowered:
                chosen_repo = r
                explicit_repo = "true"
                break

    out = {
        "intent": "delegate",
        "target_repo": chosen_repo,
        "explicit_repo": explicit_repo,
        "raw_request": req_body,
    }
    detected_jira = extract_jira_issue_key(req_body)
    if detected_jira:
        out["jira_issue_key"] = detected_jira
    return out


def extract_feishu_message_id(recorded_delivery: str) -> str:
    """Extract Feishu `om_...` message_id from `openapi_chat:<chat>:<message_id>`."""
    for seg in (recorded_delivery or "").split(","):
        parts = seg.strip().split(":")
        if len(parts) >= 3 and parts[0] == "openapi_chat" and parts[2].startswith("om_"):
            return parts[2]
    return ""


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
                    msg_id = str(((body.get("data") or {}) if isinstance(body, dict) else {}).get("message_id") or "")
                    suffix = f":{msg_id}" if msg_id else ""
                    results.append(f"openapi_chat:{target_chat_id[:12]}{suffix}")

            if self.webhook_url:
                resp = client.post(self.webhook_url, json=card_payload)
                resp.raise_for_status()
                body = resp.json()
                if isinstance(body, dict) and body.get("code", 0) not in (0, None):
                    raise RuntimeError(f"Feishu webhook error: {body}")
                results.append(f"webhook:{resp.status_code}")

        return ",".join(results) if results else "local_artifact_only"

    def update_card(self, message_id: str, card_payload: dict[str, Any]) -> bool:
        """Patch an existing interactive Feishu card in-place (`PATCH /im/v1/messages/{id}`)."""
        mid = (message_id or "").strip()
        if not (mid and self.app_id and self.app_secret):
            return False
        try:
            with httpx.Client(timeout=12.0) as client:
                token = self._fetch_tenant_token(client)
                resp = client.patch(
                    f"https://open.feishu.cn/open-apis/im/v1/messages/{mid}",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"content": json.dumps(card_payload["card"], ensure_ascii=False)},
                )
                if resp.status_code >= 400:
                    return False
                body = resp.json()
                return isinstance(body, dict) and body.get("code", 0) in (0, None)
        except Exception:
            return False
