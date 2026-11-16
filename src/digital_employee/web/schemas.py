"""Typed request/response DTOs for the Web Control Plane API."""

from __future__ import annotations

from pydantic import BaseModel, Field


class CreateTaskPayload(BaseModel):
    raw_request: str
    target_repo: str = "modular-rag"
    base_branch: str | None = None
    accept_defaults: bool = False
    auto_execute: bool = True
    publish_pr: bool = True


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
    scope: str
    rule: str
    reason: str = ""


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
