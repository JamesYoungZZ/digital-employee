"""Independent Cross-Model PR Review Agent powered by `skills/pr-code-review/SKILL.md`."""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from digital_employee.context.decision_ledger import DecisionLedger

if TYPE_CHECKING:
    from digital_employee.storage.durable_store import DurableStore
from digital_employee.llm.client import LLMClient
from digital_employee.models.contract import TaskContract
from digital_employee.models.state import (
    BlockingReviewIssue,
    DurableTaskState,
    ReviewAuditItem,
    ReviewerReport,
    ReviewObservation,
    VerificationEvidence,
)
from digital_employee.sandbox.worktree_runner import WorktreeSandbox
from digital_employee.settings import PROJECT_ROOT
from digital_employee.verification.evidence_gate import is_test_path

CHANGED_SYMBOL_RE = re.compile(r"^[+-]\s*(?:async\s+def|def|class)\s+([A-Za-z_][A-Za-z0-9_]*)\b", re.MULTILINE)


def is_loopback_url(url: str) -> bool:
    """Return True if `url` points to a local loopback interface unreachable from mobile/remote devices."""
    if not url:
        return True
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return True
    return host in ("", "127.0.0.1", "localhost", "0.0.0.0", "::1")


def build_reviewer_code_context(
    sandbox: WorktreeSandbox,
    base_commit_sha: str,
    changed_files: list[str],
    *,
    prod_diff_budget: int = 40000,
    test_diff_budget: int = 18000,
    source_context_budget: int = 12000,
) -> str:
    """Build prioritized, non-blind review context: manifest + prod diffs + test diffs + source & callers."""
    prod_files = [f for f in changed_files if not is_test_path(f)]
    test_files = [f for f in changed_files if is_test_path(f)]

    sections: list[str] = [
        f"## Modified File Manifest ({len(changed_files)} total: {len(prod_files)} production, {len(test_files)} test)\n"
        + "\n".join(f"- `{f}` ({'test' if is_test_path(f) else 'production'})" for f in changed_files)
    ]

    prod_diff = sandbox.git_diff(base_commit_sha, paths=prod_files) if prod_files else ""
    if prod_diff:
        clipped_prod = prod_diff[:prod_diff_budget]
        trunc_note = (
            f"\n... [production diff truncated at {prod_diff_budget} chars; use `inspect_requests` `read_file` to view remaining lines]"
            if len(prod_diff) > prod_diff_budget
            else ""
        )
        sections.append(
            f"## Production Source Diff ({len(prod_files)} files)\n```diff\n{clipped_prod}{trunc_note}\n```"
        )

    test_diff = sandbox.git_diff(base_commit_sha, paths=test_files) if test_files else ""
    if test_diff:
        clipped_test = test_diff[:test_diff_budget]
        trunc_note = (
            f"\n... [test diff truncated at {test_diff_budget} chars; use `inspect_requests` `read_file` to inspect specific tests]"
            if len(test_diff) > test_diff_budget
            else ""
        )
        sections.append(
            f"## Test Suite Diff ({len(test_files)} files)\n```diff\n{clipped_test}{trunc_note}\n```"
        )

    # Attach surrounding/full content of modified production code files so Reviewer sees full signatures & invariants
    src_snippets: list[str] = []
    used_src_chars = 0
    for pf in prod_files[:6]:
        if pf.lower().endswith((".md", ".lock", ".json", ".svg", ".png")):
            continue
        try:
            snippet = sandbox.read_file(pf, start_line=1, end_line=220)
        except Exception:
            continue
        if used_src_chars + len(snippet) > source_context_budget:
            remaining = source_context_budget - used_src_chars
            if remaining > 600:
                src_snippets.append(snippet[:remaining] + "\n... (use `read_file` for further lines)")
            break
        src_snippets.append(snippet)
        used_src_chars += len(snippet)

    if src_snippets:
        sections.append("## Modified Production File Context (HEAD)\n" + "\n\n".join(src_snippets))

    # Cross-reference callers of modified functions/classes across the repository
    symbols: list[str] = []
    for sym in CHANGED_SYMBOL_RE.findall(prod_diff):
        if not sym.startswith("_") and sym not in symbols:
            symbols.append(sym)
    caller_blocks: list[str] = []
    for sym in symbols[:5]:
        try:
            hits = sandbox.search_code(rf"\b{re.escape(sym)}\b", max_hits=12)
        except Exception:
            continue
        if hits and hits != "(no matches)":
            caller_blocks.append(f"### References to `{sym}`\n{hits}")
    if caller_blocks:
        sections.append("## Repository Caller Cross-References\n" + "\n\n".join(caller_blocks))

    return "\n\n".join(sections)


def execute_reviewer_inspect_requests(
    sandbox: WorktreeSandbox, requests: list[Any], *, max_items: int = 4
) -> str:
    """Execute read-only `read_file` / `search_code` / `list_files` requests for FocusedReviewer."""
    outputs: list[str] = []
    for req in requests[:max_items]:
        if not isinstance(req, dict):
            continue
        kind = str(req.get("type") or "").strip().lower()
        try:
            if kind == "read_file" and req.get("path"):
                s_ln = int(req.get("start_line") or 1)
                e_ln = int(req.get("end_line") or (s_ln + 180))
                outputs.append(sandbox.read_file(str(req["path"]), start_line=s_ln, end_line=e_ln))
            elif kind == "search_code" and req.get("pattern"):
                prefix = str(req.get("path_prefix") or "")
                hits = sandbox.search_code(str(req["pattern"]), path_prefix=prefix, max_hits=25)
                outputs.append(f"# search_code({req['pattern']!r})\n{hits}")
            elif kind == "list_files":
                prefix = str(req.get("prefix") or "")
                files = sandbox.list_files(prefix=prefix, max_entries=80)
                outputs.append(f"# list_files({prefix!r})\n" + "\n".join(files))
        except Exception as exc:
            outputs.append(f"# inspect error ({kind}): {exc}")
    return "\n\n".join(outputs) if outputs else "(no valid inspect_requests)"

FALLBACK_SKILL_PROMPT = """You are an Independent Principal PR Review Agent.
Strictly suppress style, formatting, naming, or speculative nitpicks.
Report `blocking_issues` ONLY for severe P0/P1 logic bugs, security breaches, resource leaks, test tampering, or DecisionLedger violations.
Return strict JSON with keys: verdict ("APPROVE"|"BLOCK"), ac_covered, decision_ledger_compliant, change_overview, audit_checklist, blocking_issues, notable_observations, concise_summary, residual_risk_note.
"""


def load_pr_review_skill(skill_path: Path | None = None) -> str:
    target = skill_path or (PROJECT_ROOT / "skills" / "pr-code-review" / "SKILL.md")
    if target.is_file():
        return target.read_text(encoding="utf-8")
    return FALLBACK_SKILL_PROMPT


def render_pr_review_agent_comment(
    report: ReviewerReport,
    *,
    verified_commit_sha: str,
    repro_test_target: str,
    full_report_url: str = "",
) -> str:
    """Render a crystal-clear, structured PR Review comment with full report link & expandable checklist."""
    effective_url = full_report_url or report.full_report_url
    is_approve = report.verdict == "APPROVE"
    score = report.quality_score if report.quality_score is not None else (95 if is_approve else 45)
    rec_icon = (
        "🟢"
        if report.merge_recommendation == "MERGE_READY"
        else ("🟡" if report.merge_recommendation == "MERGE_WITH_CAUTION" else "🔴")
    )
    rec_label = report.merge_recommendation_label or ("可直接合入" if is_approve else "禁止合入")
    model_badge = (
        f"`{report.reviewer_model}` · 兜底自 `{report.degraded_from_model}`"
        if report.degraded_from_model
        else f"`{report.reviewer_model}`"
    )
    header = (
        f"### ✅ AI 独立代码审查通过 · `{score}/100` 分 (`APPROVE` · 模型 {model_badge})"
        if is_approve
        else f"### 🛑 AI 独立代码审查拦截 · `{score}/100` 分 (`BLOCK` · 共 `{len(report.blocking_issues)}` 项阻断问题 · 模型 {model_badge})"
    )

    lines: list[str] = [
        "<!-- ai-pr-review-agent -->",
        header,
        "",
        f"**📊 综合质量评分**：`{score} / 100` · **合入建议**：{rec_icon} **{rec_label}**",
        "",
    ]

    # 1. Plain-Chinese overview of what actually happened in this PR
    if report.change_overview:
        lines.append("**📦 本次改动做了什么**")
        for item in report.change_overview[:4]:
            lines.append(f"- {item}")
        lines.append("")

    # 2. Core Verdict Summary
    lines.append(f"**🎯 审查结论**：{report.summary}")
    lines.append("")

    # 3. If BLOCK: Explicitly write out every blocking review comment in full detail!
    if report.blocking_issues:
        lines.append("#### 🚨 必须修改的阻断级问题 (Blocking Review Comments)")
        for idx, b in enumerate(report.blocking_issues, start=1):
            loc = f"`{b.path}:{b.line}`" if (b.path and b.line) else (f"`{b.path}`" if b.path else "`diff`")
            lines.append(f"{idx}. **[{b.severity} · {b.category}]** {loc}")
            lines.append(f"   - **问题原因**：{b.message}")
            if b.suggestion:
                lines.append(f"   - **修改建议**：{b.suggestion}")
        lines.append("")

    # 4. Real non-blocking engineering observations (if any)
    if report.notable_observations:
        lines.append("#### 💡 审查补充观察（非阻断建议）")
        for obs in report.notable_observations[:3]:
            loc = f"`{obs.path}:{obs.line}` " if (obs.path and obs.line) else (f"`{obs.path}` " if obs.path else "")
            lines.append(f"- {loc}{obs.note}")
        lines.append("")

    # 5. Full Review Report Link (only when non-loopback!) + Expandable Full 5-Dimension Audit Matrix
    if effective_url and not is_loopback_url(effective_url):
        lines.append(f"📄 **[点击打开独立全量 Code Review 报告页面（含完整核查表与逐行 Diff）]({effective_url})**")
        lines.append("")

    lines.append("<details>")
    lines.append("<summary>📋 <b>在此直接展开全量审查报告明细（5 维核查矩阵 · 回归验证 · 边界风险）</b></summary>")
    lines.append("")
    lines.append(f"- **审查提交 SHA**：`{verified_commit_sha[:10]}`")
    lines.append(f"- **确定性回归单测 (`Fail → Pass`)**：`{repro_test_target}` ✅")
    lines.append(f"- **审查技能规范**：`{report.skill_used}`")
    lines.append("")
    if report.audit_checklist:
        lines.append("| 审查维度 | 结论 | 具体核查依据 |")
        lines.append("| :--- | :---: | :--- |")
        for row in report.audit_checklist:
            badge = "✅ PASS" if row.status == "PASS" else ("⚠️ WARN" if row.status == "WARN" else "❌ FAIL")
            lines.append(f"| **{row.dimension}** | {badge} | {row.detail} |")
        lines.append("")
    if report.unverified_risks:
        lines.append(f"**⚠️ 未覆盖线上边界提示**：{report.unverified_risks[0]}")
    lines.append("</details>")

    return "\n".join(lines)


def render_full_review_report_html(state: DurableTaskState, diff_text: str) -> str:
    """Render a standalone, readable HTML Full Code Review Report page (`/reports/{task_id}/review`)."""
    ev = state.verification_evidence
    rep = ev.reviewer_report if ev else None
    esc = html.escape

    verdict = rep.verdict if rep else "PENDING"
    score_val = rep.quality_score if (rep and rep.quality_score is not None) else None
    rec_label = rep.merge_recommendation_label if rep else ""
    verdict_bg = (
        "#059669"
        if (rep and rep.merge_recommendation == "MERGE_READY")
        else ("#d97706" if verdict == "APPROVE" else ("#dc2626" if verdict == "BLOCK" else "#52525b"))
    )
    if verdict == "APPROVE":
        verdict_label = (
            f"⭐️ {score_val} / 100 · {rec_label}" if score_val is not None else "✅ 审查通过 (APPROVE)"
        )
    elif verdict == "BLOCK":
        verdict_label = (
            f"🛑 {score_val} / 100 · {rec_label}"
            if score_val is not None
            else f"🛑 审查拦截 (BLOCK · {len(rep.blocking_issues) if rep else 0} 项阻断)"
        )
    else:
        verdict_label = "⏳ 待审查"

    overview_html = "".join(
        f"<li>{esc(item)}</li>" for item in ((rep.change_overview if rep else []) or [state.contract.objective])
    )

    blocking_html = ""
    if rep and rep.blocking_issues:
        cards = []
        for b in rep.blocking_issues:
            loc = f"{b.path}:{b.line}" if (b.path and b.line) else (b.path or "diff")
            sug = f"<div style='margin-top:6px;color:#047857;'><b>修改建议：</b>{esc(b.suggestion)}</div>" if b.suggestion else ""
            cards.append(
                f"<div style='border:1px solid #fecaca;background:#fef2f2;border-radius:10px;padding:14px;margin-bottom:10px;'>"
                f"<div style='font-weight:600;color:#b91c1c;'>[{esc(b.severity)} · {esc(b.category)}] <code>{esc(loc)}</code></div>"
                f"<div style='margin-top:6px;color:#18181b;'><b>问题原因：</b>{esc(b.message)}</div>{sug}</div>"
            )
        blocking_html = f"<h2>🚨 阻断级审查意见 (Blocking Issues)</h2>{''.join(cards)}"

    obs_html = ""
    if rep and rep.notable_observations:
        items = []
        for o in rep.notable_observations:
            loc = f"<code>{esc(o.path)}{':' + str(o.line) if o.line else ''}</code> — " if o.path else ""
            items.append(f"<li>{loc}{esc(o.note)}</li>")
        obs_html = f"<h2>💡 审查补充观察（非阻断建议）</h2><ul>{''.join(items)}</ul>"

    checklist_rows = ""
    for item in (rep.audit_checklist if rep else []):
        color = "#059669" if item.status == "PASS" else ("#d97706" if item.status == "WARN" else "#dc2626")
        checklist_rows += (
            f"<tr><td style='font-weight:600;'>{esc(item.dimension)}</td>"
            f"<td style='color:{color};font-weight:700;'>{esc(item.status)}</td>"
            f"<td>{esc(item.detail)}</td></tr>"
        )
    checklist_html = (
        f"<h2>📋 五维全量核查矩阵</h2><table><thead><tr><th>审查维度</th><th>状态</th><th>核查依据</th></tr></thead>"
        f"<tbody>{checklist_rows}</tbody></table>"
        if checklist_rows
        else ""
    )

    risk_html = (
        f"<div style='background:#fffbeb;border:1px solid #fde68a;border-radius:10px;padding:12px 16px;margin-top:16px;'>"
        f"<b>⚠️ 残余未覆盖边界提示：</b>{esc(rep.unverified_risks[0])}</div>"
        if (rep and rep.unverified_risks)
        else ""
    )

    pr_link = (
        f"<a href='{esc(state.deliverable_pr_url)}' target='_blank'>GitHub PR #{state.deliverable_pr_number or ''} ↗</a>"
        if state.deliverable_pr_url
        else "本地分支"
    )

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>全量 Code Review 报告 · {esc(state.task_id)}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background:#f8fafc; color:#0f172a; margin:0; padding:32px 16px; line-height:1.6; }}
    .wrap {{ max-width:960px; margin:0 auto; background:#fff; border:1px solid #e2e8f0; border-radius:16px; padding:28px 32px; box-shadow:0 4px 20px rgba(15,23,42,0.04); }}
    h1 {{ font-size:20px; margin:0 0 8px; }}
    h2 {{ font-size:15px; margin:24px 0 10px; padding-bottom:6px; border-bottom:1px solid #f1f5f9; }}
    code {{ background:#f1f5f9; padding:2px 6px; border-radius:5px; font-size:12.5px; }}
    table {{ width:100%; border-collapse:collapse; font-size:13.5px; }}
    th, td {{ text-align:left; padding:10px 12px; border-bottom:1px solid #e2e8f0; }}
    th {{ background:#f8fafc; color:#475569; font-size:12px; }}
    pre {{ background:#09090b; color:#e4e4e7; padding:16px; border-radius:10px; overflow-x:auto; font-size:12px; line-height:1.5; }}
    .meta {{ display:flex; flex-wrap:wrap; gap:16px; font-size:13px; color:#475569; margin-top:10px; }}
    .badge {{ display:inline-block; background:{verdict_bg}; color:#fff; font-weight:600; font-size:13px; padding:4px 12px; border-radius:999px; }}
    a {{ color:#2563eb; text-decoration:none; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px;">
      <h1>独立 AI Code Review 全量审查报告</h1>
      <span class="badge">{verdict_label}</span>
    </div>
    <div class="meta">
      <span><b>任务单号：</b><code>{esc(state.task_id)}</code></span>
      <span><b>仓库分支：</b><code>{esc(state.contract.target_repo)}</code> → <code>{esc(state.contract.base_branch)}</code></span>
      <span><b>关联 PR：</b>{pr_link}</span>
      <span><b>审查模型：</b><code>{esc(rep.reviewer_model if rep else '-')}</code></span>
      <span><b>校验 Commit：</b><code>{esc((state.head_commit_sha or '')[:10])}</code></span>
      <span><a href="/#task={esc(state.task_id)}">← 返回数字员工工作台</a></span>
    </div>

    <h2>🎯 契约目标与审查结论</h2>
    <p style="margin:6px 0;"><b>任务目标：</b>{esc(state.contract.objective)}</p>
    <p style="margin:6px 0;"><b>审查总结：</b>{esc(rep.summary if rep else '暂无审查记录')}</p>

    <h2>📦 本次改动做了什么 (Change Overview)</h2>
    <ul>{overview_html}</ul>

    {blocking_html}
    {obs_html}
    {checklist_html}
    {risk_html}

    <h2>🔎 审查时的完整代码变更 (Unified Git Diff)</h2>
    <pre>{esc(diff_text or '(无代码变更)')}</pre>
  </div>
</body>
</html>"""


class FocusedReviewer:
    """Separate AI Code Review Agent (cross-model capable, skill-driven, structured full report)."""

    def __init__(
        self,
        reviewer_llm: LLMClient,
        *,
        skill_path: Path | None = None,
        store: DurableStore | None = None,
    ) -> None:
        self.llm = reviewer_llm
        self.skill_prompt = load_pr_review_skill(skill_path)
        self.store = store

    def review(
        self,
        *,
        contract: TaskContract,
        sandbox: WorktreeSandbox,
        evidence: VerificationEvidence,
        full_report_url: str = "",
    ) -> tuple[ReviewerReport, float]:
        changed = sandbox.modified_files(contract.base_commit_sha)
        ledger = (
            DecisionLedger.for_repository(self.store, sandbox.profile)
            if self.store is not None
            else DecisionLedger.from_repo(
                Path(sandbox.profile.local_path), sandbox.profile.decision_ledger_path
            )
        )

        rich_code_context = build_reviewer_code_context(
            sandbox, contract.base_commit_sha, changed
        )
        prompt = "\n\n".join(
            [
                "## Accountable TaskContract\n" + contract.model_dump_json(indent=2),
                ledger.render_truth_hierarchy_block(changed),
                "## Deterministic Sandbox Evidence Summary\n" + evidence.model_dump_json(indent=2),
                rich_code_context,
            ]
        )

        chosen_model = self.llm.default_model
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.skill_prompt},
            {"role": "user", "content": prompt},
        ]
        total_cost = 0.0
        effective_model = chosen_model
        degraded_from = ""
        fallback_reason = ""
        data: dict[str, Any] = {}

        for inspect_round in range(3):
            data, metrics = self.llm.complete_json(messages, model=chosen_model)
            total_cost = round(total_cost + metrics.estimated_cost_usd, 6)
            if metrics.actual_model:
                effective_model = metrics.actual_model
            if metrics.degraded_from:
                degraded_from = metrics.degraded_from
            if metrics.fallback_reason:
                fallback_reason = metrics.fallback_reason

            inspect_reqs = data.get("inspect_requests")
            if (
                inspect_round < 2
                and isinstance(inspect_reqs, list)
                and inspect_reqs
                and "verdict" not in data
            ):
                fetched = execute_reviewer_inspect_requests(sandbox, inspect_reqs)
                messages.append(
                    {"role": "assistant", "content": '{"inspect_requests": "executed"}'}
                )
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "## Requested Read-Only Repository Context\n"
                            + fetched
                            + "\n\nNow output the final review JSON object (`verdict`, `quality_score`, `audit_checklist`, etc.)."
                        ),
                    }
                )
                continue
            break

        raw_blocking = data.get("blocking_issues") or []
        blocking_issues: list[BlockingReviewIssue] = []
        for item in raw_blocking:
            if isinstance(item, dict) and item.get("message"):
                sev = "P0" if str(item.get("severity", "")).upper() == "P0" else "P1"
                raw_line = item.get("line")
                line_num = int(raw_line) if isinstance(raw_line, int) and raw_line > 0 else None
                blocking_issues.append(
                    BlockingReviewIssue(
                        severity=sev,
                        category=str(item.get("category") or "LOGIC_OR_REGRESSION"),
                        path=str(item.get("path") or ""),
                        line=line_num,
                        message=str(item["message"]),
                        suggestion=str(item.get("suggestion") or ""),
                    )
                )

        checklist: list[ReviewAuditItem] = []
        for row in data.get("audit_checklist") or []:
            if isinstance(row, dict) and row.get("dimension"):
                st_raw = str(row.get("status") or "PASS").upper()
                st_val = st_raw if st_raw in ("PASS", "WARN", "FAIL") else "PASS"
                checklist.append(
                    ReviewAuditItem(
                        dimension=str(row["dimension"]),
                        status=st_val,  # type: ignore[arg-type]
                        detail=str(row.get("detail") or ""),
                    )
                )

        observations: list[ReviewObservation] = []
        for obs in data.get("notable_observations") or []:
            if isinstance(obs, dict) and obs.get("note"):
                raw_ln = obs.get("line")
                ln_val = int(raw_ln) if isinstance(raw_ln, int) and raw_ln > 0 else None
                observations.append(
                    ReviewObservation(
                        path=str(obs.get("path") or ""),
                        line=ln_val,
                        note=str(obs["note"]),
                    )
                )

        overview = [
            str(x).strip()
            for x in (data.get("change_overview") or [])
            if str(x).strip()
        ]

        verdict_raw = str(data.get("verdict") or ("BLOCK" if blocking_issues else "APPROVE")).upper()
        verdict = "BLOCK" if (verdict_raw == "BLOCK" or len(blocking_issues) > 0) else "APPROVE"

        ac_covered = bool(data.get("ac_covered", verdict == "APPROVE"))
        ledger_ok = bool(data.get("decision_ledger_compliant", True))
        if not ac_covered or not ledger_ok:
            verdict = "BLOCK"

        summary = str(data.get("concise_summary") or data.get("summary") or "").strip()
        residual = str(data.get("residual_risk_note") or "").strip()
        risks = [residual] if residual else [
            str(x) for x in (data.get("unverified_risks") or []) if str(x).strip()
        ]

        raw_score = data.get("quality_score")
        parsed_score: int | None = None
        if isinstance(raw_score, (int, float)) and not isinstance(raw_score, bool):
            parsed_score = int(raw_score)

        report = ReviewerReport(
            verdict=verdict,
            quality_score=parsed_score,
            reviewer_model=effective_model,
            degraded_from_model=degraded_from,
            fallback_reason=fallback_reason,
            skill_used="pr-code-review",
            ac_covered=ac_covered,
            ac_assessment=[str(x) for x in (data.get("ac_assessment") or [])],
            decision_ledger_compliant=ledger_ok,
            ledger_violations=[str(x) for x in (data.get("ledger_violations") or [])],
            change_overview=overview,
            audit_checklist=checklist,
            blocking_issues=blocking_issues,
            notable_observations=observations,
            unverified_risks=risks,
            summary=summary or "已完成契约与边界核查。",
            full_report_url=full_report_url,
        )
        return report, total_cost
