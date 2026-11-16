"""Tests for SQLite DurableStore checkpoints, idempotency ledger & DecisionLedger."""

from __future__ import annotations

from pathlib import Path

from digital_employee.context.decision_ledger import DecisionLedger
from digital_employee.models.contract import TaskContract
from digital_employee.models.state import DurableTaskState
from digital_employee.storage.durable_store import DurableStore


def test_durable_store_checkpoints_and_idempotency(tmp_path: Path) -> None:
    store = DurableStore(tmp_path / "state.db", tmp_path / "artifacts")
    contract = TaskContract(
        objective="Fix stage timing bug",
        target_repo="modular-rag",
        base_branch="dev",
        base_commit_sha="deadbeef1234",
        reproduction_evidence="KeyError in get_stage_timings",
        permitted_scopes=["src/observability", "tests/unit"],
    )
    state = DurableTaskState(
        task_id="t-20261116-000001",
        trace_id="trace-1",
        raw_request="Fix stage timing bug",
        contract=contract,
        lifecycle_status="QUEUED",
    )
    store.save_task(state, step_name="queued", note="initial")

    calls = 0

    def external_action() -> str:
        nonlocal calls
        calls += 1
        return "pr_number=42"

    res1, reused1 = store.run_idempotent(state, "create_draft_pr", external_action)
    res2, reused2 = store.run_idempotent(state, "create_draft_pr", external_action)

    assert res1 == "pr_number=42"
    assert reused1 is False
    assert res2 == "pr_number=42"
    assert reused2 is True
    assert calls == 1

    reloaded = store.get_task(state.task_id)
    assert reloaded is not None
    assert reloaded.recorded_operations["create_draft_pr"] == "pr_number=42"
    assert len(store.list_checkpoints(state.task_id)) >= 2

    uri = store.write_artifact(state.task_id, "sample.log", "token=sk-1234567890abcdefghijklmnop")
    saved_text = Path(uri.removeprefix("file://")).read_text(encoding="utf-8")
    assert "sk-1234567890abcdefghijklmnop" not in saved_text


def test_decision_ledger_filters_workarounds(tmp_path: Path) -> None:
    ai_dir = tmp_path / ".ai"
    ai_dir.mkdir()
    (ai_dir / "DECISIONS.yaml").write_text(
        """
decisions:
  - id: DEC-1
    scope: "src/order/*"
    rule: "Use async outbox for refunds"
    reason: "Prevent connection pool starvation"
    status: APPROVED_POLICY
    owner: "alice"
  - id: DEC-2
    scope: "src/order/*"
    rule: "Temporary sleep(1) hack"
    reason: "Hotfix"
    status: ONE_OFF_WORKAROUND
    owner: "bob"
""",
        encoding="utf-8",
    )
    ledger = DecisionLedger.from_repo(tmp_path)
    policies = ledger.active_policies(["src/order/service.py"])
    assert len(policies) == 1
    assert policies[0].id == "DEC-1"
    rendered = ledger.render_truth_hierarchy_block(["src/order/service.py"])
    assert "DEC-1" in rendered
    assert "DEC-2" not in rendered

    appended = DecisionLedger.append_policy(
        tmp_path,
        scope="src/order/refund.py",
        rule="Always attach idempotency_key on refund events",
        reason="Prevent double refund",
        owner="charlie",
    )
    assert appended.status == "APPROVED_POLICY"
    reloaded = DecisionLedger.from_repo(tmp_path)
    assert len(reloaded.active_policies(["src/order/refund.py"])) == 2


def test_auto_onboard_local_git_repository(tmp_path: Path) -> None:
    import subprocess

    from digital_employee.onboarding import onboard_repository
    from digital_employee.settings import Settings

    sample_repo = tmp_path / "order-service"
    sample_repo.mkdir()
    (sample_repo / "pyproject.toml").write_text("[project]\nname='order-service'\n", encoding="utf-8")
    subprocess.run(["git", "init", "-b", "develop"], cwd=str(sample_repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:acme/order-service.git"],
        cwd=str(sample_repo),
        check=True,
        capture_output=True,
    )

    cfg = Settings(
        data_dir=tmp_path / "de-data",
        repo_registry_path=tmp_path / "empty.yaml",
    )
    profile = onboard_repository(cfg, str(sample_repo))
    assert profile.repo_id == "order-service"
    assert profile.github_repo == "acme/order-service"
    assert profile.default_base_branch == "develop"
    assert "pytest" in " ".join(profile.pytest_cmd)

    # Verify dynamic persistence and lookup
    resolved = cfg.resolve_repository("order-service")
    assert resolved.github_repo == "acme/order-service"
