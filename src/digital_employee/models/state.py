"""Durable task state, verification evidence, reviewer report & handoff models (Spec v2.0 Section 3.2 & 3.3)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from digital_employee.models.contract import JiraTicketDraft, TaskContract

MergeRecommendation = Literal["MERGE_READY", "MERGE_WITH_CAUTION", "DO_NOT_MERGE"]

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
    "WAITING_JIRA_APPROVAL",
    "WAITING_CLARIFICATION",
    "QUEUED",
    "RUNNING_IN_SANDBOX",
    "VERIFYING_EVIDENCE",
    "PR_OPEN_FOLLOWING_UP",
    "WAITING_MERGE_APPROVAL",
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
    suggestion: str = ""


class ReviewAuditItem(BaseModel):
    """Single dimension in the full structured PR Code Review audit matrix."""

    dimension: str
    status: Literal["PASS", "WARN", "FAIL"] = "PASS"
    detail: str = ""


class ReviewObservation(BaseModel):
    """Concrete non-blocking review comment worth surfacing to the human engineer."""

    path: str = ""
    line: int | None = None
    note: str


class ReviewerReport(BaseModel):
    """Independent Cross-Model PR Review Agent output (Structured overview + full audit matrix)."""

    verdict: Literal["APPROVE", "BLOCK"] = "APPROVE"
    quality_score: int | None = None
    merge_recommendation: MergeRecommendation | None = None
    merge_recommendation_label: str = ""
    reviewer_model: str = ""
    skill_used: str = "pr-code-review"
    ac_covered: bool = True
    ac_assessment: list[str] = Field(default_factory=list)
    decision_ledger_compliant: bool = True
    ledger_violations: list[str] = Field(default_factory=list)
    change_overview: list[str] = Field(default_factory=list)
    audit_checklist: list[ReviewAuditItem] = Field(default_factory=list)
    blocking_issues: list[BlockingReviewIssue] = Field(default_factory=list)
    notable_observations: list[ReviewObservation] = Field(default_factory=list)
    side_effect_findings: list[str] = Field(default_factory=list)
    unverified_risks: list[str] = Field(default_factory=list)
    summary: str = ""
    full_report_url: str = ""

    @model_validator(mode="after")
    def _calibrate_score_and_recommendation(self) -> ReviewerReport:
        warn_dims = [item for item in self.audit_checklist if item.status == "WARN"]
        fail_dims = [item for item in self.audit_checklist if item.status == "FAIL"]
        obs_cnt = len(self.notable_observations)

        is_blocked = (
            self.verdict == "BLOCK"
            or len(self.blocking_issues) > 0
            or not self.ac_covered
            or not self.decision_ledger_compliant
        )
        if is_blocked:
            p0_cnt = sum(1 for b in self.blocking_issues if b.severity == "P0")
            p1_cnt = sum(1 for b in self.blocking_issues if b.severity != "P0")
            penalty = (
                35 * p0_cnt
                + 22 * p1_cnt
                + (25 if not self.ac_covered else 0)
                + (25 if not self.decision_ledger_compliant else 0)
                + 12 * len(fail_dims)
                + 4 * len(warn_dims)
            )
            det_score = max(10, min(58, 100 - max(42, penalty)))
            self.quality_score = (
                max(10, min(59, int(self.quality_score)))
                if self.quality_score is not None
                else det_score
            )
            self.merge_recommendation = "DO_NOT_MERGE"
            self.merge_recommendation_label = (
                f"禁止合入（{len(self.blocking_issues)} 项阻断需返修）"
                if self.blocking_issues
                else "禁止合入（未达准入门禁）"
            )
            return self

        has_dim_caution = len(warn_dims) > 0 or len(fail_dims) > 0
        if has_dim_caution:
            det_score = max(
                75,
                min(
                    89,
                    100
                    - 8 * len(warn_dims)
                    - 12 * len(fail_dims)
                    - 2 * min(obs_cnt, 3),
                ),
            )
            self.quality_score = (
                max(75, min(89, int(self.quality_score)))
                if self.quality_score is not None
                else det_score
            )
            self.merge_recommendation = "MERGE_WITH_CAUTION"
            caution_cnt = len(warn_dims) + len(fail_dims)
            self.merge_recommendation_label = f"可合入（{caution_cnt} 项边界留意）"
            return self

        det_score = max(
            90,
            min(
                98,
                96 - 2 * min(obs_cnt, 2) - (1 if self.unverified_risks else 0),
            ),
        )
        score = (
            max(75, min(100, int(self.quality_score)))
            if self.quality_score is not None
            else det_score
        )
        self.quality_score = score
        if score >= 90:
            self.merge_recommendation = "MERGE_READY"
            self.merge_recommendation_label = "可直接合入"
        else:
            self.merge_recommendation = "MERGE_WITH_CAUTION"
            self.merge_recommendation_label = "可合入（留意边界提示）"
        return self


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
    noop_verified: bool = False
    noop_summary: str = ""
    anti_tampering_violations: list[str] = Field(default_factory=list)
    scope_violations: list[str] = Field(default_factory=list)
    unverified_risks: list[str] = Field(default_factory=list)
    reviewer_report: ReviewerReport | None = None
    artifact_log_uri: str = ""

    @property
    def passed_all_hard_gates(self) -> bool:
        if self.noop_verified:
            return self.existing_test_suite_passed and self.scope_compliance_check
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


class MergeApprovalCard(BaseModel):
    """Interactive Merge Approval Card emitted once all 4 automated pre-merge gates pass."""

    pr_number: int
    pr_url: str
    verified_commit_sha: str
    ci_checks_count: int = 0
    quality_score: int = 95
    merge_recommendation: str = "MERGE_READY"
    merge_recommendation_label: str = "可直接合入"
    reviewer_model: str = ""
    reviewer_verdict: str = "APPROVE"
    reviewer_summary: str = ""
    repro_test_target: str = ""
    residual_risk: str = ""
    ready_at: str = Field(default_factory=utc_now_iso)
    approved_by: str | None = None
    approved_at: str | None = None


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

    # 2. Contract, authoritative Jira ticket & lifecycle
    contract: TaskContract
    jira_draft: JiraTicketDraft | None = None
    jira_issue_key: str | None = None
    jira_issue_url: str | None = None
    jira_status: str | None = None
    lifecycle_status: LifecycleStatus = "DRAFTING_CONTRACT"
    auto_publish_pr: bool = False
    auto_merge: bool = False

    # 3. Idempotency & external side-effect ledger
    recorded_operations: dict[str, str] = Field(default_factory=dict)

    # 4. Artifact references & evidence
    branch_name: str = ""
    worktree_path: str = ""
    head_commit_sha: str | None = None
    patch_artifact_uri: str | None = None
    verification_evidence: VerificationEvidence | None = None
    merge_approval_card: MergeApprovalCard | None = None
    handoff_bundle: HandoffBundle | None = None
    deliverable_pr_url: str | None = None
    deliverable_pr_number: int | None = None
    pr_review_comment_url: str | None = None
    merged_commit_sha: str | None = None

    # 5. Failure classification & economics/time accounting
    failure_class: FailureClass = "NONE"
    failure_reason: str | None = None
    sandbox_attempt_count: int = 0
    current_step: int = 0
    max_steps: int = 32
    current_activity: str = ""
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
