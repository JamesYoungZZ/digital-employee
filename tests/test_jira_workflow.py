"""End-to-end deterministic tests for Dual-Path Jira Work-Order Integration."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from digital_employee.agents.runtime import CodingLoopOutcome
from digital_employee.integrations.feishu_cards import (
    build_jira_approval_card,
    parse_feishu_command,
)
from digital_employee.integrations.github_client import render_evidence_pr_body
from digital_employee.integrations.jira_client import (
    JiraIssueData,
    adf_to_markdown,
    extract_jira_issue_key,
    markdown_to_adf,
)
from digital_employee.integrations.jira_poller import JiraIssuePoller
from digital_employee.models.state import ReviewerReport
from digital_employee.orchestrator import DelegationOrchestrator
from digital_employee.settings import Settings
from digital_employee.web.app import create_app


def _init_mini_repo(repo_dir: Path) -> str:
    repo_dir.mkdir(parents=True)
    (repo_dir / "src").mkdir()
    (repo_dir / "tests").mkdir()
    (repo_dir / "src" / "calc.py").write_text(
        "def subtract(a: int, b: int) -> int:\n    return a + b\n",
        encoding="utf-8",
    )
    (repo_dir / "tests" / "test_calc.py").write_text(
        "from src.calc import subtract\n\n"
        "def test_zero():\n    assert subtract(5, 0) == 5\n",
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
            "init",
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


def _make_orchestrator(tmp_path: Path) -> tuple[DelegationOrchestrator, Path]:
    repo_dir = tmp_path / "demo_repo"
    _init_mini_repo(repo_dir)
    repo_cfg = tmp_path / "repos.yaml"
    repo_cfg.write_text(
        "repositories:\n"
        "  demo:\n"
        "    github_repo: acme/demo\n"
        f"    local_path: {repo_dir}\n"
        "    default_base_branch: main\n"
        f"    pytest_cmd: ['{sys.executable}', '-m', 'pytest', '-q']\n"
        "    lint_cmd: []\n",
        encoding="utf-8",
    )
    cfg = Settings(
        data_dir=tmp_path / "data",
        repo_registry_path=repo_cfg,
        llm_api_key="dummy",
        jira_base_url="https://acme.atlassian.net",
        jira_email="bot@acme.com",
        jira_api_token="jira_tok_secret",
        jira_default_project="KAN",
    )
    cfg.ensure_dirs()

    fake_llm = MagicMock()
    fake_llm.complete_json.return_value = (
        {
            "objective": "修复 subtract 函数加减号写反的缺陷",
            "jira_issue_type": "Bug",
            "jira_priority": "High",
            "jira_summary": "修复 calc.subtract 错误使用加号导致结果翻倍",
            "jira_description": "当前 `subtract(a, b)` 返回 `a + b`，需修复为 `a - b` 并补齐单测。",
            "reproduction_evidence": "subtract(10, 3) returns 13 instead of 7",
            "target_repro_test": "tests/test_calc.py",
            "acceptance_criteria": [
                "修复 src/calc.py 中 subtract 逻辑为 a - b",
                "补充非零减数回归单测并通过",
            ],
            "permitted_scopes": ["src", "tests"],
            "out_of_scope": ["禁止修改无关配置"],
            "unresolved_decisions": [],
        },
        MagicMock(estimated_cost_usd=0.0012),
    )
    orch = DelegationOrchestrator(cfg, llm=fake_llm)
    return orch, repo_dir


def test_jira_key_extraction_adf_and_feishu_commands() -> None:
    assert extract_jira_issue_key("https://acme.atlassian.net/browse/KAN-42?atlOrigin=1") == "KAN-42"
    assert extract_jira_issue_key("[modular-rag] 请直接按 RAG-108 修复") == "RAG-108"
    assert extract_jira_issue_key("Ensure UTF-8 encoding and SHA-256 hash") is None
    assert extract_jira_issue_key("Policy RAG-DEC-001 must hold") is None

    adf = markdown_to_adf("## 背景\n需要修复分词问题\n- 条件一\n- 条件二")
    assert adf["type"] == "doc"
    md = adf_to_markdown(adf)
    assert "需要修复分词问题" in md
    assert "- 条件一" in md

    assert parse_feishu_command("建单", ["modular-rag"])["intent"] == "quick_approve_jira"
    assert (
        parse_feishu_command("#t-20261117-abc123 建单", ["modular-rag"])["intent"]
        == "task_approve_jira"
    )
    delegated = parse_feishu_command("[modular-rag] KAN-88", ["modular-rag"])
    assert delegated["intent"] == "delegate"
    assert delegated["jira_issue_key"] == "KAN-88"


def test_path1_raw_request_requires_jira_approval_then_locks_to_created_jira(tmp_path: Path) -> None:
    orch, _ = _make_orchestrator(tmp_path)
    orch.feishu.send_card = MagicMock(return_value="openapi_chat:oc_123")  # type: ignore[method-assign]

    created_jira = JiraIssueData(
        key="KAN-101",
        url="https://acme.atlassian.net/browse/KAN-101",
        project_key="KAN",
        issue_type="Bug",
        summary="[已人工审定] 修复 calc.subtract 减法计算错误",
        description="严格按照人工审定后的 Jira 描述修复 subtract(a, b) = a - b。",
        status="To Do",
        priority="High",
        labels=["ai-digital-employee", "demo"],
    )
    orch.jira.create_issue = MagicMock(return_value=created_jira)  # type: ignore[method-assign]
    orch.jira.transition_issue_by_keywords = MagicMock(return_value="In Progress")  # type: ignore[method-assign]
    orch.jira.add_comment = MagicMock(return_value=True)  # type: ignore[method-assign]

    # Step 1: Delegate raw natural-language requirement -> MUST pause at WAITING_JIRA_APPROVAL!
    state = orch.submit_task(
        raw_request="calc.py 里的 subtract 写成了加法，帮我修一下",
        target_repo="demo",
        auto_execute=True,
        source_channel="feishu_ws",
    )
    assert state.lifecycle_status == "WAITING_JIRA_APPROVAL"
    assert state.jira_issue_key == ""
    assert state.jira_draft is not None
    assert state.jira_draft.project_key == "KAN"
    assert orch.jira.create_issue.call_count == 0

    card = build_jira_approval_card(state)
    assert "待你审核 Jira 工单" in card["card"]["header"]["title"]["content"]

    # Mock sandbox coder + reviewer for execution AFTER human approves Jira creation
    def _fake_coder_run(*, contract, sandbox, **_kwargs):
        # Verify contract is already locked to KAN-101 when coder runs!
        assert contract.jira_issue_key == "KAN-101"
        assert contract.objective == "[已人工审定] 修复 calc.subtract 减法计算错误"
        (sandbox.worktree_path / "src" / "calc.py").write_text(
            "def subtract(a: int, b: int) -> int:\n    return a - b\n",
            encoding="utf-8",
        )
        (sandbox.worktree_path / "tests" / "test_calc.py").write_text(
            "from src.calc import subtract\n\n"
            "def test_zero():\n    assert subtract(5, 0) == 5\n\n"
            "def test_positive_subtract():\n    assert subtract(10, 3) == 7\n",
            encoding="utf-8",
        )
        return CodingLoopOutcome(
            completed=True,
            repro_test_target="tests/test_calc.py::test_positive_subtract",
            commit_message="fix: correct subtract operator per KAN-101",
            root_cause="Plus used instead of minus",
            cost_usd=0.01,
            trace_log=["fixed calc.py"],
        )

    orch.coder.run = _fake_coder_run  # type: ignore[method-assign]
    orch.reviewer.review = MagicMock(  # type: ignore[method-assign]
        return_value=(
            ReviewerReport(
                verdict="APPROVE",
                reviewer_model="gemini-3.8-flash",
                ac_covered=True,
                decision_ledger_compliant=True,
                summary="符合 KAN-101 验收标准。",
                unverified_risks=["大整数溢出未单独压测"],
            ),
            0.002,
        )
    )

    # Step 2: Human edits summary & approves Jira creation -> creates KAN-101 and executes!
    finished = orch.approve_and_create_jira_ticket(
        state.task_id,
        draft_overrides={"summary": "[已人工审定] 修复 calc.subtract 减法计算错误"},
        approved_by="james",
        auto_execute=True,
    )
    assert orch.jira.create_issue.call_count == 1
    assert finished.jira_issue_key == "KAN-101"
    assert finished.contract.jira_issue_key == "KAN-101"
    assert finished.jira_status == "In Progress"
    assert finished.lifecycle_status == "DELIVERED_LOCAL_READY"
    assert finished.verification_evidence is not None
    assert finished.verification_evidence.passed_all_hard_gates

    pr_body = render_evidence_pr_body(finished)
    assert "[KAN-101](https://acme.atlassian.net/browse/KAN-101)" in pr_body


def test_path2_existing_jira_skips_approval_and_syncs_authoritative_updates(tmp_path: Path) -> None:
    orch, _ = _make_orchestrator(tmp_path)

    existing_issue = JiraIssueData(
        key="KAN-209",
        url="https://acme.atlassian.net/browse/KAN-209",
        project_key="KAN",
        issue_type="Task",
        summary="支持按已有 Jira 单号直接启动修复",
        description="### 验收标准\n- 直接读取 KAN-209 描述，不重复弹建卡审批",
        status="To Do",
        priority="Medium",
        labels=["demo", "ai-delegate"],
        comments=["PM: 请确保边界值 0 正常返回"],
    )
    orch.jira.get_issue = MagicMock(return_value=existing_issue)  # type: ignore[method-assign]
    orch.jira.create_issue = MagicMock()  # type: ignore[method-assign]
    orch.jira.transition_issue_by_keywords = MagicMock(return_value="In Progress")  # type: ignore[method-assign]
    orch.jira.add_comment = MagicMock(return_value=True)  # type: ignore[method-assign]

    # Submit existing Jira key with auto_execute=False so we inspect immediate post-submit state
    state = orch.submit_task(
        raw_request="https://acme.atlassian.net/browse/KAN-209",
        target_repo="demo",
        auto_execute=False,
        source_channel="web",
    )
    # Must NOT stop at WAITING_JIRA_APPROVAL and must NEVER call create_issue!
    assert state.lifecycle_status == "QUEUED"
    assert orch.jira.create_issue.call_count == 0
    assert state.jira_issue_key == "KAN-209"
    assert state.contract.jira_issue_key == "KAN-209"
    assert state.contract.objective == "支持按已有 Jira 单号直接启动修复"
    assert "PM: 请确保边界值 0 正常返回" in state.contract.reproduction_evidence

    # Simulate human updating requirements on Jira -> sync_contract_from_jira updates contract!
    updated_issue = JiraIssueData(
        key="KAN-209",
        url="https://acme.atlassian.net/browse/KAN-209",
        project_key="KAN",
        issue_type="Task",
        summary="支持按已有 Jira 单号直接启动修复（含负数边界）",
        description="新增要求：支持负数参数",
        status="In Progress",
        comments=["TechLead: 补充负数测试"],
    )
    orch.jira.get_issue = MagicMock(return_value=updated_issue)  # type: ignore[method-assign]
    synced = orch.sync_contract_from_jira(state.task_id)
    assert synced.contract.objective == "支持按已有 Jira 单号直接启动修复（含负数边界）"
    assert "TechLead: 补充负数测试" in synced.contract.reproduction_evidence


def test_jira_poller_and_web_api_endpoints(tmp_path: Path) -> None:
    orch, _ = _make_orchestrator(tmp_path)
    issue = JiraIssueData(
        key="KAN-305",
        url="https://acme.atlassian.net/browse/KAN-305",
        project_key="KAN",
        issue_type="Bug",
        summary="Poller 自动发现的待办工单",
        description="自动接单描述",
        status="To Do",
        labels=["demo", "ai-delegate"],
    )
    orch.jira.search_issues = MagicMock(return_value=["KAN-305"])  # type: ignore[method-assign]
    orch.jira.get_issue = MagicMock(return_value=issue)  # type: ignore[method-assign]
    orch.jira.transition_issue_by_keywords = MagicMock(return_value="In Progress")  # type: ignore[method-assign]
    orch.jira.add_comment = MagicMock(return_value=True)  # type: ignore[method-assign]
    orch.execute_task = MagicMock(side_effect=lambda tid, **kw: orch.store.get_task(tid))  # type: ignore[method-assign]

    poller = JiraIssuePoller(orch)
    assert poller.poll_once() == 1
    # Second poll must deduplicate KAN-305
    assert poller.poll_once() == 0

    app = create_app(orch.settings, orch, enable_background_daemons=False)
    client = TestClient(app)
    health = client.get("/api/health").json()
    assert health["jira_configured"] is True
    assert health["jira_default_project"] == "KAN"

    preview = client.get("/api/integrations/jira/issue/KAN-305")
    assert preview.status_code == 200
    assert preview.json()["summary"] == "Poller 自动发现的待办工单"
