"""Pluggable CodingRuntime abstraction: Native White-Box Kernel (`mewcode`) vs External CLI."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from digital_employee.llm.client import LLMClient
from digital_employee.models.contract import TaskContract, UnresolvedDecision
from digital_employee.sandbox.worktree_runner import WorktreeSandbox, build_sandbox_env
from digital_employee.security.dlp import redact_sensitive_text, wrap_untrusted_input
from digital_employee.settings import Settings


@dataclass
class CodingLoopOutcome:
    """Structured execution outcome returned by any `CodingRuntime` implementation."""

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


@runtime_checkable
class CodingRuntime(Protocol):
    """Unified execution contract for sandbox coding engines."""

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
    ) -> CodingLoopOutcome: ...


class ExternalCLIRuntime:
    """Reference adapter wrapping an external headless CLI (e.g. `claude -p --output-format stream-json`).

    Provided as an architectural benchmark baseline alongside `NativeKernelRuntime`.
    Note: External black-box CLIs cannot perform mid-flight SQLite pause/resume, kernel-level
    contract scope hooks, or intranet DLP scrubbing prior to cloud transmission.
    """

    def __init__(self, cli_bin: str = "claude", timeout_seconds: int = 300) -> None:
        self.cli_bin = cli_bin
        self.timeout_seconds = timeout_seconds

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
        del remaining_budget_usd, on_step, session_state_path
        outcome = CodingLoopOutcome(completed=False)
        resolved_bin = shutil.which(self.cli_bin)
        if not resolved_bin:
            outcome.trace_log.append(f"External CLI binary '{self.cli_bin}' not found in PATH.")
            return outcome

        prompt_parts = [
            "Satisfy the following TaskContract strictly within `permitted_scopes`.",
            "When complete, output a final JSON block: "
            '`{"repro_test_target": "...", "commit_message": "...", "root_cause": "...", "summary": "..."}`.',
            redact_sensitive_text(contract.model_dump_json(indent=2)),
        ]
        if prior_feedback:
            prompt_parts.append(wrap_untrusted_input("verification_gate_feedback", prior_feedback))
        if followup_comment:
            prompt_parts.append(wrap_untrusted_input("pr_review_comment", followup_comment))

        cmd = [resolved_bin, "-p", "\n\n".join(prompt_parts), "--output-format", "stream-json"]
        env = build_sandbox_env(sandbox.worktree_path)
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(sandbox.worktree_path),
                env=env,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            outcome.trace_log.append(f"External CLI timed out after {self.timeout_seconds}s.")
            return outcome

        final_text = ""
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                final_text += line + "\n"
                continue
            evt_type = evt.get("type")
            if evt_type == "tool_use":
                outcome.steps_used += 1
                outcome.trace_log.append(
                    f"[CLI Step {outcome.steps_used}] {evt.get('tool_name')}({str(evt.get('args'))[:180]})"
                )
            elif evt_type in ("assistant", "result"):
                final_text += str(evt.get("text") or evt.get("result") or "")

        if proc.returncode != 0:
            outcome.trace_log.append(f"External CLI exited with code {proc.returncode}: {proc.stderr[:400]}")
            return outcome

        # Attempt structured summary extraction
        first, last = final_text.find("{"), final_text.rfind("}")
        if first != -1 and last > first:
            try:
                payload = json.loads(final_text[first : last + 1])
                outcome.completed = True
                outcome.repro_test_target = str(
                    payload.get("repro_test_target") or contract.target_repro_test
                ).strip()
                outcome.commit_message = str(
                    payload.get("commit_message") or f"fix: {contract.objective[:65]}"
                ).strip()
                outcome.root_cause = str(payload.get("root_cause") or "").strip()
                outcome.summary = str(payload.get("summary") or "").strip()
            except json.JSONDecodeError:
                pass
        return outcome


def create_coding_runtime(
    settings: Settings,
    *,
    llm: LLMClient | None = None,
) -> CodingRuntime:
    """Instantiate the configured sandbox coding runtime (`kernel` | `external_cli`)."""
    mode = getattr(settings, "coding_runtime", "kernel")
    if mode == "external_cli":
        cli_bin = getattr(settings, "external_coding_cli", "claude")
        return ExternalCLIRuntime(cli_bin=cli_bin)

    from digital_employee.agents.kernel_runtime import NativeKernelRuntime

    return NativeKernelRuntime(settings=settings, injected_llm=llm)
