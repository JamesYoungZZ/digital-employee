"""Background Jira Poller: automatically picks up existing Jira tickets matching JQL (`ai-delegate`)."""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from digital_employee.orchestrator import DelegationOrchestrator

logger = logging.getLogger("digital_employee.jira_poller")


class JiraIssuePoller:
    """Periodically polls Jira for newly ready/labeled tickets and delegates them directly."""

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
        jql = self.orch.settings.jira_poll_jql.strip()
        if not jql:
            return 0

        keys = self.orch.jira.search_issues(jql, max_results=5)
        existing_jira_keys = {
            t.jira_issue_key
            for t in self.orch.store.list_tasks(limit=200)
            if t.jira_issue_key
        }
        dispatched = 0
        for key in keys:
            if key in existing_jira_keys:
                continue
            try:
                self.orch.submit_task(
                    raw_request=key,
                    jira_issue_key=key,
                    auto_execute=True,
                    publish_pr=self.orch.github.is_configured,
                    source_channel="jira_poller",
                )
                dispatched += 1
            except Exception as exc:
                logger.warning("Failed to dispatch polled Jira issue %s: %s", key, exc)
        return dispatched
