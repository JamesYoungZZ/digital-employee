"""Focused Single Reviewer Agent (Spec v2.0 Section 3.3.3 — Evidence & Risk Disclosure, NO numeric score)."""

from __future__ import annotations

from pathlib import Path

from digital_employee.context.decision_ledger import DecisionLedger
from digital_employee.llm.client import LLMClient
from digital_employee.models.contract import TaskContract
from digital_employee.models.state import ReviewerReport, VerificationEvidence
from digital_employee.sandbox.worktree_runner import WorktreeSandbox

REVIEWER_SYSTEM_PROMPT = """You are the Focused Single Reviewer of a Dependable Engineering Delegation System.
Do NOT output arbitrary numeric scores and do NOT nitpick code formatting/style.

Answer ONLY these three questions based on the TaskContract, DecisionLedger, Git Diff, and deterministic test evidence:
1. `ac_covered`: Does the code change genuinely satisfy every `acceptance_criteria` item without papering over the root cause?
2. `decision_ledger_compliant`: Does the diff obey all `APPROVED_POLICY` rules in the Organization Decision Ledger?
3. `unverified_risks`: Which real-world behaviors, external integrations, concurrency edges, or environment assumptions were NOT exercised by the sandbox unit tests and must be transparently disclosed to the human reviewer?

Return strict JSON:
{
  "ac_covered": true,
  "ac_assessment": ["AC 1 verified by ...", "AC 2 verified by ..."],
  "decision_ledger_compliant": true,
  "ledger_violations": [],
  "side_effect_findings": [],
  "unverified_risks": ["Sandbox only tested local JSONL fixture; concurrent file writes under multi-process load were not tested."],
  "summary": "Concise 2-sentence engineering trust summary."
}
"""


class FocusedReviewer:
    """Single high-signal reviewer checking AC alignment, DecisionLedger rules, and unverified risks."""

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

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
                "## TaskContract\n" + contract.model_dump_json(indent=2),
                ledger.render_truth_hierarchy_block(changed),
                "## Deterministic Evidence Gate Summary\n" + evidence.model_dump_json(indent=2),
                f"## Unified Git Diff ({len(changed)} files)\n```diff\n{diff_text[:14000]}\n```",
            ]
        )

        data, metrics = self.llm.complete_json(
            [
                {"role": "system", "content": REVIEWER_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            model=self.llm.settings.reasoning_model,
        )
        report = ReviewerReport(
            ac_covered=bool(data.get("ac_covered", False)),
            ac_assessment=[str(x) for x in (data.get("ac_assessment") or [])],
            decision_ledger_compliant=bool(data.get("decision_ledger_compliant", True)),
            ledger_violations=[str(x) for x in (data.get("ledger_violations") or [])],
            side_effect_findings=[str(x) for x in (data.get("side_effect_findings") or [])],
            unverified_risks=[
                str(x)
                for x in (
                    data.get("unverified_risks")
                    or ["End-to-end production environment traffic not exercised in local sandbox."]
                )
            ],
            summary=str(data.get("summary") or ""),
        )
        return report, metrics.estimated_cost_usd
