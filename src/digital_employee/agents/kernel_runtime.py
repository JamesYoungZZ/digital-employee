"""Native White-Box Agent Kernel Runtime bridging `digital_employee.kernel` with `TaskContract`."""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from digital_employee.agents.runtime import CodingLoopOutcome
from digital_employee.context.decision_ledger import DecisionLedger

if TYPE_CHECKING:
    from digital_employee.memory.claude_mem import ClaudeMemService
    from digital_employee.storage.durable_store import DurableStore
from digital_employee.kernel.agent import (
    Agent,
    CompactNotification,
    ErrorEvent,
    ThinkingText,
    ToolResultEvent,
    ToolUseEvent,
    TurnComplete,
)
from digital_employee.kernel.agents.loader import AgentLoader
from digital_employee.kernel.agents.task_manager import TaskManager
from digital_employee.kernel.agents.trace import TraceManager
from digital_employee.kernel.client import (
    AuthenticationError,
    LLMError,
    NetworkError,
    RateLimitError,
    create_client,
)
from digital_employee.kernel.client import (
    LLMClient as KernelLLMClient,
)
from digital_employee.kernel.config import MCPServerConfig, ProviderConfig
from digital_employee.kernel.conversation import ConversationManager, Message
from digital_employee.kernel.filehistory.history import FileHistory
from digital_employee.kernel.mcp import MCPManager
from digital_employee.kernel.mcp.loading_strategy import decide_and_apply
from digital_employee.kernel.memory.auto_memory import MemoryManager
from digital_employee.kernel.memory.instructions import load_instructions
from digital_employee.kernel.memory.session import (
    SessionRecord,
    make_compact_boundary,
    records_to_messages,
)
from digital_employee.kernel.permissions import (
    DangerousCommandDetector,
    PathSandbox,
    PermissionChecker,
    PermissionMode,
    RuleEngine,
)
from digital_employee.kernel.sandbox import SandboxConfig, create_sandbox
from digital_employee.kernel.skills.loader import SkillLoader
from digital_employee.kernel.tools import ToolRegistry, create_default_registry
from digital_employee.kernel.tools.agent_tool import AgentTool
from digital_employee.kernel.tools.base import (
    StreamEnd,
    StreamEvent,
    TextDelta,
    Tool,
    ToolCallComplete,
    ToolResult,
)
from digital_employee.kernel.tools.impl.tool_search import ToolSearchTool
from digital_employee.kernel.tools.load_skill import LoadSkill
from digital_employee.kernel.tools.mcp_call import McpCallTool
from digital_employee.llm.client import LLMClient as HarnessLLMClient
from digital_employee.llm.client import TransientLLMError
from digital_employee.models.contract import TaskContract, UnresolvedDecision
from digital_employee.sandbox.worktree_runner import WorktreeSandbox, build_sandbox_env
from digital_employee.security.dlp import (
    check_path_policy,
    normalize_rel_path,
    redact_sensitive_text,
    wrap_untrusted_input,
)
from digital_employee.settings import Settings

# ---------------------------------------------------------------------------
# 1. Contract-Scoped PathSandbox (Enforces `permitted_scopes` + `forbidden_paths`)
# ---------------------------------------------------------------------------


class ContractPathSandbox(PathSandbox):
    """Extends kernel `PathSandbox` to enforce hard `forbidden_paths` while allowing cross-module fixes."""

    def __init__(
        self,
        worktree_root: Path,
        *,
        permitted_scopes: list[str],
        forbidden_paths: list[str],
        extra_allow_dirs: list[str] | None = None,
    ) -> None:
        super().__init__(
            str(worktree_root),
            extra_allowed=extra_allow_dirs or [],
            deny_write=forbidden_paths,
        )
        self._worktree_root = worktree_root.resolve()
        self._permitted_scopes = list(permitted_scopes)
        self._forbidden_paths = list(forbidden_paths)
        self._extra_allow_dirs = [Path(p).resolve() for p in (extra_allow_dirs or []) if p]

    def check_deny_write(self, path: str) -> tuple[bool, str]:
        ok, err = super().check_deny_write(path)
        if not ok:
            return False, err

        real_path, resolve_err = self._resolve(path)
        if real_path is None:
            return False, resolve_err

        # Allow persistent repository memory directories configured outside the ephemeral worktree
        for allowed_dir in self._extra_allow_dirs:
            try:
                real_path.relative_to(allowed_dir)
                return True, ""
            except ValueError:
                continue

        # Allow internal `.de/` session/spill/memory state inside the worktree
        try:
            rel = real_path.relative_to(self._worktree_root)
        except ValueError:
            return False, f"Write outside sandbox worktree is forbidden: {path}"

        rel_posix = rel.as_posix()
        if rel_posix == ".de" or rel_posix.startswith(".de/"):
            return True, ""

        try:
            norm_rel = normalize_rel_path(rel_posix)
        except Exception as exc:
            return False, str(exc)

        allowed, reason = check_path_policy(
            norm_rel,
            permitted_scopes=self._permitted_scopes,
            forbidden_paths=self._forbidden_paths,
            enforce_scopes=False,
        )
        if not allowed:
            return False, reason
        return True, ""


# ---------------------------------------------------------------------------
# 2. DLP-Scrubbing & Cache-Aware Cost Metering Kernel LLM Client
# ---------------------------------------------------------------------------


def _estimate_turn_cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_creation: int = 0,
) -> float:
    """Compute cache-aware USD cost per turn (compatible with DeepSeek KV-cache & Anthropic)."""
    lower = model.lower()
    if "claude" in lower:
        in_rate, cache_hit_rate, cache_write_rate, out_rate = 3.00, 0.30, 3.75, 15.00
    else:
        is_pro = "pro" in lower or "reasoner" in lower or "gpt-4" in lower
        in_rate = 0.55 if is_pro else 0.15
        cache_hit_rate = 0.07 if is_pro else 0.015
        cache_write_rate = in_rate
        out_rate = 2.19 if is_pro else 0.60

    cost = (
        input_tokens * in_rate
        + cache_read * cache_hit_rate
        + cache_creation * cache_write_rate
        + output_tokens * out_rate
    ) / 1_000_000.0
    return round(cost, 6)


class DLPKernelLLMClient(KernelLLMClient):
    """Wraps a kernel streaming `LLMClient` with DLP outbound scrubbing & USD cost tracking."""

    def __init__(
        self,
        inner: KernelLLMClient,
        *,
        model: str,
        scrub_outbound: bool,
    ) -> None:
        self._inner = inner
        self._model = model
        self._scrub_outbound = scrub_outbound
        self.accumulated_cost_usd: float = 0.0

    def set_max_output_tokens(self, n: int) -> None:
        self._inner.set_max_output_tokens(n)

    def _scrub_conversation(self, conversation: ConversationManager) -> ConversationManager:
        if not self._scrub_outbound:
            return conversation
        cloned = copy.copy(conversation)
        scrubbed_history: list[Message] = []
        for msg in conversation.history:
            m = copy.copy(msg)
            if m.content:
                m.content = redact_sensitive_text(m.content)
            if m.tool_results:
                new_trs = []
                for tr in m.tool_results:
                    tr_copy = copy.copy(tr)
                    if tr_copy.content:
                        tr_copy.content = redact_sensitive_text(tr_copy.content)
                    new_trs.append(tr_copy)
                m.tool_results = new_trs
            scrubbed_history.append(m)
        cloned.history = scrubbed_history
        return cloned

    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        safe_conv = self._scrub_conversation(conversation)
        safe_sys = redact_sensitive_text(system) if (self._scrub_outbound and system) else system
        async for evt in self._inner.stream(safe_conv, system=safe_sys, tools=tools):
            if isinstance(evt, StreamEnd):
                self.accumulated_cost_usd = round(
                    self.accumulated_cost_usd
                    + _estimate_turn_cost_usd(
                        self._model,
                        input_tokens=evt.input_tokens,
                        output_tokens=evt.output_tokens,
                        cache_read=evt.cache_read,
                        cache_creation=evt.cache_creation,
                    ),
                    6,
                )
            yield evt


class InjectedHarnessLLMAdapter(KernelLLMClient):
    """Bridges an injected synchronous `HarnessLLMClient` (used in deterministic offline tests) into `KernelLLMClient`."""

    def __init__(self, harness_llm: HarnessLLMClient) -> None:
        self._llm = harness_llm
        self._max_tokens = 4096
        self.accumulated_cost_usd: float = 0.0

    def set_max_output_tokens(self, n: int) -> None:
        self._max_tokens = n

    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        from digital_employee.kernel.serialization import build_chat_completion_messages

        msgs: list[dict[str, Any]] = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.extend(build_chat_completion_messages(conversation.history))
        openai_tools: list[dict[str, Any]] | None = None
        if tools:
            openai_tools = []
            for t in tools:
                if "function" in t:
                    openai_tools.append(t)
                else:
                    openai_tools.append(
                        {
                            "type": "function",
                            "function": {
                                "name": t["name"],
                                "description": t.get("description", ""),
                                "parameters": t.get("parameters") or t.get("input_schema", {}),
                            },
                        }
                    )

        assistant_msg, metrics = self._llm.chat(
            msgs,
            tools=openai_tools,
            max_tokens=self._max_tokens,
        )
        self.accumulated_cost_usd = round(
            self.accumulated_cost_usd + metrics.estimated_cost_usd, 6
        )
        content = assistant_msg.get("content") or ""
        if content:
            yield TextDelta(text=str(content))

        raw_calls = assistant_msg.get("tool_calls") or []
        for idx, tc in enumerate(raw_calls):
            fn = tc.get("function") or {}
            raw_args = fn.get("arguments") or "{}"
            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            yield ToolCallComplete(
                tool_id=str(tc.get("id") or f"call_{idx}"),
                tool_name=str(fn.get("name") or ""),
                arguments=args,
            )

        yield StreamEnd(
            stop_reason="tool_use" if raw_calls else "end_turn",
            input_tokens=metrics.prompt_tokens,
            output_tokens=metrics.completion_tokens,
        )


# ---------------------------------------------------------------------------
# 3. Accountable Contract Lifecycle Tools (`finish_task`, `finish_noop`, `request_clarification`)
# ---------------------------------------------------------------------------


@dataclass
class _LifecycleSignal:
    kind: str = ""  # "finish_task" | "finish_noop" | "clarification"
    repro_test_target: str = ""
    commit_message: str = ""
    root_cause: str = ""
    summary: str = ""
    noop_test_targets: list[str] | None = None
    clarification: UnresolvedDecision | None = None


class _FinishTaskParams(BaseModel):
    repro_test_target: str = Field(
        description="Exact test file or node ID (e.g. 'tests/test_x.py::test_bug') that FAILS on base_commit_sha and PASSES now."
    )
    commit_message: str = Field(description="Conventional commit message (e.g. 'fix: handle empty payload').")
    root_cause: str = Field(description="Concise technical root cause explanation.")
    summary: str = Field(description="Summary of code changes and verification performed.")


class FinishTaskTool(Tool):
    name = "finish_task"
    description = (
        "Call ONLY after verifying your code fix and regression test in the sandbox. "
        "Submits the verified fix for independent Fail-to-Pass EvidenceGate evaluation."
    )
    params_model = _FinishTaskParams
    category = "read"

    def __init__(self, signal: _LifecycleSignal) -> None:
        self._signal = signal

    async def execute(self, params: _FinishTaskParams) -> ToolResult:
        self._signal.kind = "finish_task"
        self._signal.repro_test_target = params.repro_test_target.strip()
        self._signal.commit_message = params.commit_message.strip()
        self._signal.root_cause = params.root_cause.strip()
        self._signal.summary = params.summary.strip()
        return ToolResult(output="Task marked finished; handing off to EvidenceGate.")


class _FinishNoopParams(BaseModel):
    verified_test_targets: list[str] = Field(
        description="Test targets verified to pass with exit code 0 on base_commit_sha."
    )
    summary: str = Field(description="Verification or Ops diagnostic summary explaining why zero code edits are required.")


class FinishNoopTool(Tool):
    name = "finish_noop"
    description = (
        "Call ONLY when the target condition is ALREADY 100% satisfied on base_commit_sha with ZERO code changes needed "
        "(e.g. all tests already pass, or Ops alert diagnosis confirmed non-code external transient issue)."
    )
    params_model = _FinishNoopParams
    category = "read"

    def __init__(self, signal: _LifecycleSignal) -> None:
        self._signal = signal

    async def execute(self, params: _FinishNoopParams) -> ToolResult:
        self._signal.kind = "finish_noop"
        self._signal.noop_test_targets = [t.strip() for t in params.verified_test_targets if t.strip()] or ["tests"]
        self._signal.summary = params.summary.strip()
        return ToolResult(output="No-Op exit recorded; handing off for deterministic verification.")


class _ClarifyParams(BaseModel):
    question: str = Field(description="Clear, concise business decision question for the human delegator.")
    options: list[str] = Field(description="2-4 concrete clickable option strings.")
    default_assumption: str = Field(description="Safe default assumption if human clicks 'Use Default'.")
    impact_scope: str = Field(description="Module or behavior affected by this decision.")


class RequestClarificationTool(Tool):
    name = "request_clarification"
    description = (
        "Suspend execution ONLY if a critical business requirement contradiction is discovered in code/tests "
        "that cannot be resolved from the Truth Hierarchy."
    )
    params_model = _ClarifyParams
    category = "read"

    def __init__(self, signal: _LifecycleSignal) -> None:
        self._signal = signal

    async def execute(self, params: _ClarifyParams) -> ToolResult:
        self._signal.kind = "clarification"
        self._signal.clarification = UnresolvedDecision(
            question=params.question.strip(),
            options=[o.strip() for o in params.options if o.strip()],
            default_assumption=params.default_assumption.strip() or "Proceed conservatively",
            impact_scope=params.impact_scope.strip() or "task scope",
        )
        return ToolResult(output="Clarification requested; suspending kernel session for human micro-confirmation.")


# ---------------------------------------------------------------------------
# 4. Session Checkpoint Persistence (`DurableStore` JSONL Pause & Resume)
# ---------------------------------------------------------------------------


def save_conversation_checkpoint(
    path: Path,
    conversation: ConversationManager,
    boundaries: list[tuple[str, list[Message]]] | None = None,
) -> None:
    """Serialize `ConversationManager` history (and any compaction boundaries) to JSONL."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if boundaries:
        summary, keep = boundaries[-1]
        lines.append(make_compact_boundary(summary, keep).to_jsonl())
    else:
        for msg in conversation.history:
            for rec in SessionRecord.from_message(msg):
                lines.append(rec.to_jsonl())
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def load_conversation_checkpoint(path: Path) -> ConversationManager | None:
    """Restore a paused `ConversationManager` from a JSONL checkpoint."""
    if not path.is_file():
        return None
    records: list[SessionRecord] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        rec = SessionRecord.from_jsonl(line)
        if rec is not None:
            records.append(rec)
    if not records:
        return None
    conv = ConversationManager()
    conv.history = records_to_messages(records)
    conv.env_injected = True
    conv.ltm_injected = True
    return conv


# ---------------------------------------------------------------------------
# 5. NativeKernelRuntime (Main Entrypoint)
# ---------------------------------------------------------------------------

KERNEL_CONTRACT_DIRECTIVE = """
## Autonomous Digital Employee — Mandatory Execution Protocol (Dev + QA + Ops)
You are running inside an isolated Git Worktree governed by an accountable `TaskContract`.

1. **Probe, Recall Memory & Diagnose First**:
   - Check the `claude-mem` Historical Memory Index below or call `mem_search` / `mem_get_observations` (via `ToolSearch` / `mcp_call` or direct MCP tools) to recall past bugfixes, test gotchas, and Code Review lessons in this repository.
   - Use `Glob`, `Grep`, `ReadFile` (read-only calls batch concurrently!), or delegate broad repo exploration to the `Agent` (`explore`) subagent. Read files BEFORE calling `EditFile` or `WriteFile`.
2. **Ops / Incident Triage**: If investigating an alert or runtime failure, check available MCP diagnostics (`ToolSearch` / `mcp_call`) and skills (`LoadSkill`), correlate stack traces with source code, and determine whether a code fix or verified `finish_noop` diagnosis is warranted.
3. **TDD & Surgical Modification**:
   - Treat `permitted_scopes` as recommended starting directories; if root-cause analysis requires updating related business modules or shared helpers outside `permitted_scopes`, you MAY modify them cleanly, while NEVER touching `forbidden_paths`.
   - Write or run a focused reproduction test via `Bash` (using the repository's configured test runner) to observe the failure and verify your fix passes with zero regressions.
   - NEVER weaken tests: do NOT delete assertions, do NOT add `@skip` / `xfail` / `.skip`, and do NOT swallow exceptions.
4. **Accountable Exit**:
   - When the fix + regression test pass, call `finish_task` with the exact `repro_test_target` that FAILS on `base_commit_sha` and PASSES on your fix.
   - If the target suite/condition is ALREADY 100% green on `base_commit_sha` with zero file modifications, call `finish_noop`.
   - Call `request_clarification` ONLY for genuine, unresolvable business requirement contradictions.
"""


class NativeKernelRuntime:
    """Drives the white-box `digital_employee.kernel` inside `WorktreeSandbox`."""

    def __init__(
        self,
        settings: Settings,
        *,
        injected_llm: HarnessLLMClient | None = None,
        mcp_servers: list[MCPServerConfig] | None = None,
        store: DurableStore | None = None,
        claude_mem: ClaudeMemService | None = None,
    ) -> None:
        import sys

        from digital_employee.memory.claude_mem import ClaudeMemService as _ClaudeMemService

        self.settings = settings
        self.injected_llm = injected_llm
        self.store = store
        self.claude_mem = claude_mem or _ClaudeMemService(
            settings.claude_mem_db_path,
            external_url=settings.claude_mem_url,
            enabled=settings.enable_claude_mem,
        )
        self._explicit_mcp_override = mcp_servers is not None
        if mcp_servers is not None:
            self.mcp_servers = list(mcp_servers)
        elif settings.enable_ops_mcp and injected_llm is None:
            self.mcp_servers = [
                MCPServerConfig(
                    name="ops-diagnostics",
                    command=sys.executable,
                    args=["-m", "digital_employee.mcp_servers.ops_diagnostics"],
                )
            ]
        else:
            self.mcp_servers = []
        self.max_turns = settings.max_coding_steps

    def _build_provider_config(self) -> ProviderConfig:
        base_url = self.settings.llm_base_url.rstrip("/")
        model = self.settings.reasoning_model
        lower_url = base_url.lower()
        lower_model = model.lower()
        if "anthropic" in lower_url or lower_model.startswith("claude"):
            protocol = "anthropic"
        else:
            protocol = "openai-compat"
        thinking = "reasoner" in lower_model or "r1" in lower_model
        return ProviderConfig(
            name="digital-employee-primary",
            protocol=protocol,
            base_url=base_url,
            model=model,
            api_key=self.settings.llm_api_key,
            thinking=thinking,
        )

    def _build_os_sandbox_config(
        self, sandbox: WorktreeSandbox, contract: TaskContract
    ) -> SandboxConfig:
        del contract
        wt = sandbox.worktree_path.resolve()
        repo_mem = (self.settings.repo_memory_root / sandbox.profile.repo_id).resolve()
        allow_write: list[str] = [
            str(wt),
            str(repo_mem),
            "/tmp",
            "/private/tmp",
        ]
        deny_write = [
            str((wt / fp.rstrip("/")).resolve()) for fp in sandbox.profile.forbidden_paths
        ]
        deny_write.append(str((wt / ".git").resolve()))
        return SandboxConfig(
            allow_write=allow_write,
            deny_write=deny_write,
            network_enabled=False,
        )

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
        try:
            asyncio.get_running_loop()
            # If called from inside an active event loop, run in a dedicated worker thread
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                fut = pool.submit(
                    asyncio.run,
                    self._run_async(
                        contract=contract,
                        sandbox=sandbox,
                        prior_feedback=prior_feedback,
                        followup_comment=followup_comment,
                        remaining_budget_usd=remaining_budget_usd,
                        on_step=on_step,
                        session_state_path=session_state_path,
                    ),
                )
                return fut.result()
        except RuntimeError:
            return asyncio.run(
                self._run_async(
                    contract=contract,
                    sandbox=sandbox,
                    prior_feedback=prior_feedback,
                    followup_comment=followup_comment,
                    remaining_budget_usd=remaining_budget_usd,
                    on_step=on_step,
                    session_state_path=session_state_path,
                )
            )

    async def _run_async(
        self,
        *,
        contract: TaskContract,
        sandbox: WorktreeSandbox,
        prior_feedback: str,
        followup_comment: str,
        remaining_budget_usd: float,
        on_step: Callable[[dict[str, Any]], None] | None,
        session_state_path: Path | None,
    ) -> CodingLoopOutcome:
        work_dir = str(sandbox.worktree_path.resolve())
        provider = self._build_provider_config()

        if self.injected_llm is not None:
            kernel_client: DLPKernelLLMClient | InjectedHarnessLLMAdapter = (
                InjectedHarnessLLMAdapter(self.injected_llm)
            )
            protocol = "openai-compat"
        else:
            raw_client = create_client(provider)
            kernel_client = DLPKernelLLMClient(
                raw_client,
                model=provider.model,
                scrub_outbound=(self.settings.inference_mode == "cloud_api"),
            )
            protocol = provider.protocol

        # 1. Defense-in-depth PermissionChecker + ContractPathSandbox + OS Sandbox
        repo_mem_dir = self.settings.repo_memory_root / sandbox.profile.repo_id
        repo_mem_dir.mkdir(parents=True, exist_ok=True)
        path_sandbox = ContractPathSandbox(
            sandbox.worktree_path,
            permitted_scopes=contract.permitted_scopes,
            forbidden_paths=sandbox.profile.forbidden_paths,
            extra_allow_dirs=[str(repo_mem_dir)],
        )
        os_sandbox = create_sandbox() if self.settings.enable_os_sandbox else None
        os_sandbox_cfg = (
            self._build_os_sandbox_config(sandbox, contract)
            if os_sandbox and os_sandbox.available()
            else None
        )

        checker = PermissionChecker(
            detector=DangerousCommandDetector(),
            sandbox=path_sandbox,
            rule_engine=RuleEngine(
                user_rules_path=self.settings.data_dir / ".de" / "permissions.yaml",
                project_rules_path=sandbox.worktree_path / ".de" / "permissions.yaml",
                local_rules_path=sandbox.worktree_path / ".de" / "permissions.local.yaml",
            ),
            mode=PermissionMode.BYPASS,
            sandbox_enabled=bool(os_sandbox and os_sandbox_cfg),
        )

        # 2. ToolRegistry + OS-sandboxed Bash + FileHistory + Skills + Subagents + MCP + Contract Tools
        file_history = FileHistory(work_dir, sandbox.task_id)
        registry: ToolRegistry = create_default_registry(
            file_history=file_history, work_dir=work_dir
        )
        bash_tool = registry.get("Bash")
        if bash_tool is not None:
            bash_tool.env = build_sandbox_env(sandbox.worktree_path)  # type: ignore[attr-defined]
            if os_sandbox and os_sandbox_cfg:
                bash_tool.sandbox = os_sandbox  # type: ignore[attr-defined]
                bash_tool.sandbox_config = os_sandbox_cfg  # type: ignore[attr-defined]

        # Skills (load both Digital Employee built-in skills & target repo skills)
        skill_loader = SkillLoader(work_dir)
        skill_loader.load_all()
        builtin_skills_root = Path(__file__).resolve().parents[3] / "skills"
        if builtin_skills_root.is_dir():
            for sdef in skill_loader._scan_directory(builtin_skills_root, "builtin"):
                if sdef.name not in skill_loader._skills:
                    skill_loader._skills[sdef.name] = sdef
                    skill_loader._cache[sdef.name] = sdef

        signal = _LifecycleSignal()
        registry.register(FinishTaskTool(signal))
        registry.register(FinishNoopTool(signal))
        registry.register(RequestClarificationTool(signal))
        registry.register(ToolSearchTool(registry, protocol=protocol))
        registry.register(McpCallTool(registry))

        instructions = load_instructions(work_dir)
        # Anchor MemoryManager to persistent repo-scoped root instead of ephemeral worktree
        memory_manager = (
            MemoryManager(str(repo_mem_dir)) if self.injected_llm is None else None
        )

        agent = Agent(
            client=kernel_client,
            registry=registry,
            protocol=protocol,
            work_dir=work_dir,
            permission_checker=checker,
            context_window=provider.get_context_window(),
            instructions_content=instructions,
            memory_manager=memory_manager,
        )
        agent.session_id = sandbox.task_id
        agent.file_history = file_history
        load_skill_tool = LoadSkill()
        load_skill_tool.set_loader(skill_loader)
        load_skill_tool.set_agent(agent)
        registry.register(load_skill_tool)
        catalog_items = skill_loader.get_catalog()
        if catalog_items:
            agent.set_skill_catalog(
                "\n".join(f"- {name}: {desc}" for name, desc in catalog_items)
            )

        # Subagents (`explore`, `verification`, etc.)
        agent_loader = AgentLoader(work_dir, enable_verification=True)
        agent_loader.load_all()
        task_manager = TaskManager()
        trace_manager = TraceManager()
        if self.injected_llm is None:
            registry.register(
                AgentTool(
                    agent_loader=agent_loader,
                    task_manager=task_manager,
                    trace_manager=trace_manager,
                    parent_agent=agent,
                    enable_fork=True,
                    provider_config=provider,
                )
            )

        # Adaptive MCP Connection (`Eager` / `Native` / `Dispatch` preserving KV-cache prefix)
        import sys

        from digital_employee.settings import PROJECT_ROOT

        effective_mcp = list(self.mcp_servers)
        if (
            not self._explicit_mcp_override
            and self.injected_llm is None
            and self.claude_mem.enabled
            and not any(cfg.name == "claude-mem" for cfg in effective_mcp)
        ):
            effective_mcp.append(
                MCPServerConfig(
                    name="claude-mem",
                    command=sys.executable,
                    args=["-m", "digital_employee.mcp_servers.claude_mem_mcp"],
                    env={
                        "PYTHONPATH": str(PROJECT_ROOT / "src"),
                        "DE_CLAUDE_MEM_DB_PATH": str(self.settings.claude_mem_db_path),
                        "DE_CLAUDE_MEM_URL": self.settings.claude_mem_url,
                        "DE_CLAUDE_MEM_DEFAULT_PROJECT": sandbox.profile.repo_id,
                        "DE_CLAUDE_MEM_TASK_ID": sandbox.task_id,
                    },
                )
            )

        mcp_manager: MCPManager | None = None
        if effective_mcp:
            mcp_manager = MCPManager()
            mcp_manager.load_configs(effective_mcp)
            await mcp_manager.register_all_tools(registry)
            decide_and_apply(
                registry,
                base_url=provider.base_url,
                context_window=provider.get_context_window(),
            )

        # 3. Build or Resume Conversation History
        ledger = (
            DecisionLedger.for_repository(self.store, sandbox.profile)
            if self.store is not None
            else DecisionLedger.from_repo(
                Path(sandbox.profile.local_path), sandbox.profile.decision_ledger_path
            )
        )
        test_cmd_display = " ".join(sandbox.profile.test_cmd)
        conv: ConversationManager | None = None
        if (
            session_state_path is not None
            and not prior_feedback
            and not followup_comment
            and contract.resolved_decisions
        ):
            conv = load_conversation_checkpoint(session_state_path)
            if conv is not None:
                conv.add_user_message(
                    "<clarification-resolved>\n"
                    "Human delegator resolved the ambiguity with the following binding decisions:\n"
                    f"{json.dumps(contract.resolved_decisions, ensure_ascii=False, indent=2)}\n"
                    "Resume from your current sandbox state, complete verification, and call `finish_task` or `finish_noop`.\n"
                    "</clarification-resolved>"
                )

        if conv is None:
            conv = ConversationManager()
            prompt_parts = [
                KERNEL_CONTRACT_DIRECTIVE.strip(),
                f"## Repository Test Runner Command\n`{test_cmd_display}`",
                "## Accountable TaskContract\n" + contract.model_dump_json(indent=2),
                ledger.render_truth_hierarchy_block(contract.permitted_scopes),
            ]
            if self.claude_mem.enabled:
                mem_ctx = self.claude_mem.render_session_start_context(
                    project=sandbox.profile.repo_id,
                    objective=contract.objective,
                    suggested_scopes=contract.permitted_scopes,
                )
                if mem_ctx:
                    prompt_parts.append(mem_ctx)
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
            conv.add_user_message("\n\n".join(prompt_parts))

        outcome = CodingLoopOutcome(completed=False)
        compact_boundaries: list[tuple[str, list[Message]]] = []
        turn_count = 0

        def _emit_live(step: int, kind: str, icon: str, title: str, detail: str = "") -> None:
            if on_step is not None:
                try:
                    on_step(
                        {
                            "step": step,
                            "max_steps": self.max_turns,
                            "kind": kind,
                            "icon": icon,
                            "title": title,
                            "detail": detail[:800],
                            "step_cost_usd": 0.0,
                        }
                    )
                except Exception:
                    pass

        def _describe_kernel_tool(tool_name: str, args: dict[str, Any]) -> tuple[str, str]:
            if tool_name == "ReadFile":
                return "📖", f"读取源码 `{args.get('file_path') or args.get('path', '')}`"
            if tool_name == "EditFile":
                return "✏️", f"修改文件 `{args.get('file_path') or args.get('path', '')}`"
            if tool_name == "WriteFile":
                return "📝", f"写入文件 `{args.get('file_path') or args.get('path', '')}`"
            if tool_name == "Grep":
                return "🔎", f"全局搜索 `{args.get('pattern', '')}`"
            if tool_name == "Glob":
                return "📂", f"扫描文件 `{args.get('pattern', '')}`"
            if tool_name == "Bash":
                cmd_str = str(args.get("command") or "")[:90]
                return "🧪", f"沙箱终端执行 `{cmd_str}`"
            if tool_name == "Agent":
                sub_type = args.get("subagent_type") or "explore"
                return "🤖", f"派发子智能体 (`{sub_type}`) · {str(args.get('description', ''))[:60]}"
            if tool_name == "LoadSkill":
                return "📚", f"加载专业技能 `{args.get('skill_name', '')}`"
            if tool_name.endswith("mem_search"):
                return "🧠", f"检索仓库历史记忆 (`{args.get('query', '') or '最新索引'}`)"
            if tool_name.endswith("mem_timeline"):
                return "🕒", f"查看历史记忆时序上下文 (`#{args.get('anchor_id', '')}`)"
            if tool_name.endswith("mem_get_observations"):
                return "📖", f"读取历史记忆完整详情 (`{args.get('ids', [])}`)"
            if tool_name.endswith("mem_save_observation"):
                return "📌", f"写入仓库踩坑/发现记忆 (`{str(args.get('title', ''))[:50]}`)"
            if tool_name == "finish_task":
                return "🏁", f"完成编码，提交门禁验证 (`{args.get('repro_test_target', '')}`)"
            if tool_name == "finish_noop":
                return "✅", "确认基线已满足条件，发起 No-Op 校验"
            if tool_name == "request_clarification":
                return "⏸️", "发现关键业务歧义，挂起会话请求拍板"
            return "🛠️", f"Kernel · `{tool_name}`"

        thought_buf = ""
        last_tool_name = ""
        llm_turns = 0
        budget_warned = False
        grace_finalize_nudged = False
        warn_threshold = max(4, self.max_turns - 4)

        try:
            while turn_count < self.max_turns:
                hit_turn_ceiling = False
                async for event in agent.run(conv):
                    if isinstance(event, ThinkingText):
                        thought_buf += event.text
                    elif isinstance(event, ToolUseEvent):
                        if thought_buf.strip():
                            one_line = " ".join(thought_buf.split())[:180]
                            _emit_live(
                                llm_turns + 1,
                                "thought",
                                "💭",
                                f"Turn {llm_turns + 1}/{self.max_turns} · 内核推理摘要",
                                one_line,
                            )
                            thought_buf = ""
                        outcome.steps_used += 1
                        last_tool_name = event.tool_name
                        arg_preview = json.dumps(event.arguments, ensure_ascii=False)[:220]
                        outcome.trace_log.append(
                            f"[Kernel Step {outcome.steps_used}] {event.tool_name}({arg_preview})"
                        )
                        t_icon, t_title = _describe_kernel_tool(event.tool_name, event.arguments)
                        _emit_live(
                            llm_turns + 1,
                            "tool_call",
                            t_icon,
                            f"Turn {llm_turns + 1}/{self.max_turns} (Tool #{outcome.steps_used}) · {t_title}",
                            "" if event.tool_name in ("ReadFile", "EditFile", "WriteFile") else arg_preview,
                        )
                    elif isinstance(event, ToolResultEvent):
                        if event.is_error and "Permission denied" in event.output:
                            outcome.trace_log.append(f"  -> POLICY BLOCKED: {event.output[:220]}")
                            _emit_live(
                                llm_turns + 1,
                                "policy_block",
                                "🛡️",
                                "契约权限硬拦截",
                                event.output[:300],
                            )
                        elif last_tool_name == "Bash" and event.output.strip():
                            tail_lines = [ln.strip() for ln in event.output.splitlines() if ln.strip()]
                            tail = tail_lines[-1][:140] if tail_lines else ""
                            _emit_live(
                                llm_turns + 1,
                                "tool_result",
                                "❌" if event.is_error else "✅",
                                f"Turn {llm_turns + 1}/{self.max_turns} · 终端返回: {tail}",
                            )
                    elif isinstance(event, TurnComplete):
                        llm_turns += 1
                        # Inject countdown reminder into last user/tool message 4 turns before ceiling
                        if (
                            not budget_warned
                            and self.max_turns >= 6
                            and llm_turns >= warn_threshold
                            and conv.messages
                            and conv.messages[-1].role == "user"
                        ):
                            budget_warned = True
                            rem = max(1, self.max_turns - llm_turns)
                            reminder_txt = (
                                f"<system-reminder>⚠️ STEP BUDGET COUNTDOWN: Only {rem} turn(s) remain out of {self.max_turns}! "
                                "Stop exploratory reading immediately, run target verification test if needed, "
                                "and invoke `finish_task` (with `repro_test_target`) or `finish_noop` right now.</system-reminder>"
                            )
                            last_msg = conv.messages[-1]
                            if last_msg.tool_results:
                                last_msg.tool_results[-1].content += f"\n\n{reminder_txt}"
                            else:
                                last_msg.content = f"{last_msg.content}\n\n{reminder_txt}".strip()
                        if llm_turns >= self.max_turns or outcome.steps_used >= self.max_turns * 3:
                            hit_turn_ceiling = True
                            break
                    elif isinstance(event, CompactNotification):
                        outcome.trace_log.append(
                            f"[Context Compaction] {event.message} (before={event.before_tokens} tokens)"
                        )
                        _emit_live(
                            llm_turns + 1,
                            "compact",
                            "🗜️",
                            f"上下文自动压缩 ({event.before_tokens} tokens)",
                            event.message,
                        )
                        if event.boundary is not None:
                            compact_boundaries.append((event.boundary.summary, event.boundary.keep))
                    elif isinstance(event, ErrorEvent):
                        outcome.trace_log.append(f"[Kernel Error] {event.message}")

                    outcome.cost_usd = kernel_client.accumulated_cost_usd

                    # Immediate lifecycle signal interception
                    if signal.kind == "clarification" and signal.clarification is not None:
                        outcome.needs_clarification = signal.clarification
                        if session_state_path is not None:
                            save_conversation_checkpoint(
                                session_state_path, conv, compact_boundaries
                            )
                        return outcome

                    if signal.kind == "finish_noop":
                        outcome.completed = True
                        outcome.is_noop = True
                        outcome.noop_test_targets = signal.noop_test_targets or ["tests"]
                        outcome.summary = (
                            signal.summary
                            or "All tests already passed on base commit; zero changes required."
                        )
                        return outcome

                    if signal.kind == "finish_task":
                        outcome.completed = True
                        outcome.repro_test_target = (
                            signal.repro_test_target or contract.target_repro_test
                        )
                        outcome.commit_message = (
                            signal.commit_message or f"fix: {contract.objective[:65]}"
                        )
                        outcome.root_cause = signal.root_cause
                        outcome.summary = signal.summary
                        return outcome

                    if outcome.cost_usd >= remaining_budget_usd:
                        outcome.trace_log.append(
                            f"[Budget Ceiling] Reached ${outcome.cost_usd:.4f} >= ${remaining_budget_usd:.4f}."
                        )
                        return outcome

                if hit_turn_ceiling:
                    # Give ONE final 1-turn grace opportunity to call finish_task if work was done
                    if not grace_finalize_nudged:
                        grace_finalize_nudged = True
                        conv.add_user_message(
                            "⚠️ Turn budget reached! Do NOT call ReadFile/EditFile/Bash anymore — "
                            "immediately invoke `finish_task` (with `repro_test_target`) or `finish_noop` to submit your current work."
                        )
                        continue
                    outcome.trace_log.append(
                        f"[Step Limit] Reached {llm_turns} LLM turns ({outcome.steps_used} tool calls) without finish_task."
                    )
                    return outcome

                turn_count += 1
                # Model stopped without calling finish_task / finish_noop / request_clarification:
                # nudge once or twice so autonomous runs always produce an explicit contract outcome.
                if turn_count >= 3 or llm_turns >= self.max_turns:
                    break
                conv.add_user_message(
                    "You ended your turn without invoking `finish_task`, `finish_noop`, or `request_clarification`. "
                    "Verify your changes now and invoke `finish_task` (with `repro_test_target`) or `finish_noop`."
                )

        except (RateLimitError, NetworkError) as exc:
            raise TransientLLMError(str(exc)) from exc
        except (AuthenticationError, LLMError) as exc:
            if "429" in str(exc) or "500" in str(exc) or "502" in str(exc) or "503" in str(exc):
                raise TransientLLMError(str(exc)) from exc
            raise
        finally:
            if mcp_manager is not None:
                await mcp_manager.shutdown()

        return outcome
