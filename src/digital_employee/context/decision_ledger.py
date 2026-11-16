"""Truth Hierarchy & Organization Decision Ledger (Spec v2.0 Section 3.4)."""

from __future__ import annotations

import fnmatch
from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class DecisionEntry(BaseModel):
    """Single engineering/business rule in `.ai/DECISIONS.yaml`."""

    id: str
    scope: str = "*"
    rule: str
    reason: str = ""
    status: Literal["APPROVED_POLICY", "ONE_OFF_WORKAROUND", "DEPRECATED"] = "APPROVED_POLICY"
    owner: str = ""
    verified_at: str = Field(default_factory=lambda: date.today().isoformat())
    visibility_rbac: str = "default"


class DecisionLedger:
    """Loads, queries, and formats repository architectural/business decisions."""

    def __init__(self, entries: list[DecisionEntry] | None = None) -> None:
        self.entries: list[DecisionEntry] = entries or []

    @classmethod
    def from_repo(cls, repo_root: Path, ledger_rel_path: str = ".ai/DECISIONS.yaml") -> DecisionLedger:
        ledger_file = repo_root / ledger_rel_path
        if not ledger_file.is_file():
            return cls([])
        raw = yaml.safe_load(ledger_file.read_text(encoding="utf-8")) or {}
        items = raw.get("decisions", raw if isinstance(raw, list) else [])
        entries = [DecisionEntry.model_validate(item) for item in items if isinstance(item, dict)]
        return cls(entries)

    def active_policies(self, touched_paths: list[str] | None = None) -> list[DecisionEntry]:
        """Return only `APPROVED_POLICY` entries (never `ONE_OFF_WORKAROUND` or `DEPRECATED`)."""
        approved = [e for e in self.entries if e.status == "APPROVED_POLICY"]
        if not touched_paths:
            return approved
        matched: list[DecisionEntry] = []
        for entry in approved:
            scope = entry.scope.strip()
            if scope in ("*", "**", ""):
                matched.append(entry)
                continue
            clean_prefix = scope.rstrip("/*")
            for path in touched_paths:
                norm = path.strip().lstrip("./")
                if (
                    fnmatch.fnmatch(norm, scope)
                    or norm == clean_prefix
                    or norm.startswith(f"{clean_prefix}/")
                ):
                    matched.append(entry)
                    break
        return matched

    def render_truth_hierarchy_block(self, touched_paths: list[str] | None = None) -> str:
        """Format Spec v2.0 Section 3.4 Truth Hierarchy + active DecisionLedger policies."""
        policies = self.active_policies(touched_paths)
        lines = [
            "## Truth Hierarchy (Mandatory Conflict Resolution Order)",
            "1. [100% Authority] Current mainline repository code & passing test suite.",
            "2. [95% Authority] APPROVED_POLICY entries in `.ai/DECISIONS.yaml` (listed below).",
            "3. [85% Authority] Recent merged PR descriptions & review conclusions (<90 days).",
            "4. [80% Authority] Confirmed instructions in current `TaskContract`.",
            "5. [Reference Only] Legacy Wiki/design docs — if conflicting with code, report as 'Suspected Stale Doc'.",
            "",
            "## Active Organization Decision Ledger (`APPROVED_POLICY`)",
        ]
        if not policies:
            lines.append("- (No active repository-specific policies matched.)")
        else:
            for p in policies:
                lines.append(
                    f"- [{p.id}] (scope: `{p.scope}`, owner: {p.owner}): {p.rule} | Reason: {p.reason}"
                )
        return "\n".join(lines)

    @classmethod
    def append_policy(
        cls,
        repo_root: Path,
        *,
        scope: str,
        rule: str,
        reason: str = "",
        owner: str = "jamesyang",
        status: Literal["APPROVED_POLICY", "ONE_OFF_WORKAROUND"] = "APPROVED_POLICY",
        ledger_rel_path: str = ".ai/DECISIONS.yaml",
    ) -> DecisionEntry:
        """Append a human-confirmed engineering rule into target repo `.ai/DECISIONS.yaml`."""
        ledger_file = repo_root / ledger_rel_path
        ledger_file.parent.mkdir(parents=True, exist_ok=True)
        existing_raw: dict[str, list[dict[str, str]]] = {"decisions": []}
        if ledger_file.is_file():
            loaded = yaml.safe_load(ledger_file.read_text(encoding="utf-8")) or {}
            if isinstance(loaded, dict) and isinstance(loaded.get("decisions"), list):
                existing_raw["decisions"] = list(loaded["decisions"])
            elif isinstance(loaded, list):
                existing_raw["decisions"] = list(loaded)

        next_num = len(existing_raw["decisions"]) + 1
        prefix = repo_root.name[:6].upper().replace("-", "")
        entry = DecisionEntry(
            id=f"{prefix}-DEC-{next_num:03d}",
            scope=scope.strip() or "*",
            rule=rule.strip(),
            reason=reason.strip(),
            status=status,
            owner=owner,
        )
        existing_raw["decisions"].append(entry.model_dump())
        header = "# Team & Architecture Decision Ledger (Managed with Digital Employee)\n"
        ledger_file.write_text(
            header + yaml.safe_dump(existing_raw, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        return entry
