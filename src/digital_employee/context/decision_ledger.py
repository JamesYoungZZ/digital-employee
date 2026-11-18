"""Truth Hierarchy, Repository Governance Rules & Automated Policy Health Audit."""

from __future__ import annotations

import fnmatch
import re
from datetime import date
from itertools import islice
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator

if TYPE_CHECKING:
    from digital_employee.models.contract import RepositoryProfile
    from digital_employee.storage.durable_store import DurableStore

PolicyCategory = Literal["ARCHITECTURE", "SECURITY", "TESTING", "CODE_QUALITY"]
PolicyStatus = Literal[
    "APPROVED_POLICY",
    "CANDIDATE_DRAFT",
    "PAUSED",
    "ONE_OFF_WORKAROUND",
    "DEPRECATED",
]
PolicySourceType = Literal[
    "CONSOLE_MANUAL",
    "TASK_PROMOTED",
    "REPO_SYNC",
    "MEM_DISTILLED",
]
PolicyHealthStatus = Literal[
    "HEALTHY",
    "WARN_STALE_REF",
    "WARN_NEEDS_REVIEW",
    "INVALID_SCOPE",
]

# Product/PRD pseudo-rules from legacy YAML files that do not belong in an engineering ledger
_EXCLUDED_PRODUCT_PRD_RULE_IDS: set[str] = {
    "RAG-DEC-003",
    "COPILOT-DEC-003",
}

# Pure code, build & CI engineering rules upgraded from initial repo YAML files
_LEGACY_RULE_UPGRADES: dict[str, dict[str, str]] = {
    "RAG-DEC-001": {
        "title": "接口签名红线：敏感租户上下文严禁暴露为工具入参",
        "category": "SECURITY",
        "rule": "MCP Tool 函数签名严禁声明 tenant_id 或 corpus_root 参数；租户与语料根目录只允许在服务端初始化时从环境变量读取。",
        "reason": "防止外部调用参数覆盖服务端租户上下文或触发目录穿越读取。",
    },
    "RAG-DEC-002": {
        "title": "本地构建必跑：修改测试语料后须重建 SHA-256 与索引",
        "category": "TESTING",
        "rule": "修改 fixtures/commerce/documents/*.md 后，提交代码前必须执行 scripts/index_commerce.py --dense 同步更新 manifest.json 哈希与 vectors.json。",
        "reason": "单测启动器强制校验夹具文件 SHA-256 哈希，漏跑构建脚本会导致 pytest 收集阶段报错。",
    },
    "COPILOT-DEC-001": {
        "title": "CI 门禁必跑：增删 pytest 用例后须执行统计同步脚本",
        "category": "TESTING",
        "rule": "在 tests/ 下新增、删除或参数化测试用例后，提交代码前必须运行 .venv/bin/python scripts/update_project_stats.py 更新 README.md 用例计数。",
        "reason": "GitHub Actions (evaluation.yml) 会运行 scripts/update_project_stats.py --check，未同步会直接导致线上 CI 失败。",
    },
    "COPILOT-DEC-002": {
        "title": "模块依赖禁令：app/ 在线代码严禁 import legacy 目录",
        "category": "ARCHITECTURE",
        "rule": "app/ 目录下的所有在线运行时模块，严禁出现任何指向 app/legacy/ 的 import 语句。",
        "reason": "app/legacy/ 为已冻结的历史离线复现代码，架构单测会静态扫描并拦截该依赖。",
    },
}

_PATH_TOKEN_RE = re.compile(r"(?<![a-zA-Z0-9_:/.-])((?:[a-zA-Z0-9_.-]+/)+[a-zA-Z0-9_.*-]+/?)")
_KNOWN_CODE_EXTENSIONS = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".go",
    ".rs",
    ".java",
    ".md",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".sh",
    ".sql",
}
_IGNORED_PATH_PREFIXES = (".venv/", "venv/", "node_modules/", ".git/", "http/", "https/")


def infer_policy_category(scope: str, rule: str) -> PolicyCategory:
    """Infer an engineering category when importing rules without an explicit category."""
    text = f"{scope} {rule}".lower()
    if any(k in text for k in ("tenant", "injection", "secret", "auth", "token", "leak", "env", "安全", "越权", "脱敏", "隔离", "密钥")):
        return "SECURITY"
    if any(k in text for k in ("pytest", "test", "fixture", "ci ", "manifest.json", "测试", "用例", "门禁", "脚本")):
        return "TESTING"
    if any(k in text for k in ("import", "legacy", "layer", "mcp", "module", "depend", "架构", "分层", "依赖", "调用")):
        return "ARCHITECTURE"
    return "CODE_QUALITY"


def build_fallback_title(rule: str, scope: str = "*") -> str:
    """Create a concise readable title when legacy entries omit `title`."""
    cleaned = " ".join(rule.strip().split())
    if not cleaned:
        return f"目录 {scope} 研发规范"
    first_clause = re.split(r"[。；;.\n]", cleaned, maxsplit=1)[0].strip()
    if len(first_clause) <= 38:
        return first_clause
    return first_clause[:36].rstrip() + "…"


class PolicyHealth(BaseModel):
    """Deterministic repository filesystem & freshness validation result for a policy."""

    health_status: PolicyHealthStatus = "HEALTHY"
    health_message: str = "路径校验通过"
    matched_files_count: int | None = None
    stale_references: list[str] = Field(default_factory=list)


class DecisionEntry(BaseModel):
    """Single software engineering guardrail managed for a target repository."""

    id: str
    title: str = ""
    category: PolicyCategory = "CODE_QUALITY"
    scope: str = "*"
    rule: str
    reason: str = ""
    status: PolicyStatus = "APPROVED_POLICY"
    source_type: PolicySourceType = "CONSOLE_MANUAL"
    source_ref: str = ""
    owner: str = ""
    verified_at: str = Field(default_factory=lambda: date.today().isoformat())
    updated_at: str = Field(default_factory=lambda: date.today().isoformat())
    visibility_rbac: str = "default"

    @field_validator("category", mode="before")
    @classmethod
    def _coerce_legacy_category(cls, v: Any) -> PolicyCategory:
        if v in ("ARCHITECTURE", "SECURITY", "TESTING", "CODE_QUALITY"):
            return v
        return "CODE_QUALITY"

    def normalized(self) -> DecisionEntry:
        """Ensure title and category are always populated for display."""
        updates: dict[str, Any] = {}
        if not self.title.strip():
            updates["title"] = build_fallback_title(self.rule, self.scope)
        if not self.updated_at:
            updates["updated_at"] = self.verified_at or date.today().isoformat()
        return self.model_copy(update=updates) if updates else self


def _path_pattern_exists(repo_root: Path, raw_pattern: str) -> tuple[bool, int]:
    """Check whether a literal path or glob pattern matches anything inside `repo_root`."""
    norm = raw_pattern.strip().lstrip("./")
    if not norm or norm in ("*", "**"):
        return True, -1

    if any(ch in norm for ch in ("*", "?", "[")):
        matches = [
            p
            for p in islice(repo_root.glob(norm), 51)
            if ".git" not in p.parts and ".venv" not in p.parts
        ]
        if matches:
            return True, len(matches)
        # Also handle directory prefix patterns like `dir/*` where directory itself exists
        base_dir = norm.rstrip("/*")
        if base_dir and "*" not in base_dir and (repo_root / base_dir).is_dir():
            sub_matches = list(islice((repo_root / base_dir).rglob("*"), 51))
            if sub_matches:
                return True, len(sub_matches)
        return False, 0

    target = repo_root / norm.rstrip("/")
    if not target.exists():
        return False, 0
    if target.is_dir():
        sub_count = len(list(islice(target.rglob("*"), 51)))
        return True, max(1, sub_count)
    return True, 1


def _extract_referenced_repo_paths(repo_root: Path, text: str) -> list[str]:
    """Extract explicit file/directory paths mentioned inside rule text without false positives."""
    existing_top_dirs = {
        p.name
        for p in repo_root.iterdir()
        if p.is_dir() and not p.name.startswith(".")
    }
    candidates: list[str] = []
    for match in _PATH_TOKEN_RE.finditer(text):
        token = match.group(1).strip("`'\".,;:()[]")
        norm = token.lstrip("./")
        if not norm or norm.startswith(_IGNORED_PATH_PREFIXES):
            continue
        first_seg = norm.split("/", 1)[0]
        suffix = Path(norm.rstrip("/*")).suffix.lower()
        is_explicit_path = (
            suffix in _KNOWN_CODE_EXTENSIONS
            or token.endswith("/")
            or token.endswith("/*")
            or first_seg in existing_top_dirs
        )
        if is_explicit_path and norm not in candidates:
            candidates.append(norm)
    return candidates


def audit_policy_health(
    repo_root: Path | None,
    entry: DecisionEntry,
    *,
    stale_days_threshold: int = 90,
) -> PolicyHealth:
    """Deterministically verify that a policy's scope, referenced files, and review date are valid."""
    if repo_root is None:
        return PolicyHealth(health_status="HEALTHY", health_message="内存规则（未绑定本地目录）")

    if not repo_root.is_dir():
        return PolicyHealth(
            health_status="INVALID_SCOPE",
            health_message=f"本地仓库路径不存在：{repo_root}",
            matched_files_count=0,
        )

    scope = entry.scope.strip()
    if scope and scope not in ("*", "**"):
        ok, count = _path_pattern_exists(repo_root, scope)
        if not ok:
            return PolicyHealth(
                health_status="INVALID_SCOPE",
                health_message=f"作用路径 `{scope}` 在当前仓库中不存在，已自动暂停向 AI 注入",
                matched_files_count=0,
            )
    else:
        count = -1

    # Cross-check explicit scripts/files/dirs mentioned inside `entry.rule`
    missing_refs: list[str] = []
    for ref in _extract_referenced_repo_paths(repo_root, entry.rule):
        ref_ok, _ = _path_pattern_exists(repo_root, ref)
        if not ref_ok:
            missing_refs.append(ref)

    if missing_refs:
        joined = "、".join(f"`{r}`" for r in missing_refs[:3])
        return PolicyHealth(
            health_status="WARN_STALE_REF",
            health_message=f"规则内提及的路径 {joined} 在仓库中未找到，请复核规则是否已过期",
            matched_files_count=None if count < 0 else count,
            stale_references=missing_refs,
        )

    # Check human verification freshness
    try:
        verified_dt = date.fromisoformat(entry.verified_at[:10])
        age_days = (date.today() - verified_dt).days
        if age_days > stale_days_threshold:
            return PolicyHealth(
                health_status="WARN_NEEDS_REVIEW",
                health_message=f"距上次核验（{entry.verified_at[:10]}）已过 {age_days} 天，建议复核确认",
                matched_files_count=None if count < 0 else count,
            )
    except ValueError:
        pass

    if count < 0:
        msg = "仓库全局生效"
    elif count == 1 and not any(ch in scope for ch in ("*", "?", "[")):
        msg = "路径有效（目标文件/目录存在）"
    elif count > 50:
        msg = "路径有效（匹配 50+ 个文件）"
    else:
        msg = f"路径有效（匹配 {count} 个文件）"

    return PolicyHealth(
        health_status="HEALTHY",
        health_message=msg,
        matched_files_count=None if count < 0 else count,
    )


class DecisionLedger:
    """Loads, validates, queries, and formats repository architectural/business governance policies."""

    def __init__(
        self,
        entries: list[DecisionEntry] | None = None,
        *,
        repo_root: Path | None = None,
    ) -> None:
        self.entries: list[DecisionEntry] = [e.normalized() for e in (entries or [])]
        self.repo_root = repo_root

    @classmethod
    def from_repo(
        cls, repo_root: Path, ledger_rel_path: str = ".ai/DECISIONS.yaml"
    ) -> DecisionLedger:
        """Load directly from a repository YAML file (kept for standalone/file-mode compatibility)."""
        ledger_file = repo_root / ledger_rel_path
        if not ledger_file.is_file():
            return cls([], repo_root=repo_root)
        raw = yaml.safe_load(ledger_file.read_text(encoding="utf-8")) or {}
        items = raw.get("decisions", raw if isinstance(raw, list) else [])
        entries: list[DecisionEntry] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            payload = dict(item)
            payload.setdefault("source_type", "REPO_SYNC")
            payload.setdefault("source_ref", ledger_rel_path)
            entry = DecisionEntry.model_validate(payload).normalized()
            entries.append(entry)
        return cls(entries, repo_root=repo_root)

    @classmethod
    def for_repository(
        cls,
        store: DurableStore,
        profile: RepositoryProfile,
    ) -> DecisionLedger:
        """Load managed engineering policies from `DurableStore` (zero repo intrusion)."""
        repo_root = Path(profile.local_path)
        if not store.has_initialized_policies(profile.repo_id):
            cls._seed_from_repo_file_once(store, profile)
        elif not store.get_policy_sync_note(profile.repo_id).startswith("engineering_v2:"):
            cls._migrate_to_pure_engineering_v2(store, profile)
        entries = store.list_repository_policies(profile.repo_id)
        return cls(entries, repo_root=repo_root)

    @classmethod
    def _migrate_to_pure_engineering_v2(
        cls,
        store: DurableStore,
        profile: RepositoryProfile,
    ) -> None:
        """Purge legacy PRD pseudo-rules once and refresh synced engineering guardrails."""
        for bad_id in _EXCLUDED_PRODUCT_PRD_RULE_IDS:
            existing_bad = store.get_repository_policy(profile.repo_id, bad_id)
            if existing_bad is not None and existing_bad.source_type == "REPO_SYNC":
                store.delete_repository_policy(profile.repo_id, bad_id)

        for entry in store.list_repository_policies(profile.repo_id):
            upgrade = _LEGACY_RULE_UPGRADES.get(entry.id)
            if upgrade and entry.source_type == "REPO_SYNC":
                refreshed = entry.model_copy(
                    update={
                        "title": upgrade["title"],
                        "category": upgrade["category"],
                        "rule": upgrade["rule"],
                        "reason": upgrade["reason"],
                    }
                ).normalized()
                store.upsert_repository_policy(profile.repo_id, refreshed)

        store.mark_policies_initialized(
            profile.repo_id,
            source_note="engineering_v2:purged_prd_rules",
        )

    @classmethod
    def _seed_from_repo_file_once(
        cls,
        store: DurableStore,
        profile: RepositoryProfile,
    ) -> int:
        """One-time non-intrusive import of engineering rules from `<repo>/.ai/DECISIONS.yaml`."""
        repo_root = Path(profile.local_path)
        file_ledger = cls.from_repo(repo_root, profile.decision_ledger_path)
        imported = 0
        for entry in file_ledger.entries:
            if entry.id in _EXCLUDED_PRODUCT_PRD_RULE_IDS:
                continue
            upgrade = _LEGACY_RULE_UPGRADES.get(entry.id)
            if upgrade:
                entry = entry.model_copy(
                    update={
                        "title": upgrade["title"],
                        "category": upgrade["category"],
                        "rule": upgrade["rule"],
                        "reason": upgrade["reason"],
                    }
                )
            else:
                entry = entry.model_copy(
                    update={
                        "title": entry.title or build_fallback_title(entry.rule, entry.scope),
                        "category": infer_policy_category(entry.scope, entry.rule),
                    }
                )
            entry = entry.model_copy(
                update={
                    "source_type": "REPO_SYNC",
                    "source_ref": profile.decision_ledger_path,
                    "owner": entry.owner or "jamesyang",
                }
            ).normalized()
            store.upsert_repository_policy(profile.repo_id, entry, sort_order=imported)
            imported += 1
        store.mark_policies_initialized(
            profile.repo_id,
            source_note=f"engineering_v2:seeded {imported} from {profile.decision_ledger_path}",
        )
        return imported

    @classmethod
    def sync_incremental_from_repo(
        cls,
        store: DurableStore,
        profile: RepositoryProfile,
    ) -> int:
        """Import only new engineering policy IDs from `<repo>/.ai/DECISIONS.yaml`."""
        repo_root = Path(profile.local_path)
        file_ledger = cls.from_repo(repo_root, profile.decision_ledger_path)
        existing_ids = {e.id for e in store.list_repository_policies(profile.repo_id)}
        added = 0
        base_order = len(existing_ids)
        for entry in file_ledger.entries:
            if entry.id in existing_ids or entry.id in _EXCLUDED_PRODUCT_PRD_RULE_IDS:
                continue
            upgrade = _LEGACY_RULE_UPGRADES.get(entry.id)
            if upgrade:
                entry = entry.model_copy(update=upgrade)
            entry = entry.model_copy(
                update={
                    "source_type": "REPO_SYNC",
                    "source_ref": profile.decision_ledger_path,
                    "owner": entry.owner or "jamesyang",
                }
            ).normalized()
            store.upsert_repository_policy(
                profile.repo_id, entry, sort_order=base_order + added
            )
            added += 1
        store.mark_policies_initialized(
            profile.repo_id,
            source_note=f"engineering_v2:incremental +{added} from {profile.decision_ledger_path}",
        )
        return added

    def active_policies(
        self,
        touched_paths: list[str] | None = None,
        *,
        enforce_repo_health: bool | None = None,
    ) -> list[DecisionEntry]:
        """Return `APPROVED_POLICY` entries matching `touched_paths` (skipping broken-scope rules on real git repos)."""
        check_health = (
            enforce_repo_health
            if enforce_repo_health is not None
            else bool(self.repo_root and (self.repo_root / ".git").exists())
        )
        approved: list[DecisionEntry] = []
        for e in self.entries:
            if e.status != "APPROVED_POLICY":
                continue
            if check_health and self.repo_root is not None:
                health = audit_policy_health(self.repo_root, e)
                if health.health_status == "INVALID_SCOPE":
                    continue
            approved.append(e)

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

    def serialize_with_health(self) -> list[dict[str, Any]]:
        """Serialize all managed policies with live filesystem & verification health diagnostics."""
        items: list[dict[str, Any]] = []
        for entry in self.entries:
            health = audit_policy_health(self.repo_root, entry)
            data = entry.normalized().model_dump()
            data["health_status"] = health.health_status
            data["health_message"] = health.health_message
            data["matched_files_count"] = health.matched_files_count
            data["stale_references"] = health.stale_references
            data["effective_for_ai"] = (
                entry.status == "APPROVED_POLICY"
                and health.health_status != "INVALID_SCOPE"
            )
            items.append(data)
        return items

    def render_truth_hierarchy_block(self, touched_paths: list[str] | None = None) -> str:
        """Format Spec v2.0 Section 3.4 Truth Hierarchy + active validated policies."""
        policies = self.active_policies(touched_paths)
        lines = [
            "## Truth Hierarchy (Mandatory Conflict Resolution Order)",
            "1. [100% Authority] Current mainline repository code & passing test suite.",
            "2. [95% Authority] Active & path-validated repository governance policies (listed below).",
            "3. [85% Authority] Recent merged PR descriptions & review conclusions (<90 days).",
            "4. [80% Authority] Confirmed instructions in current `TaskContract`.",
            "5. [Reference Only] Legacy Wiki/design docs — if conflicting with code, report as 'Suspected Stale Doc'.",
            "",
            "## Active Repository Governance Ledger (`APPROVED_POLICY`)",
        ]
        if not policies:
            lines.append("- (No active repository-specific policies matched.)")
        else:
            for p in policies:
                title_part = f" {p.title} —" if p.title else ""
                owner_part = f", owner: {p.owner}" if p.owner else ""
                reason_part = f" | Reason: {p.reason}" if p.reason else ""
                lines.append(
                    f"- [{p.id}] (category: {p.category}, scope: `{p.scope}`{owner_part}):"
                    f"{title_part} {p.rule}{reason_part}"
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
        title: str = "",
        category: PolicyCategory | None = None,
        owner: str = "jamesyang",
        status: Literal["APPROVED_POLICY", "ONE_OFF_WORKAROUND"] = "APPROVED_POLICY",
        ledger_rel_path: str = ".ai/DECISIONS.yaml",
    ) -> DecisionEntry:
        """Append a rule to a standalone YAML ledger file (kept for file-mode unit test compatibility)."""
        ledger_file = repo_root / ledger_rel_path
        ledger_file.parent.mkdir(parents=True, exist_ok=True)
        existing_raw: dict[str, list[dict[str, Any]]] = {"decisions": []}
        if ledger_file.is_file():
            loaded = yaml.safe_load(ledger_file.read_text(encoding="utf-8")) or {}
            if isinstance(loaded, dict) and isinstance(loaded.get("decisions"), list):
                existing_raw["decisions"] = list(loaded["decisions"])
            elif isinstance(loaded, list):
                existing_raw["decisions"] = list(loaded)

        next_num = len(existing_raw["decisions"]) + 1
        prefix = repo_root.name[:6].upper().replace("-", "")
        resolved_cat = category or infer_policy_category(scope, rule)
        entry = DecisionEntry(
            id=f"{prefix}-DEC-{next_num:03d}",
            title=title.strip() or build_fallback_title(rule, scope),
            category=resolved_cat,
            scope=scope.strip() or "*",
            rule=rule.strip(),
            reason=reason.strip(),
            status=status,
            source_type="CONSOLE_MANUAL",
            owner=owner,
        ).normalized()
        existing_raw["decisions"].append(entry.model_dump())
        header = "# Team & Architecture Decision Ledger (Managed with Digital Employee)\n"
        ledger_file.write_text(
            header + yaml.safe_dump(existing_raw, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        return entry
