"""Runtime settings & target repository registry loader."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from digital_employee.models.contract import RepositoryProfile

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _parse_dotenv(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        cleaned = val.strip().strip('"').strip("'")
        values[key.strip()] = cleaned
    return values


class Settings(BaseModel):
    """Runtime settings for Digital Employee V1."""

    inference_mode: Literal["cloud_api", "intranet_only"] = "cloud_api"
    llm_base_url: str = "https://api.deepseek.com"
    llm_api_key: str = Field(default="", repr=False)
    fast_model: str = "deepseek-flash"
    reasoning_model: str = "deepseek-v4-pro"
    llm_timeout_seconds: float = 60.0

    # Separate Cross-Model PR Review Agent settings
    reviewer_provider: Literal["openai_compatible", "anthropic", "gemini", "gemini_cli"] = (
        "openai_compatible"
    )
    reviewer_base_url: str = "https://api.deepseek.com"
    reviewer_api_key: str = Field(default="", repr=False)
    reviewer_model: str = "deepseek-flash"
    pr_review_skill_path: Path = Field(
        default_factory=lambda: PROJECT_ROOT / "skills" / "pr-code-review" / "SKILL.md"
    )

    data_dir: Path = Field(default_factory=lambda: PROJECT_ROOT / ".local")
    repo_registry_path: Path = Field(
        default_factory=lambda: PROJECT_ROOT / "configs" / "repositories.yaml"
    )

    default_tenant_id: str = "personal"
    default_workspace_id: str = "jamesyang-mac"
    default_owner_id: str = "jamesyang"
    default_max_budget_usd: float = 1.50
    max_coding_steps: int = 32
    max_self_heal_attempts: int = 3
    coding_runtime: Literal["kernel", "external_cli"] = "kernel"
    external_coding_cli: str = "claude"
    enable_os_sandbox: bool = False
    enable_ops_mcp: bool = True

    github_token: str = Field(default="", repr=False)
    github_api_base: str = "https://api.github.com"

    feishu_webhook_url: str = Field(default="", repr=False)
    feishu_app_id: str = ""
    feishu_app_secret: str = Field(default="", repr=False)
    feishu_verification_token: str = Field(default="", repr=False)
    feishu_default_chat_id: str = ""

    # Jira Integration (Authoritative Work-Order Source of Truth)
    jira_base_url: str = ""
    jira_email: str = ""
    jira_api_token: str = Field(default="", repr=False)
    jira_default_project: str = ""
    jira_poll_jql: str = 'labels = "ai-delegate" AND statusCategory = "To Do"'
    jira_poller_enabled: bool = True

    web_host: str = "127.0.0.1"
    web_port: int = 8020

    @property
    def db_path(self) -> Path:
        return self.data_dir / "state.db"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"

    @property
    def worktrees_dir(self) -> Path:
        return self.data_dir / "worktrees"

    @property
    def managed_repos_dir(self) -> Path:
        return self.data_dir / "repos"

    @property
    def runtime_repo_registry_path(self) -> Path:
        return self.data_dir / "repositories.yaml"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)
        self.managed_repos_dir.mkdir(parents=True, exist_ok=True)

    def load_repositories(self) -> dict[str, RepositoryProfile]:
        profiles: dict[str, RepositoryProfile] = {}
        removed: set[str] = set()
        for reg_path in (self.repo_registry_path, self.runtime_repo_registry_path):
            if not reg_path.is_file():
                continue
            raw = yaml.safe_load(reg_path.read_text(encoding="utf-8")) or {}
            if isinstance(raw, dict) and isinstance(raw.get("removed_repositories"), list):
                removed.update(str(r) for r in raw["removed_repositories"])
            repos_raw = raw.get("repositories", {}) if isinstance(raw, dict) else {}
            for key, cfg in repos_raw.items():
                if not isinstance(cfg, dict):
                    continue
                payload = dict(cfg)
                payload.setdefault("repo_id", key)
                profile = RepositoryProfile.model_validate(payload)
                profiles[profile.repo_id] = profile
        for rem_id in removed:
            profiles.pop(rem_id, None)
        return profiles

    def save_repository(self, profile: RepositoryProfile) -> Path:
        """Persist or update a dynamically onboarded repository in `data_dir/repositories.yaml`."""
        self.ensure_dirs()
        target = self.runtime_repo_registry_path
        existing: dict[str, dict[str, object]] = {}
        removed: list[str] = []
        if target.is_file():
            raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
            if isinstance(raw, dict):
                if isinstance(raw.get("repositories"), dict):
                    existing = dict(raw["repositories"])
                if isinstance(raw.get("removed_repositories"), list):
                    removed = [
                        str(r) for r in raw["removed_repositories"] if str(r) != profile.repo_id
                    ]
        existing[profile.repo_id] = profile.model_dump()
        out_doc: dict[str, object] = {"repositories": existing}
        if removed:
            out_doc["removed_repositories"] = removed
        target.write_text(
            "# Managed Target Repositories (Auto-Onboarded by Digital Employee)\n"
            + yaml.safe_dump(out_doc, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        return target

    def update_repository(
        self,
        repo_id: str,
        *,
        default_base_branch: str | None = None,
        pytest_cmd: list[str] | None = None,
        lint_cmd: list[str] | None = None,
        default_jira_project: str | None = None,
    ) -> RepositoryProfile:
        """Update editable configuration fields of a registered repository."""
        current = self.resolve_repository(repo_id)
        updates: dict[str, object] = {}
        if default_base_branch is not None and default_base_branch.strip():
            updates["default_base_branch"] = default_base_branch.strip()
        if pytest_cmd is not None:
            cleaned_test = [tok.strip() for tok in pytest_cmd if tok.strip()]
            if cleaned_test:
                updates["pytest_cmd"] = cleaned_test
        if lint_cmd is not None:
            updates["lint_cmd"] = [tok.strip() for tok in lint_cmd if tok.strip()]
        if default_jira_project is not None:
            updates["default_jira_project"] = default_jira_project.strip().upper()
        updated = current.model_copy(update=updates)
        self.save_repository(updated)
        return updated

    def delete_repository(self, repo_id: str) -> bool:
        """Unbind/remove a registered repository from the control plane."""
        profiles = self.load_repositories()
        if repo_id not in profiles:
            return False
        self.ensure_dirs()
        target = self.runtime_repo_registry_path
        existing: dict[str, dict[str, object]] = {}
        removed: set[str] = set()
        if target.is_file():
            raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
            if isinstance(raw, dict):
                if isinstance(raw.get("repositories"), dict):
                    existing = dict(raw["repositories"])
                if isinstance(raw.get("removed_repositories"), list):
                    removed.update(str(r) for r in raw["removed_repositories"])
        existing.pop(repo_id, None)
        removed.add(repo_id)
        target.write_text(
            "# Managed Target Repositories (Auto-Onboarded by Digital Employee)\n"
            + yaml.safe_dump(
                {
                    "repositories": existing,
                    "removed_repositories": sorted(removed),
                },
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        return True

    def resolve_repository(self, target: str) -> RepositoryProfile:
        profiles = self.load_repositories()
        if target in profiles:
            return profiles[target]
        for profile in profiles.values():
            if target in (profile.github_repo, profile.local_path, Path(profile.local_path).name):
                return profile
        raise KeyError(
            f"Unknown target repository '{target}'. Registered: {sorted(profiles.keys())}"
        )


def load_settings(env_file: Path | None = None) -> Settings:
    """Load settings from process env -> local .env -> optional fallback .env."""
    local_env = _parse_dotenv(env_file or (PROJECT_ROOT / ".env"))

    def get_val(key: str, default: str = "") -> str:
        if key in os.environ and os.environ[key] != "":
            return os.environ[key]
        return local_env.get(key, default)

    fallback_file_str = get_val("DE_FALLBACK_ENV_FILE", "")
    fallback_env = _parse_dotenv(Path(fallback_file_str)) if fallback_file_str else {}

    api_key = (
        get_val("DE_LLM_API_KEY")
        or os.environ.get("DEEPSEEK_API_KEY", "")
        or os.environ.get("OPENAI_API_KEY", "")
        or fallback_env.get("DEEPSEEK_API_KEY", "")
        or fallback_env.get("OPENAI_API_KEY", "")
    )
    base_url = (
        get_val("DE_LLM_BASE_URL")
        or fallback_env.get("DEEPSEEK_BASE_URL", "")
        or "https://api.deepseek.com"
    )
    fast_model = (
        get_val("DE_FAST_MODEL")
        or fallback_env.get("ROUTER_MODEL", "")
        or "deepseek-flash"
    )
    reasoning_model = (
        get_val("DE_REASONING_MODEL")
        or fallback_env.get("AGENT_MODEL", "")
        or "deepseek-v4-pro"
    )

    mode_raw = get_val("DE_INFERENCE_MODE", "cloud_api")
    inference_mode: Literal["cloud_api", "intranet_only"] = (
        "intranet_only" if mode_raw == "intranet_only" else "cloud_api"
    )

    # Resolve Independent Cross-Model PR Reviewer credentials
    anthropic_key = get_val("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY", "")
    gemini_key = (
        get_val("GEMINI_API_KEY")
        or get_val("GOOGLE_API_KEY")
        or os.environ.get("GEMINI_API_KEY", "")
        or os.environ.get("GOOGLE_API_KEY", "")
    )
    openai_only_key = os.environ.get("OPENAI_API_KEY", "")
    default_rev_provider = (
        "gemini"
        if gemini_key and not get_val("DE_REVIEWER_BASE_URL")
        else ("anthropic" if anthropic_key and not get_val("DE_REVIEWER_BASE_URL") else "openai_compatible")
    )
    rev_provider_raw = get_val("DE_REVIEWER_PROVIDER", default_rev_provider).lower()
    reviewer_provider: Literal["openai_compatible", "anthropic", "gemini", "gemini_cli"] = (
        rev_provider_raw  # type: ignore[assignment]
        if rev_provider_raw in ("openai_compatible", "anthropic", "gemini", "gemini_cli")
        else "openai_compatible"
    )
    if reviewer_provider == "gemini_cli":
        reviewer_api_key = ""
        reviewer_base_url = "gemini-cli://local"
        reviewer_model = get_val("DE_REVIEWER_MODEL") or "gemini-3.8-flash"
    elif reviewer_provider == "gemini":
        reviewer_api_key = get_val("DE_REVIEWER_API_KEY") or gemini_key or api_key
        reviewer_base_url = (
            get_val("DE_REVIEWER_BASE_URL")
            or "https://generativelanguage.googleapis.com/v1beta/openai"
        )
        reviewer_model = get_val("DE_REVIEWER_MODEL") or "gemini-3.8-flash"
    elif reviewer_provider == "anthropic":
        reviewer_api_key = get_val("DE_REVIEWER_API_KEY") or anthropic_key or api_key
        reviewer_base_url = get_val("DE_REVIEWER_BASE_URL") or "https://api.anthropic.com"
        reviewer_model = get_val("DE_REVIEWER_MODEL") or "claude-sonnet-4-20250514"
    elif get_val("DE_REVIEWER_API_KEY") or (openai_only_key and openai_only_key != api_key):
        reviewer_api_key = get_val("DE_REVIEWER_API_KEY") or openai_only_key
        reviewer_base_url = get_val("DE_REVIEWER_BASE_URL") or "https://api.openai.com/v1"
        reviewer_model = get_val("DE_REVIEWER_MODEL") or "gpt-4.1"
    else:
        reviewer_api_key = get_val("DE_REVIEWER_API_KEY") or api_key
        reviewer_base_url = get_val("DE_REVIEWER_BASE_URL") or base_url
        # Default to distinct model (`fast_model` vs `reasoning_model`) when sharing provider
        default_cross = fast_model if fast_model != reasoning_model else reasoning_model
        reviewer_model = get_val("DE_REVIEWER_MODEL") or default_cross

    settings = Settings(
        inference_mode=inference_mode,
        llm_base_url=base_url,
        llm_api_key=api_key,
        fast_model=fast_model,
        reasoning_model=reasoning_model,
        llm_timeout_seconds=float(get_val("DE_LLM_TIMEOUT_SECONDS", "60")),
        reviewer_provider=reviewer_provider,
        reviewer_base_url=reviewer_base_url,
        reviewer_api_key=reviewer_api_key,
        reviewer_model=reviewer_model,
        data_dir=Path(get_val("DE_DATA_DIR", str(PROJECT_ROOT / ".local"))),
        repo_registry_path=Path(
            get_val("DE_REPO_REGISTRY", str(PROJECT_ROOT / "configs" / "repositories.yaml"))
        ),
        default_tenant_id=get_val("DE_DEFAULT_TENANT_ID", "personal"),
        default_workspace_id=get_val("DE_DEFAULT_WORKSPACE_ID", "jamesyang-mac"),
        default_owner_id=get_val("DE_DEFAULT_OWNER_ID", "jamesyang"),
        default_max_budget_usd=float(get_val("DE_DEFAULT_MAX_BUDGET_USD", "1.50")),
        max_coding_steps=int(get_val("DE_MAX_CODING_STEPS", "32")),
        max_self_heal_attempts=int(get_val("DE_MAX_SELF_HEAL_ATTEMPTS", "3")),
        coding_runtime=(
            get_val("DE_CODING_RUNTIME", "kernel")  # type: ignore[arg-type]
            if get_val("DE_CODING_RUNTIME", "kernel") in ("kernel", "external_cli")
            else "kernel"
        ),
        external_coding_cli=get_val("DE_EXTERNAL_CODING_CLI", "claude"),
        enable_os_sandbox=get_val("DE_ENABLE_OS_SANDBOX", "false").lower() in ("1", "true", "yes"),
        enable_ops_mcp=get_val("DE_ENABLE_OPS_MCP", "true").lower() not in ("0", "false", "no"),
        github_token=get_val("DE_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN", ""),
        github_api_base=get_val("DE_GITHUB_API_BASE", "https://api.github.com"),
        feishu_webhook_url=get_val("DE_FEISHU_WEBHOOK_URL", ""),
        feishu_app_id=get_val("DE_FEISHU_APP_ID", ""),
        feishu_app_secret=get_val("DE_FEISHU_APP_SECRET", ""),
        feishu_verification_token=get_val("DE_FEISHU_VERIFICATION_TOKEN", ""),
        feishu_default_chat_id=get_val("DE_FEISHU_DEFAULT_CHAT_ID", ""),
        jira_base_url=get_val("DE_JIRA_BASE_URL", ""),
        jira_email=get_val("DE_JIRA_EMAIL", ""),
        jira_api_token=get_val("DE_JIRA_API_TOKEN", ""),
        jira_default_project=get_val("DE_JIRA_DEFAULT_PROJECT", ""),
        jira_poll_jql=get_val(
            "DE_JIRA_POLL_JQL",
            'labels = "ai-delegate" AND statusCategory = "To Do"',
        ),
        jira_poller_enabled=get_val("DE_JIRA_POLLER_ENABLED", "true").lower()
        not in ("0", "false", "no"),
        web_host=get_val("DE_WEB_HOST", "127.0.0.1"),
        web_port=int(get_val("DE_WEB_PORT", "8020")),
    )
    settings.ensure_dirs()
    return settings
