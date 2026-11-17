"""End-to-end tests for NativeKernelRuntime, ContractPathSandbox, Pause/Resume & Ops MCP."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from digital_employee.agents.kernel_runtime import (
    ContractPathSandbox,
    NativeKernelRuntime,
    load_conversation_checkpoint,
)
from digital_employee.kernel.config import MCPServerConfig
from digital_employee.kernel.mcp import MCPManager
from digital_employee.kernel.mcp.loading_strategy import McpLoadingMode, decide_and_apply
from digital_employee.kernel.tools import create_default_registry
from digital_employee.kernel.tools.impl.tool_search import ToolSearchTool
from digital_employee.kernel.tools.mcp_call import McpCallTool
from digital_employee.llm.client import LLMCallMetrics, LLMClient, extract_json_object
from digital_employee.orchestrator import DelegationOrchestrator
from digital_employee.settings import Settings
from digital_employee.verification.evidence_gate import scan_test_diff_for_tampering
from digital_employee.web.app import create_app


def _init_buggy_repo(repo_dir: Path) -> str:
    repo_dir.mkdir(parents=True)
    (repo_dir / "src").mkdir()
    (repo_dir / "tests").mkdir()
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


class ScriptedHarnessLLM(LLMClient):
    """Deterministic fake Harness LLMClient driving Drafter -> Kernel -> Reviewer."""

    def __init__(self, kernel_turns: list[dict[str, Any]]) -> None:
        super().__init__(Settings(llm_api_key="dummy", enable_ops_mcp=False))
        self.kernel_turns = list(kernel_turns)
        self.seen_kernel_messages: list[list[dict[str, Any]]] = []

    def complete_json(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        max_tokens: int = 3072,
    ) -> tuple[dict[str, Any], LLMCallMetrics]:
        msg, metrics = self.chat(messages, model=model, json_mode=True, max_tokens=max_tokens)
        return extract_json_object(str(msg.get("content", ""))), metrics

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        json_mode: bool = False,
        max_tokens: int = 4096,
        max_retries: int = 2,
    ) -> tuple[dict[str, Any], LLMCallMetrics]:
        del model, max_tokens, max_retries
        metrics = LLMCallMetrics(prompt_tokens=120, completion_tokens=45, estimated_cost_usd=0.001)

        # Reviewer or ContractDrafter JSON mode call
        if json_mode or not tools:
            sys_txt = str(messages[0].get("content", "")) if messages else ""
            if "Reviewer" in sys_txt or "unverified_risks" in sys_txt or "verdict" in sys_txt:
                payload = {
                    "verdict": "APPROVE",
                    "ac_satisfied": True,
                    "blocking_issues": [],
                    "unverified_risks": ["Negative discount values not tested"],
                    "summary": "Clean subtraction fix verified by Fail-to-Pass.",
                }
            else:
                payload = {
                    "objective": "Fix apply_discount subtraction bug",
                    "reproduction_evidence": "apply_discount(100, 20) returned 120",
                    "target_repro_test": "tests/test_pricing.py::test_discount_repro",
                    "acceptance_criteria": ["apply_discount subtracts discount"],
                    "permitted_scopes": ["src", "tests"],
                    "out_of_scope": [".github"],
                    "unresolved_decisions": [],
                }
            return {"role": "assistant", "content": json.dumps(payload)}, metrics

        self.seen_kernel_messages.append(messages)
        if not self.kernel_turns:
            return {"role": "assistant", "content": "Done."}, metrics
        return self.kernel_turns.pop(0), metrics


def test_orchestrator_defaults_to_native_kernel_and_completes_fail_to_pass(tmp_path: Path) -> None:
    repo_dir = tmp_path / "repo"
    _init_buggy_repo(repo_dir)
    repo_cfg = tmp_path / "repos.yaml"
    repo_cfg.write_text(
        "repositories:\n"
        "  pricing:\n"
        f"    github_repo: acme/pricing\n"
        f"    local_path: {repo_dir}\n"
        f"    python_bin: {sys.executable}\n"
        f"    pytest_cmd: [{sys.executable}, -m, pytest]\n"
        "    default_suite_targets: [tests/test_pricing.py]\n",
        encoding="utf-8",
    )

    scripted_turns: list[dict[str, Any]] = [
        # Step 1: Concurrent read batch (ReadFile on both source & test)
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {
                        "name": "ReadFile",
                        "arguments": json.dumps({"file_path": "src/pricing.py"}),
                    },
                },
                {
                    "id": "c2",
                    "type": "function",
                    "function": {
                        "name": "ReadFile",
                        "arguments": json.dumps({"file_path": "tests/test_pricing.py"}),
                    },
                },
            ],
        },
        # Step 2: Serial write batch (EditFile on source + EditFile adding Fail-to-Pass regression test)
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c3",
                    "type": "function",
                    "function": {
                        "name": "EditFile",
                        "arguments": json.dumps(
                            {
                                "file_path": "src/pricing.py",
                                "old_string": "return price + discount",
                                "new_string": "return price - discount",
                            }
                        ),
                    },
                },
                {
                    "id": "c4",
                    "type": "function",
                    "function": {
                        "name": "EditFile",
                        "arguments": json.dumps(
                            {
                                "file_path": "tests/test_pricing.py",
                                "old_string": "    assert apply_discount(100, 0) == 100",
                                "new_string": (
                                    "    assert apply_discount(100, 0) == 100\n\n"
                                    "def test_discount_repro():\n"
                                    "    assert apply_discount(100, 20) == 80"
                                ),
                            }
                        ),
                    },
                },
            ],
        },
        # Step 3: Accountable finish_task exit
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c5",
                    "type": "function",
                    "function": {
                        "name": "finish_task",
                        "arguments": json.dumps(
                            {
                                "repro_test_target": "tests/test_pricing.py::test_discount_repro",
                                "commit_message": "fix: subtract discount in apply_discount",
                                "root_cause": "Used + operator instead of - operator",
                                "summary": "Fixed operator and added regression test.",
                            }
                        ),
                    },
                }
            ],
        },
    ]

    cfg = Settings(
        data_dir=tmp_path / "data",
        repo_registry_path=repo_cfg,
        llm_api_key="dummy",
        enable_ops_mcp=False,
    )
    cfg.ensure_dirs()
    fake_llm = ScriptedHarnessLLM(scripted_turns)
    orch = DelegationOrchestrator(cfg, llm=fake_llm)  # type: ignore[arg-type]

    assert isinstance(orch.coder, NativeKernelRuntime)

    state = orch.submit_task(
        raw_request="Fix apply_discount bug",
        target_repo="pricing",
        auto_execute=True,
    )
    assert state.lifecycle_status == "DELIVERED_LOCAL_READY"
    assert state.verification_evidence is not None
    assert state.verification_evidence.repro_test_failed_on_base is True
    assert state.verification_evidence.repro_test_passed_on_head is True
    assert state.verification_evidence.passed_all_hard_gates is True


def test_kernel_mid_flight_clarification_pauses_and_resumes_losslessly(tmp_path: Path) -> None:
    repo_dir = tmp_path / "repo_pause"
    base_sha = _init_buggy_repo(repo_dir)
    repo_cfg = tmp_path / "repos.yaml"
    repo_cfg.write_text(
        "repositories:\n"
        "  pricing:\n"
        f"    github_repo: acme/pricing\n"
        f"    local_path: {repo_dir}\n"
        f"    python_bin: {sys.executable}\n"
        f"    pytest_cmd: [{sys.executable}, -m, pytest]\n",
        encoding="utf-8",
    )

    scripted_turns: list[dict[str, Any]] = [
        # Phase A (Before pause): Read source file, then request human clarification
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "r1",
                    "type": "function",
                    "function": {
                        "name": "ReadFile",
                        "arguments": json.dumps({"file_path": "src/pricing.py"}),
                    },
                }
            ],
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "ask1",
                    "type": "function",
                    "function": {
                        "name": "request_clarification",
                        "arguments": json.dumps(
                            {
                                "question": "Should discounted price floor at 0?",
                                "options": ["Floor at 0", "Allow negative"],
                                "default_assumption": "Floor at 0",
                                "impact_scope": "src/pricing.py",
                            }
                        ),
                    },
                }
            ],
        },
        # Phase B (After human clicks option): Directly edit src/pricing.py WITHOUT re-reading
        # (proves conversation checkpoint was restored!) & read+edit test + finish_task
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "r2",
                    "type": "function",
                    "function": {
                        "name": "ReadFile",
                        "arguments": json.dumps({"file_path": "src/pricing.py"}),
                    },
                },
                {
                    "id": "r3",
                    "type": "function",
                    "function": {
                        "name": "ReadFile",
                        "arguments": json.dumps({"file_path": "tests/test_pricing.py"}),
                    },
                },
            ],
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "e1",
                    "type": "function",
                    "function": {
                        "name": "EditFile",
                        "arguments": json.dumps(
                            {
                                "file_path": "src/pricing.py",
                                "old_string": "return price + discount",
                                "new_string": "return max(0, price - discount)",
                            }
                        ),
                    },
                },
                {
                    "id": "e2",
                    "type": "function",
                    "function": {
                        "name": "EditFile",
                        "arguments": json.dumps(
                            {
                                "file_path": "tests/test_pricing.py",
                                "old_string": "    assert apply_discount(100, 0) == 100",
                                "new_string": (
                                    "    assert apply_discount(100, 0) == 100\n\n"
                                    "def test_floor_discount():\n"
                                    "    assert apply_discount(50, 80) == 0"
                                ),
                            }
                        ),
                    },
                },
            ],
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "fin1",
                    "type": "function",
                    "function": {
                        "name": "finish_task",
                        "arguments": json.dumps(
                            {
                                "repro_test_target": "tests/test_pricing.py::test_floor_discount",
                                "commit_message": "fix: subtract discount with floor at 0",
                                "root_cause": "Wrong operator and missing floor",
                                "summary": "Applied human decision Floor at 0.",
                            }
                        ),
                    },
                }
            ],
        },
    ]

    cfg = Settings(
        data_dir=tmp_path / "data",
        repo_registry_path=repo_cfg,
        llm_api_key="dummy",
        enable_ops_mcp=False,
    )
    cfg.ensure_dirs()
    fake_llm = ScriptedHarnessLLM(scripted_turns)
    orch = DelegationOrchestrator(cfg, llm=fake_llm)  # type: ignore[arg-type]

    paused = orch.submit_task(raw_request="Fix pricing", target_repo="pricing", auto_execute=True)
    assert paused.lifecycle_status == "WAITING_CLARIFICATION"
    ckpt_file = orch.store.artifacts_dir / paused.task_id / "kernel_session.jsonl"
    assert ckpt_file.is_file()
    restored_conv = load_conversation_checkpoint(ckpt_file)
    assert restored_conv is not None and len(restored_conv.history) >= 2

    resumed = orch.resolve_clarification(
        paused.task_id,
        answers={"0": "Floor at 0"},
        auto_execute=True,
    )
    assert resumed.lifecycle_status == "DELIVERED_LOCAL_READY"
    # Verify `<clarification-resolved>` was injected into the resumed conversation!
    resumed_msgs_flat = json.dumps(fake_llm.seen_kernel_messages[2], ensure_ascii=False)
    assert "clarification-resolved" in resumed_msgs_flat
    assert "Floor at 0" in resumed_msgs_flat
    del base_sha


def test_contract_path_sandbox_enforces_permitted_scopes_and_allows_de_session(tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    (wt / "src").mkdir(parents=True)
    (wt / "docs").mkdir(parents=True)
    (wt / ".github" / "workflows").mkdir(parents=True)

    sandbox = ContractPathSandbox(
        wt,
        permitted_scopes=["src"],
        forbidden_paths=[".github/workflows/", ".env"],
    )

    ok_src, _ = sandbox.check_deny_write(str(wt / "src" / "app.py"))
    assert ok_src is True

    ok_de, _ = sandbox.check_deny_write(str(wt / ".de" / "sessions" / "s1.jsonl"))
    assert ok_de is True

    ok_docs, reason_docs = sandbox.check_deny_write(str(wt / "docs" / "guide.md"))
    assert ok_docs is False
    assert "permitted_scopes" in reason_docs

    ok_ci, _ = sandbox.check_deny_write(str(wt / ".github" / "workflows" / "ci.yml"))
    assert ok_ci is False


@pytest.mark.asyncio
async def test_ops_diagnostics_mcp_stdio_integration_and_cache_dispatch_strategy(tmp_path: Path) -> None:
    repo_dir = tmp_path / "ops_repo"
    _init_buggy_repo(repo_dir)
    log_file = tmp_path / "app.log"
    log_file.write_text(
        "2026-11-17 INFO Starting worker\n"
        "2026-11-17 ERROR PaymentError token=sk-live-secret9876543210abcdef123456 failed\n",
        encoding="utf-8",
    )

    registry = create_default_registry(work_dir=str(repo_dir))
    registry.register(ToolSearchTool(registry, protocol="openai-compat"))
    registry.register(McpCallTool(registry))

    mcp_mgr = MCPManager()
    mcp_mgr.load_configs(
        [
            MCPServerConfig(
                name="ops-diagnostics",
                command=sys.executable,
                args=["-m", "digital_employee.mcp_servers.ops_diagnostics"],
            )
        ]
    )
    try:
        res = await mcp_mgr.register_all_tools(registry)
        assert not res.errors

        # Force low context window so schema exceeds 10% -> verifies DISPATCH mode on DeepSeek/OpenAI!
        decide_and_apply(registry, base_url="https://api.deepseek.com", context_window=2000)
        assert registry.mcp_loading_mode is McpLoadingMode.DISPATCH
        assert registry.expose_tool_search is True
        assert registry.expose_mcp_call is True

        mcp_call_tool = registry.get("mcp_call")
        assert mcp_call_tool is not None
        # Mark discovered via ToolSearch first (matching real model flow)
        tool_search = registry.get("ToolSearch")
        assert tool_search is not None
        await tool_search.execute(
            tool_search.params_model(query="select:mcp__ops_diagnostics__inspect_recent_commits,mcp__ops_diagnostics__search_error_logs")
        )

        commits_out = await mcp_call_tool.execute(
            mcp_call_tool.params_model(
                server="ops-diagnostics",
                tool="inspect_recent_commits",
                arguments={"repo_path": str(repo_dir), "limit": 3},
            )
        )
        assert "initial buggy commit" in commits_out.output

        logs_out = await mcp_call_tool.execute(
            mcp_call_tool.params_model(
                server="ops-diagnostics",
                tool="search_error_logs",
                arguments={"log_path": str(log_file), "pattern": "PaymentError"},
            )
        )
        assert "PaymentError" in logs_out.output
        # Verify DLP scrubbed the planted sk-live secret!
        assert "sk-live-secret9876543210abcdef123456" not in logs_out.output
    finally:
        await mcp_mgr.shutdown()


def test_ops_alert_webhook_and_polyglot_anti_tampering(tmp_path: Path) -> None:
    repo_dir = tmp_path / "alert_repo"
    _init_buggy_repo(repo_dir)
    repo_cfg = tmp_path / "repos.yaml"
    repo_cfg.write_text(
        "repositories:\n"
        "  alert-svc:\n"
        f"    github_repo: acme/alert-svc\n"
        f"    local_path: {repo_dir}\n",
        encoding="utf-8",
    )
    cfg = Settings(
        data_dir=tmp_path / "data",
        repo_registry_path=repo_cfg,
        llm_api_key="dummy",
        enable_ops_mcp=False,
    )
    cfg.ensure_dirs()
    orch = DelegationOrchestrator(cfg, llm=ScriptedHarnessLLM([]))
    app = create_app(settings=cfg, orchestrator=orch, enable_background_daemons=False)
    client = TestClient(app)

    resp = client.post(
        "/webhooks/alert",
        json={
            "target_repo": "alert-svc",
            "alert_name": "HighOrderDiscountDrift",
            "severity": "P1",
            "error_message": "apply_discount returned 120 instead of 80",
            "stack_trace": "File 'src/pricing.py', line 2, in apply_discount",
            "auto_execute": False,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["target_repo"] == "alert-svc"
    saved = orch.store.get_task(body["task_id"])
    assert saved is not None
    assert saved.source_channel == "alert_webhook"

    # Polyglot anti-tampering checks (JS/TS & Go)
    js_skip = "--- a/a.test.ts\n+++ b/a.test.ts\n@@ -1 +1 @@\n+test.skip('broken', () => {})\n"
    go_skip = "--- a/a_test.go\n+++ b/a_test.go\n@@ -1 +1 @@\n+\tt.Skip(\"flaky\")\n"
    assert scan_test_diff_for_tampering(js_skip)
    assert scan_test_diff_for_tampering(go_skip)
