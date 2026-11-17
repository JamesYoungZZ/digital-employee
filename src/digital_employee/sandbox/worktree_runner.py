"""Isolated Git Worktree Sandbox & Scrubbed Subprocess Runner (Spec v2.0 Section 3.5)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from digital_employee.models.contract import RepositoryProfile
from digital_employee.security.dlp import (
    PolicyViolationError,
    check_path_policy,
    normalize_rel_path,
    redact_sensitive_text,
    validate_branch_target,
)

SENSITIVE_ENV_PREFIXES = (
    "DE_",
    "DEEPSEEK_",
    "OPENAI_",
    "ANTHROPIC_",
    "GITHUB_",
    "FEISHU_",
    "LARK_",
    "AWS_",
)


@dataclass
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def combined_output(self) -> str:
        out = (self.stdout + "\n" + self.stderr).strip()
        return redact_sensitive_text(out)


def build_sandbox_env(repo_root: Path) -> dict[str, str]:
    """Build a minimal credential-scrubbed environment for sandbox test execution."""
    safe_env: dict[str, str] = {}
    allowed_exact = {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "USER", "SHELL"}
    for k, v in os.environ.items():
        if any(k.upper().startswith(p) for p in SENSITIVE_ENV_PREFIXES):
            continue
        if any(sec in k.upper() for sec in ("SECRET", "TOKEN", "API_KEY", "PASSWORD")):
            continue
        if k in allowed_exact or k.startswith("PYTHON") or k.startswith("UV_"):
            safe_env[k] = v
    safe_env["PYTHONPATH"] = str(repo_root)
    safe_env["PYTHONDONTWRITEBYTECODE"] = "1"
    safe_env["NO_COLOR"] = "1"
    return safe_env


class WorktreeSandbox:
    """Manages an isolated `ai-agent/{task_id}` git worktree and safe tool execution."""

    def __init__(
        self,
        profile: RepositoryProfile,
        worktrees_root: Path,
        task_id: str,
        *,
        permitted_scopes: list[str] | None = None,
    ) -> None:
        self.profile = profile
        self.repo_path = Path(profile.local_path).resolve()
        self.worktrees_root = worktrees_root.resolve()
        self.task_id = task_id
        self.branch_name = f"ai-agent/{task_id}"
        validate_branch_target(self.branch_name, profile.protected_branches)
        self.worktree_path = self.worktrees_root / task_id
        self.permitted_scopes = permitted_scopes or []

    def resolve_commit(self, rev: str) -> str:
        # Sync remote tracking branch when online so new worktrees start from latest merged origin/<rev>
        has_origin = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(self.repo_path),
            capture_output=True,
            text=True,
        )
        if has_origin.returncode == 0:
            subprocess.run(
                ["git", "fetch", "origin", rev],
                cwd=str(self.repo_path),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            remote_proc = subprocess.run(
                ["git", "rev-parse", "--verify", f"refs/remotes/origin/{rev}"],
                cwd=str(self.repo_path),
                capture_output=True,
                text=True,
            )
            if remote_proc.returncode == 0:
                return remote_proc.stdout.strip()

        proc = subprocess.run(
            ["git", "rev-parse", rev],
            cwd=str(self.repo_path),
            capture_output=True,
            text=True,
            check=True,
        )
        return proc.stdout.strip()

    def provision(self, base_commit_sha: str) -> Path:
        """Create or attach isolated git worktree at `ai-agent/{task_id}`."""
        validate_branch_target(self.branch_name, self.profile.protected_branches)
        self.worktrees_root.mkdir(parents=True, exist_ok=True)
        if self.worktree_path.exists() and (self.worktree_path / ".git").exists():
            return self.worktree_path

        # Prune stale worktree metadata first
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=str(self.repo_path),
            capture_output=True,
            text=True,
            check=False,
        )
        # Check if branch already exists from an interrupted run
        branch_check = subprocess.run(
            ["git", "show-ref", "--verify", f"refs/heads/{self.branch_name}"],
            cwd=str(self.repo_path),
            capture_output=True,
            text=True,
        )
        if branch_check.returncode == 0:
            cmd = ["git", "worktree", "add", str(self.worktree_path), self.branch_name]
        else:
            cmd = [
                "git",
                "worktree",
                "add",
                "-b",
                self.branch_name,
                str(self.worktree_path),
                base_commit_sha,
            ]
        subprocess.run(cmd, cwd=str(self.repo_path), capture_output=True, text=True, check=True)
        return self.worktree_path

    def cleanup_worktree(self, target_dir: Path | None = None) -> None:
        path = target_dir or self.worktree_path
        if not path.exists():
            return
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(path)],
            cwd=str(self.repo_path),
            capture_output=True,
            text=True,
            check=False,
        )
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)

    def create_detached_baseline_worktree(self, base_commit_sha: str) -> Path:
        """Provision a short-lived detached worktree at `base_commit_sha` for Fail-to-Pass checks."""
        base_dir = self.worktrees_root / f"{self.task_id}-base-verify"
        if base_dir.exists():
            self.cleanup_worktree(base_dir)
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(base_dir), base_commit_sha],
            cwd=str(self.repo_path),
            capture_output=True,
            text=True,
            check=True,
        )
        return base_dir

    # =========================================================================
    # Controlled File & Search Operations inside Sandbox
    # =========================================================================

    def list_files(self, prefix: str = "", max_entries: int = 120) -> list[str]:
        root = self.worktree_path if self.worktree_path.exists() else self.repo_path
        proc = subprocess.run(
            ["git", "ls-files"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=True,
        )
        files = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        if prefix:
            norm_prefix = normalize_rel_path(prefix).rstrip("/")
            files = [f for f in files if f == norm_prefix or f.startswith(f"{norm_prefix}/")]
        return files[:max_entries]

    def search_code(self, pattern: str, path_prefix: str = "", max_hits: int = 40) -> str:
        root = self.worktree_path if self.worktree_path.exists() else self.repo_path
        files = self.list_files(prefix=path_prefix, max_entries=2000)
        try:
            regex = re.compile(pattern)
        except re.error:
            regex = re.compile(re.escape(pattern))

        hits: list[str] = []
        for rel in files:
            fpath = root / rel
            if not fpath.is_file():
                continue
            try:
                content = fpath.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for idx, line in enumerate(content.splitlines(), start=1):
                if regex.search(line):
                    hits.append(f"{rel}:{idx}: {line[:200]}")
                    if len(hits) >= max_hits:
                        return "\n".join(hits)
        return "\n".join(hits) if hits else "(no matches)"

    def read_file(self, rel_path: str, start_line: int = 1, end_line: int = 250) -> str:
        norm = normalize_rel_path(rel_path)
        root = self.worktree_path if self.worktree_path.exists() else self.repo_path
        target = root / norm
        if not target.is_file():
            raise FileNotFoundError(f"File not found: {norm}")
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        s = max(1, start_line)
        e = min(len(lines), max(s, end_line))
        numbered = [f"{i}: {lines[i - 1]}" for i in range(s, e + 1)]
        header = f"# {norm} (lines {s}-{e} of {len(lines)})"
        return redact_sensitive_text(header + "\n" + "\n".join(numbered))

    def write_or_edit_file(
        self,
        rel_path: str,
        *,
        new_content: str | None = None,
        old_snippet: str | None = None,
        replacement_snippet: str | None = None,
    ) -> str:
        norm = normalize_rel_path(rel_path)
        allowed, reason = check_path_policy(
            norm,
            permitted_scopes=self.permitted_scopes,
            forbidden_paths=self.profile.forbidden_paths,
        )
        if not allowed:
            raise PolicyViolationError(reason)

        target = self.worktree_path / norm
        if new_content is not None:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_file():
                raw_old = target.read_bytes()
                if b"\r\n" in raw_old and "\r\n" not in new_content:
                    new_content = new_content.replace("\n", "\r\n")
            with target.open("w", encoding="utf-8", newline="") as fh:
                fh.write(new_content)
            return f"Wrote {norm} ({len(new_content)} bytes)."

        if old_snippet is None or replacement_snippet is None:
            raise ValueError("Provide either `new_content` or both `old_snippet` and `replacement_snippet`.")
        if not target.is_file():
            raise FileNotFoundError(f"Cannot edit non-existent file: {norm}")
        with target.open("r", encoding="utf-8", newline="") as fh:
            existing = fh.read()
        uses_crlf = "\r\n" in existing
        norm_old = old_snippet.replace("\r\n", "\n")
        norm_rep = replacement_snippet.replace("\r\n", "\n")
        if uses_crlf:
            norm_old = norm_old.replace("\n", "\r\n")
            norm_rep = norm_rep.replace("\n", "\r\n")
        if norm_old not in existing:
            raise ValueError(f"`old_snippet` not found in {norm}. Re-read file for exact lines.")
        updated = existing.replace(norm_old, norm_rep, 1)
        with target.open("w", encoding="utf-8", newline="") as fh:
            fh.write(updated)
        return f"Edited {norm} successfully."

    def run_pytest(
        self,
        targets: list[str],
        *,
        cwd: Path | None = None,
        timeout_seconds: int = 90,
    ) -> CommandResult:
        exec_dir = cwd or (
            self.worktree_path if self.worktree_path.exists() else self.repo_path
        )
        clean_targets: list[str] = []
        for t in targets:
            # Support pytest node IDs like `tests/unit/test_x.py::TestClass::test_method`
            file_part, *node_parts = t.split("::")
            norm_file = normalize_rel_path(file_part)
            clean_targets.append("::".join([norm_file, *node_parts]))

        cmd = [*self.profile.pytest_cmd, "-q", "--tb=short", *clean_targets]
        return self._run_cmd(cmd, cwd=exec_dir, timeout_seconds=timeout_seconds)

    def run_linter(self, targets: list[str], *, timeout_seconds: int = 60) -> CommandResult:
        if not self.profile.lint_cmd:
            return CommandResult(exit_code=0, stdout="No lint_cmd configured; skipped.", stderr="")
        clean_targets = [normalize_rel_path(t) for t in targets if t.endswith(".py")]
        if not clean_targets:
            return CommandResult(exit_code=0, stdout="No Python files modified.", stderr="")
        cmd = [*self.profile.lint_cmd, *clean_targets]
        return self._run_cmd(cmd, cwd=self.worktree_path, timeout_seconds=timeout_seconds)

    def _run_cmd(
        self,
        cmd: list[str],
        *,
        cwd: Path,
        timeout_seconds: int = 90,
    ) -> CommandResult:
        env = build_sandbox_env(cwd)
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(cwd),
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
            return CommandResult(
                exit_code=proc.returncode,
                stdout=proc.stdout[-12000:],
                stderr=proc.stderr[-6000:],
            )
        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                exit_code=124,
                stdout=(exc.stdout or "")[-6000:] if isinstance(exc.stdout, str) else "",
                stderr=f"Command timed out after {timeout_seconds}s.",
                timed_out=True,
            )

    def modified_files(self, base_commit_sha: str) -> list[str]:
        """List all tracked modified + untracked files relative to `base_commit_sha`."""
        tracked = subprocess.run(
            ["git", "diff", "--name-only", base_commit_sha],
            cwd=str(self.worktree_path),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=str(self.worktree_path),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        combined = sorted({f.strip() for f in [*tracked, *untracked] if f.strip()})
        return combined

    def git_diff(self, base_commit_sha: str, paths: list[str] | None = None) -> str:
        cmd = ["git", "diff", base_commit_sha]
        if paths:
            cmd.extend(["--", *paths])
        proc = subprocess.run(
            cmd,
            cwd=str(self.worktree_path),
            capture_output=True,
            text=True,
            check=True,
        )
        return redact_sensitive_text(proc.stdout)

    def commit_all(self, message: str, base_commit_sha: str) -> str:
        """Validate scope on all changed files and create a commit on `ai-agent/{task_id}`."""
        validate_branch_target(self.branch_name, self.profile.protected_branches)
        changed = self.modified_files(base_commit_sha)
        if not changed:
            return self.resolve_worktree_head()

        for rel in changed:
            allowed, reason = check_path_policy(
                rel,
                permitted_scopes=self.permitted_scopes,
                forbidden_paths=self.profile.forbidden_paths,
            )
            if not allowed:
                raise PolicyViolationError(reason)

        subprocess.run(
            ["git", "add", "-A"],
            cwd=str(self.worktree_path),
            capture_output=True,
            text=True,
            check=True,
        )
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(self.worktree_path),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if status:
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=AI Digital Employee",
                    "-c",
                    "user.email=digital-employee@local.internal",
                    "commit",
                    "-m",
                    message,
                ],
                cwd=str(self.worktree_path),
                capture_output=True,
                text=True,
                check=True,
            )
        return self.resolve_worktree_head()

    def resolve_worktree_head(self) -> str:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(self.worktree_path),
            capture_output=True,
            text=True,
            check=True,
        )
        return proc.stdout.strip()
