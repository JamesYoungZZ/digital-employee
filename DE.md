# AI Digital Employee — Kernel & Repository Instructions (`DE.md`)

## 1. System Architecture Overview (v3.0 Clean Baseline)
- **Coding Agent Engine (`src/digital_employee/kernel/` + `src/digital_employee/agents/kernel_runtime.py`)**:
  Powered by the native white-box **`mewcode` Agent Kernel** (`NativeKernelRuntime`, default `DE_CODING_RUNTIME=kernel`), replacing all legacy raw tool loops:
  - `StreamingExecutor` read-concurrent / write-serial tool batching
  - L1 disk spill (`MAX_OUTPUT_CHARS=50000`) + L2 anchored context compaction & `RecoveryState`
  - Stateful JSONL conversation checkpointing (`kernel_session.jsonl`) across `WAITING_CLARIFICATION` pause/resume
  - Subagents (`explore`, `plan`, `verification`), Skills (`LoadSkill`), Dual-Scope Auto-Memory (`~/.de/memory/` + `.local/repo_memory/<repo_id>/`), and Dual-Mode **`claude-mem` Persistent Observation Engine** (`src/digital_employee/memory/claude_mem.py`, SQLite FTS5 + optional `:37777` sidecar worker)
  - Cache-aware 3-state MCP (`Eager` / `Native` / `Dispatch`) + built-in `ops-diagnostics` & `claude-mem` (`mem_search` -> `mem_timeline` -> `mem_get_observations` + `mem_save_observation`) MCP servers
  - Defense-in-depth `ContractPathSandbox` (hard-blocking `forbidden_paths`, `.git`, and outside-worktree writes while treating `permitted_scopes` as soft guidance with cross-directory edit advisories) + optional OS kernel sandboxing (macOS `Seatbelt` / Linux `Bubblewrap`)
- **Orchestration, Rule Distillation & Quality Gates (`src/digital_employee/orchestrator.py` + `verification/evidence_gate.py` + `agents/reviewer.py`)**:
  - `ContractDrafter` -> `NativeKernelRuntime` in isolated `git worktree` (`ai-agent/<task_id>`) -> Multi-language `Fail-to-Pass` & Anti-Tampering `EvidenceGate` -> Cross-Model `FocusedReviewer` (`skills/pr-code-review/SKILL.md`, standalone `/reports/<task_id>/review`) -> Real Open GitHub PR + Feishu/Console Interactive **1-Click Auto-Fix** (`BLOCK`), **Approval-Gated Squash Merge** (`APPROVE` + Green GitHub CI), and **`claude-mem` -> `CANDIDATE_DRAFT` Governance Rule Distillation** (1-click human promotion into `DecisionLedger`).

## 2. Coding & Verification Standards
- Python 3.12+ (`ruff check src tests` must pass with 0 warnings; `pytest` runs 720+ kernel & harness tests).
- Frontend Console (`frontend/`): React 19 + TypeScript 5.7 + Vite 6 + Tailwind v4 + Radix UI (`npm run build` compiles to `src/digital_employee/web/static/`).
- Strictly respect target repository `.ai/DECISIONS.yaml` policies and `TaskContract.permitted_scopes`; never push directly to protected branches (`main`/`master`/`release`) or modify `.github/workflows/` / `.env`.
