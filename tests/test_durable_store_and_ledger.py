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


def test_managed_policy_closed_loop_and_health_circuit_breaker(tmp_path: Path) -> None:
    import subprocess

    from fastapi.testclient import TestClient

    from digital_employee.context.decision_ledger import DecisionEntry, audit_policy_health
    from digital_employee.onboarding import onboard_repository
    from digital_employee.orchestrator import DelegationOrchestrator
    from digital_employee.settings import Settings
    from digital_employee.web.app import create_app

    sample_repo = tmp_path / "billing-service"
    (sample_repo / "src" / "billing").mkdir(parents=True)
    (sample_repo / "src" / "billing" / "charge.py").write_text("def charge(): pass\n", encoding="utf-8")
    (sample_repo / "scripts").mkdir()
    (sample_repo / "scripts" / "sync_schema.py").write_text("print('ok')\n", encoding="utf-8")
    (sample_repo / "pyproject.toml").write_text("[project]\nname='billing-service'\n", encoding="utf-8")
    subprocess.run(["git", "init", "-b", "main"], cwd=str(sample_repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:acme/billing-service.git"],
        cwd=str(sample_repo),
        check=True,
        capture_output=True,
    )

    cfg = Settings(
        data_dir=tmp_path / "de-data",
        repo_registry_path=tmp_path / "empty.yaml",
        jira_poller_enabled=False,
    )
    profile = onboard_repository(cfg, str(sample_repo))
    orch = DelegationOrchestrator(cfg)

    # 1. Create a valid policy via orchestrator -> stored in SQLite, zero repo intrusion
    p1 = orch.create_repository_policy(
        profile.repo_id,
        title="扣款幂等键强制要求",
        category="SECURITY",
        scope="src/billing/*",
        rule="修改 src/billing/charge.py 后须运行 scripts/sync_schema.py 校验幂等字段；防止 collection/path 注入。",
        reason="防止重复扣款与路径注入",
        owner="alice",
    )
    assert not (sample_repo / ".ai" / "DECISIONS.yaml").exists()
    h1 = audit_policy_health(sample_repo, p1)
    assert h1.health_status == "HEALTHY"

    # 2. Create a policy pointing to a deleted/nonexistent scope -> auto circuit-broken!
    p_broken = orch.create_repository_policy(
        profile.repo_id,
        title="已废弃目录规则",
        category="ARCHITECTURE",
        scope="src/deleted_module/*",
        rule="禁止直接调用旧模块",
        owner="bob",
    )
    h_broken = audit_policy_health(sample_repo, p_broken)
    assert h_broken.health_status == "INVALID_SCOPE"

    ledger = DecisionLedger.for_repository(orch.store, profile)
    active_ids = [e.id for e in ledger.active_policies()]
    assert p1.id in active_ids
    assert p_broken.id not in active_ids  # Circuit breaker stops broken rule from misleading AI

    # 3. Referenced script deleted -> WARN_STALE_REF
    (sample_repo / "scripts" / "sync_schema.py").unlink()
    h1_after_del = audit_policy_health(sample_repo, p1)
    assert h1_after_del.health_status == "WARN_STALE_REF"
    assert "scripts/sync_schema.py" in h1_after_del.stale_references

    # 4. Stale verification date (>90 days) -> WARN_NEEDS_REVIEW, cleared by verify endpoint
    stale_entry = DecisionEntry(
        id="BILLING-DEC-099",
        title="旧核验规则",
        scope="*",
        rule="全局日志禁止打印卡号",
        verified_at="2020-01-01",
    )
    assert audit_policy_health(sample_repo, stale_entry).health_status == "WARN_NEEDS_REVIEW"

    # 5. Full REST CRUD loop via Web API
    app = create_app(cfg, orch, enable_background_daemons=False)
    client = TestClient(app)

    repos_resp = client.get("/api/repositories")
    assert repos_resp.status_code == 200
    repo_json = next(r for r in repos_resp.json() if r["repo_id"] == profile.repo_id)
    assert repo_json["policy_summary"]["total"] == 2
    assert repo_json["policy_summary"]["active_effective"] == 1
    assert repo_json["policy_summary"]["unhealthy"] == 2  # 1 INVALID_SCOPE + 1 WARN_STALE_REF

    # Fix broken scope via PUT -> becomes HEALTHY & effective_for_ai!
    upd_resp = client.put(
        f"/api/repositories/{profile.repo_id}/policies/{p_broken.id}",
        json={
            "title": "修正后的账单目录规范",
            "category": "ARCHITECTURE",
            "scope": "src/billing/charge.py",
            "rule": "账单扣款必须记录审计流水",
            "reason": "合规审计要求",
            "owner": "charlie",
            "status": "APPROVED_POLICY",
        },
    )
    assert upd_resp.status_code == 200
    assert upd_resp.json()["health_status"] == "HEALTHY"
    assert upd_resp.json()["effective_for_ai"] is True

    # Toggle pause/resume
    tog_resp = client.post(
        f"/api/repositories/{profile.repo_id}/policies/{p_broken.id}/toggle"
    )
    assert tog_resp.status_code == 200
    assert tog_resp.json()["status"] == "PAUSED"
    assert tog_resp.json()["effective_for_ai"] is False

    # Verify freshness
    ver_resp = client.post(
        f"/api/repositories/{profile.repo_id}/policies/{p_broken.id}/verify",
        json={"owner": "dave"},
    )
    assert ver_resp.status_code == 200
    assert ver_resp.json()["owner"] == "dave"

    # Delete rule
    del_resp = client.delete(
        f"/api/repositories/{profile.repo_id}/policies/{p_broken.id}"
    )
    assert del_resp.status_code == 200
    assert del_resp.json()["deleted"] is True


def test_console_management_endpoints_full_loop(tmp_path: Path) -> None:
    import subprocess

    from fastapi.testclient import TestClient

    from digital_employee.onboarding import onboard_repository
    from digital_employee.orchestrator import DelegationOrchestrator
    from digital_employee.settings import Settings
    from digital_employee.web.app import create_app

    repo_dir = tmp_path / "inventory-api"
    (repo_dir / "src").mkdir(parents=True)
    (repo_dir / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (repo_dir / "pyproject.toml").write_text("[project]\nname='inventory-api'\n", encoding="utf-8")
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "ci@example.com"], cwd=str(repo_dir), check=True)
    subprocess.run(["git", "config", "user.name", "CI"], cwd=str(repo_dir), check=True)
    subprocess.run(["git", "add", "."], cwd=str(repo_dir), check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "branch", "release/1.0"], cwd=str(repo_dir), check=True)

    cfg = Settings(
        data_dir=tmp_path / "de-data",
        repo_registry_path=tmp_path / "empty.yaml",
        jira_poller_enabled=False,
    )
    profile = onboard_repository(cfg, str(repo_dir))
    orch = DelegationOrchestrator(cfg)
    app = create_app(cfg, orch, enable_background_daemons=False)
    client = TestClient(app)

    # 1. List git branches
    br_resp = client.get(f"/api/repositories/{profile.repo_id}/branches")
    assert br_resp.status_code == 200
    assert "main" in br_resp.json()["branches"]
    assert "release/1.0" in br_resp.json()["branches"]

    # 2. Update repository config (default branch, pytest_cmd, lint_cmd, default_jira_project)
    upd_repo = client.put(
        f"/api/repositories/{profile.repo_id}",
        json={
            "default_base_branch": "release/1.0",
            "pytest_cmd": ["python3", "-m", "pytest", "-q"],
            "lint_cmd": ["ruff", "check"],
            "default_jira_project": "INV",
        },
    )
    assert upd_repo.status_code == 200
    assert upd_repo.json()["default_base_branch"] == "release/1.0"
    assert upd_repo.json()["default_jira_project"] == "INV"

    # 3. Core engine config read
    core_get = client.get("/api/integrations/core")
    assert core_get.status_code == 200
    assert "/webhooks/alert" in core_get.json()["ops_alert_webhook_url"]

    # 4. Create sample tasks, search/filter, update scope, and delete task
    t1 = DurableTaskState(
        task_id="t-20261118-100001",
        trace_id="tr-1",
        raw_request="Fix stock deduction race condition",
        source_channel="alert_webhook",
        contract=TaskContract(
            objective="Fix stock deduction race condition",
            target_repo=profile.repo_id,
            base_branch="release/1.0",
            base_commit_sha="12345678",
            reproduction_evidence="Concurrent requests cause negative stock",
            permitted_scopes=["src/stock/*"],
        ),
        lifecycle_status="HANDED_OFF_INCOMPLETE",
        failure_class="SECURITY_OR_SCOPE_VIOLATION",
    )
    orch.store.save_task(t1, step_name="handed_off")

    search_hit = client.get("/api/tasks", params={"repo_id": profile.repo_id, "q": "deduction"})
    assert search_hit.status_code == 200
    assert len(search_hit.json()) == 1

    search_miss = client.get("/api/tasks", params={"q": "nonexistent_keyword_xyz"})
    assert search_miss.status_code == 200
    assert len(search_miss.json()) == 0

    # Delete task record
    del_task = client.delete(f"/api/tasks/{t1.task_id}")
    assert del_task.status_code == 200
    assert del_task.json()["deleted"] is True
    assert orch.store.get_task(t1.task_id) is None

    # 5. Unbind repository cleanly
    del_repo = client.delete(f"/api/repositories/{profile.repo_id}")
    assert del_repo.status_code == 200
    assert profile.repo_id not in cfg.load_repositories()
