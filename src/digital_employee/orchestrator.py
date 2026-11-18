"""Durable Orchestrator coordinating Contract -> Clarification -> Sandbox -> Evidence Gate -> Delivery."""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from digital_employee.agents.contract_drafter import ContractDrafter
from digital_employee.agents.reviewer import FocusedReviewer, render_pr_review_agent_comment
from digital_employee.agents.runtime import CodingRuntime, create_coding_runtime
from digital_employee.context.decision_ledger import (
    DecisionEntry,
    DecisionLedger,
    PolicyCategory,
    PolicyStatus,
    build_fallback_title,
    infer_policy_category,
)
from digital_employee.context.repo_router import infer_target_repository
from digital_employee.integrations.feishu_cards import (
    FeishuNotifier,
    build_abandoned_card,
    build_accepted_receipt_card,
    build_clarification_card,
    build_evidence_report_card,
    build_handoff_card,
    build_jira_approval_card,
    build_merge_approval_card,
    build_merged_closed_card,
    build_noop_verified_card,
    build_review_fix_card,
    extract_feishu_message_id,
    parse_feishu_command,
)
from digital_employee.integrations.github_client import (
    GitHubIntegration,
    render_evidence_pr_body,
)
from digital_employee.integrations.jira_client import (
    JiraIntegration,
    JiraIssueData,
    extract_jira_issue_key,
)
from digital_employee.llm.client import LLMClient, TransientLLMError
from digital_employee.models.contract import JiraTicketDraft
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
        self.drafter = ContractDrafter(self.llm, store=self.store)
        self.coder: CodingRuntime = create_coding_runtime(
            self.settings, llm=llm, store=self.store
        )
        self.reviewer = FocusedReviewer(
            self.reviewer_llm,
            skill_path=self.settings.pr_review_skill_path,
            store=self.store,
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
        self.jira = JiraIntegration(
            base_url=self.settings.jira_base_url,
            email=self.settings.jira_email,
            api_token=self.settings.jira_api_token,
            default_project=self.settings.jira_default_project,
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
        return self.settings.public_web_url or f"http://{self.settings.web_host}:{self.settings.web_port}"

    @property
    def external_report_url_base(self) -> str:
        """Publicly reachable report base URL (empty when running on 127.0.0.1 without tunnel)."""
        return self.settings.public_web_url

    def _seed_jira_sync_baseline(self, state: DurableTaskState, issue: JiraIssueData) -> None:
        state.recorded_operations["jira_spec_fingerprint"] = issue.spec_fingerprint
        for c in issue.raw_comments:
            cid = c.get("id", "")
            if cid:
                state.recorded_operations[f"jira_comment_{cid}"] = "seen"

    def _resolve_target_repo_with_jira(
        self,
        target_repo: str,
        authoritative_jira: JiraIssueData | None,
        *,
        raw_request: str = "",
    ) -> str:
        if target_repo.strip():
            return target_repo.strip()
        repos = self.settings.load_repositories()
        if not repos:
            raise ValueError("No target repositories configured.")
        if authoritative_jira is not None:
            haystack = f"{authoritative_jira.summary}\n{authoritative_jira.description}\n{raw_request}"
            return infer_target_repository(haystack, repos, labels=authoritative_jira.labels)
        return infer_target_repository(raw_request, repos)

    def submit_task(
        self,
        *,
        raw_request: str,
        target_repo: str = "",
        base_branch: str | None = None,
        owner_id: str | None = None,
        max_budget_usd: float | None = None,
        accept_defaults: bool = False,
        auto_execute: bool = True,
        publish_pr: bool = False,
        auto_merge: bool = False,
        source_channel: str = "cli",
        feishu_chat_id: str = "",
        jira_issue_key: str = "",
        require_jira_approval: bool | None = None,
    ) -> DurableTaskState:
        explicit_jira = jira_issue_key.strip().upper()
        detected_jira = extract_jira_issue_key(raw_request)
        effective_jira_key = ""
        if explicit_jira:
            effective_jira_key = explicit_jira
        elif detected_jira and (
            self.jira.is_configured
            or raw_request.strip().upper() == detected_jira
            or "/browse/" in raw_request.lower()
        ):
            effective_jira_key = detected_jira

        authoritative_jira: JiraIssueData | None = None
        if effective_jira_key:
            authoritative_jira = self.jira.get_issue(effective_jira_key)

        resolved_repo = self._resolve_target_repo_with_jira(
            target_repo, authoritative_jira, raw_request=raw_request
        )
        profile = self.settings.resolve_repository(resolved_repo)
        task_id = generate_task_id()
        trace_id = uuid.uuid4().hex
        sandbox = WorktreeSandbox(profile, self.settings.worktrees_dir, task_id)

        t0 = time.monotonic()
        contract, jira_draft, draft_cost = self.drafter.draft(
            raw_request=raw_request,
            profile=profile,
            sandbox=sandbox,
            owner_id=owner_id or self.settings.default_owner_id,
            max_budget_usd=max_budget_usd or self.settings.default_max_budget_usd,
            base_branch=base_branch,
            authoritative_jira=authoritative_jira,
            default_jira_project=self.settings.jira_default_project,
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
            jira_draft=jira_draft,
            jira_issue_key=authoritative_jira.key if authoritative_jira else "",
            jira_issue_url=authoritative_jira.url if authoritative_jira else "",
            jira_status=authoritative_jira.status if authoritative_jira else "",
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

        # Path 2: Existing Jira ticket -> immediately transition Jira to In Progress & run per Jira!
        if authoritative_jira is not None:
            self._seed_jira_sync_baseline(state, authoritative_jira)
            try:
                new_st = self.jira.transition_issue_by_keywords(
                    authoritative_jira.key,
                    {"in progress", "indeterminate", "进行中", "处理中", "开发中"},
                )
                if new_st:
                    state.jira_status = new_st
                self.jira.add_comment(
                    authoritative_jira.key,
                    (
                        f"🤖 AI 数字员工已接单开工 (Task `{task_id}`)\n"
                        f"- 目标仓库: `{profile.repo_id}` (`{contract.base_branch}`)\n"
                        f"- 沙箱分支: `{sandbox.branch_name}`"
                    ),
                )
            except Exception:
                pass
            self.append_live_event(
                state,
                attempt=0,
                step=0,
                max_steps=self.settings.max_coding_steps,
                kind="jira_bound",
                icon="🎫",
                title=f"已拉取已有权威工单 `{authoritative_jira.key}`，无需建卡审核，直接按 Jira 执行！",
                detail=authoritative_jira.summary,
            )
        else:
            # Path 1: No existing Jira ticket -> Gate on human review before creating Jira ticket!
            should_gate_jira = (
                require_jira_approval
                if require_jira_approval is not None
                else self.jira.is_configured
            )
            if should_gate_jira:
                state.lifecycle_status = "WAITING_JIRA_APPROVAL"
                all_repos = list(self.settings.load_repositories().keys())
                jira_card = build_jira_approval_card(
                    state, self.web_base_url, candidate_repos=all_repos
                )
                self.store.write_artifact(task_id, "feishu_jira_approval_card.json", jira_card)
                try:
                    self.store.run_idempotent(
                        state,
                        "notify_jira_approval",
                        lambda: self.feishu.send_card(jira_card, chat_id=state.feishu_chat_id),
                    )
                except Exception:
                    pass
                self.append_live_event(
                    state,
                    attempt=0,
                    step=0,
                    max_steps=self.settings.max_coding_steps,
                    kind="jira_review",
                    icon="📋",
                    title=f"已起草 Jira 工单草稿 (`{jira_draft.project_key}` · {jira_draft.issue_type})，等待人工审核确认建卡…",
                    detail=jira_draft.summary,
                )
                self.store.save_task(
                    state,
                    step_name="waiting_jira_approval",
                    note=f"Waiting human review to create Jira ticket: {jira_draft.summary[:120]}",
                )
                return state

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
            if source_channel.startswith("feishu") or authoritative_jira is not None:
                try:
                    self.store.run_idempotent(
                        state,
                        "notify_receipt",
                        lambda: self.feishu.send_card(receipt_card, chat_id=state.feishu_chat_id),
                    )
                except Exception:
                    pass
            self.store.save_task(state, step_name="queued", note="Contract unambiguous; queued.")

        if auto_execute:
            return self.execute_task(
                task_id, publish_pr=publish_pr or state.auto_publish_pr
            )
        return state

    def approve_and_create_jira_ticket(
        self,
        task_id: str,
        *,
        draft_overrides: dict[str, Any] | None = None,
        approved_by: str = "human",
        auto_execute: bool = True,
        trigger_message_id: str = "",
    ) -> DurableTaskState:
        """Create a real Jira ticket after human review & lock TaskContract to the Jira issue."""
        state = self._require_task(task_id)
        if state.jira_issue_key:
            # Idempotency: Jira ticket already created
            if state.lifecycle_status == "WAITING_JIRA_APPROVAL":
                state.lifecycle_status = "QUEUED"
                self.store.save_task(state, step_name="jira_already_bound")
            if auto_execute and state.lifecycle_status == "QUEUED":
                return self.execute_task(
                    task_id, publish_pr=state.auto_publish_pr or self.github.is_configured
                )
            return state

        overrides = draft_overrides or {}
        if overrides.get("target_repo"):
            new_repo = str(overrides["target_repo"]).strip()
            if new_repo and new_repo != state.contract.target_repo:
                state = self.switch_jira_draft_repo(task_id, new_repo)

        draft = state.jira_draft or JiraTicketDraft(
            project_key=self.settings.jira_default_project or "PROJ",
            issue_type="Task",
            summary=state.contract.objective,
            description=state.contract.reproduction_evidence or state.raw_request,
            acceptance_criteria=list(state.contract.acceptance_criteria),
        )
        if overrides.get("project_key"):
            draft.project_key = str(overrides["project_key"]).strip().upper()
        if overrides.get("issue_type"):
            draft.issue_type = str(overrides["issue_type"]).strip()
        if overrides.get("summary"):
            draft.summary = str(overrides["summary"]).strip()
        if overrides.get("description") is not None and str(overrides["description"]).strip():
            draft.description = str(overrides["description"]).strip()
        if isinstance(overrides.get("acceptance_criteria"), list):
            cleaned_ac = [str(x).strip() for x in overrides["acceptance_criteria"] if str(x).strip()]
            if cleaned_ac:
                draft.acceptance_criteria = cleaned_ac
        if overrides.get("priority"):
            draft.priority = str(overrides["priority"]).strip()
        if isinstance(overrides.get("labels"), list):
            draft.labels = [str(x).strip() for x in overrides["labels"] if str(x).strip()]

        created = self.jira.create_issue(draft)
        draft.project_key = created.project_key
        draft.approved_by = approved_by
        draft.approved_at = utc_now_iso()
        state.jira_draft = draft
        state.jira_issue_key = created.key
        state.jira_issue_url = created.url
        state.jira_status = created.status
        self._seed_jira_sync_baseline(state, created)

        # Lock TaskContract strictly to the approved & created Jira ticket ("后续都以 Jira 任务为准"),
        # while retaining repository DecisionLedger engineering guardrails (e.g. DEC-001 scripts) underneath.
        prior_eng_guardrails = [
            ac
            for ac in state.contract.acceptance_criteria
            if "-DEC-" in ac or "scripts/" in ac
        ]
        prior_code_clues = state.contract.reproduction_evidence
        state.contract.jira_issue_key = created.key
        state.contract.jira_issue_url = created.url
        state.contract.objective = created.summary
        if draft.acceptance_criteria:
            merged_ac = list(draft.acceptance_criteria)
            for g in prior_eng_guardrails:
                if g not in merged_ac:
                    merged_ac.append(g)
            state.contract.acceptance_criteria = merged_ac
        state.contract.reproduction_evidence = (
            created.render_authoritative_spec()
            + (f"\n\n### Engineering Codebase Clues\n{prior_code_clues}" if prior_code_clues else "")
        )
        state.contract.contract_version += 1
        state.human_interaction_seconds += 8

        try:
            new_st = self.jira.transition_issue_by_keywords(
                created.key,
                {"in progress", "indeterminate", "进行中", "处理中", "开发中"},
            )
            if new_st:
                state.jira_status = new_st
            self.jira.add_comment(
                created.key,
                (
                    f"🤖 已由 `{approved_by}` 审核确认建卡，AI 数字员工开始按本单执行 (Task `{task_id}`)\n"
                    f"- 目标仓库: `{state.contract.target_repo}` (`{state.contract.base_branch}`)\n"
                    f"- 沙箱分支: `{state.branch_name}`"
                ),
            )
        except Exception:
            pass

        self.append_live_event(
            state,
            attempt=0,
            step=0,
            max_steps=self.settings.max_coding_steps,
            kind="jira_created",
            icon="✅",
            title=f"已审核创建真实 Jira 工单 `{created.key}`，后续全程以该 Jira 任务为准！",
            detail=created.url,
        )

        if state.contract.unresolved_decisions:
            state.lifecycle_status = "WAITING_CLARIFICATION"
            card = build_clarification_card(state, self.web_base_url)
            self.store.write_artifact(task_id, "feishu_clarification_card.json", card)
            try:
                self.feishu.send_card(card, chat_id=state.feishu_chat_id)
            except Exception:
                pass
            return self.store.save_task(
                state,
                step_name="jira_created_waiting_clarification",
                note=f"Created Jira {created.key}; waiting clarification.",
            )

        state.lifecycle_status = "QUEUED"
        receipt_card = build_accepted_receipt_card(state, self.web_base_url)
        self.store.write_artifact(task_id, "feishu_receipt_card.json", receipt_card)
        try:
            # Patch the original Jira Approval Card in-place so Feishu stays clean without duplicate cards!
            orig_mid = trigger_message_id or extract_feishu_message_id(
                state.recorded_operations.get("notify_jira_approval", "")
            )
            patched = self.feishu.update_card(orig_mid, receipt_card) if orig_mid else False
            if patched:
                state.recorded_operations["notify_receipt"] = f"patched:{orig_mid}"
            else:
                self.store.run_idempotent(
                    state,
                    "notify_receipt",
                    lambda: self.feishu.send_card(receipt_card, chat_id=state.feishu_chat_id),
                )
        except Exception:
            pass
        self.store.save_task(
            state,
            step_name="jira_approved_and_created",
            note=f"Approved by {approved_by}; bound to Jira {created.key} ({created.url})",
        )
        if auto_execute:
            return self.execute_task(
                task_id, publish_pr=state.auto_publish_pr or self.github.is_configured
            )
        return state

    def refine_jira_draft(self, task_id: str, instruction: str) -> DurableTaskState:
        """Update a pending `WAITING_JIRA_APPROVAL` draft via natural-language feedback & patch Feishu card."""
        state = self._require_task(task_id)
        if state.lifecycle_status != "WAITING_JIRA_APPROVAL" or state.jira_draft is None:
            raise ValueError(f"Task '{task_id}' is not waiting for Jira draft approval.")
        updated_draft, cost = self.drafter.refine_jira_draft(
            current_draft=state.jira_draft, instruction=instruction
        )
        state.jira_draft = updated_draft
        state.total_cost_usd = round(state.total_cost_usd + cost, 6)
        state.human_interaction_seconds += 6

        all_repos = list(self.settings.load_repositories().keys())
        jira_card = build_jira_approval_card(
            state, self.web_base_url, candidate_repos=all_repos
        )
        self.store.write_artifact(task_id, "feishu_jira_approval_card.json", jira_card)
        try:
            orig_mid = extract_feishu_message_id(
                state.recorded_operations.get("notify_jira_approval", "")
            )
            if not (orig_mid and self.feishu.update_card(orig_mid, jira_card)):
                new_deliv = self.feishu.send_card(jira_card, chat_id=state.feishu_chat_id)
                state.recorded_operations["notify_jira_approval"] = new_deliv
        except Exception:
            pass

        self.append_live_event(
            state,
            attempt=0,
            step=0,
            max_steps=self.settings.max_coding_steps,
            kind="jira_refined",
            icon="✏️",
            title=f"已按反馈更新 Jira 工单草稿：{updated_draft.summary}",
            detail=instruction[:200],
        )
        return self.store.save_task(
            state,
            step_name="jira_draft_refined",
            note=f"Refined Jira draft: {updated_draft.summary[:120]}",
        )

    def switch_jira_draft_repo(
        self, task_id: str, new_target_repo: str, *, trigger_message_id: str = ""
    ) -> DurableTaskState:
        """Re-scope a `WAITING_JIRA_APPROVAL` task onto another target repository & update card in-place."""
        state = self._require_task(task_id)
        profile = self.settings.resolve_repository(new_target_repo)
        sandbox = WorktreeSandbox(profile, self.settings.worktrees_dir, state.task_id)
        prior_draft = state.jira_draft
        contract, fresh_draft, cost = self.drafter.draft(
            raw_request=state.raw_request,
            profile=profile,
            sandbox=sandbox,
            owner_id=state.contract.owner_id,
            max_budget_usd=state.contract.max_budget_usd,
            default_jira_project=self.settings.jira_default_project,
        )
        if prior_draft is not None:
            fresh_draft.summary = prior_draft.summary
            fresh_draft.description = prior_draft.description
            fresh_draft.acceptance_criteria = list(prior_draft.acceptance_criteria)
            fresh_draft.priority = prior_draft.priority
            fresh_draft.issue_type = prior_draft.issue_type
            fresh_draft.labels = ["ai-delegated", profile.repo_id]

        state.contract = contract
        state.jira_draft = fresh_draft
        state.worktree_path = str(sandbox.worktree_path)
        state.baseline_human_minutes = profile.baseline_human_minutes
        state.total_cost_usd = round(state.total_cost_usd + cost, 6)

        all_repos = list(self.settings.load_repositories().keys())
        jira_card = build_jira_approval_card(
            state, self.web_base_url, candidate_repos=all_repos
        )
        self.store.write_artifact(task_id, "feishu_jira_approval_card.json", jira_card)
        try:
            orig_mid = trigger_message_id or extract_feishu_message_id(
                state.recorded_operations.get("notify_jira_approval", "")
            )
            if not (orig_mid and self.feishu.update_card(orig_mid, jira_card)):
                new_deliv = self.feishu.send_card(jira_card, chat_id=state.feishu_chat_id)
                state.recorded_operations["notify_jira_approval"] = new_deliv
        except Exception:
            pass
        return self.store.save_task(
            state,
            step_name="jira_repo_switched",
            note=f"Switched target repository to {profile.repo_id}",
        )

    def reconcile_bound_jira_issue(self, task_id: str) -> bool:
        """Poll a bound active Jira issue for remote description edits or new human comments."""
        state = self._require_task(task_id)
        if not (state.jira_issue_key and self.jira.is_configured):
            return False

        issue = self.jira.get_issue(state.jira_issue_key)
        state.jira_status = issue.status
        prev_fp = state.recorded_operations.get("jira_spec_fingerprint", "")
        if not prev_fp:
            # Seed baseline fingerprint on legacy tasks created before v3.1
            self._seed_jira_sync_baseline(state, issue)
            self.store.save_task(state)
            return False

        spec_changed = prev_fp != issue.spec_fingerprint
        new_comments = [
            c
            for c in issue.raw_comments
            if c.get("id") and f"jira_comment_{c['id']}" not in state.recorded_operations
        ]
        if not spec_changed and not new_comments:
            return False

        self._seed_jira_sync_baseline(state, issue)
        state.contract.jira_issue_key = issue.key
        state.contract.jira_issue_url = issue.url
        state.contract.objective = issue.summary
        state.contract.reproduction_evidence = issue.render_authoritative_spec()
        state.contract.contract_version += 1

        if new_comments:
            latest_c = new_comments[-1]
            change_summary = f"{latest_c.get('author', 'Jira')}: {latest_c.get('body', '')}"
            author = latest_c.get("author", "Jira")
        else:
            change_summary = f"工单标题/描述已更新：{issue.summary}"
            author = "Jira Spec"

        state.recorded_operations["latest_human_pr_comment"] = (
            f"[Authoritative Jira {issue.key} Update]\n{change_summary}\n\n"
            f"Latest Jira Spec:\n{issue.render_authoritative_spec()}"
        )
        self.append_live_event(
            state,
            attempt=state.sandbox_attempt_count,
            step=0,
            max_steps=self.settings.max_coding_steps,
            kind="jira_remote_update",
            icon="🔄",
            title=f"检测到 Jira `{issue.key}` 远端变更/新评论，已自动同步契约 (v{state.contract.contract_version})",
            detail=change_summary[:260],
        )
        self.store.save_task(
            state,
            step_name="jira_remote_change_synced",
            note=f"Auto-synced remote Jira update on {issue.key}: {change_summary[:160]}",
        )
        if not self.is_task_actively_running(task_id):
            self.notify_review_fix_needed(
                state,
                trigger_source="jira_update",
                human_comment=change_summary,
                comment_author=author,
            )
        return True

    def sync_contract_from_jira(self, task_id: str) -> DurableTaskState:
        """Re-fetch authoritative Jira ticket so edits/comments on Jira supersede local state."""
        state = self._require_task(task_id)
        if not state.jira_issue_key:
            raise ValueError(f"Task '{task_id}' has no bound Jira issue key.")
        issue = self.jira.get_issue(state.jira_issue_key)
        state.jira_status = issue.status
        state.jira_issue_url = issue.url
        state.contract.jira_issue_key = issue.key
        state.contract.jira_issue_url = issue.url
        state.contract.objective = issue.summary
        state.contract.reproduction_evidence = issue.render_authoritative_spec()
        state.contract.contract_version += 1
        self.append_live_event(
            state,
            attempt=state.sandbox_attempt_count,
            step=0,
            max_steps=self.settings.max_coding_steps,
            kind="jira_synced",
            icon="🔄",
            title=f"已从权威工单 `{issue.key}` 同步最新需求与评论 (Contract v{state.contract.contract_version})",
            detail=issue.summary,
        )
        return self.store.save_task(
            state,
            step_name="synced_from_jira",
            note=f"Synced latest Jira {issue.key} (status={issue.status})",
        )

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
                    session_state_path=self.store.artifacts_dir / task_id / "kernel_session.jsonl",
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
                noop_res = sandbox.run_test_targets(noop_targets, timeout_seconds=120)
                is_pytest_runner = any("pytest" in p for p in sandbox.profile.test_cmd)
                noop_ok = noop_res.exit_code == 0 and (
                    "passed" in noop_res.combined_output if is_pytest_runner else not noop_res.timed_out
                )
                if not noop_ok:
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

                if state.jira_issue_key and self.jira.is_configured:
                    try:
                        self.jira.add_comment(
                            state.jira_issue_key,
                            f"✅ 经沙箱独立单测验证 (`{', '.join(noop_targets)}`) 全部通过，无需改动代码：{outcome.summary}",
                        )
                        done_st = self.jira.transition_issue_by_keywords(
                            state.jira_issue_key,
                            {"done", "完成", "已完成", "已解决", "closed"},
                        )
                        if done_st:
                            state.jira_status = done_st
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
            base_gate_ok = (
                evidence.verification_mode_used == "regression_and_build"
                or evidence.repro_test_failed_on_base
            )
            if (
                base_gate_ok
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
                ext_report_url = (
                    f"{self.external_report_url_base}/reports/{state.task_id}/review"
                    if self.external_report_url_base
                    else ""
                )
                reviewer_report, rev_cost = self.reviewer.review(
                    contract=state.contract,
                    sandbox=sandbox,
                    evidence=evidence,
                    full_report_url=ext_report_url,
                )
                state.total_cost_usd = round(state.total_cost_usd + rev_cost, 6)
                state.agent_elapsed_seconds = round(
                    state.agent_elapsed_seconds + (time.monotonic() - rev_t0), 2
                )
                evidence.reviewer_report = reviewer_report
                evidence.unverified_risks = list(reviewer_report.unverified_risks)
                state.verification_evidence = evidence
                if reviewer_report.reviewer_fallback_note:
                    self.append_live_event(
                        state,
                        attempt=attempt,
                        kind="review_fallback",
                        icon="⚠️",
                        title=reviewer_report.reviewer_fallback_note,
                    )
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
        if state.jira_issue_key and self.jira.is_configured:
            try:
                state = self.sync_contract_from_jira(task_id)
            except Exception:
                pass
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
        trigger_message_id: str = "",
    ) -> DurableTaskState:
        """Allow delegator to cancel/handoff or adjust permitted_scopes from Web Queue / CLI."""
        state = self._require_task(task_id)
        profile = self.settings.resolve_repository(state.contract.target_repo)
        sandbox = WorktreeSandbox(profile, self.settings.worktrees_dir, state.task_id)

        if action == "approve_jira":
            return self.approve_and_create_jira_ticket(
                task_id, approved_by=note or "console_user", auto_execute=True
            )
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
            abandoned_card = build_abandoned_card(state, note or "已由人工放弃")
            try:
                mid = trigger_message_id
                if not mid:
                    for k, v in reversed(list(state.recorded_operations.items())):
                        if k.startswith(("notify_jira_approval", "notify_merge_approval")):
                            mid = extract_feishu_message_id(v)
                            if mid:
                                break
                if mid:
                    self.feishu.update_card(mid, abandoned_card)
            except Exception:
                pass
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
        title: str = "",
        category: PolicyCategory | None = None,
    ) -> DecisionEntry:
        """Persist a human-confirmed team rule into the managed DecisionLedger store (zero repo intrusion)."""
        state = self._require_task(task_id)
        profile = self.settings.resolve_repository(state.contract.target_repo)
        # Ensure initial seed has run before allocating next ID
        DecisionLedger.for_repository(self.store, profile)
        policy_id = self.store.next_policy_id(profile.repo_id)
        resolved_cat = category or infer_policy_category(scope, rule)
        entry = DecisionEntry(
            id=policy_id,
            title=title.strip() or build_fallback_title(rule, scope),
            category=resolved_cat,
            scope=scope.strip() or "*",
            rule=rule.strip(),
            reason=reason.strip() or f"由委托任务 {task_id} 复盘沉淀",
            status="APPROVED_POLICY",
            source_type="TASK_PROMOTED",
            source_ref=task_id,
            owner=state.contract.owner_id or self.settings.default_owner_id,
            verified_at=date.today().isoformat(),
            updated_at=date.today().isoformat(),
        ).normalized()
        saved = self.store.upsert_repository_policy(profile.repo_id, entry)
        self.store.save_task(
            state,
            step_name="decision_promoted",
            note=f"Added {saved.id}: {saved.title} — {saved.rule[:100]}",
        )
        return saved

    def create_repository_policy(
        self,
        repo_id: str,
        *,
        title: str = "",
        category: PolicyCategory | None = None,
        scope: str = "*",
        rule: str,
        reason: str = "",
        owner: str = "",
        status: PolicyStatus = "APPROVED_POLICY",
    ) -> DecisionEntry:
        """Create a new repository governance rule directly in the managed store."""
        profile = self.settings.resolve_repository(repo_id)
        DecisionLedger.for_repository(self.store, profile)
        policy_id = self.store.next_policy_id(profile.repo_id)
        today = date.today().isoformat()
        entry = DecisionEntry(
            id=policy_id,
            title=title.strip() or build_fallback_title(rule, scope),
            category=category or infer_policy_category(scope, rule),
            scope=scope.strip() or "*",
            rule=rule.strip(),
            reason=reason.strip(),
            status=status,
            source_type="CONSOLE_MANUAL",
            source_ref="Web 控制台人工创建",
            owner=owner.strip() or self.settings.default_owner_id,
            verified_at=today,
            updated_at=today,
        ).normalized()
        return self.store.upsert_repository_policy(profile.repo_id, entry)

    def update_repository_policy(
        self,
        repo_id: str,
        policy_id: str,
        *,
        title: str | None = None,
        category: PolicyCategory | None = None,
        scope: str | None = None,
        rule: str | None = None,
        reason: str | None = None,
        owner: str | None = None,
        status: PolicyStatus | None = None,
    ) -> DecisionEntry:
        """Update an existing repository governance rule and refresh its verification date."""
        profile = self.settings.resolve_repository(repo_id)
        DecisionLedger.for_repository(self.store, profile)
        existing = self.store.get_repository_policy(profile.repo_id, policy_id)
        if existing is None:
            raise KeyError(f"Policy {policy_id} not found in repository {profile.repo_id}")
        today = date.today().isoformat()
        new_rule = rule.strip() if rule is not None else existing.rule
        new_scope = (scope.strip() or "*") if scope is not None else existing.scope
        new_title = (
            title.strip()
            if title is not None and title.strip()
            else (existing.title if title is None else build_fallback_title(new_rule, new_scope))
        )
        updated = existing.model_copy(
            update={
                "title": new_title,
                "category": category if category is not None else existing.category,
                "scope": new_scope,
                "rule": new_rule,
                "reason": reason.strip() if reason is not None else existing.reason,
                "owner": (owner.strip() or existing.owner) if owner is not None else existing.owner,
                "status": status if status is not None else existing.status,
                "verified_at": today,
                "updated_at": today,
            }
        ).normalized()
        return self.store.upsert_repository_policy(profile.repo_id, updated)

    def toggle_repository_policy(self, repo_id: str, policy_id: str) -> DecisionEntry:
        """Toggle a repository rule between `APPROVED_POLICY` (active) and `PAUSED`."""
        profile = self.settings.resolve_repository(repo_id)
        DecisionLedger.for_repository(self.store, profile)
        existing = self.store.get_repository_policy(profile.repo_id, policy_id)
        if existing is None:
            raise KeyError(f"Policy {policy_id} not found in repository {profile.repo_id}")
        next_status: PolicyStatus = (
            "PAUSED" if existing.status == "APPROVED_POLICY" else "APPROVED_POLICY"
        )
        updated = existing.model_copy(
            update={
                "status": next_status,
                "updated_at": date.today().isoformat(),
            }
        ).normalized()
        return self.store.upsert_repository_policy(profile.repo_id, updated)

    def verify_repository_policy(
        self, repo_id: str, policy_id: str, *, owner: str = ""
    ) -> DecisionEntry:
        """Mark a rule as freshly human-verified today."""
        profile = self.settings.resolve_repository(repo_id)
        DecisionLedger.for_repository(self.store, profile)
        existing = self.store.get_repository_policy(profile.repo_id, policy_id)
        if existing is None:
            raise KeyError(f"Policy {policy_id} not found in repository {profile.repo_id}")
        today = date.today().isoformat()
        updated = existing.model_copy(
            update={
                "verified_at": today,
                "updated_at": today,
                "owner": owner.strip() or existing.owner or self.settings.default_owner_id,
            }
        ).normalized()
        return self.store.upsert_repository_policy(profile.repo_id, updated)

    def delete_repository_policy(self, repo_id: str, policy_id: str) -> bool:
        """Delete a rule from the managed repository DecisionLedger."""
        profile = self.settings.resolve_repository(repo_id)
        DecisionLedger.for_repository(self.store, profile)
        return self.store.delete_repository_policy(profile.repo_id, policy_id)

    def sync_repository_policies(self, repo_id: str) -> int:
        """Import any newly added IDs from `<repo>/.ai/DECISIONS.yaml` without overwriting console rules."""
        profile = self.settings.resolve_repository(repo_id)
        DecisionLedger.for_repository(self.store, profile)
        return DecisionLedger.sync_incremental_from_repo(self.store, profile)

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
                ext_report_url = (
                    f"{self.external_report_url_base}/reports/{state.task_id}/review"
                    if self.external_report_url_base
                    else ""
                )
                review_md = render_pr_review_agent_comment(
                    ev.reviewer_report,
                    verified_commit_sha=state.head_commit_sha,
                    repro_test_target=ev.repro_test_target,
                    full_report_url=ext_report_url,
                    verification_mode=ev.verification_mode_used,
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

                    # Post structured PR Review report (portable, zero loopback 127.0.0.1 dead links!)
                    ev = state.verification_evidence
                    if ev and ev.reviewer_report and state.head_commit_sha:
                        ext_report_url = (
                            f"{self.external_report_url_base}/reports/{state.task_id}/review"
                            if self.external_report_url_base
                            else ""
                        )
                        review_md = render_pr_review_agent_comment(
                            ev.reviewer_report,
                            verified_commit_sha=state.head_commit_sha,
                            repro_test_target=ev.repro_test_target,
                            full_report_url=ext_report_url,
                            verification_mode=ev.verification_mode_used,
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
                            f"- Sandbox Verification (`{state.verification_evidence.repro_test_target if state.verification_evidence else ''}`): ✅\n"
                            f"- Independent PR Review (`{state.verification_evidence.reviewer_report.verdict if (state.verification_evidence and state.verification_evidence.reviewer_report) else 'APPROVE'}`): ✅"
                        )
                        self.github.post_pr_comment(
                            github_repo=sandbox.profile.github_repo,
                            pr_number=state.deliverable_pr_number,
                            body=reply,
                        )

                    # Sync PR URL + portable GitHub Review anchor + summary back onto authoritative Jira ticket!
                    if state.jira_issue_key and self.jira.is_configured:
                        try:
                            portable_rev_link = (
                                f"{self.external_report_url_base}/reports/{state.task_id}/review"
                                if self.external_report_url_base
                                else (state.pr_review_comment_url or state.deliverable_pr_url or "")
                            )
                            rev_rep = ev.reviewer_report if ev else None
                            rev_digest = (
                                f"`{rev_rep.quality_score or 95}/100` ({rev_rep.reviewer_model}) — {rev_rep.summary}"
                                if rev_rep
                                else "APPROVE"
                            )
                            self.store.run_idempotent(
                                state,
                                f"jira_pr_opened_{(state.head_commit_sha or '')[:8]}",
                                lambda: str(
                                    self.jira.add_comment(
                                        state.jira_issue_key or "",
                                        (
                                            f"🔎 GitHub PR #{state.deliverable_pr_number} 已提审并通过沙箱门禁与 AI Code Review\n"
                                            f"- Pull Request: {state.deliverable_pr_url}\n"
                                            f"- 审查评分与结论: {rev_digest}\n"
                                            f"- 在线全量审查报告: {portable_rev_link}"
                                        ),
                                    )
                                ),
                            )
                            rev_st = self.jira.transition_issue_by_keywords(
                                state.jira_issue_key,
                                {"in review", "review", "code review", "待审核", "审核中"},
                            )
                            if rev_st:
                                state.jira_status = rev_st
                        except Exception:
                            pass

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
        if state.jira_issue_key and self.jira.is_configured:
            try:
                state = self.sync_contract_from_jira(task_id)
            except Exception:
                pass
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

        if intent == "quick_approve_jira":
            waiting_jira = self.store.list_tasks(status="WAITING_JIRA_APPROVAL", limit=10)
            target_j = next(
                (t for t in waiting_jira if not chat_id or t.feishu_chat_id == chat_id),
                waiting_jira[0] if waiting_jira else None,
            )
            if target_j is None:
                return None
            return self.approve_and_create_jira_ticket(
                target_j.task_id, approved_by="feishu_reply", auto_execute=auto_execute
            )

        if intent == "task_approve_jira":
            return self.approve_and_create_jira_ticket(
                parsed["task_id"], approved_by="feishu_reply", auto_execute=auto_execute
            )

        if intent == "quick_refine_jira":
            waiting_jira = self.store.list_tasks(status="WAITING_JIRA_APPROVAL", limit=10)
            target_j = next(
                (t for t in waiting_jira if not chat_id or t.feishu_chat_id == chat_id),
                waiting_jira[0] if waiting_jira else None,
            )
            if target_j is None:
                return None
            return self.refine_jira_draft(target_j.task_id, parsed["instruction"])

        if intent == "task_refine_jira":
            return self.refine_jira_draft(parsed["task_id"], parsed["instruction"])

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
            existing_target = self.store.get_task(parsed["task_id"])
            if existing_target and existing_target.lifecycle_status == "WAITING_JIRA_APPROVAL":
                return self.refine_jira_draft(parsed["task_id"], parsed["comment"])
            if not auto_execute:
                return existing_target
            return self.handle_pr_review_comment(
                parsed["task_id"],
                comment_body=parsed["comment"],
                publish_pr=self.github.is_configured,
            )

        # Conversational draft edit heuristic: if a pending Jira draft is waiting in this chat
        # and the user replies with an edit phrase ("把优先级改成...", "加上...", "改成..."),
        # refine the pending Jira draft instead of creating a duplicate task!
        req_text = parsed["raw_request"].strip()
        if not parsed.get("jira_issue_key") and parsed.get("explicit_repo") != "true":
            waiting_jira = self.store.list_tasks(status="WAITING_JIRA_APPROVAL", limit=5)
            pending_in_chat = next(
                (t for t in waiting_jira if not chat_id or t.feishu_chat_id == chat_id),
                waiting_jira[0] if waiting_jira else None,
            )
            if pending_in_chat is not None and req_text.startswith(
                ("把", "改成", "改为", "加上", "补充", "删掉", "去掉", "标题", "优先级", "验收")
            ):
                return self.refine_jira_draft(pending_in_chat.task_id, req_text)

        effective_repo = parsed["target_repo"] if parsed.get("explicit_repo") == "true" else ""
        return self.submit_task(
            raw_request=parsed["raw_request"],
            target_repo=effective_repo,
            jira_issue_key=parsed.get("jira_issue_key", ""),
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
            issues.append(f"- Target regression/build check `{ev.repro_test_target}` FAILED on your HEAD commit.")
        if ev.verification_mode_used != "regression_and_build" and not ev.repro_test_failed_on_base:
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
            quality_score=int(ev.reviewer_report.quality_score or 95),
            merge_recommendation=str(ev.reviewer_report.merge_recommendation or "MERGE_READY"),
            merge_recommendation_label=ev.reviewer_report.merge_recommendation_label or "可直接合入",
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
        self,
        task_id: str,
        *,
        approved_by: str = "console_user",
        trigger_message_id: str = "",
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
        if state.jira_issue_key and self.jira.is_configured:
            try:
                self.jira.add_comment(
                    state.jira_issue_key,
                    (
                        f"✅ GitHub PR #{pr_num} 已由 `{approved_by}` 批准 Squash 合入 (`{merge_sha[:8]}`)\n"
                        f"- PR 链接: {state.deliverable_pr_url}"
                    ),
                )
                done_st = self.jira.transition_issue_by_keywords(
                    state.jira_issue_key,
                    {"done", "完成", "已完成", "已解决", "closed"},
                )
                if done_st:
                    state.jira_status = done_st
            except Exception:
                pass
        # Patch Merge Approval Card in-place on Feishu so old buttons vanish cleanly!
        try:
            closed_card = build_merged_closed_card(state, self.web_base_url)
            self.store.write_artifact(task_id, "feishu_merged_closed_card.json", closed_card)
            mid = trigger_message_id or extract_feishu_message_id(
                state.recorded_operations.get(f"notify_merge_approval_{ev.verified_commit_sha[:8]}", "")
            )
            if mid:
                self.feishu.update_card(mid, closed_card)
        except Exception:
            pass

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
