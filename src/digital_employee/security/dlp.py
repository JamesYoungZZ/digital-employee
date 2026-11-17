"""Security, DLP redaction, untrusted-input isolation, and hard policy guards (Spec v2.0 Section 1.3)."""

from __future__ import annotations

import fnmatch
import html
import posixpath
import re
from pathlib import PurePosixPath

SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"), "[REDACTED_API_KEY]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "[REDACTED_GITHUB_PAT]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED_AWS_KEY]"),
    (re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._\-]{16,}"), r"\1[REDACTED_BEARER_TOKEN]"),
    (
        re.compile(
            r"(?im)^(\s*[A-Z0-9_]*(?:API_KEY|SECRET|TOKEN|PASSWORD)[A-Z0-9_]*\s*=\s*)([^\s#]+)"
        ),
        r"\1[REDACTED_ENV_SECRET]",
    ),
]


class PolicyViolationError(RuntimeError):
    """Raised when an operation violates a hard non-prompt security boundary."""


def redact_sensitive_text(text: str) -> str:
    """Scrub credentials, tokens, and .env secrets before Cloud API transmission or logging."""
    if not text:
        return text
    redacted = text
    for pattern, replacement in SECRET_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def wrap_untrusted_input(source: str, content: str) -> str:
    """Isolate external untrusted text (chat, issue, PR comment, logs, wiki) inside sanitized XML tags."""
    safe_source = html.escape(source, quote=True)
    # Neutralize any attempt to close the XML tag or inject system directives
    neutralized = content.replace("</untrusted_input>", "&lt;/untrusted_input&gt;")
    neutralized = redact_sensitive_text(neutralized)
    return (
        f'<untrusted_input source="{safe_source}" trust_level="data_only">\n'
        f"{neutralized}\n"
        f"</untrusted_input>"
    )


def validate_branch_target(branch_name: str, protected_branches: list[str] | None = None) -> None:
    """Enforce that Digital Employee only writes to `ai-agent/*` and never protected branches."""
    clean = branch_name.strip()
    protected = {b.lower() for b in (protected_branches or ["main", "master", "release"])}
    if clean.lower() in protected or any(clean.lower().startswith(f"{p}/") for p in protected):
        raise PolicyViolationError(
            f"Hard boundary violation: branch '{clean}' is protected ({sorted(protected)})."
        )
    if not clean.startswith("ai-agent/"):
        raise PolicyViolationError(
            f"Hard boundary violation: branch '{clean}' must use prefix 'ai-agent/'."
        )


def normalize_rel_path(rel_path: str) -> str:
    """Normalize a repo-relative path and block path traversal (`..` or absolute paths)."""
    raw = rel_path.strip().replace("\\", "/")
    if not raw or raw.startswith("/"):
        raise PolicyViolationError(f"Path must be repository-relative, got: '{rel_path}'")
    norm = posixpath.normpath(raw)
    if norm == ".." or norm.startswith("../") or "/../" in f"/{norm}/":
        raise PolicyViolationError(f"Path traversal blocked: '{rel_path}'")
    if norm == ".git" or norm.startswith(".git/"):
        raise PolicyViolationError(f"Direct .git manipulation blocked: '{rel_path}'")
    return norm


def check_path_policy(
    rel_path: str,
    permitted_scopes: list[str] | None = None,
    forbidden_paths: list[str] | None = None,
) -> tuple[bool, str]:
    """Validate whether a file path is permitted by contract scope and not in forbidden paths."""
    try:
        norm = normalize_rel_path(rel_path)
    except PolicyViolationError as exc:
        return False, str(exc)

    default_forbidden = [".github/workflows/", ".env", ".ai/DECISIONS.yaml", ".ai/unit_backlog.txt"]
    all_forbidden = list(dict.fromkeys((forbidden_paths or []) + default_forbidden))
    for pattern in all_forbidden:
        pat = pattern.strip().replace("\\", "/")
        if not pat:
            continue
        if pat.endswith("/"):
            if norm.startswith(pat) or norm == pat.rstrip("/"):
                return False, f"File '{norm}' matches forbidden directory '{pat}'."
        elif fnmatch.fnmatch(norm, pat) or PurePosixPath(norm).name == pat:
            return False, f"File '{norm}' matches forbidden path policy '{pat}'."

    scopes = [s.strip().replace("\\", "/") for s in (permitted_scopes or []) if s.strip()]
    if not scopes or "*" in scopes or "**" in scopes:
        return True, "ok"

    for scope in scopes:
        clean_scope = scope.rstrip("/")
        if (
            norm == clean_scope
            or norm.startswith(f"{clean_scope}/")
            or fnmatch.fnmatch(norm, scope)
            or fnmatch.fnmatch(norm, f"{clean_scope}/*")
        ):
            return True, "ok"

    return False, f"File '{norm}' is outside TaskContract.permitted_scopes ({scopes})."
