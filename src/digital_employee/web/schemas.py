"""Typed request/response DTOs for the Web Control Plane API."""

from __future__ import annotations

from pydantic import BaseModel, Field

from digital_employee.context.decision_ledger import PolicyCategory, PolicyStatus


class CreateTaskPayload(BaseModel):
    raw_request: str
    target_repo: str = ""
    base_branch: str | None = None
    accept_defaults: bool = False
    auto_execute: bool = True
    publish_pr: bool = True
    auto_merge: bool = False
    jira_issue_key: str = ""
    require_jira_approval: bool | None = None


class ApproveJiraPayload(BaseModel):
    target_repo: str = ""
    project_key: str = ""
    issue_type: str = ""
    summary: str = ""
    description: str | None = None
    acceptance_criteria: list[str] | None = None
    priority: str = ""
    labels: list[str] | None = None
    auto_execute: bool = True


class RefineJiraPayload(BaseModel):
    instruction: str


class ClarifyPayload(BaseModel):
    answers: dict[str, str] = Field(default_factory=dict)
    use_defaults: bool = False
    interaction_seconds: int = 5
    auto_execute: bool = True
    publish_pr: bool = True


class ControlPayload(BaseModel):
    action: str  # cancel | update_scope | accept | abandon | publish_pr
    permitted_scopes: list[str] | None = None
    note: str = ""


class FollowupPayload(BaseModel):
    comment_body: str
    publish_pr: bool = True


class PromoteDecisionPayload(BaseModel):
    title: str = ""
    category: PolicyCategory | None = None
    scope: str
    rule: str
    reason: str = ""


class UpsertPolicyPayload(BaseModel):
    title: str = ""
    category: PolicyCategory = "CODE_QUALITY"
    scope: str = "*"
    rule: str
    reason: str = ""
    owner: str = ""
    status: PolicyStatus = "APPROVED_POLICY"


class VerifyPolicyPayload(BaseModel):
    owner: str = ""


class CreateMemoryPayload(BaseModel):
    title: str
    narrative: str
    obs_type: str = "discovery"
    files_modified: list[str] = Field(default_factory=list)


class OnboardRepoPayload(BaseModel):
    source: str
    repo_id: str | None = None
    base_branch: str | None = None
    bootstrap_venv: bool = False


class FeishuConfigPayload(BaseModel):
    webhook_url: str = ""
    app_id: str = ""
    app_secret: str = ""


class FeishuSimulatePayload(BaseModel):
    raw_text: str
    dispatch_real_task: bool = False


class ReviewerConfigPayload(BaseModel):
    provider: str = "gemini_cli"  # gemini_cli | gemini | anthropic | openai_compatible
    model: str = "gemini-3.8-flash"
    api_key: str = ""
    base_url: str = ""


class JiraConfigPayload(BaseModel):
    base_url: str = ""
    email: str = ""
    api_token: str = ""
    default_project: str = ""
    poll_jql: str = 'labels = "ai-delegate" AND statusCategory = "To Do" ORDER BY created DESC'
    poller_enabled: bool = True
