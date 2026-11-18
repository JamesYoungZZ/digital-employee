"""SQLite WAL durable state store, checkpoint history, idempotency ledger & artifact storage."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from digital_employee.context.decision_ledger import DecisionEntry
from digital_employee.models.state import DurableTaskState, utc_now_iso
from digital_employee.security.dlp import redact_sensitive_text


def make_idempotency_key(task_id: str, step_name: str, contract_version: int = 1) -> str:
    payload = f"{task_id}:{step_name}:v{contract_version}".encode()
    return hashlib.sha256(payload).hexdigest()[:24]


class DurableStore:
    """Crash-recoverable relational state engine for Digital Employee tasks."""

    def __init__(self, db_path: Path, artifacts_dir: Path) -> None:
        self.db_path = db_path
        self.artifacts_dir = artifacts_dir
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    lifecycle_status TEXT NOT NULL,
                    target_repo TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS checkpoints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    step_name TEXT NOT NULL,
                    lifecycle_status TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(task_id)
                );

                CREATE TABLE IF NOT EXISTS idempotency_ledger (
                    idempotency_key TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    step_name TEXT NOT NULL,
                    result_value TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS repository_policies (
                    repo_id TEXT NOT NULL,
                    policy_id TEXT NOT NULL,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    policy_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (repo_id, policy_id)
                );

                CREATE TABLE IF NOT EXISTS repository_policy_sync (
                    repo_id TEXT PRIMARY KEY,
                    initialized_at TEXT NOT NULL,
                    source_note TEXT NOT NULL DEFAULT ''
                );

                CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(lifecycle_status);
                CREATE INDEX IF NOT EXISTS idx_checkpoints_task ON checkpoints(task_id);
                CREATE INDEX IF NOT EXISTS idx_repo_policies_repo ON repository_policies(repo_id, sort_order);
                """
            )

    def save_task(
        self,
        state: DurableTaskState,
        *,
        step_name: str | None = None,
        note: str = "",
    ) -> DurableTaskState:
        """Persist current task state and optionally append an explicit milestone checkpoint."""
        state.updated_at = utc_now_iso()
        serialized = state.model_dump_json()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO tasks (
                    task_id, tenant_id, workspace_id, lifecycle_status,
                    target_repo, owner_id, state_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    lifecycle_status = excluded.lifecycle_status,
                    target_repo = excluded.target_repo,
                    owner_id = excluded.owner_id,
                    state_json = excluded.state_json,
                    updated_at = excluded.updated_at
                """,
                (
                    state.task_id,
                    state.tenant_id,
                    state.workspace_id,
                    state.lifecycle_status,
                    state.contract.target_repo,
                    state.contract.owner_id,
                    serialized,
                    state.created_at,
                    state.updated_at,
                ),
            )
            if step_name and step_name != "state_update":
                conn.execute(
                    """
                    INSERT INTO checkpoints (
                        task_id, step_name, lifecycle_status, state_json, note, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        state.task_id,
                        step_name,
                        state.lifecycle_status,
                        serialized,
                        redact_sensitive_text(note)[:1000],
                        state.updated_at,
                    ),
                )
        return state

    def get_task(self, task_id: str) -> DurableTaskState | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state_json FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            return None
        return DurableTaskState.model_validate_json(row["state_json"])

    def list_tasks(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> list[DurableTaskState]:
        query = "SELECT state_json FROM tasks"
        params: list[Any] = []
        if status:
            query += " WHERE lifecycle_status = ?"
            params.append(status)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [DurableTaskState.model_validate_json(r["state_json"]) for r in rows]

    def list_checkpoints(self, task_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, step_name, lifecycle_status, note, created_at
                FROM checkpoints
                WHERE task_id = ?
                ORDER BY id ASC
                """,
                (task_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def run_idempotent(
        self,
        state: DurableTaskState,
        step_name: str,
        action: Callable[[], str],
    ) -> tuple[str, bool]:
        """Execute external side-effect at most once per (task_id, step_name, contract_version).

        Returns `(result_value, was_reused)`.
        """
        idem_key = make_idempotency_key(
            state.task_id, step_name, state.contract.contract_version
        )
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT result_value FROM idempotency_ledger WHERE idempotency_key = ?",
                (idem_key,),
            ).fetchone()
        if existing is not None:
            val = str(existing["result_value"])
            state.recorded_operations[step_name] = val
            return val, True

        result_val = action()
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO idempotency_ledger (
                    idempotency_key, task_id, step_name, result_value, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (idem_key, state.task_id, step_name, result_val, now),
            )
        state.recorded_operations[step_name] = result_val
        self.save_task(state, step_name=f"idempotent:{step_name}", note=result_val)
        return result_val, False

    def write_artifact(self, task_id: str, name: str, content: str | dict[str, Any]) -> str:
        """Store large diffs, execution logs, or JSON reports outside task state JSON."""
        task_dir = self.artifacts_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        safe_name = name.replace("/", "_").replace("..", "_")
        target = task_dir / safe_name
        if isinstance(content, dict):
            raw_text = json.dumps(content, ensure_ascii=False, indent=2)
        else:
            raw_text = content
        target.write_text(redact_sensitive_text(raw_text), encoding="utf-8")
        return f"file://{target.resolve()}"

    def compute_funnel_metrics(self) -> dict[str, Any]:
        """Compute Spec v2.0 Section 4 funnel & Net Human Time Saved metrics."""
        tasks = self.list_tasks(limit=1000)
        total = len(tasks)
        clarified_or_queued = sum(
            1 for t in tasks if t.lifecycle_status != "WAITING_CLARIFICATION"
        )
        evidence_passed = sum(
            1
            for t in tasks
            if t.verification_evidence and t.verification_evidence.passed_all_hard_gates
        )
        delivered_states = (
            "PR_OPEN_FOLLOWING_UP",
            "WAITING_MERGE_APPROVAL",
            "DELIVERED_LOCAL_READY",
            "ACCEPTED_AND_CLOSED",
        )
        delivered = sum(1 for t in tasks if t.lifecycle_status in delivered_states)
        pending_approval = sum(
            1 for t in tasks if t.lifecycle_status == "WAITING_MERGE_APPROVAL"
        )
        accepted = sum(1 for t in tasks if t.lifecycle_status == "ACCEPTED_AND_CLOSED")
        handed_off = sum(1 for t in tasks if t.lifecycle_status == "HANDED_OFF_INCOMPLETE")
        abandoned = sum(1 for t in tasks if t.lifecycle_status == "ABANDONED_OR_REVERTED")

        net_minutes_saved = sum(
            t.estimated_net_minutes_saved
            for t in tasks
            if t.lifecycle_status in delivered_states
        )
        total_cost = round(sum(t.total_cost_usd for t in tasks), 4)

        return {
            "total_delegated": total,
            "contract_confirmed": clarified_or_queued,
            "evidence_gate_passed": evidence_passed,
            "delivered_ready": delivered,
            "pending_merge_approval": pending_approval,
            "accepted_without_rework": accepted,
            "handed_off_incomplete": handed_off,
            "abandoned_or_reverted": abandoned,
            "total_cost_usd": total_cost,
            "estimated_net_human_minutes_saved": round(net_minutes_saved, 2),
        }

    # =========================================================================
    # Repository Governance Policies (Zero-Intrusion Managed Decision Ledger)
    # =========================================================================

    def has_initialized_policies(self, repo_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM repository_policy_sync WHERE repo_id = ?",
                (repo_id,),
            ).fetchone()
        return row is not None

    def get_policy_sync_note(self, repo_id: str) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT source_note FROM repository_policy_sync WHERE repo_id = ?",
                (repo_id,),
            ).fetchone()
        return str(row["source_note"]) if row is not None else ""

    def mark_policies_initialized(self, repo_id: str, source_note: str = "") -> None:
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO repository_policy_sync (repo_id, initialized_at, source_note)
                VALUES (?, ?, ?)
                ON CONFLICT(repo_id) DO UPDATE SET
                    initialized_at = excluded.initialized_at,
                    source_note = excluded.source_note
                """,
                (repo_id, now, source_note[:500]),
            )

    def list_repository_policies(self, repo_id: str) -> list[DecisionEntry]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT policy_json
                FROM repository_policies
                WHERE repo_id = ?
                ORDER BY sort_order ASC, created_at ASC, policy_id ASC
                """,
                (repo_id,),
            ).fetchall()
        return [DecisionEntry.model_validate_json(r["policy_json"]).normalized() for r in rows]

    def get_repository_policy(self, repo_id: str, policy_id: str) -> DecisionEntry | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT policy_json
                FROM repository_policies
                WHERE repo_id = ? AND policy_id = ?
                """,
                (repo_id, policy_id),
            ).fetchone()
        if row is None:
            return None
        return DecisionEntry.model_validate_json(row["policy_json"]).normalized()

    def upsert_repository_policy(
        self,
        repo_id: str,
        entry: DecisionEntry,
        *,
        sort_order: int | None = None,
    ) -> DecisionEntry:
        norm_entry = entry.normalized()
        now = utc_now_iso()
        serialized = norm_entry.model_dump_json()
        with self._connect() as conn:
            if sort_order is None:
                existing = conn.execute(
                    "SELECT sort_order FROM repository_policies WHERE repo_id = ? AND policy_id = ?",
                    (repo_id, norm_entry.id),
                ).fetchone()
                if existing is not None:
                    resolved_order = int(existing["sort_order"])
                else:
                    max_row = conn.execute(
                        "SELECT COALESCE(MAX(sort_order), -1) + 1 AS next_order FROM repository_policies WHERE repo_id = ?",
                        (repo_id,),
                    ).fetchone()
                    resolved_order = int(max_row["next_order"]) if max_row else 0
            else:
                resolved_order = sort_order

            conn.execute(
                """
                INSERT INTO repository_policies (
                    repo_id, policy_id, sort_order, policy_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(repo_id, policy_id) DO UPDATE SET
                    sort_order = excluded.sort_order,
                    policy_json = excluded.policy_json,
                    updated_at = excluded.updated_at
                """,
                (repo_id, norm_entry.id, resolved_order, serialized, now, now),
            )
        return norm_entry

    def delete_repository_policy(self, repo_id: str, policy_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM repository_policies WHERE repo_id = ? AND policy_id = ?",
                (repo_id, policy_id),
            )
        return cur.rowcount > 0

    def next_policy_id(self, repo_id: str) -> str:
        existing = self.list_repository_policies(repo_id)
        prefix = ""
        max_num = 0
        for item in existing:
            m = re.match(r"^([A-Z0-9]+-DEC)-(\d+)$", item.id.upper())
            if m:
                if not prefix:
                    prefix = m.group(1)
                max_num = max(max_num, int(m.group(2)))
        if not prefix:
            clean_token = re.sub(r"[^A-Z0-9]", "", repo_id.split("-")[-1].upper())
            if len(clean_token) < 3:
                clean_token = re.sub(r"[^A-Z0-9]", "", repo_id.upper())[:8] or "REPO"
            prefix = f"{clean_token[:8]}-DEC"
        return f"{prefix}-{max_num + 1:03d}"
