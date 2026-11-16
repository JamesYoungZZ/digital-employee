"""Auto-Onboarding Engine: register any local or remote Git repository in seconds."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

from digital_employee.models.contract import RepositoryProfile
from digital_employee.settings import Settings

GITHUB_SLUG_RE = re.compile(
    r"(?:github\.com[:/])([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+?)(?:\.git)?$"
)
SHORTHAND_RE = re.compile(r"^[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+$")


def extract_github_slug(remote_or_source: str) -> str:
    s = remote_or_source.strip()
    if SHORTHAND_RE.match(s) and not Path(s).exists():
        return s.removesuffix(".git")
    m = GITHUB_SLUG_RE.search(s)
    if m:
        return m.group(1)
    return "local/repository"


def _detect_default_branch(repo_dir: Path) -> str:
    sym = subprocess.run(
        ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
    )
    if sym.returncode == 0 and "/" in sym.stdout.strip():
        return sym.stdout.strip().split("/", 1)[1]

    cur = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
    )
    branch = cur.stdout.strip()
    return branch if (cur.returncode == 0 and branch and branch != "HEAD") else "main"


def _detect_runner_commands(
    repo_dir: Path, *, bootstrap_venv: bool = False
) -> tuple[str, list[str], list[str]]:
    """Auto-detect Python / Node / Go test & lint commands for a repository."""
    is_python = any(
        (repo_dir / marker).exists()
        for marker in ("pyproject.toml", "pytest.ini", "requirements.txt", "setup.py")
    )
    if is_python:
        venv_py = repo_dir / ".venv" / "bin" / "python"
        if not venv_py.is_file() and bootstrap_venv:
            uv_bin = shutil.which("uv") or "/opt/homebrew/bin/uv"
            if Path(uv_bin).is_file():
                subprocess.run(
                    [uv_bin, "venv", str(repo_dir / ".venv")],
                    cwd=str(repo_dir),
                    check=True,
                    capture_output=True,
                )
                if (repo_dir / "pyproject.toml").is_file():
                    subprocess.run(
                        [uv_bin, "pip", "install", "--python", str(venv_py), "-e", ".[dev]"],
                        cwd=str(repo_dir),
                        check=False,
                        capture_output=True,
                    )

        py_bin = str(venv_py) if venv_py.is_file() else sys.executable
        ruff_bin = repo_dir / ".venv" / "bin" / "ruff"
        lint_cmd = [str(ruff_bin), "check"] if ruff_bin.is_file() else []
        return py_bin, [py_bin, "-m", "pytest"], lint_cmd

    if (repo_dir / "package.json").is_file():
        runner = "pnpm" if (repo_dir / "pnpm-lock.yaml").is_file() else "npm"
        return sys.executable, [runner, "test", "--"], []

    if (repo_dir / "go.mod").is_file():
        return sys.executable, ["go", "test"], []

    return sys.executable, [sys.executable, "-m", "pytest"], []


def onboard_repository(
    settings: Settings,
    source: str,
    *,
    repo_id: str | None = None,
    base_branch: str | None = None,
    bootstrap_venv: bool = False,
) -> RepositoryProfile:
    """Clone (if remote) or attach (if local) a Git repo, detect stack, and register it."""
    settings.ensure_dirs()
    raw = source.strip()
    candidate_path = Path(raw).expanduser()

    if candidate_path.is_dir() and (candidate_path / ".git").exists():
        repo_dir = candidate_path.resolve()
        origin_proc = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
        )
        remote_url = origin_proc.stdout.strip() if origin_proc.returncode == 0 else ""
        gh_slug = extract_github_slug(remote_url) if remote_url else f"local/{repo_dir.name}"
        inferred_id = repo_id or repo_dir.name.lower().replace("_", "-")
    else:
        gh_slug = extract_github_slug(raw)
        if SHORTHAND_RE.match(raw):
            clone_url = f"git@github.com:{raw}.git"
        else:
            clone_url = raw
        inferred_id = (
            repo_id
            or gh_slug.split("/")[-1].lower().replace("_", "-")
        )
        repo_dir = (settings.managed_repos_dir / inferred_id).resolve()
        if not (repo_dir / ".git").exists():
            repo_dir.parent.mkdir(parents=True, exist_ok=True)
            proc = subprocess.run(
                ["git", "clone", clone_url, str(repo_dir)],
                capture_output=True,
                text=True,
                timeout=180,
            )
            if proc.returncode != 0 and SHORTHAND_RE.match(raw):
                # Fallback to HTTPS clone if SSH was not configured
                https_url = f"https://github.com/{raw}.git"
                subprocess.run(
                    ["git", "clone", https_url, str(repo_dir)],
                    capture_output=True,
                    text=True,
                    timeout=180,
                    check=True,
                )
            elif proc.returncode != 0:
                raise RuntimeError(f"git clone failed: {proc.stderr.strip()}")
        else:
            subprocess.run(
                ["git", "fetch", "origin"],
                cwd=str(repo_dir),
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )

    chosen_branch = base_branch or _detect_default_branch(repo_dir)
    py_bin, pytest_cmd, lint_cmd = _detect_runner_commands(
        repo_dir, bootstrap_venv=bootstrap_venv
    )

    profile = RepositoryProfile(
        repo_id=inferred_id,
        github_repo=gh_slug,
        local_path=str(repo_dir),
        default_base_branch=chosen_branch,
        python_bin=py_bin,
        pytest_cmd=pytest_cmd,
        lint_cmd=lint_cmd,
        default_suite_targets=[],
    )
    settings.save_repository(profile)
    return profile
