"""Interactive Sandbox Coding Agent Loop (Spec v2.0 Section 2 & 3.3)."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from digital_employee.context.decision_ledger import DecisionLedger
from digital_employee.llm.client import LLMClient
from digital_employee.models.contract import TaskContract, UnresolvedDecision
from digital_employee.sandbox.worktree_runner import WorktreeSandbox
from digital_employee.security.dlp import PolicyViolationError, wrap_untrusted_input

CODING_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List tracked files in the sandbox worktree, optionally filtered by directory prefix.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prefix": {"type": "string", "description": "Directory prefix, e.g. 'src/mcp_server'"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "Search regex or literal pattern across tracked files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path_prefix": {"type": "string", "default": ""},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read lines from a repository file (1-indexed inclusive).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer", "default": 1},
                    "end_line": {"type": "integer", "default": 220},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace an exact snippet in an existing file within permitted_scopes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_snippet": {"type": "string"},
                    "replacement_snippet": {"type": "string"},
                },
                "required": ["path", "old_snippet", "replacement_snippet"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file within permitted_scopes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_pytest",
            "description": "Run pytest inside the isolated sandbox on specific test files or node IDs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "targets": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "e.g. ['tests/unit/test_trace_service.py::TestTraceService::test_get_stage_timings']",
                    }
                },
                "required": ["targets"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_diff",
            "description": "Inspect current unified git diff against base_commit_sha.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_clarification",
            "description": "Suspend execution ONLY if a critical business requirement contradiction is discovered that cannot be resolved from code/tests.",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}},
                    "default_assumption": {"type": "string"},
                    "impact_scope": {"type": "string"},
                },
                "required": ["question", "options", "default_assumption", "impact_scope"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish_task",
            "description": "Call when the fix and regression test are complete and verified via run_pytest.",
            "parameters": {
                "type": "object",
                "properties": {
                    "repro_test_target": {
                        "type": "string",
                        "description": "Exact pytest node id or file that FAILS on base_commit_sha and PASSES now.",
                    },
                    "commit_message": {"type": "string"},
                    "root_cause": {"type": "string"},
                    "summary": {"type": "string"},
                },
                "required": ["repro_test_target", "commit_message", "root_cause", "summary"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish_noop",
            "description": (
                "Call ONLY when the task's target condition is ALREADY 100% satisfied on base_commit_sha "
                "with ZERO code changes needed (e.g., delegator asked to run & fix failing tests, and `run_pytest` "
                "proved the entire suite already passes with exit_code=0). NEVER use this to skip implementing a requested feature or fixing an actual failing test."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "verified_test_targets": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Test directory/files verified to pass with exit_code=0 (e.g. ['tests']).",
                    },
                    "summary": {
                        "type": "string",
                        "description": "Concise verification summary (e.g. '604 passed, 7 skipped, 0 failed on base commit').",
                    },
                },
                "required": ["verified_test_targets", "summary"],
            },
        },
    },
]

CODING_SYSTEM_PROMPT = """You are the Sandbox Coding Agent of a Dependable Engineering Delegation System.
Work methodically inside the isolated git worktree to satisfy the `TaskContract`.

MANDATORY WORKFLOW:
1. Inspect the relevant test(s) and production source files first (`read_file` / `search_code`).
2. Run the failing test (or write a focused regression unit test first) with `run_pytest` to observe the exact failure before/while fixing.
3. Apply minimal, surgical code fixes strictly inside `permitted_scopes`. Preserve unrelated comments and docstrings.
4. NEVER weaken existing tests: do NOT delete `assert` lines, do NOT add `@pytest.mark.skip` / `xfail`, and do NOT swallow exceptions.
5. Re-run `run_pytest` on both the target regression test and its broader test module to verify zero regressions.
6. Call `finish_task` with the exact `repro_test_target` (prefer specific pytest node ID like `tests/unit/test_foo.py::TestBar::test_baz`) that fails on `base_commit_sha` and passes on your fix.
7. **NO-OP CLEAN EXIT**: If the task asks to run/fix failing tests (or fix a bug that no longer reproduces) and `run_pytest` proves the suite is ALREADY 100% green (`exit_code=0`, 0 failures) on `base_commit_sha` with zero file modifications needed, do NOT call `request_clarification` and do NOT make spurious edits — call `finish_noop` directly with the verified test targets and summary!
"""


@dataclass
class CodingLoopOutcome:
    completed: bool
    is_noop: bool = False
    noop_test_targets: list[str] = field(default_factory=list)
    repro_test_target: str = ""
    commit_message: str = ""
    root_cause: str = ""
    summary: str = ""
    needs_clarification: UnresolvedDecision | None = None
    policy_violation: str | None = None
    cost_usd: float = 0.0
    steps_used: int = 0
    trace_log: list[str] = field(default_factory=list)


def _summarize_tool_start(fn_name: str, args: dict[str, Any]) -> tuple[str, str]:
    if fn_name == "read_file":
        return "📖", f"读取源码 `{args.get('path', '')}` (L{args.get('start_line', 1)}-{args.get('end_line', 220)})"
    if fn_name == "search_code":
        return "🔎", f"搜索符号 `{args.get('pattern', '')}`"
    if fn_name == "list_files":
        return "📂", f"扫描目录 `{args.get('prefix', '') or '/'}`"
    if fn_name == "edit_file":
        return "✏️", f"修改文件 `{args.get('path', '')}`"
    if fn_name == "write_file":
        return "📝", f"写入文件 `{args.get('path', '')}`"
    if fn_name == "run_pytest":
        targets = ", ".join(str(x) for x in (args.get("targets") or []))
        return "🧪", f"运行单测 `{targets}`"
    if fn_name == "git_diff":
        return "📋", "检查当前 git diff"
    return "⚙️", f"调用工具 `{fn_name}`"


def _summarize_tool_result(fn_name: str, tool_output: str) -> tuple[str, str]:
    if tool_output.startswith("POLICY_VIOLATION:"):
        return "🚫", tool_output[:200]
    if tool_output.startswith("TOOL_ERROR"):
        return "⚠️", tool_output[:200]
    if fn_name == "run_pytest":
        lines = [ln.strip() for ln in tool_output.splitlines() if ln.strip()]
        exit_part = lines[0] if lines else "exit_code=?"
        tail = lines[-1] if len(lines) > 1 else ""
        ok = exit_part == "exit_code=0"
        return ("✅" if ok else "❌"), f"pytest {'通过' if ok else '未通过'} ({exit_part}) · {tail[:120]}"
    if fn_name in ("edit_file", "write_file"):
        return "✅", tool_output.splitlines()[0][:140] if tool_output else "已更新文件"
    return "✓", ""


class SandboxCodingAgent:
    """Executes the bounded interactive probe -> edit -> test loop inside `WorktreeSandbox`."""

    def __init__(self, llm: LLMClient, max_steps: int = 18) -> None:
        self.llm = llm
        self.max_steps = max_steps

    def run(
        self,
        *,
        contract: TaskContract,
        sandbox: WorktreeSandbox,
        prior_feedback: str = "",
        followup_comment: str = "",
        remaining_budget_usd: float = 1.50,
        on_step: Callable[[dict[str, Any]], None] | None = None,
        session_state_path: Path | None = None,
    ) -> CodingLoopOutcome:
        del session_state_path

        def _emit(
            *,
            step: int,
            kind: str,
            icon: str,
            title: str,
            detail: str = "",
            step_cost_usd: float = 0.0,
        ) -> None:
            if on_step is not None:
                try:
                    on_step(
                        {
                            "step": step,
                            "max_steps": self.max_steps,
                            "kind": kind,
                            "icon": icon,
                            "title": title,
                            "detail": detail[:800],
                            "step_cost_usd": step_cost_usd,
                        }
                    )
                except Exception:
                    pass
        ledger = DecisionLedger.from_repo(
            Path(sandbox.profile.local_path), sandbox.profile.decision_ledger_path
        )
        prompt_parts = [
            "## Accountable TaskContract\n" + contract.model_dump_json(indent=2),
            ledger.render_truth_hierarchy_block(contract.permitted_scopes),
        ]
        if prior_feedback:
            prompt_parts.append(
                "## Previous Verification Gate Feedback (Must Resolve)\n"
                + wrap_untrusted_input("verification_gate_feedback", prior_feedback)
            )
        if followup_comment:
            prompt_parts.append(
                "## Human PR Review Follow-Up Instruction\n"
                + wrap_untrusted_input("pr_review_comment", followup_comment)
            )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": CODING_SYSTEM_PROMPT},
            {"role": "user", "content": "\n\n".join(prompt_parts)},
        ]

        outcome = CodingLoopOutcome(completed=False)

        for step in range(1, self.max_steps + 1):
            if outcome.cost_usd >= remaining_budget_usd:
                outcome.trace_log.append(
                    f"[Step {step}] Budget ceiling reached (${outcome.cost_usd:.4f} >= ${remaining_budget_usd:.4f})."
                )
                return outcome

            _emit(
                step=step,
                kind="thinking",
                icon="🧠",
                title=f"Step {step}/{self.max_steps} · 模型推理规划中 ({self.llm.settings.reasoning_model})…",
            )

            assistant_msg, metrics = self.llm.chat(
                messages,
                model=self.llm.settings.reasoning_model,
                tools=CODING_TOOLS,
                max_tokens=4096,
            )
            outcome.cost_usd = round(outcome.cost_usd + metrics.estimated_cost_usd, 6)
            outcome.steps_used = step
            messages.append(assistant_msg)

            thought_raw = str(
                assistant_msg.get("reasoning_content") or assistant_msg.get("content") or ""
            ).strip()
            if thought_raw:
                one_line_thought = " ".join(thought_raw.split())[:180]
                _emit(
                    step=step,
                    kind="thought",
                    icon="💭",
                    title=f"Step {step}/{self.max_steps} · 思考摘要",
                    detail=one_line_thought,
                    step_cost_usd=metrics.estimated_cost_usd,
                )

            tool_calls = assistant_msg.get("tool_calls") or []
            if not tool_calls:
                messages.append(
                    {
                        "role": "user",
                        "content": "Please invoke a tool (`read_file`, `edit_file`, `run_pytest`, or `finish_task` when verified).",
                    }
                )
                continue

            for call in tool_calls:
                fn_name = call["function"]["name"]
                raw_args = call["function"].get("arguments") or "{}"
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except json.JSONDecodeError as exc:
                    tool_output = f"Invalid JSON tool arguments: {exc}"
                    messages.append(
                        {"role": "tool", "tool_call_id": call["id"], "content": tool_output}
                    )
                    continue

                outcome.trace_log.append(f"[Step {step}] {fn_name}({json.dumps(args, ensure_ascii=False)[:240]})")

                if fn_name == "request_clarification":
                    _emit(
                        step=step,
                        kind="clarify",
                        icon="⏸️",
                        title=f"Step {step}/{self.max_steps} · 发现关键歧义，请求你拍板",
                        detail=str(args.get("question", "")),
                    )
                    outcome.needs_clarification = UnresolvedDecision(
                        question=str(args.get("question", "")),
                        options=[str(o) for o in (args.get("options") or [])],
                        default_assumption=str(args.get("default_assumption", "Proceed conservatively")),
                        impact_scope=str(args.get("impact_scope", "task scope")),
                    )
                    return outcome

                if fn_name == "finish_noop":
                    _emit(
                        step=step,
                        kind="finish_noop",
                        icon="✅",
                        title=f"Step {step}/{self.max_steps} · 基线已满足条件，发起 No-Op 校验",
                        detail=str(args.get("summary", "")),
                    )
                    outcome.completed = True
                    outcome.is_noop = True
                    outcome.noop_test_targets = [
                        str(t).strip()
                        for t in (args.get("verified_test_targets") or ["tests"])
                        if str(t).strip()
                    ] or ["tests"]
                    outcome.summary = str(
                        args.get("summary") or "All tests already passed on base commit; zero changes required."
                    ).strip()
                    return outcome

                if fn_name == "finish_task":
                    _emit(
                        step=step,
                        kind="finish_task",
                        icon="🏁",
                        title=f"Step {step}/{self.max_steps} · 编码完成，提交进入硬核证据门禁",
                        detail=f"回归目标: {args.get('repro_test_target', '')} | {args.get('summary', '')}",
                    )
                    outcome.completed = True
                    outcome.repro_test_target = str(
                        args.get("repro_test_target") or contract.target_repro_test
                    ).strip()
                    outcome.commit_message = str(
                        args.get("commit_message") or f"fix: {contract.objective[:65]}"
                    ).strip()
                    outcome.root_cause = str(args.get("root_cause") or "").strip()
                    outcome.summary = str(args.get("summary") or "").strip()
                    return outcome

                t_icon, t_title = _summarize_tool_start(fn_name, args)
                _emit(
                    step=step,
                    kind="tool_run",
                    icon=t_icon,
                    title=f"Step {step}/{self.max_steps} · {t_title}",
                )

                tool_output = self._dispatch_tool(sandbox, contract, fn_name, args, outcome)
                r_icon, r_title = _summarize_tool_result(fn_name, tool_output)
                if r_title:
                    _emit(
                        step=step,
                        kind="tool_result",
                        icon=r_icon,
                        title=f"Step {step}/{self.max_steps} · {r_title}",
                        detail=tool_output[-500:] if fn_name == "run_pytest" else "",
                    )

                if outcome.policy_violation:
                    # Still feed violation back once unless repeated; let agent correct path if simply outside scope
                    outcome.trace_log.append(f"  -> POLICY BLOCKED: {outcome.policy_violation}")
                    outcome.policy_violation = None

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": tool_output[:8000],
                    }
                )

            if self.max_steps - step == 3:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "⚠️ STEP BUDGET WARNING: Only 3 steps remaining in this attempt! "
                            "Stop exploring unrelated files. Verify your current fix with `run_pytest` and call `finish_task` immediately."
                        ),
                    }
                )

        outcome.trace_log.append(f"Max coding steps ({self.max_steps}) exhausted without finish_task.")
        return outcome

    def _dispatch_tool(
        self,
        sandbox: WorktreeSandbox,
        contract: TaskContract,
        fn_name: str,
        args: dict[str, Any],
        outcome: CodingLoopOutcome,
    ) -> str:
        try:
            if fn_name == "list_files":
                files = sandbox.list_files(prefix=str(args.get("prefix", "")))
                return "\n".join(files) if files else "(no files)"
            if fn_name == "search_code":
                return sandbox.search_code(
                    pattern=str(args["pattern"]),
                    path_prefix=str(args.get("path_prefix", "")),
                )
            if fn_name == "read_file":
                return sandbox.read_file(
                    rel_path=str(args["path"]),
                    start_line=int(args.get("start_line", 1)),
                    end_line=int(args.get("end_line", 220)),
                )
            if fn_name == "edit_file":
                return sandbox.write_or_edit_file(
                    rel_path=str(args["path"]),
                    old_snippet=str(args["old_snippet"]),
                    replacement_snippet=str(args["replacement_snippet"]),
                )
            if fn_name == "write_file":
                return sandbox.write_or_edit_file(
                    rel_path=str(args["path"]),
                    new_content=str(args["content"]),
                )
            if fn_name == "run_pytest":
                targets = [str(t) for t in (args.get("targets") or []) if str(t).strip()]
                res = sandbox.run_pytest(targets)
                return f"exit_code={res.exit_code}\n{res.combined_output}"
            if fn_name == "git_diff":
                diff = sandbox.git_diff(contract.base_commit_sha)
                return diff if diff.strip() else "(no changes yet)"
            return f"Unknown tool: {fn_name}"
        except PolicyViolationError as exc:
            outcome.policy_violation = str(exc)
            return f"POLICY_VIOLATION: {exc}. Stay strictly within permitted_scopes={contract.permitted_scopes}."
        except Exception as exc:
            return f"TOOL_ERROR ({type(exc).__name__}): {exc}"
