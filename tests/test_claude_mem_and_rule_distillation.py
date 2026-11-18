"""Tests for `claude-mem` dual-mode memory, 3-stage MCP retrieval, rule distillation & soft scopes."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from digital_employee.context.decision_ledger import DecisionLedger
from digital_employee.integrations.feishu_cards import build_merge_approval_card
from digital_employee.integrations.github_client import render_evidence_pr_body
from digital_employee.kernel.config import MCPServerConfig
from digital_employee.kernel.mcp import MCPManager
from digital_employee.kernel.tools import create_default_registry
from digital_employee.kernel.tools.impl.tool_search import ToolSearchTool
from digital_employee.kernel.tools.mcp_call import McpCallTool
from digital_employee.memory.claude_mem import ClaudeMemService
from digital_employee.models.contract import RepositoryProfile, TaskContract
from digital_employee.orchestrator import DelegationOrchestrator
from digital_employee.sandbox.worktree_runner import WorktreeSandbox
from digital_employee.security.dlp import PolicyViolationError
from digital_employee.settings import PROJECT_ROOT, Settings
from digital_employee.verification.evidence_gate import EvidenceGate
from digital_employee.web.app import create_app


def _init_cross_scope_repo(repo_dir: Path) -> str:
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / "src" / "order").mkdir(parents=True, exist_ok=True)
    (repo_dir / "src" / "common").mkdir(parents=True, exist_ok=True)
    (repo_dir / "tests").mkdir(parents=True, exist_ok=True)

    (repo_dir / "src" / "common" / "math_util.py").write_text(
        "def clamp_non_negative(val: int) -> int:\n    return val\n",
        encoding="utf-8",
    )
    (repo_dir / "src" / "order" / "pricing.py").write_text(
        "from src.common.math_util import clamp_non_negative\n\n"
        "def compute_total(price: int, discount: int) -> int:\n"
        "    return clamp_non_negative(price - discount)\n",
        encoding="utf-8",
    )
    (repo_dir / "tests" / "test_pricing.py").write_text(
        "from src.order.pricing import compute_total\n\n"
        "def test_basic_total() -> None:\n"
        "    assert compute_total(100, 20) == 80\n",
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
            "initial commit",
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


def test_claude_mem_dlp_scrubbing_and_3_stage_progressive_disclosure(tmp_path: Path) -> None:
    svc = ClaudeMemService(tmp_path / "claude_mem.db")

    obs1 = svc.save_observation(
        project="my-service",
        task_id="t-001",
        obs_type="discovery",
        title="排查订单模块缓存初始化流程",
        narrative="读取了 src/order/cache.py，发现测试环境使用 token sk-live1234567890abcdefghij 连接。",
        files_read=["src/order/cache.py"],
        verified=False,
    )
    obs2 = svc.save_observation(
        project="my-service",
        task_id="t-001",
        obs_type="pitfall",
        title="修改 fixtures 后必须重建 manifest 哈希",
        subtitle="漏跑 index_commerce.py 会导致 pytest 收集报错",
        narrative="修改 fixtures/commerce/*.md 后必须执行 scripts/index_commerce.py 更新 SHA-256。",
        facts=["修改测试夹具后必须同步执行 scripts/index_commerce.py"],
        files_modified=["fixtures/commerce/doc.md"],
        concepts=["gotcha", "pytest"],
        verified=True,
    )
    obs3 = svc.save_observation(
        project="other-repo",
        task_id="t-999",
        obs_type="bugfix",
        title="无关仓库的修复记录",
        narrative="这条记录属于 other-repo，不应混入 my-service 检索结果。",
        verified=True,
    )
    assert obs1 is not None and obs2 is not None and obs3 is not None

    # Verify DLP scrubbed the secret key before SQLite persistence
    fetched_1 = svc.get_observations([obs1.id])[0]
    assert "sk-live1234567890abcdefghij" not in fetched_1.narrative
    assert "[REDACTED_API_KEY]" in fetched_1.narrative

    # Stage 1: Lightweight index search scoped strictly to `my-service`
    hits = svc.search(project="my-service", query="manifest 哈希 pytest")
    assert len(hits) >= 1
    assert hits[0].id == obs2.id
    assert all(h.project == "my-service" for h in hits)
    index_txt = svc.format_search_index(hits)
    assert f"#{obs2.id}" in index_txt
    assert "mem_get_observations" in index_txt

    # Stage 2: Chronological timeline around `obs2.id`
    tl = svc.timeline(anchor_id=obs2.id, project="my-service", before=2, after=2)
    assert [item.id for item in tl] == [obs1.id, obs2.id]
    tl_txt = svc.format_timeline(tl, anchor_id=obs2.id)
    assert f">>> #{obs2.id}" in tl_txt

    # Stage 3: Full detail retrieval
    detail_txt = svc.format_observations_detail(svc.get_observations([obs2.id]))
    assert "scripts/index_commerce.py" in detail_txt
    assert "VERIFIED" in detail_txt


def test_claude_mem_external_sidecar_http_bridge(tmp_path: Path) -> None:
    pushed_payloads: list[dict] = []

    class _MockClaudeMemWorker(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/api/health"):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            body = json.dumps(
                {
                    "observations": [
                        {
                            "id": 777,
                            "project": "ext-repo",
                            "type": "bugfix",
                            "title": "外部原版 claude-mem Worker 历史记录",
                            "narrative": "来自 :37777 外部服务的经验卡片",
                            "files_modified": ["src/ext.py"],
                        }
                    ]
                }
            ).encode("utf-8")
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8")
            pushed_payloads.append(json.loads(raw))
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"saved":true}')

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            del format, args

    server = HTTPServer(("127.0.0.1", 0), _MockClaudeMemWorker)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        svc = ClaudeMemService(
            tmp_path / "hybrid.db",
            external_url=f"http://127.0.0.1:{port}",
        )
        assert svc.mode == "hybrid_external"
        health = svc.check_external_health()
        assert health["reachable"] is True

        svc.save_observation(
            project="ext-repo",
            title="本地新增同时同步至外部 Worker",
            narrative="双写验证",
            verified=True,
        )
        assert len(pushed_payloads) == 1
        assert pushed_payloads[0]["project"] == "ext-repo"

        merged = svc.search(project="ext-repo", query="外部")
        assert any("外部原版 claude-mem" in m.title for m in merged)
    finally:
        server.shutdown()


@pytest.mark.asyncio
async def test_claude_mem_mcp_stdio_server_roundtrip(tmp_path: Path) -> None:
    db_file = tmp_path / "mcp_mem.db"
    svc = ClaudeMemService(db_file)
    obs = svc.save_observation(
        project="mcp-demo-repo",
        task_id="t-mcp-1",
        obs_type="review_block",
        title="Code Review 拦截：禁止在路由层裸拼 SQL",
        narrative="所有数据库查询必须使用参数化占位符 `?`，防止 SQL 注入。",
        files_modified=["src/web/routes.py"],
        verified=True,
    )
    assert obs is not None

    registry = create_default_registry(work_dir=str(tmp_path))
    registry.register(ToolSearchTool(registry, protocol="openai-compat"))
    registry.register(McpCallTool(registry))

    mcp_mgr = MCPManager()
    mcp_mgr.load_configs(
        [
            MCPServerConfig(
                name="claude-mem",
                command=sys.executable,
                args=["-m", "digital_employee.mcp_servers.claude_mem_mcp"],
                env={
                    "PYTHONPATH": str(PROJECT_ROOT / "src"),
                    "DE_CLAUDE_MEM_DB_PATH": str(db_file),
                    "DE_CLAUDE_MEM_DEFAULT_PROJECT": "mcp-demo-repo",
                    "DE_CLAUDE_MEM_TASK_ID": "t-mcp-2",
                },
            )
        ]
    )
    try:
        conn_res = await mcp_mgr.register_all_tools(registry)
        assert not conn_res.errors
        mcp_call = registry.get("mcp_call")
        assert mcp_call is not None

        # 1) Stage-1 Search via MCP
        s_res = await mcp_call.execute(
            mcp_call.params_model(
                server="claude-mem", tool="mem_search", arguments={"query": "SQL"}
            )
        )
        assert not s_res.is_error
        assert f"#{obs.id}" in s_res.output

        # 2) Stage-3 Deep Fetch via MCP
        d_res = await mcp_call.execute(
            mcp_call.params_model(
                server="claude-mem",
                tool="mem_get_observations",
                arguments={"ids": [obs.id]},
            )
        )
        assert not d_res.is_error
        assert "参数化占位符" in d_res.output

        # 3) Agent proactive save via MCP
        w_res = await mcp_call.execute(
            mcp_call.params_model(
                server="claude-mem",
                tool="mem_save_observation",
                arguments={
                    "title": "发现隐藏环境变量依赖",
                    "narrative": "跑集成测试前需设置 TZ=UTC。",
                    "obs_type": "pitfall",
                },
            )
        )
        assert not w_res.is_error
        assert "Saved observation" in w_res.output
        assert len(svc.search(project="mcp-demo-repo", query="TZ=UTC")) == 1
    finally:
        await mcp_mgr.shutdown()


def test_soft_scopes_allow_cross_module_fix_with_advisory_while_blocking_forbidden(
    tmp_path: Path,
) -> None:
    repo_dir = tmp_path / "repo"
    base_sha = _init_cross_scope_repo(repo_dir)

    profile = RepositoryProfile(
        repo_id="pricing-svc",
        github_repo="acme/pricing-svc",
        local_path=str(repo_dir),
        default_base_branch="main",
        pytest_cmd=[sys.executable, "-m", "pytest"],
        lint_cmd=[],
        default_suite_targets=["tests/test_pricing.py"],
    )
    # Initial contract only suggested `src/order` and `tests`, NOT `src/common`!
    contract = TaskContract(
        objective="优惠金额大于商品原价时总价不能为负数",
        target_repo="pricing-svc",
        base_branch="main",
        base_commit_sha=base_sha,
        reproduction_evidence="compute_total(50, 80) returns -30 instead of 0",
        target_repro_test="tests/test_pricing.py::test_discount_floor",
        acceptance_criteria=["优惠超额时返回 0"],
        permitted_scopes=["src/order", "tests"],
    )

    sandbox = WorktreeSandbox(
        profile,
        tmp_path / "worktrees",
        "t-soft-scope",
        permitted_scopes=contract.permitted_scopes,
    )
    sandbox.provision(base_sha)

    #Forbidden file `.env` MUST still be hard-blocked!
    with pytest.raises(PolicyViolationError):
        sandbox.write_or_edit_file(".env", new_content="SECRET=123\n")

    # Cross-directory fix in `src/common/math_util.py` (outside `src/order`) MUST succeed!
    sandbox.write_or_edit_file(
        "src/common/math_util.py",
        new_content="def clamp_non_negative(val: int) -> int:\n    return max(0, val)\n",
    )
    sandbox.write_or_edit_file(
        "tests/test_pricing.py",
        new_content=(
            "from src.order.pricing import compute_total\n\n"
            "def test_basic_total() -> None:\n"
            "    assert compute_total(100, 20) == 80\n\n"
            "def test_discount_floor() -> None:\n"
            "    assert compute_total(50, 80) == 0\n"
        ),
    )
    head_sha = sandbox.commit_all("fix: clamp negative total in math_util", base_sha)

    gate_res = EvidenceGate(sandbox, contract).evaluate(
        head_sha, "tests/test_pricing.py::test_discount_floor"
    )
    ev = gate_res.evidence
    assert ev.passed_all_hard_gates is True
    assert ev.scope_compliance_check is True
    assert ev.out_of_scope_files == ["src/common/math_util.py"]

    # Verify PR body & Feishu merge card surface the cross-directory edit advisory
    from digital_employee.models.state import DurableTaskState

    state = DurableTaskState(
        task_id="t-soft-scope",
        trace_id="tr-1",
        raw_request="fix negative pricing",
        contract=contract,
        branch_name=sandbox.branch_name,
        head_commit_sha=head_sha,
        verification_evidence=ev,
        distilled_policy_ids=["PRICIN-DEC-001"],
    )
    pr_md = render_evidence_pr_body(state)
    assert "src/common/math_util.py" in pr_md
    assert "Cross-Scope Edits" in pr_md

    card = build_merge_approval_card(state)
    card_str = json.dumps(card, ensure_ascii=False)
    assert "src/common/math_util.py" in card_str
    assert "候选治理规则草稿" in card_str


def test_rule_distillation_and_approval_api_workflow(tmp_path: Path) -> None:
    repo_dir = tmp_path / "repo_api"
    _init_cross_scope_repo(repo_dir)
    reg_file = tmp_path / "repositories.yaml"
    reg_file.write_text(
        f"""
repositories:
  pricing-svc:
    github_repo: "acme/pricing-svc"
    local_path: "{repo_dir.as_posix()}"
    default_base_branch: "main"
    pytest_cmd: ["{sys.executable}", "-m", "pytest"]
    lint_cmd: []
""",
        encoding="utf-8",
    )

    settings = Settings(
        data_dir=tmp_path / ".local",
        repo_registry_path=reg_file,
        enable_claude_mem=True,
    )
    orch = DelegationOrchestrator(settings=settings, llm=object())  # type: ignore[arg-type]

    # Seed high-signal review_block & human_feedback observations in `claude-mem`
    orch.claude_mem.record_pitfall_or_review_lesson(
        project="pricing-svc",
        task_id="t-101",
        lesson_kind="review_block",
        title="Code Review 拦截：金额计算严禁使用 float 浮点数",
        detail="涉及金额与折扣计算必须统一使用整数分（int）或 Decimal，防止精度丢失。",
        files_modified=["src/order/pricing.py"],
        facts=["涉及金额与折扣计算必须统一使用整数分（int）或 Decimal，严禁使用 float"],
    )

    app = create_app(settings=settings, orchestrator=orch)
    client = TestClient(app)

    # 1) Check `/api/repositories/{repo_id}/memories`
    mem_resp = client.get("/api/repositories/pricing-svc/memories?query=Decimal")
    assert mem_resp.status_code == 200
    assert len(mem_resp.json()["observations"]) == 1

    # 2) Trigger `/api/repositories/{repo_id}/policies/distill-from-memory`
    distill_resp = client.post("/api/repositories/pricing-svc/policies/distill-from-memory")
    assert distill_resp.status_code == 200
    body = distill_resp.json()
    assert body["created_count"] == 1
    draft_policy = body["candidates"][0]
    assert draft_policy["status"] == "CANDIDATE_DRAFT"
    assert draft_policy["source_type"] == "MEM_DISTILLED"
    policy_id = draft_policy["id"]

    # Candidate drafts MUST NOT be injected into AI until human approves!
    profile = settings.resolve_repository("pricing-svc")
    ledger_before = DecisionLedger.for_repository(orch.store, profile)
    assert all(p.id != policy_id for p in ledger_before.active_policies())

    # 3) Human clicks "采纳生效" (`/approve`) -> becomes active `APPROVED_POLICY`!
    approve_resp = client.post(f"/api/repositories/pricing-svc/policies/{policy_id}/approve")
    assert approve_resp.status_code == 200
    assert approve_resp.json()["status"] == "APPROVED_POLICY"
    assert approve_resp.json()["effective_for_ai"] is True

    ledger_after = DecisionLedger.for_repository(orch.store, profile)
    assert any(p.id == policy_id for p in ledger_after.active_policies())
