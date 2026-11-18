"""End-to-end deterministic tests for Anti-Tampering, Fail-to-Pass EvidenceGate & Web API."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from digital_employee.models.contract import RepositoryProfile, TaskContract
from digital_employee.sandbox.worktree_runner import WorktreeSandbox
from digital_employee.settings import Settings
from digital_employee.verification.evidence_gate import (
    EvidenceGate,
    scan_test_diff_for_tampering,
)
from digital_employee.web.app import create_app


def test_anti_tampering_scanner_catches_weakened_tests() -> None:
    bad_skip_diff = """
--- a/tests/test_calc.py
+++ b/tests/test_calc.py
@@ -1,4 +1,5 @@
+@pytest.mark.skip(reason="flaky")
 def test_discount():
     assert calc(100) == 90
"""
    assert scan_test_diff_for_tampering(bad_skip_diff)

    deleted_assert_diff = """
--- a/tests/test_calc.py
+++ b/tests/test_calc.py
@@ -1,4 +1,3 @@
 def test_discount():
     res = calc(100)
-    assert res == 90
"""
    assert scan_test_diff_for_tampering(deleted_assert_diff)

    good_diff = """
--- a/tests/test_calc.py
+++ b/tests/test_calc.py
@@ -1,3 +1,5 @@
 def test_discount():
     assert calc(100) == 90
+def test_zero_amount():
+    assert calc(0) == 0
"""
    assert scan_test_diff_for_tampering(good_diff) == []


def _init_mini_buggy_repo(repo_dir: Path) -> str:
    repo_dir.mkdir(parents=True)
    (repo_dir / "src").mkdir()
    (repo_dir / "tests").mkdir()
    # Planted bug: adds instead of subtracts discount
    (repo_dir / "src" / "pricing.py").write_text(
        "def apply_discount(price: int, discount: int) -> int:\n"
        "    return price + discount\n",
        encoding="utf-8",
    )
    (repo_dir / "tests" / "test_pricing.py").write_text(
        "from src.pricing import apply_discount\n\n"
        "def test_zero_discount():\n"
        "    assert apply_discount(100, 0) == 100\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "initial buggy commit",
        ],
        cwd=str(repo_dir),
        check=True,
        capture_output=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo_dir),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_evidence_gate_enforces_true_fail_to_pass(tmp_path: Path) -> None:
    repo_dir = tmp_path / "mini-repo"
    base_sha = _init_mini_buggy_repo(repo_dir)

    profile = RepositoryProfile(
        repo_id="mini-repo",
        github_repo="acme/mini-repo",
        local_path=str(repo_dir),
        default_base_branch="main",
        python_bin=sys.executable,
        pytest_cmd=[sys.executable, "-m", "pytest"],
        default_suite_targets=["tests/test_pricing.py"],
    )
    contract = TaskContract(
        objective="Fix apply_discount to subtract discount instead of adding it",
        target_repo="mini-repo",
        base_branch="main",
        base_commit_sha=base_sha,
        reproduction_evidence="apply_discount(100, 15) returns 115 instead of 85",
        permitted_scopes=["src", "tests"],
    )
    sandbox = WorktreeSandbox(
        profile, tmp_path / "worktrees", "t-test-f2p", permitted_scopes=contract.permitted_scopes
    )
    sandbox.provision(base_sha)

    # Fix production code AND add a true regression test
    sandbox.write_or_edit_file(
        "src/pricing.py",
        new_content=(
            "def apply_discount(price: int, discount: int) -> int:\n"
            "    return price - discount\n"
        ),
    )
    sandbox.write_or_edit_file(
        "tests/test_pricing.py",
        new_content=(
            "from src.pricing import apply_discount\n\n"
            "def test_zero_discount():\n"
            "    assert apply_discount(100, 0) == 100\n\n"
            "def test_nonzero_discount_repro():\n"
            "    assert apply_discount(100, 15) == 85\n"
        ),
    )
    head_sha = sandbox.commit_all("fix: subtract discount", base_sha)

    gate = EvidenceGate(sandbox, contract)

    # Case A: Pointing to `test_zero_discount` (already passed on baseline!) -> MUST FAIL Fail-to-Pass!
    false_proof = gate.evaluate(head_sha, "tests/test_pricing.py::test_zero_discount")
    assert false_proof.evidence.repro_test_passed_on_head is True
    assert false_proof.evidence.repro_test_failed_on_base is False
    assert false_proof.evidence.passed_all_hard_gates is False

    # Case B: Pointing to `test_nonzero_discount_repro` -> FAILS on baseline (115 != 85) and PASSES on head!
    true_proof = gate.evaluate(head_sha, "tests/test_pricing.py::test_nonzero_discount_repro")
    assert true_proof.evidence.repro_test_failed_on_base is True
    assert true_proof.evidence.repro_test_passed_on_head is True
    assert true_proof.evidence.existing_test_suite_passed is True
    assert true_proof.evidence.tests_weakened_check is True
    assert true_proof.evidence.passed_all_hard_gates is True


def test_web_control_plane_endpoints_and_feishu_challenge(tmp_path: Path) -> None:
    cfg = Settings(
        data_dir=tmp_path / "data",
        llm_api_key="dummy",
    )
    cfg.ensure_dirs()
    client = TestClient(create_app(settings=cfg, enable_background_daemons=False))

    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    dash = client.get("/")
    assert dash.status_code == 200
    assert "AI 研发数字员工控制台" in dash.text

    import re

    asset_match = re.search(r'src="(/static/assets/[^"]+\.js)"', dash.text)
    assert asset_match is not None
    js_asset = client.get(asset_match.group(1))
    assert js_asset.status_code == 200

    challenge = client.post("/webhooks/feishu", json={"challenge": "lark-verify-xyz"})
    assert challenge.status_code == 200
    assert challenge.json() == {"challenge": "lark-verify-xyz"}

    sim = client.post(
        "/api/integrations/feishu/simulate",
        json={
            "raw_text": "@_user_1 #t-20261116-1886fe 补充空数组单测",
            "dispatch_real_task": False,
        },
    )
    assert sim.status_code == 200
    parsed = sim.json()["parsed_intent"]
    assert parsed["intent"] == "followup"
    assert parsed["task_id"] == "t-20261116-1886fe"
    assert "补充空数组单测" in parsed["comment"]


def test_sandbox_preserves_crlf_line_endings_for_surgical_diffs(tmp_path: Path) -> None:
    repo_dir = tmp_path / "crlf-repo"
    repo_dir.mkdir()
    (repo_dir / "src").mkdir()
    target = repo_dir / "src" / "demo.py"
    with target.open("wb") as fh:
        fh.write(b"def a():\r\n    return 1\r\n\r\ndef b():\r\n    return 2\r\n")
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@e.com", "commit", "-m", "init"],
        cwd=str(repo_dir),
        check=True,
        capture_output=True,
    )
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo_dir), check=True, capture_output=True, text=True
    ).stdout.strip()

    profile = RepositoryProfile(
        repo_id="crlf-repo",
        github_repo="acme/crlf",
        local_path=str(repo_dir),
    )
    sb = WorktreeSandbox(profile, tmp_path / "wt", "t-crlf", permitted_scopes=["src"])
    sb.provision(base_sha)
    sb.write_or_edit_file(
        "src/demo.py",
        old_snippet="def b():\n    return 2",
        replacement_snippet="def b():\n    return 20",
    )
    raw = (sb.worktree_path / "src" / "demo.py").read_bytes()
    assert b"\r\n" in raw
    diff = sb.git_diff(base_sha)
    assert "def a():" not in [line[1:].strip() for line in diff.splitlines() if line.startswith(("-", "+")) and not line.startswith(("---", "+++"))]


def test_github_poller_reconciles_comments_idempotently_and_merge_state(tmp_path: Path) -> None:
    from unittest.mock import MagicMock

    from digital_employee.integrations.github_poller import GitHubFollowThroughPoller
    from digital_employee.models.state import DurableTaskState
    from digital_employee.orchestrator import DelegationOrchestrator

    repo_cfg = tmp_path / "repos.yaml"
    repo_cfg.write_text(
        "repositories:\n"
        "  demo:\n"
        "    github_repo: acme/demo\n"
        "    local_path: /tmp/demo\n",
        encoding="utf-8",
    )
    cfg = Settings(
        data_dir=tmp_path / "data",
        repo_registry_path=repo_cfg,
        llm_api_key="dummy",
        github_token="ghp_dummy_token_for_test_only_12345",
    )
    cfg.ensure_dirs()
    orch = DelegationOrchestrator(cfg)
    state = DurableTaskState(
        task_id="t-poll-1",
        trace_id="tr-1",
        raw_request="fix demo",
        contract=TaskContract(
            objective="fix demo",
            target_repo="demo",
            base_branch="dev",
            base_commit_sha="11111111",
            reproduction_evidence="err",
            permitted_scopes=["src", "tests"],
        ),
        lifecycle_status="PR_OPEN_FOLLOWING_UP",
        deliverable_pr_number=7,
        deliverable_pr_url="https://github.com/acme/demo/pull/7",
    )
    orch.store.save_task(state)

    orch.github.fetch_pr_state = MagicMock(return_value={"state": "open", "merged": False, "head": {"sha": "abcdef1234"}})  # type: ignore[method-assign]
    orch.github.fetch_human_pr_comments = MagicMock(  # type: ignore[method-assign]
        return_value=[{"id": "issue_comment_99", "body": "Add edge case assertion", "created_at": "2026-11-16T03:00:00Z", "user": "alice"}]
    )
    orch.github.fetch_failed_blocking_checks = MagicMock(return_value=[])  # type: ignore[method-assign]
    orch.notify_review_fix_needed = MagicMock(return_value="ok")  # type: ignore[method-assign]

    poller = GitHubFollowThroughPoller(orch)
    assert poller.poll_once() == 1
    assert orch.notify_review_fix_needed.call_count == 1

    # Second poll on identical comment must be skipped via recorded_operations idempotency
    assert poller.poll_once() == 0
    assert orch.notify_review_fix_needed.call_count == 1

    # When PR is merged on GitHub, poller transitions task to ACCEPTED_AND_CLOSED
    orch.github.fetch_pr_state = MagicMock(return_value={"state": "closed", "merged": True})  # type: ignore[method-assign]
    assert poller.poll_once() == 1
    updated = orch.store.get_task("t-poll-1")
    assert updated is not None and updated.lifecycle_status == "ACCEPTED_AND_CLOSED"


def test_five_gate_auto_merge_and_blocking_pr_review_skill(tmp_path: Path) -> None:
    from unittest.mock import MagicMock

    from digital_employee.agents.reviewer import (
        load_pr_review_skill,
        render_pr_review_agent_comment,
    )
    from digital_employee.models.state import (
        BlockingReviewIssue,
        DurableTaskState,
        ReviewerReport,
        VerificationEvidence,
    )
    from digital_employee.orchestrator import DelegationOrchestrator

    skill_md = load_pr_review_skill()
    assert "pr-code-review" in skill_md
    assert "Blocking" in skill_md

    repo_cfg = tmp_path / "repos.yaml"
    repo_cfg.write_text(
        "repositories:\n"
        "  demo:\n"
        f"    github_repo: acme/demo\n"
        f"    local_path: {tmp_path}\n",
        encoding="utf-8",
    )
    cfg = Settings(
        data_dir=tmp_path / "data",
        repo_registry_path=repo_cfg,
        llm_api_key="dummy",
        github_token="ghp_dummy_token_for_test_only_12345",
    )
    cfg.ensure_dirs()
    orch = DelegationOrchestrator(cfg)

    blocked_report = ReviewerReport(
        verdict="BLOCK",
        reviewer_model="claude-sonnet-4-20250514",
        blocking_issues=[
            BlockingReviewIssue(
                severity="P0",
                category="LOGIC_OR_REGRESSION",
                path="src/app.py",
                line=12,
                message="Null dereference on empty input",
            )
        ],
        summary="Blocked due to null dereference.",
    )
    comment_md = render_pr_review_agent_comment(
        blocked_report, verified_commit_sha="aaaa11112222", repro_test_target="tests/test_app.py"
    )
    assert "<!-- ai-pr-review-agent -->" in comment_md
    assert "P0 · LOGIC_OR_REGRESSION" in comment_md

    ev = VerificationEvidence(
        verified_commit_sha="aaaa11112222",
        repro_test_target="tests/test_app.py",
        repro_test_failed_on_base=True,
        repro_test_passed_on_head=True,
        existing_test_suite_passed=True,
        tests_weakened_check=True,
        reviewer_report=blocked_report,
    )
    assert ev.passed_all_hard_gates is False

    state = DurableTaskState(
        task_id="t-merge-1",
        trace_id="tr-m1",
        raw_request="fix app",
        contract=TaskContract(
            objective="fix app",
            target_repo="demo",
            base_branch="dev",
            base_commit_sha="0000",
            reproduction_evidence="err",
            permitted_scopes=["src", "tests"],
        ),
        lifecycle_status="PR_OPEN_FOLLOWING_UP",
        auto_merge=False,
        deliverable_pr_number=9,
        deliverable_pr_url="https://github.com/acme/demo/pull/9",
        head_commit_sha="aaaa11112222",
        verification_evidence=ev,
    )
    orch.store.save_task(state)

    # Gate 2 blocks approval card when PR Review Agent verdict is BLOCK
    res_blocked = orch.check_and_emit_merge_approval("t-merge-1")
    assert res_blocked["ready"] is False
    assert res_blocked["merged"] is False

    # Upgrade report to APPROVE (0 blocking issues)
    state.verification_evidence.reviewer_report = ReviewerReport(
        verdict="APPROVE",
        reviewer_model="claude-sonnet-4-20250514",
        blocking_issues=[],
        summary="Clean surgical fix.",
    )
    orch.store.save_task(state)

    orch.github.fetch_pr_state = MagicMock(  # type: ignore[method-assign]
        return_value={"state": "open", "draft": False, "merged": False, "mergeable": True, "head": {"sha": "aaaa11112222"}}
    )
    # Gate 3 waits while GitHub Actions CI is pending
    orch.github.inspect_commit_ci_status = MagicMock(  # type: ignore[method-assign]
        return_value={"state": "pending", "failed_checks": [], "total": 2}
    )
    assert orch.check_and_emit_merge_approval("t-merge-1")["ready"] is False

    # Once CI turns green, emits MergeApprovalCard & enters WAITING_MERGE_APPROVAL (does NOT auto-merge!)
    orch.github.inspect_commit_ci_status = MagicMock(  # type: ignore[method-assign]
        return_value={"state": "passed", "failed_checks": [], "total": 2}
    )
    orch.github.merge_pull_request = MagicMock(return_value={"sha": "merged9999"})  # type: ignore[method-assign]
    orch.github.post_pr_comment = MagicMock(return_value="ok")  # type: ignore[method-assign]

    approval_res = orch.check_and_emit_merge_approval("t-merge-1")
    assert approval_res["ready"] is True
    assert approval_res["merged"] is False
    waiting_state = orch.store.get_task("t-merge-1")
    assert waiting_state is not None
    assert waiting_state.lifecycle_status == "WAITING_MERGE_APPROVAL"
    assert waiting_state.merge_approval_card is not None
    assert orch.github.merge_pull_request.call_count == 0

    # When human clicks Approve on Console or Feishu Card -> Squash Merge & ACCEPTED_AND_CLOSED!
    merged_res = orch.approve_and_merge("t-merge-1", approved_by="feishu_card")
    assert merged_res["merged"] is True
    final_state = orch.store.get_task("t-merge-1")
    assert final_state is not None
    assert final_state.lifecycle_status == "ACCEPTED_AND_CLOSED"
    assert final_state.merged_commit_sha == "merged9999"
    assert final_state.merge_approval_card is not None
    assert final_state.merge_approval_card.approved_by == "feishu_card"


def test_noop_clean_exit_verified_vs_rejected_when_tests_fail(tmp_path: Path) -> None:
    """Verify `finish_noop` closes cleanly ONLY when pytest exits 0 with 0 edits, and rejects lazy claims when tests fail."""
    from unittest.mock import MagicMock

    from digital_employee.agents.runtime import CodingLoopOutcome
    from digital_employee.models.state import DurableTaskState
    from digital_employee.orchestrator import DelegationOrchestrator

    repo_dir = tmp_path / "green_repo"
    base_sha = _init_mini_buggy_repo(repo_dir)

    repo_cfg = tmp_path / "repos.yaml"
    repo_cfg.write_text(
        "repositories:\n"
        "  green_repo:\n"
        f"    github_repo: acme/green_repo\n"
        f"    local_path: {repo_dir}\n"
        f"    python_bin: {sys.executable}\n"
        f"    pytest_cmd: [{sys.executable}, -m, pytest]\n",
        encoding="utf-8",
    )
    cfg = Settings(
        data_dir=tmp_path / "data",
        repo_registry_path=repo_cfg,
        llm_api_key="dummy",
        max_self_heal_attempts=1,
    )
    cfg.ensure_dirs()
    orch = DelegationOrchestrator(cfg)

    # Case 1: Suite actually passes (`tests/test_pricing.py` passes) -> `finish_noop` accepted & closed!
    state_ok = DurableTaskState(
        task_id="t-noop-ok",
        trace_id="tr-noop-1",
        raw_request="跑一下单测，修复测试",
        contract=TaskContract(
            objective="Run unit tests and fix failures if any",
            target_repo="green_repo",
            base_branch="main",
            base_commit_sha=base_sha,
            reproduction_evidence="none",
            permitted_scopes=["src", "tests"],
        ),
        lifecycle_status="QUEUED",
    )
    orch.store.save_task(state_ok)
    orch.coder.run = MagicMock(  # type: ignore[method-assign]
        return_value=CodingLoopOutcome(
            completed=True,
            is_noop=True,
            noop_test_targets=["tests/test_pricing.py"],
            summary="All unit tests passed (1 passed, 0 failed).",
        )
    )
    done = orch.execute_task("t-noop-ok")
    assert done.lifecycle_status == "ACCEPTED_AND_CLOSED"
    assert done.verification_evidence is not None
    assert done.verification_evidence.noop_verified is True
    assert done.verification_evidence.passed_all_hard_gates is True

    # Case 2: Lazy agent claims `finish_noop` on a non-existent/failing target -> deterministically rejected!
    state_bad = DurableTaskState(
        task_id="t-noop-bad",
        trace_id="tr-noop-2",
        raw_request="跑一下单测，修复测试",
        contract=TaskContract(
            objective="Run unit tests",
            target_repo="green_repo",
            base_branch="main",
            base_commit_sha=base_sha,
            reproduction_evidence="none",
            permitted_scopes=["src", "tests"],
        ),
        lifecycle_status="QUEUED",
    )
    orch.store.save_task(state_bad)
    orch.coder.run = MagicMock(  # type: ignore[method-assign]
        return_value=CodingLoopOutcome(
            completed=True,
            is_noop=True,
            noop_test_targets=["tests/non_existent_failing_suite.py"],
            summary="Pretending tests passed.",
        )
    )
    rejected = orch.execute_task("t-noop-bad")
    assert rejected.lifecycle_status == "HANDED_OFF_INCOMPLETE"


def test_github_ci_status_fine_grained_pat_actions_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify `inspect_commit_ci_status` falls back to `/actions/runs` + `/jobs` when `/check-runs` returns 403."""
    import httpx

    from digital_employee.integrations.github_client import GitHubIntegration

    def _mock_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/check-runs"):
            return httpx.Response(
                403,
                headers={"x-accepted-github-permissions": "checks=read"},
                json={"message": "Resource not accessible by personal access token"},
            )
        if path.endswith("/actions/runs"):
            sha = request.url.params.get("head_sha")
            if sha == "sha_green":
                return httpx.Response(
                    200,
                    json={
                        "workflow_runs": [
                            {
                                "id": 102,
                                "workflow_id": 1,
                                "name": "CI",
                                "event": "pull_request",
                                "run_number": 5,
                                "run_attempt": 2,
                                "status": "completed",
                                "conclusion": "success",
                            },
                            {
                                "id": 101,
                                "workflow_id": 1,
                                "name": "CI",
                                "event": "pull_request",
                                "run_number": 5,
                                "run_attempt": 1,
                                "status": "completed",
                                "conclusion": "failure",
                            },
                        ]
                    },
                )
            if sha == "sha_failed":
                return httpx.Response(
                    200,
                    json={
                        "workflow_runs": [
                            {
                                "id": 201,
                                "workflow_id": 1,
                                "name": "CI",
                                "event": "pull_request",
                                "run_number": 6,
                                "run_attempt": 1,
                                "status": "completed",
                                "conclusion": "failure",
                            }
                        ]
                    },
                )
            return httpx.Response(200, json={"workflow_runs": []})
        if path.endswith("/actions/runs/102/jobs"):
            return httpx.Response(
                200,
                json={
                    "jobs": [
                        {"name": "offline-integrity", "status": "completed", "conclusion": "success"},
                        {"name": "backing-services", "status": "completed", "conclusion": "success"},
                    ]
                },
            )
        if path.endswith("/actions/runs/201/jobs"):
            return httpx.Response(
                200,
                json={
                    "jobs": [
                        {"name": "offline-integrity", "status": "completed", "conclusion": "failure"},
                        {"name": "backing-services", "status": "completed", "conclusion": "success"},
                    ]
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(_mock_handler)
    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda *args, **kwargs: real_client(*args, transport=transport, **kwargs),
    )

    gh = GitHubIntegration("github_pat_test")
    green = gh.inspect_commit_ci_status(github_repo="acme/demo", commit_sha="sha_green")
    assert green == {"state": "passed", "failed_checks": [], "total": 2}

    red = gh.inspect_commit_ci_status(github_repo="acme/demo", commit_sha="sha_failed")
    assert red == {"state": "failed", "failed_checks": ["offline-integrity"], "total": 2}

    empty_expect = gh.inspect_commit_ci_status(
        github_repo="acme/demo", commit_sha="sha_none", expect_workflows=True
    )
    assert empty_expect["state"] == "pending"


def test_reviewer_score_and_feishu_merge_card_formatting() -> None:
    """Verify 3-tier review score calibration and zero-backtick Feishu merge approval card."""
    import json

    from digital_employee.integrations.feishu_cards import build_merge_approval_card
    from digital_employee.models.state import (
        BlockingReviewIssue,
        DurableTaskState,
        MergeApprovalCard,
        ReviewAuditItem,
        ReviewerReport,
        ReviewObservation,
        VerificationEvidence,
    )

    clean_rep = ReviewerReport(
        verdict="APPROVE",
        reviewer_model="gemini-3.8-flash",
        audit_checklist=[ReviewAuditItem(dimension="需求与验收覆盖", status="PASS", detail="ok")],
        summary="Clean fix.",
    )
    assert clean_rep.quality_score is not None and clean_rep.quality_score >= 90
    assert clean_rep.merge_recommendation == "MERGE_READY"
    assert clean_rep.merge_recommendation_label == "可直接合入"

    caution_rep = ReviewerReport(
        verdict="APPROVE",
        reviewer_model="deepseek-flash",
        change_overview=["app/reply_safety.py: 新增 `prohibited_advertising_claim` 极限词告警"],
        audit_checklist=[
            ReviewAuditItem(dimension="需求与验收覆盖", status="PASS", detail="ok"),
            ReviewAuditItem(dimension="业务逻辑与边界条件", status="WARN", detail="否定词可能软误报"),
        ],
        notable_observations=[
            ReviewObservation(path="app/reply_safety.py", line=48, note="裸词误报"),
            ReviewObservation(path="app/reply_safety.py", line=92, note="break 提前退出"),
        ],
        unverified_risks=["单测未覆盖 `无法根治` 否定话术"],
        summary="可合入。",
    )
    assert caution_rep.quality_score == 88
    assert caution_rep.merge_recommendation == "MERGE_WITH_CAUTION"

    block_rep = ReviewerReport(
        verdict="BLOCK",
        quality_score=95,  # Hallucinated high score must be clamped down when BLOCKed!
        blocking_issues=[BlockingReviewIssue(severity="P0", message="Crash on empty input")],
    )
    assert block_rep.quality_score is not None and block_rep.quality_score < 60
    assert block_rep.merge_recommendation == "DO_NOT_MERGE"

    state = DurableTaskState(
        task_id="t-card-1",
        trace_id="tr-c1",
        raw_request="test",
        contract=TaskContract(
            objective="为 `reply_safety` 新增广告极限词告警",
            target_repo="livestream-copilot",
            base_branch="main",
            base_commit_sha="12345678",
            reproduction_evidence="none",
            permitted_scopes=["app"],
        ),
        deliverable_pr_number=1,
        deliverable_pr_url="https://github.com/acme/demo/pull/1",
        verification_evidence=VerificationEvidence(
            verified_commit_sha="7f4726be",
            repro_test_target="tests/test_reply_safety.py",
            repro_test_failed_on_base=True,
            repro_test_passed_on_head=True,
            existing_test_suite_passed=True,
            tests_weakened_check=True,
            unverified_risks=caution_rep.unverified_risks,
            reviewer_report=caution_rep,
        ),
        merge_approval_card=MergeApprovalCard(
            pr_number=1,
            pr_url="https://github.com/acme/demo/pull/1",
            verified_commit_sha="7f4726be",
            ci_checks_count=2,
            quality_score=caution_rep.quality_score or 88,
            merge_recommendation=caution_rep.merge_recommendation or "MERGE_WITH_CAUTION",
            merge_recommendation_label=caution_rep.merge_recommendation_label,
            reviewer_model="deepseek-flash",
            reviewer_verdict="APPROVE",
            reviewer_summary=caution_rep.summary,
        ),
    )
    card = build_merge_approval_card(state)
    card_json = json.dumps(card, ensure_ascii=False)
    assert "88 / 100" in card_json
    assert "可合入（1 项边界留意）" in card_json
    assert "autofix_caution" in card_json
    assert "hold_merge" in card_json
    assert "`" not in card_json


def test_review_flow_five_fixes_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify the 5 PR Review flow fixes: strong fallback, loopback dead-link, inspect loop, hold/caution & PR sync."""
    import json
    from unittest.mock import MagicMock

    import httpx

    from digital_employee.agents.reviewer import FocusedReviewer, render_pr_review_agent_comment
    from digital_employee.integrations.feishu_cards import (
        build_merge_approval_card,
        parse_feishu_command,
    )
    from digital_employee.integrations.github_client import (
        GitHubIntegration,
        infer_conventional_commit_prefix,
    )
    from digital_employee.llm.client import LLMCallMetrics, LLMClient
    from digital_employee.models.state import (
        DurableTaskState,
        ReviewAuditItem,
        ReviewerReport,
        VerificationEvidence,
    )
    from digital_employee.orchestrator import DelegationOrchestrator

    # 1. Conventional commit prefix inference (no longer hardcoded `fix:` for features/refactors)
    assert infer_conventional_commit_prefix("为 reply_safety 新增广告违禁极限词告警") == "feat"
    assert infer_conventional_commit_prefix("重构 reply_safety 正则匹配模块") == "refactor"
    assert infer_conventional_commit_prefix("修复空指针崩溃异常") == "fix"

    # 2. Loopback dead-link suppression on GitHub & mobile Feishu card fallback to GitHub PR comment URL
    rep_fb = ReviewerReport(
        verdict="APPROVE",
        reviewer_model="deepseek-v4-pro",
        degraded_from_model="gemini-3.8-flash-high",
        fallback_reason="Eligibility check failed",
        audit_checklist=[ReviewAuditItem(dimension="业务逻辑与边界条件", status="WARN", detail="裸词边界")],
        summary="逻辑通过。",
    )
    gh_md_local = render_pr_review_agent_comment(
        rep_fb,
        verified_commit_sha="7f4726be44",
        repro_test_target="tests/test_reply.py",
        full_report_url="http://127.0.0.1:8020/reports/t-1/review",
    )
    assert "127.0.0.1" not in gh_md_local
    assert "兜底自 `gemini-3.8-flash-high`" in gh_md_local

    gh_md_public = render_pr_review_agent_comment(
        rep_fb,
        verified_commit_sha="7f4726be44",
        repro_test_target="tests/test_reply.py",
        full_report_url="https://de.example.com/reports/t-1/review",
    )
    assert "https://de.example.com/reports/t-1/review" in gh_md_public

    # 3. FocusedReviewer multi-round read-only `inspect_requests` + prioritized prod diff
    repo_dir = tmp_path / "rev_repo"
    base_sha = _init_mini_buggy_repo(repo_dir)
    profile = RepositoryProfile(
        repo_id="rev_repo",
        github_repo="acme/rev_repo",
        local_path=str(repo_dir),
        default_base_branch="main",
        python_bin=sys.executable,
        pytest_cmd=[sys.executable, "-m", "pytest"],
    )
    sandbox = WorktreeSandbox(profile, tmp_path / "wt", "t-rev-ctx", permitted_scopes=["src", "tests"])
    sandbox.provision(base_sha)
    sandbox.write_or_edit_file(
        "src/pricing.py",
        new_content="def apply_discount(price: float, discount: float) -> float:\n    return round(price - discount, 2)\n",
    )
    head_sha = sandbox.commit_all("fix discount", base_sha)

    fake_rev_llm = MagicMock(spec=LLMClient)
    fake_rev_llm.default_model = "gemini-3.8-flash"
    fake_rev_llm.complete_json.side_effect = [
        (
            {"inspect_requests": [{"type": "read_file", "path": "tests/test_pricing.py", "start_line": 1, "end_line": 30}]},
            LLMCallMetrics(estimated_cost_usd=0.001, actual_model="deepseek-v4-pro", degraded_from="gemini-3.8-flash-high"),
        ),
        (
            {
                "verdict": "APPROVE",
                "quality_score": 94,
                "ac_covered": True,
                "decision_ledger_compliant": True,
                "change_overview": ["src/pricing.py: 修正减法折扣计算"],
                "audit_checklist": [{"dimension": "需求与验收覆盖", "status": "PASS", "detail": "已核对 test_pricing.py 调用方"}],
                "concise_summary": "上下文核对无误。",
            },
            LLMCallMetrics(estimated_cost_usd=0.002, actual_model="deepseek-v4-pro"),
        ),
    ]
    contract = TaskContract(
        objective="新增折扣安全计算",
        target_repo="rev_repo",
        base_branch="main",
        base_commit_sha=base_sha,
        reproduction_evidence="err",
        permitted_scopes=["src", "tests"],
    )
    ev = VerificationEvidence(
        verified_commit_sha=head_sha,
        repro_test_target="tests/test_pricing.py",
        repro_test_failed_on_base=True,
        repro_test_passed_on_head=True,
        existing_test_suite_passed=True,
        tests_weakened_check=True,
    )
    reviewer = FocusedReviewer(fake_rev_llm)
    final_rep, cost = reviewer.review(contract=contract, sandbox=sandbox, evidence=ev)
    assert fake_rev_llm.complete_json.call_count == 2
    assert final_rep.quality_score == 94
    assert final_rep.reviewer_model == "deepseek-v4-pro"
    assert final_rep.degraded_from_model == "gemini-3.8-flash-high"
    assert cost == 0.003

    # 4. Existing PR reconciliation PATCHes updated body & title
    patched_bodies: list[dict[str, object]] = []

    def _gh_mock(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path.endswith("/pulls"):
            return httpx.Response(200, json=[{"number": 7, "html_url": "https://github.com/acme/rev_repo/pull/7", "draft": False}])
        if req.method == "PATCH" and req.url.path.endswith("/pulls/7"):
            patched_bodies.append(json.loads(req.content.decode("utf-8")))
            return httpx.Response(200, json={"number": 7})
        return httpx.Response(404)

    transport = httpx.MockTransport(_gh_mock)
    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: real_client(*a, transport=transport, **kw))

    ev.reviewer_report = final_rep
    st_pr = DurableTaskState(
        task_id="t-rev-ctx",
        trace_id="tr-ctx",
        raw_request="test",
        contract=contract,
        branch_name="ai-agent/t-rev-ctx",
        head_commit_sha=head_sha,
        deliverable_pr_number=7,
        deliverable_pr_url="https://github.com/acme/rev_repo/pull/7",
        pr_review_comment_url="https://github.com/acme/rev_repo/pull/7#pullrequestreview-99",
        verification_evidence=ev,
    )
    gh = GitHubIntegration("ghp_test")
    rec = gh.reconcile_or_create_pr(github_repo="acme/rev_repo", state=st_pr, protected_branches=["main"])
    assert rec["reconciled"] is True
    assert len(patched_bodies) == 1
    assert "94/100" in str(patched_bodies[0].get("body", ""))

    # Mobile Feishu card uses GitHub PR review permalink instead of 127.0.0.1!
    m_card = build_merge_approval_card(st_pr, "http://127.0.0.1:8020")
    m_card_str = json.dumps(m_card, ensure_ascii=False)
    assert "pullrequestreview-99" in m_card_str
    assert "127.0.0.1:8020/reports" not in m_card_str

    # 5. `hold_merge` keeps task in WAITING_MERGE_APPROVAL & natural Feishu follow-up commands parse cleanly
    assert parse_feishu_command("修边界", ["rev_repo"]) == {"intent": "quick_autofix_caution"}
    assert parse_feishu_command("追改：把无法根治排除掉", ["rev_repo"]) == {
        "intent": "quick_followup_pr",
        "comment": "把无法根治排除掉",
    }

    repo_cfg = tmp_path / "repos.yaml"
    repo_cfg.write_text(
        f"repositories:\n  rev_repo:\n    repo_id: rev_repo\n    github_repo: acme/rev_repo\n    local_path: {repo_dir}\n",
        encoding="utf-8",
    )
    cfg = Settings(
        data_dir=tmp_path / "data",
        repo_registry_path=repo_cfg,
        llm_api_key="dummy",
        github_token="ghp_test",
        feishu_webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/dummy",
    )
    cfg.ensure_dirs()
    orch = DelegationOrchestrator(cfg)
    st_pr.lifecycle_status = "WAITING_MERGE_APPROVAL"
    orch.store.save_task(st_pr)

    held = orch.update_scope_or_cancel("t-rev-ctx", action="hold_merge")
    assert held.lifecycle_status == "WAITING_MERGE_APPROVAL"
    assert "merge_held_by_human" in held.recorded_operations

    # Feishu merge failure receipt is sent when GitHub Merge API fails
    orch.feishu.send_card = MagicMock(return_value="sent")  # type: ignore[method-assign]
    orch.github.fetch_pr_state = MagicMock(  # type: ignore[method-assign]
        return_value={"state": "open", "draft": False, "merged": False, "mergeable": True, "head": {"sha": head_sha}}
    )
    orch.github.inspect_commit_ci_status = MagicMock(  # type: ignore[method-assign]
        return_value={"state": "passed", "failed_checks": [], "total": 2}
    )
    orch.github.merge_pull_request = MagicMock(side_effect=RuntimeError("405 Protected branch"))  # type: ignore[method-assign]
    fail_merge = orch.approve_and_merge("t-rev-ctx", approved_by="feishu_ws_card")
    assert fail_merge["merged"] is False
    assert orch.feishu.send_card.call_count == 1





