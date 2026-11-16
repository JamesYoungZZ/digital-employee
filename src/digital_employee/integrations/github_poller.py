"""Background GitHub PR Comment, Merge Outcome & CI Failure Poller (No public IP needed)."""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from digital_employee.orchestrator import DelegationOrchestrator

logger = logging.getLogger("digital_employee.poller")


class GitHubFollowThroughPoller:
    """Periodically reconciles open Draft PRs for review comments, CI failures, and merge outcomes."""

    def __init__(self, orchestrator: DelegationOrchestrator, interval_seconds: float = 30.0) -> None:
        self.orch = orchestrator
        self.interval_seconds = interval_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.orch.github.is_configured:
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="de-github-poller",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.poll_once()
            except Exception as exc:
                logger.warning("GitHub poller cycle error: %s", exc)
            self._stop_event.wait(self.interval_seconds)

    def poll_once(self) -> int:
        """Inspect all `PR_OPEN_FOLLOWING_UP` tasks once. Returns count of actions triggered."""
        if not self.orch.github.is_configured:
            return 0

        actions = 0
        open_tasks = self.orch.store.list_tasks(status="PR_OPEN_FOLLOWING_UP", limit=30)
        for state in open_tasks:
            if not state.deliverable_pr_number:
                continue
            try:
                profile = self.orch.settings.resolve_repository(state.contract.target_repo)
            except KeyError:
                continue

            pr_num = state.deliverable_pr_number
            gh_repo = profile.github_repo

            # 1. Reconcile PR merged / closed state
            try:
                pr_data = self.orch.github.fetch_pr_state(github_repo=gh_repo, pr_number=pr_num)
            except Exception:
                continue

            if pr_data.get("merged"):
                state.lifecycle_status = "ACCEPTED_AND_CLOSED"
                self.orch.store.save_task(
                    state,
                    step_name="pr_merged_reconciled",
                    note=f"PR #{pr_num} merged on GitHub.",
                )
                actions += 1
                continue

            if pr_data.get("state") == "closed":
                state.lifecycle_status = "ABANDONED_OR_REVERTED"
                self.orch.store.save_task(
                    state,
                    step_name="pr_closed_unmerged",
                    note=f"PR #{pr_num} closed without merge.",
                )
                actions += 1
                continue

            # 2. Check for new human review / issue comments on the PR
            comments = self.orch.github.fetch_human_pr_comments(
                github_repo=gh_repo, pr_number=pr_num
            )
            handled_comment = False
            for cmt in comments:
                op_key = f"pr_comment_{cmt['id']}"
                if op_key in state.recorded_operations:
                    continue
                state.recorded_operations[op_key] = cmt["created_at"]
                self.orch.store.save_task(
                    state,
                    step_name=op_key,
                    note=f"Processing PR comment by {cmt['user']}: {cmt['body'][:120]}",
                )
                self.orch.handle_pr_review_comment(
                    state.task_id,
                    comment_body=cmt["body"],
                    publish_pr=True,
                )
                actions += 1
                handled_comment = True
                break

            if handled_comment:
                continue

            # 3. Check for blocking GitHub Actions CI failures on current head_commit_sha
            head_sha = str((pr_data.get("head") or {}).get("sha") or state.head_commit_sha or "")
            ci_op_key = f"ci_auto_heal_{head_sha[:10]}"
            if head_sha and ci_op_key not in state.recorded_operations:
                failed_checks = self.orch.github.fetch_failed_blocking_checks(
                    github_repo=gh_repo, commit_sha=head_sha
                )
                if failed_checks:
                    state.recorded_operations[ci_op_key] = ",".join(failed_checks)
                    self.orch.store.save_task(
                        state,
                        step_name=ci_op_key,
                        note=f"Auto-healing failed GitHub checks: {failed_checks}",
                    )
                    self.orch.handle_pr_review_comment(
                        state.task_id,
                        comment_body=(
                            f"GitHub Actions CI check(s) {failed_checks} failed on commit {head_sha[:8]}. "
                            "Inspect any regressions in scope and push a fix."
                        ),
                        publish_pr=True,
                    )
                    actions += 1

        return actions
