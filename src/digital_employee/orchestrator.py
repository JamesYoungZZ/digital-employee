"""Durable Orchestrator coordinating Contract -> Clarification -> Sandbox -> Evidence Gate -> Delivery."""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from digital_employee.agents.coding_loop import SandboxCodingAgent
from digital_employee.agents.contract_drafter import ContractDrafter
from digital_employee.agents.reviewer import FocusedReviewer, render_pr_review_agent_comment
from digital_employee.context.decision_ledger import DecisionEntry, DecisionLedger
from digital_employee.integrations.feishu_cards import (
    FeishuNotifier,
    build_accepted_receipt_card,
    build_clarification_card,
    build_evidence_report_card,
    build_handoff_card,
    build_merge_approval_card,
    build_noop_verified_card,
    build_review_fix_card,
    parse_feishu_command,
)
from digital_employee.integrations.github_client import (
    GitHubIntegration,
    render_evidence_pr_body,
)
from digital_employee.llm.client import LLMClient, TransientLLMError
from digital_employee.models.state import (
    DurableTaskState,
    FailureClass,
    HandoffBundle,
    MergeApprovalCard,
    VerificationEvidence,
    utc_now_iso,
)
from digital_employee.sandbox.worktree_runner import WorktreeSandbox
from digital_employee.security.dlp import PolicyViolationError
from digital_employee.settings import Settings, load_settings
from digital_employee.storage.durable_store import DurableStore
from digital_employee.verification.evidence_gate import EvidenceGate


def generate_task_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d")
    short = uuid.uuid4().hex[:6]
    return f"t-{stamp}-{short}"


class DelegationOrchestrator:
    """End-to-end execution & recovery coordinator for Digital Employee V1."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        llm: LLMClient | None = None,
        store: DurableStore | None = None,
    ) -> None:
        self.settings = settings or load_settings()
        self.store = store or DurableStore(self.settings.db_path, self.settings.artifacts_dir)
        self.llm = llm or LLMClient(self.settings)
        self.reviewer_llm = (
            llm
            if llm is not None
            else LLMClient(
                self.settings,
                provider=self.settings.reviewer_provider,
                base_url=self.settings.reviewer_base_url,
                api_key=self.settings.reviewer_api_key,
                default_model=self.settings.reviewer_model,
            )
        )
        self.drafter = ContractDrafter(self.llm)
        self.coder = SandboxCodingAgent(self.llm, max_steps=self.settings.max_coding_steps)
        self.reviewer = FocusedReviewer(
            self.reviewer_llm, skill_path=self.settings.pr_review_skill_path
        )
        self.github = GitHubIntegration(
            token=self.settings.github_token,
            api_base=self.settings.github_api_base,
        )
        self.feishu = FeishuNotifier(
            self.settings.feishu_webhook_url,
            app_id=self.settings.feishu_app_id,
            app_secret=self.settings.feishu_app_secret,
            default_chat_id=self.settings.feishu_default_chat_id,
        )
        self._active_lock = threading.Lock()
        self._active_tasks: set[str] = set()

    def is_task_actively_running(self, task_id: str) -> bool:
        with self._active_lock:
            return task_id in self._active_tasks

    def _live_stream_file(self, task_id: str) -> Path:
        tdir = self.settings.artifacts_dir / task_id
        tdir.mkdir(parents=True, exist_ok=True)
        return tdir / "live_stream.jsonl"

    def append_live_event(
        self,
        state: DurableTaskState,
        *,
        attempt: int = 1,
        step: int = 0,
        max_steps: int = 32,
        kind: str = "info",
        icon: str = "•",
        title: str,
        detail: str = "",
        step_cost_usd: float = 0.0,
    ) -> None:
        entry = {
            "ts": utc_now_iso(),
            "attempt": attempt,
            "step": step,
            "max_steps": max_steps,
            "kind": kind,
            "icon": icon,
            "title": title,
            "detail": detail,
            "cost_usd": round(state.total_cost_usd + step_cost_usd, 6),
        }
        try:
            with self._live_stream_file(state.task_id).open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass
        if step > 0:
            state.current_step = step
        state.max_steps = max_steps
        state.current_activity = f"{icon} {title}"
        self.store.save_task(state)

    def read_live_events(self, task_id: str, limit: int = 120) -> list[dict[str, Any]]:
        fp = self.settings.artifacts_dir / task_id / "live_stream.jsonl"
        if not fp.is_file():
            return []
        out: list[dict[str, Any]] = []
        for ln in fp.read_text(encoding="utf-8").splitlines()[-limit:]:
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
        return out

    def recover_orphaned_tasks(self) -> list[str]:
        """Auto-resume tasks left in `RUNNING_IN_SANDBOX`/`VERIFYING_EVIDENCE`/`QUEUED` after a daemon restart."""
        resumed: list[str] = []
        for st_name in ("RUNNING_IN_SANDBOX", "VERIFYING_EVIDENCE", "QUEUED"):
            for st in self.store.list_tasks(status=st_name, limit=20):
                if self.is_task_actively_running(st.task_id):
                    continue
                if st_name in ("RUNNING_IN_SANDBOX", "VERIFYING_EVIDENCE") and st.sandbox_attempt_count > 0:
                    st.sandbox_attempt_count = max(0, st.sandbox_attempt_count - 1)
                self.append_live_event(
                    st,
                    attempt=st.sandbox_attempt_count + 1,
                    step=0,
                    max_steps=self.settings.max_coding_steps,
                    kind="recovery",
                    icon="🔄",
                    title="检测到服务重启中断，已自动恢复沙箱后台执行…",
                )
                self.store.save_task(
                    st,
                    step_name="auto_resumed_after_restart",
                    note="Automatically resumed orphaned in-flight task after daemon restart.",
                )
                threading.Thread(
                    target=self.execute_task,
                    args=(st.task_id,),
                    kwargs={"publish_pr": st.auto_publish_pr or self.github.is_configured},
                    name=f"de-recover-{st.task_id}",
                    daemon=True,
                ).start()
                resumed.append(st.task_id)
        return resumed

    def bind_feishu_default_chat_id(self, chat_id: str) -> None:
        """Auto-bind the active Feishu chat_id so Web/CLI tasks also push approval cards to Feishu."""
        cid = chat_id.strip()
        if not cid or cid == self.settings.feishu_default_chat_id:
            return
        self.settings.feishu_default_chat_id = cid
        self.feishu.default_chat_id = cid
        env_path = self.settings.data_dir.parent / ".env"
        if env_path.is_file():
            lines = env_path.read_text(encoding="utf-8").splitlines()
            updated = False
            out: list[str] = []
            for ln in lines:
                if ln.strip().startswith("DE_FEISHU_DEFAULT_CHAT_ID="):
                    out.append(f"DE_FEISHU_DEFAULT_CHAT_ID={cid}")
                    updated = True
                else:
                    out.append(ln)
            if not updated:
                out.append(f"DE_FEISHU_DEFAULT_CHAT_ID={cid}")
            env_path.write_text("\n".join(out) + "\n", encoding="utf-8")

    @property
    def web_base_url(self) -> str:
        return f"http://{self.settings.web_host}:{self.settings.web_port}"

    def submit_task(
        self,
        *,
        raw_request: str,
        target_repo: str,
        base_branch: str | None = None,
        owner_id: str | None = None,
        max_budget_usd: float | None = None,
        accept_defaults: bool = False,
        auto_execute: bool = True,
        publish_pr: bool = False,
        auto_merge: bool = False,
        source_channel: str = "cli",
        feishu_chat_id: str = "",
    ) -> DurableTaskState:
        profile = self.settings.resolve_repository(target_repo)
        task_id = generate_task_id()
        trace_id = uuid.uuid4().hex
        sandbox = WorktreeSandbox(profile, self.settings.worktrees_dir, task_id)

        t0 = time.monotonic()
        contract, draft_cost = self.drafter.draft(
            raw_request=raw_request,
            profile=profile,
            sandbox=sandbox,
            owner_id=owner_id or self.settings.default_owner_id,
            max_budget_usd=max_budget_usd or self.settings.default_max_budget_usd,
            base_branch=base_branch,
        )
        elapsed = round(time.monotonic() - t0, 2)

        state = DurableTaskState(
            tenant_id=self.settings.default_tenant_id,
            workspace_id=self.settings.default_workspace_id,
            task_id=task_id,
            trace_id=trace_id,
            raw_request=raw_request,
            source_channel=source_channel,
            feishu_chat_id=feishu_chat_id,
            contract=contract,
            lifecycle_status="DRAFTING_CONTRACT",
            auto_publish_pr=publish_pr,
            auto_merge=auto_merge,
            branch_name=sandbox.branch_name,
            worktree_path=str(sandbox.worktree_path),
            total_cost_usd=round(draft_cost, 6),
            agent_elapsed_seconds=elapsed,
            baseline_human_minutes=profile.baseline_human_minutes,
        )
        self.store.save_task(state, step_name="contract_drafted", note=contract.objective)

        if contract.unresolved_decisions:
            if accept_defaults:
                for dec in contract.unresolved_decisions:
                    contract.resolved_decisions[dec.question] = dec.default_assumption
                contract.unresolved_decisions = []
                contract.contract_version += 1
                state.lifecycle_status = "QUEUED"
                self.store.save_task(
                    state,
                    step_name="clarification_auto_defaulted",
                    note="Delegator opted into default assumptions.",
                )
            else:
                state.lifecycle_status = "WAITING_CLARIFICATION"
                card = build_clarification_card(state, self.web_base_url)
                self.store.write_artifact(task_id, "feishu_clarification_card.json", card)
                self.store.run_idempotent(
                    state,
                    "notify_clarification",
                    lambda: self.feishu.send_card(card, chat_id=state.feishu_chat_id),
                )
                self.store.save_task(
                    state,
                    step_name="waiting_clarification",
                    note=f"{len(contract.unresolved_decisions)} unresolved decision(s)",
                )
                return state
        else:
            state.lifecycle_status = "QUEUED"
            receipt_card = build_accepted_receipt_card(state, self.web_base_url)
            self.store.write_artifact(task_id, "feishu_receipt_card.json", receipt_card)
            if source_channel.startswith("feishu"):
                self.store.run_idempotent(
                    state,
                    "notify_receipt",
                    lambda: self.feishu.send_card(receipt_card, chat_id=state.feishu_chat_id),
                )
            self.store.save_task(state, step_name="queued", note="Contract unambiguous; queued.")

        if auto_execute:
            return self.execute_task(
                task_id, publish_pr=publish_pr or state.auto_publish_pr
            )
        return state

    def resolve_clarification(
        self,
        task_id: str,
        *,
        answers: dict[str, str] | None = None,
        use_defaults: bool = False,
        interaction_seconds: int = 5,
        auto_execute: bool = True,
        publish_pr: bool = False,
    ) -> DurableTaskState:
        state = self._require_task(task_id)
        provided = answers or {}
        for idx, dec in enumerate(state.contract.unresolved_decisions):
            ans = (
                provided.get(dec.question)
                or provided.get(str(idx))
                or provided.get(f"Q{idx + 1}")
            )
            if not ans and use_defaults:
                ans = dec.default_assumption
            if ans:
                state.contract.resolved_decisions[dec.question] = ans

        state.contract.unresolved_decisions = []
        state.contract.contract_version += 1
        state.human_interaction_seconds += max(0, interaction_seconds)
        state.failure_class = "NONE"
        state.failure_reason = None
        state.lifecycle_status = "QUEUED"
        self.store.save_task(
            state,
            step_name="clarification_resolved",
            note=json.dumps(state.contract.resolved_decisions, ensure_ascii=False),
        )
        if auto_execute:
            return self.execute_task(
                task_id, publish_pr=publish_pr or state.auto_publish_pr
            )
        return state

    def execute_task(
        self,
        task_id: str,
        *,
        publish_pr: bool = False,
        followup_comment: str = "",
    ) -> DurableTaskState:
        with self._active_lock:
            if task_id in self._active_tasks:
                return self._require_task(task_id)
            self._active_tasks.add(task_id)
        try:
            return self._execute_task_inner(
                task_id, publish_pr=publish_pr, followup_comment=followup_comment
            )
        finally:
            with self._active_lock:
                self._active_tasks.discard(task_id)

    def _execute_task_inner(
        self,
        task_id: str,
        *,
        publish_pr: bool = False,
        followup_comment: str = "",
    ) -> DurableTaskState:
        state = self._require_task(task_id)
        profile = self.settings.resolve_repository(state.contract.target_repo)
        sandbox = WorktreeSandbox(
            profile,
            self.settings.worktrees_dir,
            state.task_id,
            permitted_scopes=state.contract.permitted_scopes,
        )

        # Idempotently provision isolated git worktree
        self.append_live_event(
            state,
            attempt=1,
            step=0,
            max_steps=self.settings.max_coding_steps,
            kind="provision",
            icon="📦",
            title=f"正在准备独立隔离沙箱 git worktree (`{profile.repo_id}` @ `{state.contract.base_commit_sha[:8]}`)…",
        )
        try:
            self.store.run_idempotent(
                state,
                "provision_worktree",
                lambda: str(sandbox.provision(state.contract.base_commit_sha)),
            )
            # Ensure worktree directory exists if resuming after cleanup
            sandbox.provision(state.contract.base_commit_sha)
        except Exception as exc:
            return self._transition_to_handoff(
                state,
                sandbox=sandbox,
                failure_class="SANDBOX_ENV_BROKEN",
                reason=f"Failed to provision git worktree: {exc}",
                clues=[],
            )

        feedback = ""
        all_traces: list[str] = []
        max_attempts = self.settings.max_self_heal_attempts

        for attempt in range(1, max_attempts + 1):
            state.sandbox_attempt_count += 1
            state.lifecycle_status = "RUNNING_IN_SANDBOX"
            self.append_live_event(
                state,
                attempt=attempt,
                step=0,
                max_steps=self.settings.max_coding_steps,
                kind="attempt_start",
                icon="🚀",
                title=f"启动第 {attempt}/{max_attempts} 轮交互式编码循环…",
                detail=feedback[:300] if feedback else "",
            )
            self.store.save_task(
                state,
                step_name=f"sandbox_attempt_{attempt}",
                note=f"Starting interactive coding attempt {attempt}/{max_attempts}",
            )

            remaining_budget = max(0.0, state.contract.max_budget_usd - state.total_cost_usd)
            if remaining_budget <= 0:
                return self._transition_to_handoff(
                    state,
                    sandbox=sandbox,
                    failure_class="BUDGET_OR_LOOP_EXCEEDED",
                    reason=f"Budget limit ${state.contract.max_budget_usd:.2f} reached.",
                    clues=all_traces[-8:],
                )

            t0 = time.monotonic()
            try:
                outcome = self.coder.run(
                    contract=state.contract,
                    sandbox=sandbox,
                    prior_feedback=feedback,
                    followup_comment=followup_comment if attempt == 1 else "",
                    remaining_budget_usd=remaining_budget,
                    on_step=lambda ev, att=attempt: self.append_live_event(
                        state,
                        attempt=att,
                        step=int(ev.get("step", 0)),
                        max_steps=int(ev.get("max_steps", self.settings.max_coding_steps)),
                        kind=str(ev.get("kind", "step")),
                        icon=str(ev.get("icon", "•")),
                        title=str(ev.get("title", "")),
                        detail=str(ev.get("detail", "")),
                        step_cost_usd=float(ev.get("step_cost_usd", 0.0)),
                    ),
                )
            except TransientLLMError as exc:
                state.failure_class = "TRANSIENT_INFRA"
                state.failure_reason = str(exc)
                self.append_live_event(
                    state,
                    attempt=attempt,
                    kind="error",
                    icon="⚠️",
                    title=f"模型网络/接口异常: {str(exc)[:140]}",
                )
                self.store.save_task(state, step_name="transient_infra_error", note=str(exc))
                raise

            state.total_cost_usd = round(state.total_cost_usd + outcome.cost_usd, 6)
            state.agent_elapsed_seconds = round(
                state.agent_elapsed_seconds + (time.monotonic() - t0), 2
            )
            all_traces.extend(outcome.trace_log)

            # Mid-execution ambiguity discovered by Coding Agent
            if outcome.needs_clarification is not None:
                state.contract.unresolved_decisions = [outcome.needs_clarification]
                state.lifecycle_status = "WAITING_CLARIFICATION"
                state.failure_class = "REQUIREMENT_AMBIGUITY"
                state.failure_reason = outcome.needs_clarification.question
                card = build_clarification_card(state, self.web_base_url)
                self.store.write_artifact(task_id, "feishu_clarification_card.json", card)
                try:
                    self.feishu.send_card(card, chat_id=state.feishu_chat_id)
                except Exception:
                    pass
                self.store.save_task(
                    state,
                    step_name="mid_flight_clarification",
                    note=outcome.needs_clarification.question,
                )
                return state

            if not outcome.completed:
                feedback = "Coding loop exhausted step limit without calling `finish_task`. Focus directly on minimal fix + `finish_task`."
                continue

            if outcome.is_noop:
                dirty_files = sandbox.modified_files(state.contract.base_commit_sha)
                if dirty_files:
                    feedback = (
                        f"`finish_noop` rejected: worktree has modified files {dirty_files}. "
                        "Either revert unintended edits before `finish_noop`, or verify via `finish_task`."
                    )
                    self.store.save_task(
                        state,
                        step_name=f"noop_rejected_{attempt}",
                        note=feedback[:300],
                    )
                    continue

                noop_targets = outcome.noop_test_targets or ["tests"]
                state.lifecycle_status = "VERIFYING_EVIDENCE"
                self.store.save_task(
                    state,
                    step_name=f"verifying_noop_{attempt}",
                    note=f"Deterministically verifying No-Op claim on {noop_targets}",
                )
                noop_res = sandbox.run_pytest(noop_targets, timeout_seconds=120)
                if noop_res.exit_code != 0 or "passed" not in noop_res.combined_output:
                    feedback = (
                        f"`finish_noop` rejected: independent pytest run on {noop_targets} "
                        f"exited with code {noop_res.exit_code}.\n{noop_res.combined_output[-1500:]}"
                    )
                    self.store.save_task(
                        state,
                        step_name=f"noop_rejected_{attempt}",
                        note=feedback[:400],
                    )
                    continue

                full_noop_log = (
                    "=== Agent Trace ===\n"
                    + "\n".join(all_traces)
                    + f"\n\n=== Deterministic No-Op Verification ({noop_targets}) ===\n"
                    + f"exit_code={noop_res.exit_code}\n{noop_res.combined_output}"
                )
                log_uri = self.store.write_artifact(
                    task_id, f"verification_noop_{attempt}.log", full_noop_log
                )
                state.head_commit_sha = state.contract.base_commit_sha
                state.verification_evidence = VerificationEvidence(
                    verified_commit_sha=state.contract.base_commit_sha,
                    repro_test_target=", ".join(noop_targets),
                    repro_test_failed_on_base=False,
                    repro_test_passed_on_head=True,
                    existing_test_suite_passed=True,
                    tests_weakened_check=True,
                    scope_compliance_check=True,
                    static_analysis_passed=True,
                    noop_verified=True,
                    noop_summary=outcome.summary,
                    artifact_log_uri=log_uri,
                )
                state.failure_class = "NONE"
                state.failure_reason = None
                state.handoff_bundle = None
                state.lifecycle_status = "ACCEPTED_AND_CLOSED"

                noop_card = build_noop_verified_card(state, self.web_base_url)
                self.store.write_artifact(task_id, "feishu_noop_card.json", noop_card)
                try:
                    self.store.run_idempotent(
                        state,
                        "notify_noop_card",
                        lambda nc=noop_card: self.feishu.send_card(nc, chat_id=state.feishu_chat_id),
                    )
                except Exception:
                    pass

                return self.store.save_task(
                    state,
                    step_name="noop_verified_and_closed",
                    note=f"No-Op verified (`{', '.join(noop_targets)}` passed with 0 edits): {outcome.summary[:240]}",
                )

            # Commit changes on `ai-agent/{task_id}` and save patch artifact
            try:
                head_sha = sandbox.commit_all(
                    outcome.commit_message, state.contract.base_commit_sha
                )
            except PolicyViolationError as exc:
                return self._transition_to_handoff(
                    state,
                    sandbox=sandbox,
                    failure_class="SECURITY_OR_SCOPE_VIOLATION",
                    reason=str(exc),
                    clues=[outcome.root_cause] if outcome.root_cause else [],
                )

            state.head_commit_sha = head_sha
            diff_text = sandbox.git_diff(state.contract.base_commit_sha)
            state.patch_artifact_uri = self.store.write_artifact(
                task_id, f"patch_attempt_{attempt}.diff", diff_text
            )

            # Run Deterministic Evidence Gate (Fail-to-Pass + Anti-Tampering + Suite + Lint)
            state.lifecycle_status = "VERIFYING_EVIDENCE"
            self.append_live_event(
                state,
                attempt=attempt,
                kind="gate",
                icon="🛡️",
                title=f"正在执行硬核证据门禁 (`Fail-to-Pass` 基线对比 `{outcome.repro_test_target}`)…",
            )
            self.store.save_task(
                state,
                step_name=f"verifying_evidence_{attempt}",
                note=f"Verifying commit {head_sha[:10]} against {outcome.repro_test_target}",
            )

            gate = EvidenceGate(sandbox, state.contract)
            gate_res = gate.evaluate(head_sha, outcome.repro_test_target)
            evidence = gate_res.evidence

            full_log = (
                "=== Agent Trace ===\n"
                + "\n".join(all_traces)
                + "\n\n"
                + gate_res.diagnostic_log
            )
            evidence.artifact_log_uri = self.store.write_artifact(
                task_id, f"verification_attempt_{attempt}.log", full_log
            )

            # Only invoke Focused Single Reviewer if deterministic hard checks passed
            if (
                evidence.repro_test_failed_on_base
                and evidence.repro_test_passed_on_head
                and evidence.existing_test_suite_passed
                and evidence.tests_weakened_check
                and evidence.scope_compliance_check
                and evidence.static_analysis_passed
            ):
                self.append_live_event(
                    state,
                    attempt=attempt,
                    kind="review",
                    icon="🔍",
                    title=f"单测门禁通过！正在调用独立 AI Reviewer (`{self.settings.reviewer_model}`) 审查代码…",
                )
                rev_t0 = time.monotonic()
                report_url = f"{self.web_base_url}/reports/{state.task_id}/review"
                reviewer_report, rev_cost = self.reviewer.review(
                    contract=state.contract,
                    sandbox=sandbox,
                    evidence=evidence,
                    full_report_url=report_url,
                )
                state.total_cost_usd = round(state.total_cost_usd + rev_cost, 6)
                state.agent_elapsed_seconds = round(
                    state.agent_elapsed_seconds + (time.monotonic() - rev_t0), 2
                )
                evidence.reviewer_report = reviewer_report
                evidence.unverified_risks = list(reviewer_report.unverified_risks)
                state.verification_evidence = evidence
                self.append_live_event(
                    state,
                    attempt=attempt,
                    kind="review_done",
                    icon="✅" if reviewer_report.verdict == "APPROVE" else "🛑",
                    title=f"独立 Code Review 完成：`{reviewer_report.verdict}` ({len(reviewer_report.blocking_issues)} 阻断项)",
                    detail=reviewer_report.summary,
                )

                # If BLOCKED on a PR workflow, still publish the branch/PR & post the BLOCK review comment
                # onto GitHub so every real review finding is visibly documented on the PR timeline!
                if (
                    reviewer_report.verdict == "BLOCK"
                    and (publish_pr or state.auto_publish_pr)
                    and self.github.is_configured
                ):
                    self._sync_pr_and_post_review(state, sandbox=sandbox)

            state.verification_evidence = evidence

            if evidence.passed_all_hard_gates:
                state.failure_class = "NONE"
                state.failure_reason = None
                self.append_live_event(
                    state,
                    attempt=attempt,
                    kind="deliver",
                    icon="🚀",
                    title="所有本地门禁与 AI Review 已通过！正在推送分支并创建正式 PR…",
                )
                return self._deliver_verified_result(
                    state,
                    sandbox=sandbox,
                    publish_pr=publish_pr or state.auto_publish_pr,
                    followup_comment=followup_comment,
                )

            # Prepare concise diagnostic feedback for the next self-heal iteration
            feedback = self._format_gate_failures(evidence, gate_res.diagnostic_log)
            self.append_live_event(
                state,
                attempt=attempt,
                kind="gate_rejected",
                icon="↩️",
                title=f"第 {attempt} 轮门禁未通过，正在携带报错反馈进入自愈修正…",
                detail=feedback[:400],
            )
            self.store.save_task(
                state,
                step_name=f"gate_rejected_{attempt}",
                note=feedback[:500],
            )

        return self._transition_to_handoff(
            state,
            sandbox=sandbox,
            failure_class="REGRESSION_TEST_FAILED",
            reason=f"Did not pass all hard evidence gates within {max_attempts} attempts.",
            clues=[feedback[:800]] if feedback else all_traces[-5:],
        )

    def handle_pr_review_comment(
        self,
        task_id: str,
        *,
        comment_body: str,
        publish_pr: bool = True,
    ) -> DurableTaskState:
        """Post-PR Follow-Through Loop: wake up task worktree to address human review comment."""
        state = self._require_task(task_id)
        state.contract.contract_version += 1
        self.store.save_task(
            state,
            step_name="pr_comment_received",
            note=comment_body[:300],
        )
        return self.execute_task(
            task_id,
            publish_pr=publish_pr,
            followup_comment=comment_body,
        )

    def update_scope_or_cancel(
        self,
        task_id: str,
        *,
        action: str,
        permitted_scopes: list[str] | None = None,
        note: str = "",
    ) -> DurableTaskState:
        """Allow delegator to cancel/handoff or adjust permitted_scopes from Web Queue / CLI."""
        state = self._require_task(task_id)
        profile = self.settings.resolve_repository(state.contract.target_repo)
        sandbox = WorktreeSandbox(profile, self.settings.worktrees_dir, state.task_id)

        if action == "cancel":
            return self._transition_to_handoff(
                state,
                sandbox=sandbox,
                failure_class="BUDGET_OR_LOOP_EXCEEDED",
                reason=note or "Cancelled by delegator; workspace preserved for handoff.",
                clues=["Task interrupted manually by delegator."],
            )
        if action == "update_scope" and permitted_scopes is not None:
            state.contract.permitted_scopes = permitted_scopes
            state.contract.contract_version += 1
            return self.store.save_task(
                state,
                step_name="scope_updated",
                note=f"New scopes: {permitted_scopes}. {note}",
            )
        if action == "accept":
            state.lifecycle_status = "ACCEPTED_AND_CLOSED"
            return self.store.save_task(state, step_name="accepted_by_human", note=note)
        if action == "abandon":
            state.lifecycle_status = "ABANDONED_OR_REVERTED"
            return self.store.save_task(state, step_name="abandoned_by_human", note=note)
        if action == "publish_pr":
            if not (state.verification_evidence and state.verification_evidence.passed_all_hard_gates):
                raise ValueError("Cannot publish PR before passing all hard evidence gates.")
            return self._deliver_verified_result(
                state,
                sandbox=sandbox,
                publish_pr=True,
            )
        if action == "approve_merge" or action == "auto_merge":
            res = self.approve_and_merge(task_id, approved_by=note or "console_user")
            if not res.get("merged"):
                raise ValueError(f"Merge gate not satisfied: {res.get('reason')}")
            return self._require_task(task_id)

        raise ValueError(f"Unsupported control action: {action}")

    def promote_decision_rule(
        self,
        task_id: str,
        *,
        scope: str,
        rule: str,
        reason: str = "",
    ) -> DecisionEntry:
        """Persist a human-confirmed team rule into target repo `.ai/DECISIONS.yaml`."""
        state = self._require_task(task_id)
        profile = self.settings.resolve_repository(state.contract.target_repo)
        entry = DecisionLedger.append_policy(
            Path(profile.local_path),
            scope=scope,
            rule=rule,
            reason=reason or f"Promoted from task {task_id}",
            owner=state.contract.owner_id,
            ledger_rel_path=profile.decision_ledger_path,
        )
        self.store.save_task(
            state,
            step_name="decision_promoted",
            note=f"Added {entry.id}: {entry.rule[:120]}",
        )
        return entry

    # =========================================================================
    # Internal Delivery & Handoff Helpers
    # =========================================================================

    def _sync_pr_and_post_review(
        self, state: DurableTaskState, *, sandbox: WorktreeSandbox
    ) -> None:
        """Push branch, ensure PR exists, and post the structured PR Review report (including BLOCK findings)."""
        try:
            self.store.run_idempotent(
                state,
                f"push_branch_{(state.head_commit_sha or '')[:10]}",
                lambda: self.github.push_agent_branch(
                    worktree_path=Path(state.worktree_path),
                    branch_name=state.branch_name,
                    protected_branches=sandbox.profile.protected_branches,
                ),
            )

            def _create_or_reconcile() -> str:
                info = self.github.reconcile_or_create_pr(
                    github_repo=sandbox.profile.github_repo,
                    state=state,
                    protected_branches=sandbox.profile.protected_branches,
                    draft=False,
                )
                return json.dumps(info)

            pr_json, _ = self.store.run_idempotent(state, "create_pr", _create_or_reconcile)
            pr_info = json.loads(pr_json)
            state.deliverable_pr_url = pr_info["html_url"]
            state.deliverable_pr_number = int(pr_info["number"])

            ev = state.verification_evidence
            if ev and ev.reviewer_report and state.head_commit_sha:
                report_url = f"{self.web_base_url}/reports/{state.task_id}/review"
                review_md = render_pr_review_agent_comment(
                    ev.reviewer_report,
                    verified_commit_sha=state.head_commit_sha,
                    repro_test_target=ev.repro_test_target,
                    full_report_url=report_url,
                )
                rev_url, _ = self.store.run_idempotent(
                    state,
                    f"post_pr_review_{state.head_commit_sha[:10]}",
                    lambda: self.github.submit_pr_review_report(
                        github_repo=sandbox.profile.github_repo,
                        pr_number=state.deliverable_pr_number or 0,
                        commit_sha=state.head_commit_sha or "",
                        report_markdown=review_md,
                        blocking_issues=ev.reviewer_report.blocking_issues,
                    ),
                )
                state.pr_review_comment_url = rev_url
            self.store.save_task(state)
        except Exception:
            pass

    def _deliver_verified_result(
        self,
        state: DurableTaskState,
        *,
        sandbox: WorktreeSandbox,
        publish_pr: bool,
        followup_comment: str = "",
    ) -> DurableTaskState:
        pr_body = render_evidence_pr_body(state)
        self.store.write_artifact(state.task_id, "draft_pr_preview.md", pr_body)

        if publish_pr:
            branch_pushed = False
            try:
                self.store.run_idempotent(
                    state,
                    f"push_branch_{(state.head_commit_sha or '')[:10]}",
                    lambda: self.github.push_agent_branch(
                        worktree_path=Path(state.worktree_path),
                        branch_name=state.branch_name,
                        protected_branches=sandbox.profile.protected_branches,
                    ),
                )
                branch_pushed = True
            except Exception as push_exc:
                state.failure_reason = f"Remote branch push failed: {push_exc}"

            if branch_pushed and self.github.is_configured:
                try:
                    def _create_or_reconcile() -> str:
                        info = self.github.reconcile_or_create_pr(
                            github_repo=sandbox.profile.github_repo,
                            state=state,
                            protected_branches=sandbox.profile.protected_branches,
                            draft=False,
                        )
                        return json.dumps(info)

                    pr_json, _ = self.store.run_idempotent(
                        state, "create_pr", _create_or_reconcile
                    )
                    pr_info = json.loads(pr_json)
                    state.deliverable_pr_url = pr_info["html_url"]
                    state.deliverable_pr_number = int(pr_info["number"])
                    state.lifecycle_status = "PR_OPEN_FOLLOWING_UP"

                    # Post structured PR Review report with full report link
                    ev = state.verification_evidence
                    if ev and ev.reviewer_report and state.head_commit_sha:
                        report_url = f"{self.web_base_url}/reports/{state.task_id}/review"
                        review_md = render_pr_review_agent_comment(
                            ev.reviewer_report,
                            verified_commit_sha=state.head_commit_sha,
                            repro_test_target=ev.repro_test_target,
                            full_report_url=report_url,
                        )
                        rev_url, _ = self.store.run_idempotent(
                            state,
                            f"post_pr_review_{state.head_commit_sha[:10]}",
                            lambda: self.github.submit_pr_review_report(
                                github_repo=sandbox.profile.github_repo,
                                pr_number=state.deliverable_pr_number or 0,
                                commit_sha=state.head_commit_sha or "",
                                report_markdown=review_md,
                                blocking_issues=ev.reviewer_report.blocking_issues,
                            ),
                        )
                        state.pr_review_comment_url = rev_url

                    if followup_comment and state.deliverable_pr_number:
                        reply = (
                            f"Updated branch `{state.branch_name}` at `{(state.head_commit_sha or '')[:8]}` "
                            f"in response to review feedback.\n\n"
                            f"- Fail-to-Pass (`{state.verification_evidence.repro_test_target if state.verification_evidence else ''}`): ✅\n"
                            f"- Independent PR Review (`{state.verification_evidence.reviewer_report.verdict if (state.verification_evidence and state.verification_evidence.reviewer_report) else 'APPROVE'}`): ✅"
                        )
                        self.github.post_pr_comment(
                            github_repo=sandbox.profile.github_repo,
                            pr_number=state.deliverable_pr_number,
                            body=reply,
                        )

                    # Save before evaluating pre-merge readiness gates
                    self.store.save_task(
                        state,
                        step_name="pr_opened_and_reviewed",
                        note=f"PR #{state.deliverable_pr_number} opened & reviewed.",
                    )
                    self.check_and_emit_merge_approval(state.task_id)
                    refreshed = self.store.get_task(state.task_id)
                    if refreshed is not None:
                        state = refreshed
                except Exception as pr_exc:
                    # Branch is already pushed to GitHub; provide 1-click Compare/Create-PR link
                    state.deliverable_pr_url = (
                        f"https://github.com/{sandbox.profile.github_repo}/compare/"
                        f"{state.contract.base_branch}...{state.branch_name}?expand=1"
                    )
                    state.lifecycle_status = "DELIVERED_LOCAL_READY"
                    state.failure_reason = (
                        f"GitHub API 建单被拒 ({pr_exc}) — 请在 GitHub Token 中开启 "
                        f"`Pull requests: Read and write` 权限；分支 `{state.branch_name}` 已推送到远端。"
                    )
            elif branch_pushed:
                state.deliverable_pr_url = (
                    f"https://github.com/{sandbox.profile.github_repo}/compare/"
                    f"{state.contract.base_branch}...{state.branch_name}?expand=1"
                )
                state.lifecycle_status = "DELIVERED_LOCAL_READY"
            else:
                state.lifecycle_status = "DELIVERED_LOCAL_READY"
        else:
            state.lifecycle_status = "DELIVERED_LOCAL_READY"

        # Only send local-delivery card when NOT opening a GitHub PR.
        # For PR tasks, wait until BOTH Independent Code Review AND GitHub CI finish,
        # then push ONE single Merge Approval Card carrying the full Review report!
        if state.lifecycle_status == "DELIVERED_LOCAL_READY":
            card = build_evidence_report_card(state, self.web_base_url)
            self.store.write_artifact(state.task_id, "feishu_evidence_card.json", card)
            self.store.run_idempotent(
                state,
                f"notify_evidence_card_{(state.head_commit_sha or '')[:8]}",
                lambda: self.feishu.send_card(card, chat_id=state.feishu_chat_id),
            )
        return self.store.save_task(
            state,
            step_name="delivered",
            note=state.deliverable_pr_url or f"Local branch {state.branch_name} verified.",
        )

    def _transition_to_handoff(
        self,
        state: DurableTaskState,
        *,
        sandbox: WorktreeSandbox,
        failure_class: FailureClass,
        reason: str,
        clues: list[str],
    ) -> DurableTaskState:
        changed: list[str] = []
        if sandbox.worktree_path.exists():
            try:
                changed = sandbox.modified_files(state.contract.base_commit_sha)
            except Exception:
                changed = []

        state.failure_class = failure_class
        state.failure_reason = reason
        state.lifecycle_status = "HANDED_OFF_INCOMPLETE"
        state.handoff_bundle = HandoffBundle(
            reason=reason,
            failure_class=failure_class,
            root_cause_clues=clues,
            attempted_branch=state.branch_name,
            worktree_path=str(sandbox.worktree_path),
            modified_files=changed,
            failing_test_summary=clues[-1][:500] if clues else "",
            recommended_next_step=(
                f"Inspect preserved worktree at `{sandbox.worktree_path}` on branch `{state.branch_name}`."
            ),
        )
        has_review_block = bool(
            state.verification_evidence
            and state.verification_evidence.reviewer_report
            and state.verification_evidence.reviewer_report.verdict == "BLOCK"
        )
        card = (
            build_review_fix_card(state, self.web_base_url, trigger_source="ai_review")
            if has_review_block
            else build_handoff_card(state, self.web_base_url)
        )
        self.store.write_artifact(state.task_id, "feishu_handoff_card.json", card)
        self.store.write_artifact(
            state.task_id,
            "handoff_bundle.json",
            state.handoff_bundle.model_dump(),
        )
        self.store.run_idempotent(
            state,
            f"notify_handoff_card_{(state.head_commit_sha or 'none')[:8]}",
            lambda: self.feishu.send_card(card, chat_id=state.feishu_chat_id),
        )
        return self.store.save_task(state, step_name="handed_off", note=reason)

    def notify_review_fix_needed(
        self,
        state: DurableTaskState,
        *,
        trigger_source: str = "ai_review",
        human_comment: str = "",
        comment_author: str = "",
    ) -> str:
        """Push an interactive 1-Click Review Auto-Fix card to Feishu & store preview artifact."""
        card = build_review_fix_card(
            state,
            self.web_base_url,
            trigger_source=trigger_source,
            human_comment=human_comment,
            comment_author=comment_author,
        )
        self.store.write_artifact(state.task_id, "feishu_review_fix_card.json", card)
        try:
            return self.feishu.send_card(card, chat_id=state.feishu_chat_id)
        except Exception:
            return "local_artifact_only"

    def autofix_review_issues(
        self,
        task_id: str,
        *,
        extra_comment: str = "",
        triggered_by: str = "feishu_card",
    ) -> DurableTaskState:
        """1-Click Auto-Fix: wake up sandbox Coding Agent to resolve blocking AI/human Review comments."""
        state = self._require_task(task_id)
        parts: list[str] = []
        ev = state.verification_evidence
        if ev and ev.reviewer_report and ev.reviewer_report.blocking_issues:
            parts.append("Please resolve ALL of the following blocking PR Code Review issues:")
            for idx, b in enumerate(ev.reviewer_report.blocking_issues, start=1):
                loc = f"{b.path}:{b.line}" if (b.path and b.line) else (b.path or "diff")
                sug = f" | Suggested fix: {b.suggestion}" if b.suggestion else ""
                parts.append(f"{idx}. [{b.severity} · {b.category}] {loc} — {b.message}{sug}")

        human_cmt = extra_comment.strip() or state.recorded_operations.get("latest_human_pr_comment", "")
        if human_cmt:
            parts.append(f"Human PR Review Comment to address:\n{human_cmt}")

        instruction = "\n".join(parts) or "Address the latest PR review feedback and re-verify tests."
        state.lifecycle_status = "QUEUED"
        state.failure_class = "NONE"
        state.failure_reason = None
        state.handoff_bundle = None
        self.append_live_event(
            state,
            attempt=state.sandbox_attempt_count + 1,
            kind="autofix",
            icon="🔧",
            title=f"收到一键修复指令 (`{triggered_by}`)，正在按 Review 意见自动修复…",
            detail=instruction[:300],
        )
        self.store.save_task(
            state,
            step_name="autofix_review_triggered",
            note=f"Triggered by {triggered_by}: {instruction[:200]}",
        )
        return self.execute_task(
            task_id,
            publish_pr=state.auto_publish_pr or self.github.is_configured,
            followup_comment=instruction,
        )

    def handle_feishu_text_message(
        self,
        *,
        raw_text: str,
        chat_id: str = "",
        source_channel: str = "feishu",
        auto_execute: bool = True,
    ) -> DurableTaskState | None:
        """Unified Feishu incoming text router: handles new tasks, quick clarifications & follow-ups."""
        repos = list(self.settings.load_repositories().keys())
        parsed = parse_feishu_command(raw_text, repos)
        intent = parsed.get("intent")

        if intent == "empty":
            return None

        if intent == "bind_ping":
            if chat_id:
                self.bind_feishu_default_chat_id(chat_id)
            from digital_employee.integrations.feishu_cards import build_test_connection_card

            try:
                self.feishu.send_card(build_test_connection_card(self.web_base_url), chat_id=chat_id)
            except Exception:
                pass
            return None

        if intent == "quick_approve_merge":
            waiting_merge = self.store.list_tasks(status="WAITING_MERGE_APPROVAL", limit=10)
            target_m = next(
                (t for t in waiting_merge if not chat_id or t.feishu_chat_id == chat_id),
                waiting_merge[0] if waiting_merge else None,
            )
            if target_m is None:
                return None
            self.approve_and_merge(target_m.task_id, approved_by="feishu_reply")
            return self.store.get_task(target_m.task_id)

        if intent == "quick_autofix_review":
            candidates = self.store.list_tasks(limit=20)
            target_fix = next(
                (
                    t
                    for t in candidates
                    if not self.is_task_actively_running(t.task_id)
                    and (
                        (
                            t.verification_evidence
                            and t.verification_evidence.reviewer_report
                            and t.verification_evidence.reviewer_report.verdict == "BLOCK"
                        )
                        or "latest_human_pr_comment" in t.recorded_operations
                    )
                ),
                None,
            )
            if target_fix is None:
                return None
            return self.autofix_review_issues(target_fix.task_id, triggered_by="feishu_reply")

        if intent == "task_approve_merge":
            self.approve_and_merge(parsed["task_id"], approved_by="feishu_reply")
            return self.store.get_task(parsed["task_id"])

        if intent == "quick_clarify":
            waiting = self.store.list_tasks(status="WAITING_CLARIFICATION", limit=10)
            target = next(
                (t for t in waiting if not chat_id or t.feishu_chat_id == chat_id),
                waiting[0] if waiting else None,
            )
            if target is None:
                return None
            choice = parsed.get("choice", "默认")
            answers: dict[str, str] = {}
            if choice in ("1", "2") and target.contract.unresolved_decisions:
                idx = int(choice) - 1
                opts = target.contract.unresolved_decisions[0].options
                if 0 <= idx < len(opts):
                    answers["0"] = opts[idx]
            return self.resolve_clarification(
                target.task_id,
                answers=answers,
                use_defaults=not bool(answers),
                auto_execute=auto_execute,
                publish_pr=self.github.is_configured,
            )

        if intent == "task_clarify_default":
            return self.resolve_clarification(
                parsed["task_id"],
                use_defaults=True,
                auto_execute=auto_execute,
                publish_pr=self.github.is_configured,
            )

        if intent == "followup":
            if not auto_execute:
                return self.store.get_task(parsed["task_id"])
            return self.handle_pr_review_comment(
                parsed["task_id"],
                comment_body=parsed["comment"],
                publish_pr=self.github.is_configured,
            )

        return self.submit_task(
            raw_request=parsed["raw_request"],
            target_repo=parsed["target_repo"],
            auto_execute=auto_execute,
            publish_pr=self.github.is_configured,
            source_channel=source_channel,
            feishu_chat_id=chat_id,
        )

    def _format_gate_failures(self, ev, diag_log: str) -> str:
        issues: list[str] = []
        if not ev.scope_compliance_check:
            issues.append(f"- Scope violations: {ev.scope_violations}")
        if not ev.tests_weakened_check:
            issues.append(f"- Anti-tampering violations (do NOT remove asserts or add skips): {ev.anti_tampering_violations}")
        if not ev.repro_test_passed_on_head:
            issues.append(f"- Target regression test `{ev.repro_test_target}` FAILED on your HEAD commit.")
        if not ev.repro_test_failed_on_base:
            issues.append(
                f"- Target regression test `{ev.repro_test_target}` did NOT fail on baseline `{ev.verified_commit_sha[:8]}`! "
                "Provide or write a unit test in `tests/` that reproduces the bug on baseline (FAIL) and passes with your fix (PASS)."
            )
        if not ev.existing_test_suite_passed:
            issues.append("- Existing related test suite failed on HEAD.")
        if not ev.static_analysis_passed:
            issues.append("- Static linter check failed on modified Python files.")
        if ev.reviewer_report and ev.reviewer_report.blocking_issues:
            formatted_blocks = [
                f"[{b.severity} {b.category}] {b.path}:{b.line or '?'} -> {b.message}"
                for b in ev.reviewer_report.blocking_issues
            ]
            issues.append(f"- Independent PR Review BLOCKING issues: {formatted_blocks}")
        if ev.reviewer_report and not ev.reviewer_report.ac_covered:
            issues.append(f"- Reviewer found unmet Acceptance Criteria: {ev.reviewer_report.ac_assessment}")
        if ev.reviewer_report and not ev.reviewer_report.decision_ledger_compliant:
            issues.append(f"- Reviewer found DecisionLedger policy violations: {ev.reviewer_report.ledger_violations}")

        return "\n".join(issues) + "\n\nRecent test log tail:\n" + diag_log[-2500:]

    def check_and_emit_merge_approval(self, task_id: str) -> dict[str, str | bool]:
        """Evaluate the 4 Automated Pre-Merge Gates.

        Once all 4 pass (Sandbox Fail-to-Pass + Independent PR Review APPROVE + Remote CI Green +
        Conflict-free SHA lock), push an Interactive Merge Approval Card to Console & Feishu and
        wait for human approval (`WAITING_MERGE_APPROVAL`), unless unattended `auto_merge` is forced.
        """
        state = self._require_task(task_id)
        if not (self.github.is_configured and state.deliverable_pr_number):
            return {"ready": False, "merged": False, "reason": "no_remote_pr"}

        ev = state.verification_evidence
        if not (ev and ev.passed_all_hard_gates and ev.reviewer_report):
            return {"ready": False, "merged": False, "reason": "local_or_review_gate_not_passed"}
        if ev.reviewer_report.verdict != "APPROVE" or len(ev.reviewer_report.blocking_issues) > 0:
            return {"ready": False, "merged": False, "reason": "pr_review_blocked"}

        profile = self.settings.resolve_repository(state.contract.target_repo)
        gh_repo = profile.github_repo
        pr_num = state.deliverable_pr_number

        pr_data = self.github.fetch_pr_state(github_repo=gh_repo, pr_number=pr_num)
        if pr_data.get("merged"):
            state.lifecycle_status = "ACCEPTED_AND_CLOSED"
            self.store.save_task(state, step_name="pr_already_merged", note=f"PR #{pr_num} merged.")
            return {"ready": True, "merged": True, "reason": "already_merged"}
        if pr_data.get("state") != "open":
            return {"ready": False, "merged": False, "reason": "pr_not_open"}

        if pr_data.get("draft") and pr_data.get("node_id"):
            self.github.mark_pr_ready_for_review(str(pr_data["node_id"]))

        remote_head_sha = str((pr_data.get("head") or {}).get("sha") or "")
        if remote_head_sha != ev.verified_commit_sha:
            return {"ready": False, "merged": False, "reason": "head_sha_mismatch"}
        if pr_data.get("mergeable") is False:
            return {"ready": False, "merged": False, "reason": "merge_conflict"}

        expect_wf = (Path(profile.local_path) / ".github" / "workflows").is_dir()
        ci_info = self.github.inspect_commit_ci_status(
            github_repo=gh_repo,
            commit_sha=ev.verified_commit_sha,
            expect_workflows=expect_wf,
        )
        if ci_info["state"] != "passed":
            return {"ready": False, "merged": False, "reason": f"ci_{ci_info['state']}"}

        # All 4 Automated Gates GREEN -> Build & Push Merge Approval Card!
        state.merge_approval_card = MergeApprovalCard(
            pr_number=pr_num,
            pr_url=state.deliverable_pr_url or pr_data.get("html_url", ""),
            verified_commit_sha=ev.verified_commit_sha,
            ci_checks_count=int(ci_info["total"]),
            reviewer_model=ev.reviewer_report.reviewer_model,
            reviewer_verdict=ev.reviewer_report.verdict,
            reviewer_summary=ev.reviewer_report.summary,
            repro_test_target=ev.repro_test_target,
            residual_risk=(ev.unverified_risks[0] if ev.unverified_risks else ""),
        )

        if state.auto_merge:
            return self.approve_and_merge(task_id, approved_by="unattended_auto_policy")

        if state.lifecycle_status != "WAITING_MERGE_APPROVAL":
            state.lifecycle_status = "WAITING_MERGE_APPROVAL"
            feishu_card = build_merge_approval_card(state, self.web_base_url)
            self.store.write_artifact(task_id, "feishu_merge_approval_card.json", feishu_card)
            self.store.run_idempotent(
                state,
                f"notify_merge_approval_{ev.verified_commit_sha[:8]}",
                lambda: self.feishu.send_card(feishu_card, chat_id=state.feishu_chat_id),
            )
            self.store.save_task(
                state,
                step_name="waiting_merge_approval",
                note=f"PR #{pr_num} passed Review & CI ({ci_info['total']} green); pushed approval card.",
            )
        return {"ready": True, "merged": False, "reason": "waiting_human_approval"}

    def approve_and_merge(
        self, task_id: str, *, approved_by: str = "console_user"
    ) -> dict[str, str | bool]:
        """Execute atomic SHA-locked Squash Merge after human approval on Console or Feishu Card."""
        state = self._require_task(task_id)
        if not (self.github.is_configured and state.deliverable_pr_number):
            return {"merged": False, "reason": "no_remote_pr"}

        ev = state.verification_evidence
        if not (ev and ev.passed_all_hard_gates and ev.reviewer_report):
            return {"merged": False, "reason": "local_or_review_gate_not_passed"}
        if ev.reviewer_report.verdict != "APPROVE" or len(ev.reviewer_report.blocking_issues) > 0:
            return {"merged": False, "reason": "pr_review_blocked"}

        profile = self.settings.resolve_repository(state.contract.target_repo)
        gh_repo = profile.github_repo
        pr_num = state.deliverable_pr_number

        pr_data = self.github.fetch_pr_state(github_repo=gh_repo, pr_number=pr_num)
        if pr_data.get("merged"):
            state.lifecycle_status = "ACCEPTED_AND_CLOSED"
            self.store.save_task(state, step_name="pr_already_merged", note=f"PR #{pr_num} merged.")
            return {"merged": True, "reason": "already_merged"}
        if pr_data.get("state") != "open":
            return {"merged": False, "reason": "pr_not_open"}

        if pr_data.get("draft") and pr_data.get("node_id"):
            self.github.mark_pr_ready_for_review(str(pr_data["node_id"]))

        remote_head_sha = str((pr_data.get("head") or {}).get("sha") or "")
        if remote_head_sha != ev.verified_commit_sha:
            return {"merged": False, "reason": "head_sha_mismatch"}
        if pr_data.get("mergeable") is False:
            return {"merged": False, "reason": "merge_conflict"}

        expect_wf = (Path(profile.local_path) / ".github" / "workflows").is_dir()
        ci_info = self.github.inspect_commit_ci_status(
            github_repo=gh_repo,
            commit_sha=ev.verified_commit_sha,
            expect_workflows=expect_wf,
        )
        if ci_info["state"] != "passed":
            return {"merged": False, "reason": f"ci_{ci_info['state']}"}

        merge_res = self.github.merge_pull_request(
            github_repo=gh_repo,
            pr_number=pr_num,
            expected_head_sha=ev.verified_commit_sha,
            commit_title=f"fix: {state.contract.objective[:72]} (#{pr_num})",
        )
        merge_sha = str(merge_res.get("sha") or "")
        state.merged_commit_sha = merge_sha
        state.lifecycle_status = "ACCEPTED_AND_CLOSED"
        if state.merge_approval_card is not None:
            state.merge_approval_card.approved_by = approved_by
            state.merge_approval_card.approved_at = utc_now_iso()

        self.github.post_pr_comment(
            github_repo=gh_repo,
            pr_number=pr_num,
            body=(
                f"✅ **Approved by `{approved_by}` & Squash-Merged (`{merge_sha[:8]}`)**\n"
                f"- Sandbox `Fail → Pass` & Anti-Tampering ✅\n"
                f"- Independent PR Review (`{ev.reviewer_report.reviewer_model}`) `APPROVE` ✅\n"
                f"- GitHub Actions CI (`{ci_info['total']}` green) ✅"
            ),
        )
        self.store.save_task(
            state,
            step_name="approved_and_merged_pr",
            note=f"Approved by {approved_by}; merged PR #{pr_num} -> {merge_sha[:10]}",
        )
        return {"merged": True, "reason": "merged", "sha": merge_sha}

    # Alias kept for unit-test compatibility when auto_merge=True is explicitly tested
    def try_auto_merge(self, task_id: str) -> dict[str, str | bool]:
        return self.check_and_emit_merge_approval(task_id)

    def _require_task(self, task_id: str) -> DurableTaskState:
        state = self.store.get_task(task_id)
        if state is None:
            raise KeyError(f"Task '{task_id}' not found in DurableStore.")
        return state
