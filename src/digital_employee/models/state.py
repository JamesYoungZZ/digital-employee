"""Durable task state, verification evidence, reviewer report & handoff models (Spec v2.0 Section 3.2 & 3.3)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

from digital_employee.models.contract import TaskContract

FailureClass = Literal[
    "NONE",
    "TRANSIENT_INFRA",
    "SANDBOX_ENV_BROKEN",
    "REGRESSION_TEST_FAILED",
    "REQUIREMENT_AMBIGUITY",
    "CONCURRENT_CONFLICT",
    "BUDGET_OR_LOOP_EXCEEDED",
    "SECURITY_OR_SCOPE_VIOLATION",
]

LifecycleStatus = Literal[
    "DRAFTING_CONTRACT",
    "WAITING_CLARIFICATION",
    "QUEUED",
    "RUNNING_IN_SANDBOX",
    "VERIFYING_EVIDENCE",
    "PR_OPEN_FOLLOWING_UP",
    "DELIVERED_LOCAL_READY",
    "HANDED_OFF_INCOMPLETE",
    "ACCEPTED_AND_CLOSED",
    "ABANDONED_OR_REVERTED",
]


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


class BlockingReviewIssue(BaseModel):
    """Severe P0/P1 defect reported by the Independent PR Review Agent."""

    severity: Literal["P0", "P1"] = "P1"
    category: str = "LOGIC_OR_REGRESSION"
    path: str = ""
    line: int | None = None
    message: str


class ReviewerReport(BaseModel):
    """Independent Cross-Model PR Review Agent output (Blocking-only, zero nitpicks)."""

    verdict: Literal["APPROVE", "BLOCK"] = "APPROVE"
    reviewer_model: str = ""
    skill_used: str = "pr-code-review"
    ac_covered: bool = True
    ac_assessment: list[str] = Field(default_factory=list)
    decision_ledger_compliant: bool = True
    ledger_violations: list[str] = Field(default_factory=list)
    blocking_issues: list[BlockingReviewIssue] = Field(default_factory=list)
    side_effect_findings: list[str] = Field(default_factory=list)
    unverified_risks: list[str] = Field(default_factory=list)
    summary: str = ""


class VerificationEvidence(BaseModel):
    """Hard deterministic & Fail-to-Pass verification evidence checklist."""

    verified_commit_sha: str
    repro_test_target: str
    repro_test_failed_on_base: bool
    repro_test_passed_on_head: bool
    existing_test_suite_passed: bool
    tests_weakened_check: bool
    scope_compliance_check: bool = True
    static_analysis_passed: bool = True
    anti_tampering_violations: list[str] = Field(default_factory=list)
    scope_violations: list[str] = Field(default_factory=list)
    unverified_risks: list[str] = Field(default_factory=list)
    reviewer_report: ReviewerReport | None = None
    artifact_log_uri: str = ""

    @property
    def passed_all_hard_gates(self) -> bool:
        reviewer_ok = True
        if self.reviewer_report is not None:
            reviewer_ok = (
                self.reviewer_report.verdict == "APPROVE"
                and len(self.reviewer_report.blocking_issues) == 0
                and self.reviewer_report.ac_covered
                and self.reviewer_report.decision_ledger_compliant
            )
        return (
            self.repro_test_failed_on_base
            and self.repro_test_passed_on_head
            and self.existing_test_suite_passed
            and self.tests_weakened_check
            and self.scope_compliance_check
            and self.static_analysis_passed
            and reviewer_ok
        )


class HandoffBundle(BaseModel):
    """Structured handoff package when a task is cancelled, blocked, or exceeds budget/retries."""

    reason: str
    failure_class: FailureClass
    root_cause_clues: list[str] = Field(default_factory=list)
    attempted_branch: str = ""
    worktree_path: str = ""
    modified_files: list[str] = Field(default_factory=list)
    failing_test_summary: str = ""
    recommended_next_step: str = ""


class DurableTaskState(BaseModel):
    """Persistent task state stored in SQLite with externalized artifact URIs."""

    # 1. Tenant & identity isolation
    tenant_id: str = "personal"
    workspace_id: str = "jamesyang-mac"
    task_id: str
    trace_id: str
    raw_request: str
    source_channel: str = "cli"  # cli | web | feishu | feishu_ws
    feishu_chat_id: str = ""

    # 2. Contract & lifecycle
    contract: TaskContract
    lifecycle_status: LifecycleStatus = "DRAFTING_CONTRACT"
    auto_publish_pr: bool = False
    auto_merge: bool = True

    # 3. Idempotency & external side-effect ledger
    recorded_operations: dict[str, str] = Field(default_factory=dict)

    # 4. Artifact references & evidence
    branch_name: str = ""
    worktree_path: str = ""
    head_commit_sha: str | None = None
    patch_artifact_uri: str | None = None
    verification_evidence: VerificationEvidence | None = None
    handoff_bundle: HandoffBundle | None = None
    deliverable_pr_url: str | None = None
    deliverable_pr_number: int | None = None
    pr_review_comment_url: str | None = None
    merged_commit_sha: str | None = None

    # 5. Failure classification & economics/time accounting
    failure_class: FailureClass = "NONE"
    failure_reason: str | None = None
    sandbox_attempt_count: int = 0
    total_cost_usd: float = 0.0
    agent_elapsed_seconds: float = 0.0
    human_interaction_seconds: int = 0
    human_review_seconds: int = 0
    baseline_human_minutes: float = 35.0
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)

    @property
    def estimated_net_minutes_saved(self) -> float:
        """Net Time Saved = Baseline - (Clarification + Supervision/Review)."""
        human_spent_minutes = (self.human_interaction_seconds + self.human_review_seconds) / 60.0
        return round(self.baseline_human_minutes - human_spent_minutes, 2)
