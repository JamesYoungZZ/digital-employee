"""Background Jira Poller:
1. Auto-dispatches newly labeled `ai-delegate` Jira tickets (`To Do`).
2. Watches bound active Jira tickets for description/summary edits & new human comments (zero public IP needed!).
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from digital_employee.orchestrator import DelegationOrchestrator

logger = logging.getLogger("digital_employee.jira_poller")


class JiraIssuePoller:
    """Periodically polls Jira for newly delegated tickets AND active bound ticket updates."""

    def __init__(self, orchestrator: DelegationOrchestrator, interval_seconds: float = 30.0) -> None:
        self.orch = orchestrator
        self.interval_seconds = interval_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not (self.orch.jira.is_configured and self.orch.settings.jira_poller_enabled):
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="de-jira-poller",
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
                logger.warning("Jira poller cycle error: %s", exc)
            self._stop_event.wait(self.interval_seconds)

    def poll_once(self) -> int:
        if not (self.orch.jira.is_configured and self.orch.settings.jira_poller_enabled):
            return 0

        acted = 0

        # 1. Reconcile active bound Jira tickets for human description edits or new comments
        active_tasks = [
            t
            for t in self.orch.store.list_tasks(limit=80)
            if t.jira_issue_key
            and t.lifecycle_status
            not in ("ACCEPTED_AND_CLOSED", "ABANDONED_OR_REVERTED", "WAITING_JIRA_APPROVAL")
        ]
        for task in active_tasks:
            try:
                if self.orch.reconcile_bound_jira_issue(task.task_id):
                    acted += 1
            except Exception as exc:
                logger.warning(
                    "Failed to reconcile active Jira issue %s (%s): %s",
                    task.jira_issue_key,
                    task.task_id,
                    exc,
                )

        # 2. Discover & dispatch newly labeled `ai-delegate` To-Do tickets
        jql = self.orch.settings.jira_poll_jql.strip()
        if not jql:
            return acted

        keys = self.orch.jira.search_issues(jql, max_results=5)
        existing_jira_keys = {
            (t.jira_issue_key or "").upper()
            for t in self.orch.store.list_tasks(limit=200)
            if t.jira_issue_key
        }
        for key in keys:
            if key.upper() in existing_jira_keys:
                continue
            try:
                self.orch.submit_task(
                    raw_request=key,
                    jira_issue_key=key,
                    auto_execute=True,
                    publish_pr=self.orch.github.is_configured,
                    source_channel="jira_poller",
                )
                acted += 1
            except Exception as exc:
                logger.warning("Failed to dispatch polled Jira issue %s: %s", key, exc)
        return acted
