"""External platform integrations."""

from digital_employee.integrations.feishu_cards import (
    FeishuNotifier,
    build_clarification_card,
    build_evidence_report_card,
    build_handoff_card,
)
from digital_employee.integrations.github_client import (
    GitHubIntegration,
    render_evidence_pr_body,
)

__all__ = [
    "FeishuNotifier",
    "GitHubIntegration",
    "build_clarification_card",
    "build_evidence_report_card",
    "build_handoff_card",
    "render_evidence_pr_body",
]
