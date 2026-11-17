---
name: ops-incident-triage
description: 7x24 On-Call Incident Triage & Root-Cause Diagnosis workflow for correlating runtime alerts, error logs, and recent commits before choosing between No-Op diagnosis or surgical Fail-to-Pass repair.
---

# 7×24 Ops Incident Triage & Automated Remediation Skill

Activate this skill whenever handling production/staging alerts, runtime stack traces, health-check degradations, or on-call investigation requests.

## Mandatory 4-Step Triage Protocol

1. **Correlate Telemetry & Recent Changes (Read-Only First)**
   - Inspect the alert payload, stack trace, or error log snippet carefully.
   - If observability MCP tools are available (`inspect_recent_commits`, `search_error_logs`, `probe_service_health`), invoke them (via `ToolSearch` + `mcp_call` when deferred) or run read-only `Grep` / `ReadFile` to map stack frames to exact repository lines.
   - Check `.ai/DECISIONS.yaml` policies before assuming behavior is a bug.

2. **Classify Fault Category**
   - **Category A — Code Regression / Logic Defect**: Reproducible in repository code/tests within `permitted_scopes`.
   - **Category B — Transient External / Config / Already Resolved**: Existing unit/integration test suite passes 100% on `base_commit_sha`, or root cause lies in external network/upstream infra outside repository code.

3. **Execute Deterministically**
   - **For Category A (Code Defect)**:
     1. Write a minimal reproduction test first that fails on the current unpatched code.
     2. Apply the smallest surgical fix strictly within `permitted_scopes`.
     3. Verify both the reproduction test and related test suite pass, then invoke `finish_task` with the exact `repro_test_target`.
   - **For Category B (Non-Code / Already Green)**:
     1. Verify relevant repository tests pass with zero file modifications.
     2. Invoke `finish_noop` with a structured Ops Root-Cause Diagnosis summary (root cause, evidence, and recommended operational action).
