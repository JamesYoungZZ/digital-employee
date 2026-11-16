"""End-to-end deterministic tests for Anti-Tampering, Fail-to-Pass EvidenceGate & Web API."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

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

    js_asset = client.get("/static/app.js")
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
    orch.handle_pr_review_comment = MagicMock(return_value=state)  # type: ignore[method-assign]

    poller = GitHubFollowThroughPoller(orch)
    assert poller.poll_once() == 1
    assert orch.handle_pr_review_comment.call_count == 1

    # Second poll on identical comment must be skipped via recorded_operations idempotency
    assert poller.poll_once() == 0
    assert orch.handle_pr_review_comment.call_count == 1

    # When PR is merged on GitHub, poller transitions task to ACCEPTED_AND_CLOSED
    orch.github.fetch_pr_state = MagicMock(return_value={"state": "closed", "merged": True})  # type: ignore[method-assign]
    assert poller.poll_once() == 1
    updated = orch.store.get_task("t-poll-1")
    assert updated is not None and updated.lifecycle_status == "ACCEPTED_AND_CLOSED"

