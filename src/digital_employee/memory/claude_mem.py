"""Dual-mode `claude-mem` persistent observation memory & governance rule distillation engine.

Architecture:
1. Native Built-in Engine (Default, zero Node/Bun dependency):
   - SQLite WAL + FTS5 full-text index (`claude_mem.db`) + CJK substring hybrid ranking.
   - Strict `project = repo_id` normalization so ephemeral `git worktree` paths never fragment memory.
   - Automatic DLP secret scrubbing (`redact_sensitive_text`) prior to storage or external dispatch.
2. External `thedotmack/claude-mem` Sidecar Adapter (Optional via `DE_CLAUDE_MEM_URL`):
   - Bridges search/observation reads & writes to an external `claude-mem` HTTP worker (e.g. `:37777`),
     while keeping local SQLite synced as a resilient cache.
3. 3-Stage Progressive Disclosure Retrieval (MCP & Prompt Injection):
   - Stage 1 (`search`): Lightweight index (~30 tokens/hit).
   - Stage 2 (`timeline`): Chronological sequence around an observation ID.
   - Stage 3 (`get_observations`): Full narrative, atomic facts & file citations for selected IDs.
4. Governance Rule Distillation (`MEM_DISTILLED` -> `CANDIDATE_DRAFT`):
   - Mines high-signal observations (human PR feedback, Code Review blocks, multi-attempt pitfalls,
     and cross-task recurring directory issues) into human-approvable `DecisionEntry` drafts.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import httpx
from pydantic import BaseModel, Field

from digital_employee.context.decision_ledger import (
    DecisionEntry,
    PolicyCategory,
    build_fallback_title,
    infer_policy_category,
)
from digital_employee.security.dlp import redact_sensitive_text

if TYPE_CHECKING:
    from digital_employee.llm.client import LLMClient

logger = logging.getLogger("digital_employee.claude_mem")

ObservationType = Literal[
    "bugfix",
    "feature",
    "pitfall",
    "review_block",
    "discovery",
    "ops_triage",
]

VALID_OBSERVATION_TYPES: set[str] = {
    "bugfix",
    "feature",
    "pitfall",
    "review_block",
    "discovery",
    "ops_triage",
}

_FTS_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]{2,}")


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _common_scope_from_files(files: list[str]) -> str:
    """Infer a concise repository scope glob (e.g. `src/order/*`) from touched non-test files."""
    code_files = [
        f.strip().lstrip("./")
        for f in files
        if f.strip() and not f.strip().startswith(("tests/", "test_"))
    ]
    targets = code_files or [f.strip().lstrip("./") for f in files if f.strip()]
    if not targets:
        return "*"
    first_parts = targets[0].split("/")
    if len(first_parts) == 1:
        return targets[0]
    prefix = "/".join(first_parts[:-1])
    if all(t.startswith(f"{prefix}/") for t in targets):
        return f"{prefix}/*"
    top_dir = first_parts[0]
    if all(t.startswith(f"{top_dir}/") for t in targets):
        return f"{top_dir}/*"
    return "*"


class MemoryObservation(BaseModel):
    """Atomic, structured coding observation inspired by `thedotmack/claude-mem`."""

    id: int = 0
    project: str
    task_id: str = ""
    obs_type: str = "discovery"
    title: str
    subtitle: str = ""
    narrative: str
    facts: list[str] = Field(default_factory=list)
    files_read: list[str] = Field(default_factory=list)
    files_modified: list[str] = Field(default_factory=list)
    concepts: list[str] = Field(default_factory=list)
    verified: bool = False
    created_at: str = Field(default_factory=_utc_now_iso)


class ClaudeMemService:
    """Dual-mode persistent observation store + 3-layer retrieval + rule distiller."""

    def __init__(
        self,
        db_path: Path,
        *,
        external_url: str = "",
        enabled: bool = True,
    ) -> None:
        self.db_path = db_path
        self.external_url = external_url.strip().rstrip("/")
        self.enabled = enabled
        self._fts_available = False
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @property
    def mode(self) -> str:
        if not self.enabled:
            return "disabled"
        return "hybrid_external" if self.external_url else "native_sqlite_fts5"

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project TEXT NOT NULL,
                    task_id TEXT NOT NULL DEFAULT '',
                    obs_type TEXT NOT NULL DEFAULT 'discovery',
                    title TEXT NOT NULL,
                    subtitle TEXT NOT NULL DEFAULT '',
                    narrative TEXT NOT NULL,
                    facts_json TEXT NOT NULL DEFAULT '[]',
                    files_read_json TEXT NOT NULL DEFAULT '[]',
                    files_modified_json TEXT NOT NULL DEFAULT '[]',
                    concepts_json TEXT NOT NULL DEFAULT '[]',
                    verified INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_obs_project_created
                    ON observations(project, id DESC);
                CREATE INDEX IF NOT EXISTS idx_obs_project_type
                    ON observations(project, obs_type);
                CREATE INDEX IF NOT EXISTS idx_obs_task
                    ON observations(task_id);
                """
            )
            try:
                conn.executescript(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS observations_fts USING fts5(
                        project,
                        title,
                        subtitle,
                        narrative,
                        facts_text,
                        files_text,
                        concepts_text,
                        tokenize='unicode61'
                    );
                    """
                )
                self._fts_available = True
            except sqlite3.OperationalError:
                self._fts_available = False

    def _row_to_obs(self, row: sqlite3.Row) -> MemoryObservation:
        def _parse_list(raw: str) -> list[str]:
            try:
                loaded = json.loads(raw or "[]")
                return [str(x) for x in loaded if str(x).strip()] if isinstance(loaded, list) else []
            except Exception:
                return []

        return MemoryObservation(
            id=int(row["id"]),
            project=str(row["project"]),
            task_id=str(row["task_id"] or ""),
            obs_type=str(row["obs_type"] or "discovery"),
            title=str(row["title"]),
            subtitle=str(row["subtitle"] or ""),
            narrative=str(row["narrative"]),
            facts=_parse_list(row["facts_json"]),
            files_read=_parse_list(row["files_read_json"]),
            files_modified=_parse_list(row["files_modified_json"]),
            concepts=_parse_list(row["concepts_json"]),
            verified=bool(row["verified"]),
            created_at=str(row["created_at"]),
        )

    # ------------------------------------------------------------------
    # External `thedotmack/claude-mem` Sidecar HTTP Bridge (Optional)
    # ------------------------------------------------------------------

    def check_external_health(self, timeout: float = 1.5) -> dict[str, Any]:
        """Probe optional external `claude-mem` worker (e.g. `http://127.0.0.1:37777/api/health`)."""
        if not self.external_url:
            return {"configured": False, "reachable": False, "url": ""}
        for ep in ("/api/health", "/api/readiness"):
            try:
                with httpx.Client(timeout=timeout) as client:
                    resp = client.get(f"{self.external_url}{ep}")
                    if resp.status_code < 400:
                        return {
                            "configured": True,
                            "reachable": True,
                            "url": self.external_url,
                            "status_code": resp.status_code,
                        }
            except Exception:
                continue
        return {"configured": True, "reachable": False, "url": self.external_url}

    def _push_to_external_worker(self, obs: MemoryObservation) -> None:
        if not self.external_url:
            return
        payload = {
            "project": obs.project,
            "session_id": obs.task_id or "de-session",
            "type": obs.obs_type,
            "title": obs.title,
            "subtitle": obs.subtitle,
            "narrative": obs.narrative,
            "facts": obs.facts,
            "files_read": obs.files_read,
            "files_modified": obs.files_modified,
            "concepts": obs.concepts,
        }
        try:
            with httpx.Client(timeout=2.0) as client:
                client.post(f"{self.external_url}/api/observations", json=payload)
        except Exception as exc:
            logger.debug("External claude-mem worker push skipped: %s", exc)

    def _search_external_worker(
        self, project: str, query: str, limit: int
    ) -> list[MemoryObservation]:
        if not self.external_url:
            return []
        try:
            params: dict[str, Any] = {"project": project, "limit": limit}
            if query.strip():
                params["query"] = query.strip()
            endpoint = "/api/search" if query.strip() else "/api/observations"
            with httpx.Client(timeout=2.0) as client:
                resp = client.get(f"{self.external_url}{endpoint}", params=params)
                if resp.status_code >= 400:
                    return []
                data = resp.json()
            raw_items = (
                data.get("observations")
                or data.get("results")
                or (data if isinstance(data, list) else [])
            )
            results: list[MemoryObservation] = []
            for idx, item in enumerate(raw_items[:limit]):
                if not isinstance(item, dict):
                    continue
                title = redact_sensitive_text(str(item.get("title") or item.get("summary") or ""))
                narrative = redact_sensitive_text(
                    str(item.get("narrative") or item.get("text") or item.get("content") or title)
                )
                if not title:
                    continue
                results.append(
                    MemoryObservation(
                        id=int(item.get("id") or (900_000 + idx)),
                        project=str(item.get("project") or project),
                        task_id=str(item.get("session_id") or ""),
                        obs_type=str(item.get("type") or "discovery"),
                        title=title[:160],
                        subtitle=redact_sensitive_text(str(item.get("subtitle") or ""))[:180],
                        narrative=narrative[:4000],
                        files_modified=[
                            str(f) for f in (item.get("files_modified") or []) if str(f).strip()
                        ],
                        verified=False,
                        created_at=str(item.get("created_at") or _utc_now_iso()),
                    )
                )
            return results
        except Exception as exc:
            logger.debug("External claude-mem search skipped: %s", exc)
            return []

    # ------------------------------------------------------------------
    # Write / Upsert Observations (DLP-Scrubbed & Deduplicated)
    # ------------------------------------------------------------------

    def save_observation(
        self,
        *,
        project: str,
        title: str,
        narrative: str,
        obs_type: str = "discovery",
        subtitle: str = "",
        task_id: str = "",
        facts: list[str] | None = None,
        files_read: list[str] | None = None,
        files_modified: list[str] | None = None,
        concepts: list[str] | None = None,
        verified: bool = False,
    ) -> MemoryObservation | None:
        """Scrub secrets via DLP and persist an atomic observation under `project` (`repo_id`)."""
        if not self.enabled:
            return None
        clean_project = project.strip() or "default"
        clean_type = obs_type.strip().lower()
        if clean_type not in VALID_OBSERVATION_TYPES:
            clean_type = "discovery"

        clean_title = " ".join(redact_sensitive_text(title).split())[:160]
        clean_sub = " ".join(redact_sensitive_text(subtitle).split())[:200]
        clean_narrative = redact_sensitive_text(narrative).strip()[:6000]
        if not clean_title or not clean_narrative:
            return None

        clean_facts = [
            redact_sensitive_text(str(f)).strip()[:300]
            for f in (facts or [])
            if str(f).strip()
        ][:12]
        clean_read = list(
            dict.fromkeys(str(f).strip().lstrip("./") for f in (files_read or []) if str(f).strip())
        )[:25]
        clean_mod = list(
            dict.fromkeys(
                str(f).strip().lstrip("./") for f in (files_modified or []) if str(f).strip()
            )
        )[:25]
        clean_concepts = list(
            dict.fromkeys(str(c).strip().lower() for c in (concepts or []) if str(c).strip())
        )[:15]
        now = _utc_now_iso()

        with self._connect() as conn:
            # Deduplicate identical observation within same project & title
            existing = conn.execute(
                """
                SELECT * FROM observations
                WHERE project = ? AND title = ? AND obs_type = ?
                ORDER BY id DESC LIMIT 1
                """,
                (clean_project, clean_title, clean_type),
            ).fetchone()

            if existing is not None:
                obs_id = int(existing["id"])
                merged_verified = int(bool(existing["verified"]) or verified)
                conn.execute(
                    """
                    UPDATE observations
                    SET subtitle = ?, narrative = ?, facts_json = ?,
                        files_read_json = ?, files_modified_json = ?,
                        concepts_json = ?, verified = ?, created_at = ?
                    WHERE id = ?
                    """,
                    (
                        clean_sub or existing["subtitle"],
                        clean_narrative,
                        json.dumps(clean_facts, ensure_ascii=False),
                        json.dumps(clean_read, ensure_ascii=False),
                        json.dumps(clean_mod, ensure_ascii=False),
                        json.dumps(clean_concepts, ensure_ascii=False),
                        merged_verified,
                        now,
                        obs_id,
                    ),
                )
                if self._fts_available:
                    conn.execute("DELETE FROM observations_fts WHERE rowid = ?", (obs_id,))
                    conn.execute(
                        """
                        INSERT INTO observations_fts(
                            rowid, project, title, subtitle, narrative,
                            facts_text, files_text, concepts_text
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            obs_id,
                            clean_project,
                            clean_title,
                            clean_sub,
                            clean_narrative,
                            " ".join(clean_facts),
                            " ".join([*clean_mod, *clean_read]),
                            " ".join(clean_concepts),
                        ),
                    )
                row = conn.execute("SELECT * FROM observations WHERE id = ?", (obs_id,)).fetchone()
                saved = self._row_to_obs(row)
            else:
                cur = conn.execute(
                    """
                    INSERT INTO observations (
                        project, task_id, obs_type, title, subtitle, narrative,
                        facts_json, files_read_json, files_modified_json,
                        concepts_json, verified, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        clean_project,
                        task_id.strip(),
                        clean_type,
                        clean_title,
                        clean_sub,
                        clean_narrative,
                        json.dumps(clean_facts, ensure_ascii=False),
                        json.dumps(clean_read, ensure_ascii=False),
                        json.dumps(clean_mod, ensure_ascii=False),
                        json.dumps(clean_concepts, ensure_ascii=False),
                        1 if verified else 0,
                        now,
                    ),
                )
                obs_id = int(cur.lastrowid or 0)
                if self._fts_available and obs_id > 0:
                    conn.execute(
                        """
                        INSERT INTO observations_fts(
                            rowid, project, title, subtitle, narrative,
                            facts_text, files_text, concepts_text
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            obs_id,
                            clean_project,
                            clean_title,
                            clean_sub,
                            clean_narrative,
                            " ".join(clean_facts),
                            " ".join([*clean_mod, *clean_read]),
                            " ".join(clean_concepts),
                        ),
                    )
                row = conn.execute("SELECT * FROM observations WHERE id = ?", (obs_id,)).fetchone()
                saved = self._row_to_obs(row)

        self._push_to_external_worker(saved)
        return saved

    def delete_observation(self, obs_id: int, *, project: str = "") -> bool:
        with self._connect() as conn:
            if project:
                cur = conn.execute(
                    "DELETE FROM observations WHERE id = ? AND project = ?",
                    (obs_id, project),
                )
            else:
                cur = conn.execute("DELETE FROM observations WHERE id = ?", (obs_id,))
            deleted = cur.rowcount > 0
            if deleted and self._fts_available:
                conn.execute("DELETE FROM observations_fts WHERE rowid = ?", (obs_id,))
            return deleted

    def stats(self, project: str = "") -> dict[str, Any]:
        with self._connect() as conn:
            if project:
                total = conn.execute(
                    "SELECT COUNT(*) FROM observations WHERE project = ?", (project,)
                ).fetchone()[0]
                verified = conn.execute(
                    "SELECT COUNT(*) FROM observations WHERE project = ? AND verified = 1",
                    (project,),
                ).fetchone()[0]
                pitfalls = conn.execute(
                    "SELECT COUNT(*) FROM observations WHERE project = ? AND obs_type IN ('pitfall', 'review_block')",
                    (project,),
                ).fetchone()[0]
            else:
                total = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
                verified = conn.execute(
                    "SELECT COUNT(*) FROM observations WHERE verified = 1"
                ).fetchone()[0]
                pitfalls = conn.execute(
                    "SELECT COUNT(*) FROM observations WHERE obs_type IN ('pitfall', 'review_block')"
                ).fetchone()[0]
        return {
            "mode": self.mode,
            "total": int(total),
            "verified": int(verified),
            "pitfalls_and_reviews": int(pitfalls),
            "external_url": self.external_url,
        }

    # ------------------------------------------------------------------
    # 3-Stage Progressive Disclosure Retrieval (`search` -> `timeline` -> `get_observations`)
    # ------------------------------------------------------------------

    def search(
        self,
        *,
        project: str = "",
        query: str = "",
        obs_type: str = "",
        verified_only: bool = False,
        limit: int = 15,
    ) -> list[MemoryObservation]:
        """Hybrid FTS5 BM25 + CJK substring + verified-boosted search across observations."""
        if not self.enabled:
            return []
        cap = max(1, min(limit, 50))
        clean_proj = project.strip()
        clean_type = obs_type.strip().lower()
        clean_query = query.strip()

        scored_ids: dict[int, float] = {}
        rows_by_id: dict[int, MemoryObservation] = {}

        with self._connect() as conn:
            if not clean_query:
                clauses: list[str] = []
                params: list[Any] = []
                if clean_proj:
                    clauses.append("project = ?")
                    params.append(clean_proj)
                if clean_type:
                    clauses.append("obs_type = ?")
                    params.append(clean_type)
                if verified_only:
                    clauses.append("verified = 1")
                where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
                rows = conn.execute(
                    f"SELECT * FROM observations {where_sql} ORDER BY verified DESC, id DESC LIMIT ?",
                    [*params, cap],
                ).fetchall()
                local_obs = [self._row_to_obs(r) for r in rows]
            else:
                tokens = _FTS_TOKEN_RE.findall(clean_query)
                # 1) FTS5 BM25 match
                if self._fts_available and tokens:
                    fts_expr = " OR ".join(f'"{t}"*' for t in tokens[:8])
                    try:
                        fts_sql = """
                            SELECT o.*, bm25(observations_fts) AS rank_score
                            FROM observations_fts
                            JOIN observations o ON o.id = observations_fts.rowid
                            WHERE observations_fts MATCH ?
                        """
                        fts_params: list[Any] = [fts_expr]
                        if clean_proj:
                            fts_sql += " AND o.project = ?"
                            fts_params.append(clean_proj)
                        if clean_type:
                            fts_sql += " AND o.obs_type = ?"
                            fts_params.append(clean_type)
                        if verified_only:
                            fts_sql += " AND o.verified = 1"
                        fts_sql += " ORDER BY rank_score ASC, o.id DESC LIMIT ?"
                        fts_params.append(cap * 2)
                        for r in conn.execute(fts_sql, fts_params).fetchall():
                            obs = self._row_to_obs(r)
                            rows_by_id[obs.id] = obs
                            # bm25 in SQLite is negative (more negative = better match)
                            base_score = -float(r["rank_score"] or 0.0) + (2.5 if obs.verified else 0.0)
                            scored_ids[obs.id] = max(scored_ids.get(obs.id, 0.0), base_score)
                    except sqlite3.OperationalError:
                        pass

                # 2) Substring / path / CJK phrase fallback so Chinese substrings & exact file paths always hit
                sub_terms = list(dict.fromkeys([clean_query, *tokens]))[:8]
                clauses = []
                params = []
                if clean_proj:
                    clauses.append("project = ?")
                    params.append(clean_proj)
                if clean_type:
                    clauses.append("obs_type = ?")
                    params.append(clean_type)
                if verified_only:
                    clauses.append("verified = 1")
                term_ors: list[str] = []
                for term in sub_terms:
                    like_pat = f"%{term}%"
                    term_ors.append(
                        "(title LIKE ? OR subtitle LIKE ? OR narrative LIKE ? "
                        "OR files_modified_json LIKE ? OR concepts_json LIKE ?)"
                    )
                    params.extend([like_pat, like_pat, like_pat, like_pat, like_pat])
                if term_ors:
                    clauses.append("(" + " OR ".join(term_ors) + ")")
                where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
                for r in conn.execute(
                    f"SELECT * FROM observations {where_sql} ORDER BY id DESC LIMIT ?",
                    [*params, cap * 2],
                ).fetchall():
                    obs = self._row_to_obs(r)
                    rows_by_id[obs.id] = obs
                    haystack = " ".join(
                        [
                            obs.title,
                            obs.subtitle,
                            obs.narrative,
                            " ".join(obs.files_modified),
                            " ".join(obs.concepts),
                        ]
                    ).lower()
                    hit_score = sum(
                        2.0 if t.lower() in obs.title.lower() else 1.0
                        for t in sub_terms
                        if t.lower() in haystack
                    )
                    if obs.verified:
                        hit_score += 2.0
                    scored_ids[obs.id] = scored_ids.get(obs.id, 0.0) + hit_score

                ranked_ids = sorted(
                    scored_ids.keys(),
                    key=lambda oid: (scored_ids[oid], oid),
                    reverse=True,
                )[:cap]
                local_obs = [rows_by_id[oid] for oid in ranked_ids]

        if self.external_url and len(local_obs) < cap:
            ext_obs = self._search_external_worker(clean_proj, clean_query, cap - len(local_obs))
            seen_titles = {o.title for o in local_obs}
            for eo in ext_obs:
                if eo.title not in seen_titles:
                    local_obs.append(eo)
                    seen_titles.add(eo.title)

        return local_obs[:cap]

    def timeline(
        self,
        *,
        anchor_id: int,
        project: str = "",
        before: int = 3,
        after: int = 3,
    ) -> list[MemoryObservation]:
        """Stage 2 retrieval: return chronological observations surrounding `anchor_id`."""
        if not self.enabled:
            return []
        b_cap = max(0, min(before, 15))
        a_cap = max(0, min(after, 15))
        with self._connect() as conn:
            anchor = conn.execute(
                "SELECT * FROM observations WHERE id = ?", (anchor_id,)
            ).fetchone()
            if anchor is None:
                return []
            proj = project.strip() or str(anchor["project"])
            prev_rows = conn.execute(
                """
                SELECT * FROM observations
                WHERE project = ? AND id < ?
                ORDER BY id DESC LIMIT ?
                """,
                (proj, anchor_id, b_cap),
            ).fetchall()
            next_rows = conn.execute(
                """
                SELECT * FROM observations
                WHERE project = ? AND id > ?
                ORDER BY id ASC LIMIT ?
                """,
                (proj, anchor_id, a_cap),
            ).fetchall()
            ordered = [*reversed(prev_rows), anchor, *next_rows]
            return [self._row_to_obs(r) for r in ordered]

    def get_observations(
        self, ids: list[int], *, project: str = ""
    ) -> list[MemoryObservation]:
        """Stage 3 retrieval: fetch full details for explicitly selected observation IDs."""
        if not self.enabled or not ids:
            return []
        clean_ids = [int(i) for i in ids[:25] if int(i) > 0]
        if not clean_ids:
            return []
        placeholders = ",".join("?" for _ in clean_ids)
        with self._connect() as conn:
            if project.strip():
                rows = conn.execute(
                    f"SELECT * FROM observations WHERE project = ? AND id IN ({placeholders}) ORDER BY id DESC",
                    [project.strip(), *clean_ids],
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT * FROM observations WHERE id IN ({placeholders}) ORDER BY id DESC",
                    clean_ids,
                ).fetchall()
            return [self._row_to_obs(r) for r in rows]

    # ------------------------------------------------------------------
    # Compact Prompt & MCP Output Formatters
    # ------------------------------------------------------------------

    def format_search_index(self, items: list[MemoryObservation]) -> str:
        """Format lightweight Stage-1 index (~30 tokens/hit) to preserve context window."""
        if not items:
            return "(no matching historical observations in claude-mem)"
        lines = [
            "ID | Type | Verified | Title | Touched Files | Date",
            "--- | --- | --- | --- | --- | ---",
        ]
        for o in items:
            v_badge = "✓" if o.verified else "·"
            files = ", ".join(o.files_modified[:3] or o.files_read[:2]) or "-"
            short_date = o.created_at[:10]
            hook = f"{o.title} ({o.subtitle})" if o.subtitle else o.title
            lines.append(f"#{o.id} | {o.obs_type} | {v_badge} | {hook[:120]} | {files} | {short_date}")
        lines.append(
            "\nTip: Call `mem_timeline(anchor_id=ID)` for chronological flow, "
            "or `mem_get_observations(ids=[ID])` for full root-cause & code notes."
        )
        return "\n".join(lines)

    def format_timeline(self, items: list[MemoryObservation], *, anchor_id: int) -> str:
        if not items:
            return f"(observation #{anchor_id} not found)"
        lines = [f"Chronological Timeline around #{anchor_id} (project `{items[0].project}`):"]
        for o in items:
            marker = ">>>" if o.id == anchor_id else "   "
            v_tag = "[verified]" if o.verified else "[draft]"
            files = f" | files: {', '.join(o.files_modified[:4])}" if o.files_modified else ""
            lines.append(
                f"{marker} #{o.id} ({o.created_at[:19]}Z) [{o.obs_type}] {v_tag} {o.title}{files}"
            )
            if o.subtitle:
                lines.append(f"      ↳ {o.subtitle}")
        return "\n".join(lines)

    def format_observations_detail(self, items: list[MemoryObservation]) -> str:
        if not items:
            return "(no observations found for requested IDs)"
        blocks: list[str] = []
        for o in items:
            v_label = "VERIFIED" if o.verified else "UNVERIFIED_EXPLORATION"
            header = (
                f"### Observation #{o.id} · [{o.obs_type.upper()} · {v_label}] {o.title}\n"
                f"- Project: `{o.project}` | Task: `{o.task_id or 'cli'}` | Recorded: `{o.created_at[:19]}Z`"
            )
            if o.files_modified:
                header += f"\n- Modified Files: `{', '.join(o.files_modified)}`"
            if o.files_read:
                header += f"\n- Referenced Files: `{', '.join(o.files_read[:6])}`"
            body = f"\n\n{o.narrative}"
            if o.facts:
                body += "\n\n**Key Takeaways**:\n" + "\n".join(f"- {f}" for f in o.facts)
            blocks.append(header + body)
        return "\n\n---\n\n".join(blocks)

    def render_session_start_context(
        self,
        *,
        project: str,
        objective: str = "",
        suggested_scopes: list[str] | None = None,
        limit: int = 6,
    ) -> str:
        """Build `SessionStart` lightweight memory index for `NativeKernelRuntime`."""
        if not self.enabled:
            return ""
        query_parts = [objective]
        if suggested_scopes:
            query_parts.extend(s.rstrip("/*") for s in suggested_scopes if s not in ("*", "**"))
        relevant = self.search(
            project=project,
            query=" ".join(p for p in query_parts if p).strip(),
            limit=limit,
        )
        if not relevant:
            # Fall back to most recent verified pitfalls/fixes in this repo
            relevant = self.search(project=project, verified_only=True, limit=min(4, limit))
        if not relevant:
            return ""

        return (
            "## Historical Repository Memory Index (`claude-mem` · [75% Authority Reference])\n"
            "> Historical point-in-time observations from past tasks in this repository. "
            "If any memory conflicts with current mainline code (100%) or `APPROVED_POLICY` (95%), "
            "ALWAYS trust current code & tests. Use `mem_get_observations` / `mem_search` MCP tools to inspect details.\n\n"
            + self.format_search_index(relevant)
        )

    # ------------------------------------------------------------------
    # Lifecycle Event Recording (Coding Trace, Gate Pitfalls, Review Blocks)
    # ------------------------------------------------------------------

    def record_coding_attempt_trace(
        self,
        *,
        project: str,
        task_id: str,
        objective: str,
        trace_log: list[str],
        files_modified: list[str],
        root_cause: str = "",
        summary: str = "",
        repro_test_target: str = "",
        is_noop: bool = False,
        verified: bool = False,
    ) -> MemoryObservation | None:
        """Compress a sandbox coding run into a structured `claude-mem` observation."""
        if not self.enabled:
            return None

        files_read: list[str] = []
        bash_failures: list[str] = []
        for entry in trace_log:
            if "ReadFile(" in entry:
                m = re.search(r'"(?:file_path|path)":\s*"([^"]+)"', entry)
                if m:
                    files_read.append(m.group(1))
            elif "POLICY BLOCKED" in entry or "exit=" in entry:
                bash_failures.append(entry[:200])

        if is_noop:
            obs_type: ObservationType = "ops_triage"
            title = f"No-Op 诊断确认：{objective[:80]}"
            subtitle = summary[:140]
            narrative = f"目标基线已满足条件，无需修改代码。\n诊断结论：{summary}"
            facts = [summary] if summary else []
            concepts = ["noop-verified", "triage"]
        else:
            obs_type = "bugfix" if root_cause else "feature"
            title = f"{objective[:90]}"
            subtitle = (root_cause or summary)[:150]
            parts = []
            if root_cause:
                parts.append(f"根因定位：{root_cause}")
            if summary:
                parts.append(f"修改摘要：{summary}")
            if repro_test_target:
                parts.append(f"回归验证用例：`{repro_test_target}`")
            narrative = "\n".join(parts) or objective
            facts = [x for x in [root_cause, summary] if x]
            concepts = ["verified-fix" if verified else "sandbox-attempt"]
            if bash_failures:
                concepts.append("multi-step-debug")

        return self.save_observation(
            project=project,
            task_id=task_id,
            obs_type=obs_type,
            title=title,
            subtitle=subtitle,
            narrative=narrative,
            facts=facts,
            files_read=files_read,
            files_modified=files_modified,
            concepts=concepts,
            verified=verified,
        )

    def record_pitfall_or_review_lesson(
        self,
        *,
        project: str,
        task_id: str,
        lesson_kind: Literal["review_block", "pitfall", "human_feedback"],
        title: str,
        detail: str,
        files_modified: list[str] | None = None,
        facts: list[str] | None = None,
    ) -> MemoryObservation | None:
        """Persist high-signal mistakes (Reviewer BLOCK, multi-attempt test trap, human PR comment)."""
        if not self.enabled:
            return None
        obs_type: ObservationType = (
            "review_block" if lesson_kind == "review_block" else "pitfall"
        )
        concepts = [lesson_kind.replace("_", "-"), "gotcha"]
        if lesson_kind == "human_feedback":
            concepts.append("human-feedback")
        return self.save_observation(
            project=project,
            task_id=task_id,
            obs_type=obs_type,
            title=title,
            subtitle=detail.splitlines()[0][:150] if detail else "",
            narrative=detail,
            facts=facts or [],
            files_modified=files_modified or [],
            concepts=concepts,
            verified=True,
        )

    # ------------------------------------------------------------------
    # Governance Rule Distillation (`claude-mem` -> `DecisionLedger` Candidate Drafts)
    # ------------------------------------------------------------------

    def distill_candidate_policies(
        self,
        *,
        project: str,
        existing_policies: list[DecisionEntry],
        next_id_fn: Any,
        owner: str = "jamesyang",
        llm: LLMClient | None = None,
        focus_task_id: str = "",
        max_candidates: int = 3,
    ) -> list[DecisionEntry]:
        """Distill high-signal or recurring `claude-mem` pitfalls into `CANDIDATE_DRAFT` governance rules.

        Triggers & Signal Selection:
        1. Explicit human feedback / PR review corrections (`human-feedback` / `clarification`).
        2. Blocking PR Code Review lessons (`review_block`).
        3. Recurring pitfalls across >=2 observations in the same module/directory (`pitfall`).
        """
        if not self.enabled:
            return []

        all_obs = self.search(project=project, limit=40)
        if not all_obs:
            return []

        # Identify high-signal observations eligible for rule distillation
        scope_buckets: dict[str, list[MemoryObservation]] = {}
        high_signal: list[MemoryObservation] = []

        for o in all_obs:
            inferred_scope = _common_scope_from_files(o.files_modified or o.files_read)
            if o.obs_type in ("pitfall", "review_block"):
                scope_buckets.setdefault(inferred_scope, []).append(o)
            is_direct_signal = (
                o.obs_type == "review_block"
                or "human-feedback" in o.concepts
                or "clarification-resolved" in o.concepts
            )
            if focus_task_id:
                if o.task_id == focus_task_id and (is_direct_signal or o.obs_type == "pitfall"):
                    high_signal.append(o)
            elif is_direct_signal:
                high_signal.append(o)

        # Also include recurring module pitfalls (>=2 observations in the same directory/scope)
        for _scope, bucket in scope_buckets.items():
            distinct_tasks = {b.task_id or f"obs-{b.id}" for b in bucket}
            if len(distinct_tasks) >= 2:
                for b in bucket[:2]:
                    if b not in high_signal:
                        high_signal.append(b)

        if not high_signal:
            return []

        existing_rules_lower = [
            f"{p.title.lower()} {p.rule.lower()}" for p in existing_policies
        ]
        existing_refs = {p.source_ref for p in existing_policies if p.source_ref}

        raw_drafts: list[dict[str, str]] = []
        if llm is not None:
            raw_drafts = self._llm_distill_drafts(
                project=project,
                observations=high_signal[:10],
                existing_policies=existing_policies,
                llm=llm,
                max_candidates=max_candidates,
            )

        if not raw_drafts:
            raw_drafts = self._heuristic_distill_drafts(
                observations=high_signal,
                max_candidates=max_candidates,
            )

        created: list[DecisionEntry] = []
        today = date.today().isoformat()
        for draft in raw_drafts[:max_candidates]:
            rule_text = draft.get("rule", "").strip()
            title_text = draft.get("title", "").strip()
            source_ref = draft.get("source_ref", "").strip()
            if not rule_text:
                continue
            if source_ref and source_ref in existing_refs:
                continue
            # Avoid near-duplicate rules
            probe = f"{title_text.lower()} {rule_text.lower()}"
            if any(
                (title_text and len(title_text) >= 6 and title_text.lower() in ex)
                or rule_text[:28].lower() in ex
                for ex in existing_rules_lower
            ):
                continue

            scope = draft.get("scope", "*").strip() or "*"
            cat_raw = draft.get("category", "").strip().upper()
            category: PolicyCategory = (
                cat_raw  # type: ignore[assignment]
                if cat_raw in ("ARCHITECTURE", "SECURITY", "TESTING", "CODE_QUALITY")
                else infer_policy_category(scope, rule_text)
            )
            entry = DecisionEntry(
                id=next_id_fn(),
                title=title_text or build_fallback_title(rule_text, scope),
                category=category,
                scope=scope,
                rule=rule_text,
                reason=draft.get("reason", "").strip() or "由 claude-mem 历史踩坑与审查记忆自动提炼",
                status="CANDIDATE_DRAFT",
                source_type="MEM_DISTILLED",
                source_ref=source_ref or "claude-mem",
                owner=owner,
                verified_at=today,
                updated_at=today,
            ).normalized()
            created.append(entry)
            existing_rules_lower.append(probe)
            if source_ref:
                existing_refs.add(source_ref)

        return created

    def _llm_distill_drafts(
        self,
        *,
        project: str,
        observations: list[MemoryObservation],
        existing_policies: list[DecisionEntry],
        llm: LLMClient,
        max_candidates: int,
    ) -> list[dict[str, str]]:
        obs_text = "\n\n".join(
            f"[Obs #{o.id} | task={o.task_id} | type={o.obs_type} | files={o.files_modified}]\n"
            f"Title: {o.title}\nNarrative: {o.narrative[:500]}"
            for o in observations
        )
        existing_text = "\n".join(
            f"- [{p.id}] ({p.scope}): {p.title} — {p.rule}" for p in existing_policies[:25]
        ) or "(none)"

        sys_prompt = (
            "你是资深研发架构治理专家。请根据仓库的历史踩坑/审查拦截记忆（claude-mem Observations），"
            "提炼出值得长期固化的【通用工程治理规则草稿】（仅限架构边界、安全鉴权、测试/CI 必跑步骤、代码质量红线；"
            "严禁把一次性业务需求细节写成规则，严禁与已有规则重复）。\n"
            "请仅输出严格 JSON 对象，格式如下：\n"
            '{"candidates": [{"title": "简明规则标题(≤24字)", "category": "ARCHITECTURE|SECURITY|TESTING|CODE_QUALITY", '
            '"scope": "* 或目录前缀如 src/api/*", "rule": "清晰可执行的硬性工程约束(中文)", '
            '"reason": "提炼依据与防踩坑背景", "source_ref": "claude-mem#ID"}]}'
        )
        user_prompt = (
            f"目标仓库: `{project}`\n"
            f"最多提炼条数: {max_candidates}\n\n"
            f"## 已有仓库规则（请勿重复）\n{existing_text}\n\n"
            f"## 近期高信号记忆 (claude-mem)\n{obs_text}"
        )
        try:
            data, _ = llm.chat_json(
                [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                use_reasoning_model=False,
                max_tokens=1200,
            )
            items = data.get("candidates") if isinstance(data, dict) else None
            if not isinstance(items, list):
                return []
            out: list[dict[str, str]] = []
            for item in items:
                if isinstance(item, dict) and str(item.get("rule") or "").strip():
                    out.append({k: str(v) for k, v in item.items() if v is not None})
            return out
        except Exception as exc:
            logger.debug("LLM rule distillation fell back to heuristic mode: %s", exc)
            return []

    def _heuristic_distill_drafts(
        self,
        *,
        observations: list[MemoryObservation],
        max_candidates: int,
    ) -> list[dict[str, str]]:
        """Deterministic offline rule synthesis when LLM is injected/offline."""
        out: list[dict[str, str]] = []
        for o in observations:
            if len(out) >= max_candidates:
                break
            scope = _common_scope_from_files(o.files_modified or o.files_read)
            core_fact = (o.facts[0] if o.facts else "") or o.subtitle or o.narrative.splitlines()[0]
            core_fact = core_fact.strip()
            if len(core_fact) < 10:
                continue
            prefix = f"修改 `{scope}` 相关代码时，" if scope != "*" else "提交代码与审查时，"
            rule = core_fact if core_fact.startswith("修改") or "必须" in core_fact or "严禁" in core_fact else f"{prefix}必须注意：{core_fact}"
            out.append(
                {
                    "title": build_fallback_title(o.title, scope),
                    "category": infer_policy_category(scope, rule),
                    "scope": scope,
                    "rule": rule[:260],
                    "reason": f"源自记忆 #{o.id} ({o.task_id or o.obs_type})：{o.title[:80]}",
                    "source_ref": f"claude-mem#{o.id}",
                }
            )
        return out
