"""Core contract models (Spec v2.0 Section 3.1 & 3.5)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class UnresolvedDecision(BaseModel):
    """A material business or scope ambiguity requiring human micro-confirmation."""

    question: str = Field(..., description="Clear, concise question for the delegator.")
    options: list[str] = Field(default_factory=list, description="Clickable choice options.")
    default_assumption: str = Field(
        ..., description="Default assumption used if delegator clicks 'Continue with defaults'."
    )
    impact_scope: str = Field(..., description="Module or behavior affected by this decision.")


class JiraTicketDraft(BaseModel):
    """Structured Jira issue draft requiring human review & approval before creation."""

    project_key: str = "DE"
    issue_type: str = "Task"  # Bug | Task | Story
    summary: str
    description: str
    acceptance_criteria: list[str] = Field(default_factory=list)
    priority: str = "Medium"  # Highest | High | Medium | Low
    labels: list[str] = Field(default_factory=list)
    approved_by: str | None = None
    approved_at: str | None = None


class TaskContract(BaseModel):
    """First-class accountable delegation contract anchored to an authoritative Jira ticket."""

    contract_version: int = 1
    jira_issue_key: str = ""
    jira_issue_url: str = ""
    objective: str
    target_repo: str
    base_branch: str
    base_commit_sha: str
    reproduction_evidence: str
    target_repro_test: str = Field(
        default="",
        description="Pytest node id or test file path proving Fail-to-Pass regression.",
    )
    verification_mode: Literal["fail_to_pass", "regression_and_build"] = Field(
        default="fail_to_pass",
        description="`fail_to_pass` for bugs/logic features; `regression_and_build` for pure UI/refactor/docs/config.",
    )
    acceptance_criteria: list[str] = Field(default_factory=list)
    permitted_scopes: list[str] = Field(
        default_factory=list,
        description="Glob/prefix whitelist of directories or files allowed to be modified.",
    )
    out_of_scope: list[str] = Field(
        default_factory=list,
        description="Explicit boundaries that must not be touched.",
    )
    unresolved_decisions: list[UnresolvedDecision] = Field(default_factory=list)
    resolved_decisions: dict[str, str] = Field(
        default_factory=dict,
        description="Human answers keyed by question text or index.",
    )
    max_budget_usd: float = 1.50
    owner_id: str = "jamesyang"


class RepositoryProfile(BaseModel):
    """Target repository sandbox & verification profile."""

    repo_id: str
    github_repo: str
    local_path: str
    default_base_branch: str = "main"
    default_jira_project: str = ""
    python_bin: str = "python3"
    pytest_cmd: list[str] = Field(default_factory=lambda: ["pytest"])
    test_runner_cmd: list[str] = Field(default_factory=list)
    lint_cmd: list[str] = Field(default_factory=list)
    default_suite_targets: list[str] = Field(default_factory=list)
    decision_ledger_path: str = ".ai/DECISIONS.yaml"
    protected_branches: list[str] = Field(
        default_factory=lambda: ["main", "master", "release"]
    )
    forbidden_paths: list[str] = Field(
        default_factory=lambda: [".github/workflows/", ".env", ".ai/DECISIONS.yaml"]
    )
    baseline_human_minutes: float = 35.0

    @property
    def test_cmd(self) -> list[str]:
        return self.test_runner_cmd if self.test_runner_cmd else self.pytest_cmd
