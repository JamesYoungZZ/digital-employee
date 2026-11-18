"""Smart Semantic Repository Router — infers target repository without requiring `[repo]` prefix."""

from __future__ import annotations

import re
from pathlib import Path

from digital_employee.models.contract import RepositoryProfile

# Built-in high-precision domain cues for common business verticals + general repo keyword matching
_VERTICAL_KEYWORDS: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
    (
        ("live", "stream", "copilot", "broadcast", "danmaku"),
        (
            "直播",
            "弹幕",
            "助播",
            "场控",
            "主播",
            "公屏",
            "连麦",
            "话术",
            "免单",
            "补发",
            "改价",
            "违禁词",
            "极限词",
            "微信号",
            "评论聚合",
            "reply_safety",
            "live_context",
            "coalescer",
        ),
    ),
    (
        ("rag", "search", "retrieval", "mcp", "knowledge", "kb"),
        (
            "检索",
            "知识库",
            "分词",
            "向量",
            "稀疏",
            "重排",
            "召回",
            "切片",
            "类目筛选",
            "连字符",
            "sparse",
            "dense",
            "bm25",
            "rerank",
            "embedding",
            "chunk",
            "jieba",
            "commerce_documents",
        ),
    ),
]

_README_SUMMARY_CACHE: dict[str, str] = {}


def _load_repo_fingerprint_text(profile: RepositoryProfile) -> str:
    cached = _README_SUMMARY_CACHE.get(profile.local_path)
    if cached is not None:
        return cached

    parts = [profile.repo_id.lower(), profile.github_repo.lower()]
    root = Path(profile.local_path)
    if root.is_dir():
        readme = root / "README.md"
        if readme.is_file():
            try:
                parts.append(readme.read_text(encoding="utf-8", errors="ignore")[:2000].lower())
            except OSError:
                pass
        try:
            entries = [p.name.lower() for p in list(root.iterdir())[:40]]
            parts.append(" ".join(entries))
        except OSError:
            pass

    joined = " ".join(parts)
    _README_SUMMARY_CACHE[profile.local_path] = joined
    return joined


def infer_target_repository(
    text: str,
    profiles: dict[str, RepositoryProfile],
    *,
    labels: list[str] | None = None,
) -> str:
    """Infer the best-matching registered `repo_id` from natural-language text & Jira labels."""
    if not profiles:
        raise ValueError("No target repositories configured.")
    if len(profiles) == 1:
        return next(iter(profiles.keys()))

    haystack = (text or "").lower()
    label_set = {lbl.lower().strip() for lbl in (labels or []) if lbl.strip()}

    # 1. Exact repo_id or github_repo name mention in labels or text
    for repo_id, prof in profiles.items():
        rid_low = repo_id.lower()
        gh_name = prof.github_repo.split("/")[-1].lower()
        if rid_low in label_set or gh_name in label_set:
            return repo_id
        if rid_low in haystack or gh_name in haystack:
            return repo_id

    # 2. Score each repository using vertical domain cues + README/identifier overlap
    scores: dict[str, int] = {rid: 0 for rid in profiles}
    tokens = [
        tok
        for tok in re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{2,}|[\u4e00-\u9fff]{2,4}", haystack)
        if len(tok) >= 2
    ]

    for repo_id, prof in profiles.items():
        fp = _load_repo_fingerprint_text(prof)
        id_blob = f"{repo_id.lower()} {prof.github_repo.lower()}"

        # Vertical keyword boost
        for repo_hints, domain_words in _VERTICAL_KEYWORDS:
            if any(rh in id_blob for rh in repo_hints):
                for dw in domain_words:
                    if dw in haystack:
                        scores[repo_id] += 5

        # Token overlap with repository README & top-level entries
        for tok in set(tokens):
            if tok in fp:
                scores[repo_id] += 2

    best_repo = max(scores, key=lambda k: scores[k])
    if scores[best_repo] > 0:
        return best_repo

    return next(iter(profiles.keys()))
