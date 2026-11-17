"""Built-in Read-Only Ops Observability & Incident Diagnostics MCP Server (stdio)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import httpx
from mcp.server import MCPServer

from digital_employee.security.dlp import redact_sensitive_text

mcp = MCPServer("ops-diagnostics")


@mcp.tool(description="List recent git commits and touched files in a repository to identify regression culprits.")
def inspect_recent_commits(repo_path: str, limit: int = 8) -> str:
    root = Path(repo_path).expanduser().resolve()
    if not (root / ".git").exists():
        return f"Not a git repository: {root}"
    capped = max(1, min(limit, 25))
    proc = subprocess.run(
        ["git", "log", f"-n{capped}", "--stat", "--oneline"],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if proc.returncode != 0:
        return f"git log error: {redact_sensitive_text(proc.stderr[:500])}"
    return redact_sensitive_text(proc.stdout[:8000]) or "(no commits)"


@mcp.tool(description="Search application log files for regex/keyword patterns with automatic DLP credential redaction.")
def search_error_logs(log_path: str, pattern: str = "ERROR|Exception|Traceback", max_matches: int = 40) -> str:
    target = Path(log_path).expanduser().resolve()
    if not target.exists():
        return f"Log path not found: {target}"

    files: list[Path] = [target] if target.is_file() else sorted(target.glob("**/*.log"))[:15]
    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error:
        regex = re.compile(re.escape(pattern), re.IGNORECASE)

    hits: list[str] = []
    cap = max(1, min(max_matches, 100))
    for fp in files:
        try:
            lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for lineno, line in enumerate(lines[-2000:], start=max(1, len(lines) - 1999)):
            if regex.search(line):
                hits.append(f"{fp.name}:{lineno}: {redact_sensitive_text(line[:280])}")
                if len(hits) >= cap:
                    return "\n".join(hits)
    return "\n".join(hits) if hits else "(no matching log entries)"


@mcp.tool(description="Perform a read-only HTTP GET health check against a local or internal service endpoint.")
def probe_service_health(url: str, timeout_seconds: float = 5.0) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"Unsupported URL scheme: {parsed.scheme}"
    capped_timeout = max(0.5, min(timeout_seconds, 15.0))
    try:
        with httpx.Client(timeout=capped_timeout, follow_redirects=False) as client:
            resp = client.get(url)
            snippet = redact_sensitive_text(resp.text[:500])
            return f"HTTP {resp.status_code} ({resp.elapsed.total_seconds():.3f}s)\n{snippet}"
    except Exception as exc:
        return f"Health probe failed: {type(exc).__name__}: {exc}"


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
