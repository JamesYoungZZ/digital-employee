"""Independent Cross-Model PR Review Agent powered by `skills/pr-code-review/SKILL.md`."""

from __future__ import annotations

from pathlib import Path

from digital_employee.context.decision_ledger import DecisionLedger
from digital_employee.llm.client import LLMClient
from digital_employee.models.contract import TaskContract
from digital_employee.models.state import (
    BlockingReviewIssue,
    ReviewerReport,
    VerificationEvidence,
)
from digital_employee.sandbox.worktree_runner import WorktreeSandbox
from digital_employee.settings import PROJECT_ROOT

FALLBACK_SKILL_PROMPT = """You are an Independent Principal PR Review Agent.
Strictly suppress style, formatting, naming, or speculative nitpicks.
Report `blocking_issues` ONLY for severe P0/P1 logic bugs, security breaches, resource leaks, test tampering, or DecisionLedger violations.
Return strict JSON with keys: verdict ("APPROVE"|"BLOCK"), ac_covered, decision_ledger_compliant, blocking_issues, concise_summary, residual_risk_note.
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
) -> str:
    """Render a short, high-signal PR Review Agent report comment for GitHub PRs."""
    icon = "✅" if report.verdict == "APPROVE" else "🛑"
    lines = [
        "<!-- ai-pr-review-agent -->",
        f"### {icon} Independent PR Review (`{report.verdict}` · Reviewer: `{report.reviewer_model}` · Skill: `{report.skill_used}`)",
        f"- **Commit Audited**: `{verified_commit_sha[:10]}` | **Regression Proof**: `{repro_test_target}` (`Fail → Pass`)",
        f"- **Verdict**: **{report.verdict}** (`{len(report.blocking_issues)}` blocking issue(s))",
        f"- **Summary**: {report.summary}",
    ]
    if report.blocking_issues:
        lines.append("\n#### 🚨 Blocking Issues (Must Resolve Before Merge)")
        for b in report.blocking_issues:
            loc = f"`{b.path}:{b.line}`" if (b.path and b.line) else (f"`{b.path}`" if b.path else "`diff`")
            lines.append(f"- **[{b.severity} · {b.category}]** {loc} — {b.message}")
    elif report.unverified_risks:
        lines.append(f"- **Residual Boundary Note**: {report.unverified_risks[0]}")

    return "\n".join(lines)


class FocusedReviewer:
    """Separate AI Code Review Agent (cross-model capable, skill-driven, blocking-only)."""

    def __init__(self, reviewer_llm: LLMClient, *, skill_path: Path | None = None) -> None:
        self.llm = reviewer_llm
        self.skill_prompt = load_pr_review_skill(skill_path)

    def review(
        self,
        *,
        contract: TaskContract,
        sandbox: WorktreeSandbox,
        evidence: VerificationEvidence,
    ) -> tuple[ReviewerReport, float]:
        changed = sandbox.modified_files(contract.base_commit_sha)
        diff_text = sandbox.git_diff(contract.base_commit_sha)
        ledger = DecisionLedger.from_repo(
            Path(sandbox.profile.local_path), sandbox.profile.decision_ledger_path
        )

        prompt = "\n\n".join(
            [
                "## Accountable TaskContract\n" + contract.model_dump_json(indent=2),
                ledger.render_truth_hierarchy_block(changed),
                "## Deterministic Sandbox Evidence Summary\n" + evidence.model_dump_json(indent=2),
                f"## Unified Git Diff ({len(changed)} files)\n```diff\n{diff_text[:16000]}\n```",
            ]
        )

        chosen_model = self.llm.default_model
        data, metrics = self.llm.complete_json(
            [
                {"role": "system", "content": self.skill_prompt},
                {"role": "user", "content": prompt},
            ],
            model=chosen_model,
        )

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
                    )
                )

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

        report = ReviewerReport(
            verdict=verdict,
            reviewer_model=chosen_model,
            skill_used="pr-code-review",
            ac_covered=ac_covered,
            ac_assessment=[str(x) for x in (data.get("ac_assessment") or [])],
            decision_ledger_compliant=ledger_ok,
            ledger_violations=[str(x) for x in (data.get("ledger_violations") or [])],
            blocking_issues=blocking_issues,
            unverified_risks=risks,
            summary=summary or "Diff audited against TaskContract and DecisionLedger.",
        )
        return report, metrics.estimated_cost_usd
