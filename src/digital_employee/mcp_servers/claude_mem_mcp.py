"""Built-in `claude-mem` Progressive Disclosure Memory MCP Server (stdio).

Exposes the 3-stage retrieval pipeline (`mem_search` -> `mem_timeline` -> `mem_get_observations`)
plus proactive discovery recording (`mem_save_observation`) directly to the `mewcode` Coding Agent.
"""

from __future__ import annotations

import os
from pathlib import Path

from mcp.server import MCPServer

from digital_employee.memory.claude_mem import ClaudeMemService
from digital_employee.settings import PROJECT_ROOT

mcp = MCPServer("claude-mem")


def _get_service() -> tuple[ClaudeMemService, str]:
    raw_db = os.environ.get("DE_CLAUDE_MEM_DB_PATH", "").strip()
    db_path = Path(raw_db) if raw_db else (PROJECT_ROOT / ".local" / "claude_mem.db")
    ext_url = os.environ.get("DE_CLAUDE_MEM_URL", "").strip()
    default_project = os.environ.get("DE_CLAUDE_MEM_DEFAULT_PROJECT", "").strip()
    return ClaudeMemService(db_path, external_url=ext_url, enabled=True), default_project


@mcp.tool(
    description=(
        "Stage 1 (Lightweight Index Search): Search persistent repository memory (`claude-mem`) "
        "for historical bugfixes, Code Review blocks, test gotchas, and module discoveries. "
        "Returns a compact index table (~30 tokens/hit). Always call this first before fetching details."
    )
)
def mem_search(
    query: str = "",
    obs_type: str = "",
    project: str = "",
    limit: int = 12,
) -> str:
    svc, default_proj = _get_service()
    eff_proj = project.strip() or default_proj
    items = svc.search(project=eff_proj, query=query, obs_type=obs_type, limit=limit)
    return svc.format_search_index(items)


@mcp.tool(
    description=(
        "Stage 2 (Chronological Context): Inspect the timeline of observations recorded "
        "immediately before and after a specific `anchor_id` returned by `mem_search`."
    )
)
def mem_timeline(
    anchor_id: int,
    before: int = 3,
    after: int = 3,
    project: str = "",
) -> str:
    svc, default_proj = _get_service()
    eff_proj = project.strip() or default_proj
    items = svc.timeline(anchor_id=anchor_id, project=eff_proj, before=before, after=after)
    return svc.format_timeline(items, anchor_id=anchor_id)


@mcp.tool(
    description=(
        "Stage 3 (Deep Retrieval): Fetch full narrative, root-cause analysis, atomic facts, "
        "and modified file paths ONLY for specific observation IDs selected from `mem_search`."
    )
)
def mem_get_observations(ids: list[int], project: str = "") -> str:
    svc, default_proj = _get_service()
    eff_proj = project.strip() or default_proj
    items = svc.get_observations(ids, project=eff_proj)
    return svc.format_observations_detail(items)


@mcp.tool(
    description=(
        "Record a non-obvious repository gotcha, hidden test runner requirement, or architectural "
        "discovery into `claude-mem` so future coding sessions in this repository can recall it."
    )
)
def mem_save_observation(
    title: str,
    narrative: str,
    obs_type: str = "discovery",
    files_modified: list[str] | None = None,
    concepts: list[str] | None = None,
    project: str = "",
) -> str:
    svc, default_proj = _get_service()
    eff_proj = project.strip() or default_proj or "default"
    task_id = os.environ.get("DE_CLAUDE_MEM_TASK_ID", "").strip()
    saved = svc.save_observation(
        project=eff_proj,
        task_id=task_id,
        obs_type=obs_type,
        title=title,
        narrative=narrative,
        files_modified=files_modified or [],
        concepts=concepts or ["agent-recorded"],
        verified=False,
    )
    if saved is None:
        return "Observation skipped (empty title/narrative)."
    return f"Saved observation #{saved.id} to project `{saved.project}`: {saved.title}"


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
