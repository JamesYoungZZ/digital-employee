---
name: mem-search
description: Search persistent cross-session repository memory (claude-mem) using 3-stage progressive disclosure (index -> timeline -> full details) to recall past bugfixes, test gotchas, and review lessons without bloating context.
---

# Progressive Repository Memory Retrieval (`claude-mem`)

Use the `claude-mem` MCP tools whenever investigating an unfamiliar module, diagnosing a recurring failure, or checking past Code Review pitfalls in this repository.

## 1. 3-Stage Progressive Disclosure Protocol (Token-Efficient)
1. **Stage 1 — Lightweight Index (`mem_search`)**:
   - Start with `mem_search(query="<module, error, or file name>")`.
   - Scan the compact index table (`#ID | Type | Verified | Title | Touched Files`). Do NOT guess details from titles alone.
2. **Stage 2 — Chronological Context (`mem_timeline`, optional)**:
   - If you want to see the sequence of debugging steps around a promising hit `#ID`, call `mem_timeline(anchor_id=ID)`.
3. **Stage 3 — Selective Deep Fetch (`mem_get_observations`)**:
   - Call `mem_get_observations(ids=[...])` ONLY for the 1–3 most relevant observation IDs to read their full root-cause narrative and key takeaways.

## 2. Recording Non-Obvious Gotchas (`mem_save_observation`)
- When you uncover a hidden build/test requirement or subtle module trap that is NOT obvious from reading a single file, record a concise note via `mem_save_observation(title=..., narrative=..., obs_type="pitfall" | "discovery")`.

## 3. Authority Guardrail
- Historical memories are **point-in-time observations (75% reference authority)**.
- Always verify file paths and behavior against **current mainline code & tests (100% authority)** and **active `APPROVED_POLICY` rules (95% authority)**.
