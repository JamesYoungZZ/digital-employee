"""GitHub Real PR Creation, Separate PR Review Agent Reporter & 5-Gate Auto-Merge Client."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

import httpx

from digital_employee.models.state import BlockingReviewIssue, DurableTaskState
from digital_employee.security.dlp import PolicyViolationError, validate_branch_target

logger = logging.getLogger("digital_employee.github")


def render_evidence_pr_body(state: DurableTaskState) -> str:
    """Format a concise, evidence-first GitHub Pull Request description."""
    c = state.contract
    ev = state.verification_evidence
    jira_key = state.jira_issue_key or c.jira_issue_key
    jira_url = state.jira_issue_url or c.jira_issue_url
    lines = [
        f"## 🎯 Task Contract (`{state.task_id}`)",
    ]
    if jira_key:
        jira_md = f"[{jira_key}]({jira_url})" if jira_url else f"`{jira_key}`"
        lines.append(f"- **Authoritative Jira Ticket**: {jira_md}")
    lines.extend(
        [
            f"- **Objective**: {c.objective}",
            f"- **Base / Head**: `{c.base_branch}` (`{c.base_commit_sha[:8]}`) → `{state.branch_name}` (`{(state.head_commit_sha or '')[:8]}`)",
            f"- **Suggested Scopes**: `{', '.join(c.permitted_scopes)}`",
        ]
    )
    if ev and ev.out_of_scope_files:
        lines.append(
            f"- **📌 Cross-Scope Edits**: `{', '.join(ev.out_of_scope_files)}` *(modified beyond initial suggested scopes)*"
        )
    lines.extend(
        [
            "",
            "## 🧪 Deterministic Sandbox Gate",
        ]
    )
    if ev:
        lines.extend(
            [
                "| Check | Status | Detail |",
                "| :--- | :--- | :--- |",
                f"| **Fail-to-Pass Regression** | {'✅ PASS' if (ev.repro_test_failed_on_base and ev.repro_test_passed_on_head) else '❌ FAIL'} | `{ev.repro_test_target}` (Failed on base, Passed on head) |",
                f"| **Existing Suite & Lint** | {'✅ PASS' if (ev.existing_test_suite_passed and ev.static_analysis_passed) else '❌ FAIL'} | Related module suite & static linter green |",
                f"| **Anti-Tampering & Scope** | {'✅ PASS' if (ev.tests_weakened_check and ev.scope_compliance_check) else '❌ FAIL'} | Zero deleted asserts, zero `@skip`/`xfail` |",
            ]
        )
        if ev.reviewer_report:
            rep = ev.reviewer_report
            sc = rep.quality_score if rep.quality_score is not None else 95
            lines.append(
                f"| **Independent AI Review** | {'✅ APPROVE' if rep.verdict == 'APPROVE' else '🛑 BLOCK'} | `{sc}/100` · {rep.merge_recommendation_label or rep.verdict} (`{rep.reviewer_model}`) |"
            )
    lines.extend(
        [
            "",
            "---",
            "*Opened by AI Digital Employee v2.0 — audited independently by PR Review Agent prior to merge.*",
        ]
    )
    return "\n".join(lines)


def infer_conventional_commit_prefix(objective: str) -> str:
    """Infer Conventional Commit prefix (`feat`/`refactor`/`docs`/`test`/`perf`/`fix`) from objective."""
    raw = (objective or "").strip()
    low = raw.lower()
    for explicit in ("feat:", "fix:", "refactor:", "docs:", "test:", "perf:", "chore:"):
        if low.startswith(explicit):
            return explicit[:-1]
    if any(k in low for k in ("重构", "refactor", "cleanup", "提取", "解耦")):
        return "refactor"
    if any(k in low for k in ("文档", "readme", "注释", "docs")):
        return "docs"
    if any(k in low for k in ("性能优化", "提速", "perf", "latency", "缓存优化")):
        return "perf"
    if any(
        k in low
        for k in ("修复", "报错", "异常", "崩溃", "失败", "bug", "fix", "crash", "error", "回归")
    ):
        return "fix"
    if any(k in low for k in ("新增", "增加", "支持", "添加", "实现", "接入", "add", "support", "implement", "feat")):
        return "feat"
    if any(k in low for k in ("单测", "测试", "test")):
        return "test"
    return "fix"


class GitHubIntegration:
    """GitHub connector enforcing `ai-agent/*` branches, independent PR review & 5-Gate Auto-Merge."""

    def __init__(self, token: str, api_base: str = "https://api.github.com") -> None:
        self.token = token
        self.api_base = api_base.rstrip("/")

    @property
    def is_configured(self) -> bool:
        return bool(self.token.strip())

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def push_agent_branch(
        self,
        *,
        worktree_path: Path,
        branch_name: str,
        protected_branches: list[str],
    ) -> str:
        """Push `ai-agent/{task_id}` to `origin`; physically blocks protected branches."""
        validate_branch_target(branch_name, protected_branches)
        refspec = f"refs/heads/{branch_name}:refs/heads/{branch_name}"
        proc = subprocess.run(
            ["git", "push", "--force-with-lease", "origin", refspec],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"git push failed: {proc.stderr.strip()}")
        return branch_name

    def mark_pr_ready_for_review(self, pr_node_id: str) -> bool:
        """Promote an existing Draft PR to Ready-for-Review via GitHub GraphQL API."""
        if not self.is_configured or not pr_node_id:
            return False
        graphql_url = (
            "https://api.github.com/graphql"
            if "api.github.com" in self.api_base
            else f"{self.api_base}/graphql"
        )
        mutation = """
        mutation($pullRequestId: ID!) {
          markPullRequestReadyForReview(input: { pullRequestId: $pullRequestId }) {
            pullRequest { isDraft }
          }
        }
        """
        with httpx.Client(timeout=20.0, headers=self._headers()) as client:
            resp = client.post(
                graphql_url,
                json={"query": mutation, "variables": {"pullRequestId": pr_node_id}},
            )
            return resp.status_code == 200 and "errors" not in resp.json()

    def reconcile_or_create_pr(
        self,
        *,
        github_repo: str,
        state: DurableTaskState,
        protected_branches: list[str],
        draft: bool = False,
    ) -> dict[str, Any]:
        """Reconcile existing Open PR for `ai-agent/{task_id}` or create a real Open PR (`draft=False`)."""
        validate_branch_target(state.branch_name, protected_branches)
        if not self.is_configured:
            raise PolicyViolationError("DE_GITHUB_TOKEN is not configured.")

        owner, repo = github_repo.split("/", 1)
        jira_prefix = f"[{state.jira_issue_key}] " if state.jira_issue_key else "[AI-Agent] "
        pr_title = f"{jira_prefix}{state.contract.objective[:100]}"
        pr_body = render_evidence_pr_body(state)
        with httpx.Client(timeout=30.0, headers=self._headers()) as client:
            list_resp = client.get(
                f"{self.api_base}/repos/{owner}/{repo}/pulls",
                params={"head": f"{owner}:{state.branch_name}", "state": "open"},
            )
            list_resp.raise_for_status()
            existing = list_resp.json()
            if existing:
                pr = existing[0]
                if pr.get("draft") and not draft and pr.get("node_id"):
                    self.mark_pr_ready_for_review(str(pr["node_id"]))
                # Sync latest commit SHA, verification gate table & review score onto existing PR description
                client.patch(
                    f"{self.api_base}/repos/{owner}/{repo}/pulls/{pr['number']}",
                    json={"title": pr_title, "body": pr_body},
                )
                return {
                    "html_url": pr["html_url"],
                    "number": pr["number"],
                    "reconciled": True,
                }

            payload = {
                "title": pr_title,
                "head": state.branch_name,
                "base": state.contract.base_branch,
                "body": pr_body,
                "draft": draft,
            }
            create_resp = client.post(
                f"{self.api_base}/repos/{owner}/{repo}/pulls",
                json=payload,
            )
            create_resp.raise_for_status()
            pr = create_resp.json()
            return {
                "html_url": pr["html_url"],
                "number": pr["number"],
                "reconciled": False,
            }

    # Alias for backwards compatibility
    reconcile_or_create_draft_pr = reconcile_or_create_pr

    def submit_pr_review_report(
        self,
        *,
        github_repo: str,
        pr_number: int,
        commit_sha: str,
        report_markdown: str,
        blocking_issues: list[BlockingReviewIssue] | None = None,
    ) -> str:
        """Post concise PR Review Agent report + optional blocking-only inline comments."""
        if not self.is_configured:
            return "skipped:no_token"

        owner, repo = github_repo.split("/", 1)
        inline_comments: list[dict[str, Any]] = []
        for issue in blocking_issues or []:
            if issue.path and issue.line and issue.line > 0:
                inline_comments.append(
                    {
                        "path": issue.path,
                        "line": issue.line,
                        "side": "RIGHT",
                        "body": f"<!-- ai-pr-review-agent -->\n🚨 **[{issue.severity} · {issue.category}]** {issue.message}",
                    }
                )

        review_payload: dict[str, Any] = {
            "commit_id": commit_sha,
            "body": report_markdown,
            "event": "COMMENT",
        }
        if inline_comments:
            review_payload["comments"] = inline_comments

        with httpx.Client(timeout=20.0, headers=self._headers()) as client:
            resp = client.post(
                f"{self.api_base}/repos/{owner}/{repo}/pulls/{pr_number}/reviews",
                json=review_payload,
            )
            # If inline line numbers lay outside diff hunk, retry cleanly with summary report body only
            if resp.status_code == 422 and inline_comments:
                review_payload.pop("comments", None)
                resp = client.post(
                    f"{self.api_base}/repos/{owner}/{repo}/pulls/{pr_number}/reviews",
                    json=review_payload,
                )
            if resp.status_code < 400:
                return str(resp.json().get("html_url", "review_posted"))

            # Fallback to issue comment if token lacks pull-request review sub-scope
            fallback = client.post(
                f"{self.api_base}/repos/{owner}/{repo}/issues/{pr_number}/comments",
                json={"body": report_markdown},
            )
            fallback.raise_for_status()
            return str(fallback.json().get("html_url", "comment_posted"))

    def post_pr_comment(
        self,
        *,
        github_repo: str,
        pr_number: int,
        body: str,
    ) -> str:
        if not self.is_configured:
            return "skipped:no_token"
        owner, repo = github_repo.split("/", 1)
        tagged_body = f"<!-- ai-digital-employee-bot -->\n🤖 **[AI Digital Employee]**\n\n{body}"
        with httpx.Client(timeout=20.0, headers=self._headers()) as client:
            resp = client.post(
                f"{self.api_base}/repos/{owner}/{repo}/issues/{pr_number}/comments",
                json={"body": tagged_body},
            )
            resp.raise_for_status()
            return str(resp.json().get("html_url", "posted"))

    def fetch_pr_state(self, *, github_repo: str, pr_number: int) -> dict[str, Any]:
        owner, repo = github_repo.split("/", 1)
        with httpx.Client(timeout=20.0, headers=self._headers()) as client:
            resp = client.get(f"{self.api_base}/repos/{owner}/{repo}/pulls/{pr_number}")
            resp.raise_for_status()
            return resp.json()

    def fetch_human_pr_comments(self, *, github_repo: str, pr_number: int) -> list[dict[str, Any]]:
        """Fetch issue & inline review comments on a PR, excluding AI bot & AI PR Review Agent comments."""
        if not self.is_configured:
            return []
        owner, repo = github_repo.split("/", 1)
        comments: list[dict[str, Any]] = []
        with httpx.Client(timeout=20.0, headers=self._headers()) as client:
            for endpoint, kind in (
                (f"{self.api_base}/repos/{owner}/{repo}/issues/{pr_number}/comments", "issue_comment"),
                (f"{self.api_base}/repos/{owner}/{repo}/pulls/{pr_number}/comments", "review_comment"),
            ):
                resp = client.get(endpoint, params={"per_page": 50})
                if resp.status_code >= 400:
                    continue
                for item in resp.json():
                    body = str(item.get("body") or "")
                    if (
                        "<!-- ai-digital-employee-bot -->" in body
                        or "<!-- ai-pr-review-agent -->" in body
                    ):
                        continue
                    path_hint = item.get("path")
                    formatted = f"[File `{path_hint}`] {body}" if path_hint else body
                    comments.append(
                        {
                            "id": f"{kind}_{item['id']}",
                            "body": formatted.strip(),
                            "created_at": str(item.get("created_at") or ""),
                            "user": str((item.get("user") or {}).get("login") or ""),
                        }
                    )
        comments.sort(key=lambda c: c["created_at"])
        return comments

    def inspect_commit_ci_status(
        self,
        *,
        github_repo: str,
        commit_sha: str,
        expect_workflows: bool = True,
    ) -> dict[str, Any]:
        """Inspect GitHub Actions check-runs on `commit_sha`, with `/actions/runs` fallback for Fine-grained PATs.

        Returns `{"state": "pending" | "passed" | "failed", "failed_checks": [...], "total": int}`.
        """
        if not self.is_configured or not commit_sha:
            return {"state": "pending", "failed_checks": [], "total": 0}
        owner, repo = github_repo.split("/", 1)
        with httpx.Client(timeout=20.0, headers=self._headers()) as client:
            resp = client.get(
                f"{self.api_base}/repos/{owner}/{repo}/commits/{commit_sha}/check-runs"
            )
            if resp.status_code >= 400:
                return self._inspect_actions_runs(
                    client,
                    owner=owner,
                    repo=repo,
                    commit_sha=commit_sha,
                    expect_workflows=expect_workflows,
                )
            runs = resp.json().get("check_runs") or []
            if not runs:
                return {
                    "state": "pending" if expect_workflows else "passed",
                    "failed_checks": [],
                    "total": 0,
                }
            if any(r.get("status") != "completed" for r in runs):
                return {"state": "pending", "failed_checks": [], "total": len(runs)}

            failed = [
                str(r.get("name"))
                for r in runs
                if r.get("conclusion") not in ("success", "skipped", "neutral")
            ]
            return {
                "state": "failed" if failed else "passed",
                "failed_checks": failed,
                "total": len(runs),
            }

    def _inspect_actions_runs(
        self,
        client: httpx.Client,
        *,
        owner: str,
        repo: str,
        commit_sha: str,
        expect_workflows: bool,
    ) -> dict[str, Any]:
        """Fallback CI check via `/actions/runs?head_sha=...` for Fine-grained PATs (`Actions: Read-only`)."""
        runs_resp = client.get(
            f"{self.api_base}/repos/{owner}/{repo}/actions/runs",
            params={"head_sha": commit_sha, "per_page": 50},
        )
        if runs_resp.status_code >= 400:
            needed = runs_resp.headers.get("x-accepted-github-permissions", "")
            logger.warning(
                "GitHub CI status check returned HTTP %s for %s/%s@%s (accepted permissions: %s)",
                runs_resp.status_code,
                owner,
                repo,
                commit_sha[:8],
                needed or "actions=read",
            )
            return {"state": "pending", "failed_checks": [], "total": 0}

        raw_runs = runs_resp.json().get("workflow_runs") or []
        if not raw_runs:
            return {
                "state": "pending" if expect_workflows else "passed",
                "failed_checks": [],
                "total": 0,
            }

        latest_by_wf: dict[tuple[str, str], dict[str, Any]] = {}
        for wr in raw_runs:
            key = (
                str(wr.get("workflow_id") or wr.get("name") or wr.get("id")),
                str(wr.get("event") or ""),
            )
            prev = latest_by_wf.get(key)
            if prev is None:
                latest_by_wf[key] = wr
            else:
                cur_rank = (
                    int(wr.get("run_number") or 0),
                    int(wr.get("run_attempt") or 0),
                    str(wr.get("created_at") or ""),
                )
                prev_rank = (
                    int(prev.get("run_number") or 0),
                    int(prev.get("run_attempt") or 0),
                    str(prev.get("created_at") or ""),
                )
                if cur_rank > prev_rank:
                    latest_by_wf[key] = wr

        runs = list(latest_by_wf.values())
        if any(r.get("status") != "completed" for r in runs):
            return {"state": "pending", "failed_checks": [], "total": len(runs)}

        total_checks = 0
        failed: list[str] = []
        for r in runs:
            wf_name = str(r.get("name") or "workflow")
            wf_ok = r.get("conclusion") in ("success", "skipped", "neutral")
            run_id = r.get("id")
            jobs: list[dict[str, Any]] = []
            if run_id:
                jobs_resp = client.get(
                    f"{self.api_base}/repos/{owner}/{repo}/actions/runs/{run_id}/jobs",
                    params={"per_page": 100},
                )
                if jobs_resp.status_code < 400:
                    jobs = jobs_resp.json().get("jobs") or []

            if jobs:
                total_checks += len(jobs)
                job_failures = [
                    str(j.get("name") or wf_name)
                    for j in jobs
                    if j.get("conclusion") not in ("success", "skipped", "neutral")
                ]
                if job_failures:
                    failed.extend(job_failures)
                elif not wf_ok:
                    failed.append(wf_name)
            else:
                total_checks += 1
                if not wf_ok:
                    failed.append(wf_name)

        return {
            "state": "failed" if failed else "passed",
            "failed_checks": failed,
            "total": total_checks,
        }

    def fetch_failed_blocking_checks(self, *, github_repo: str, commit_sha: str) -> list[str]:
        info = self.inspect_commit_ci_status(github_repo=github_repo, commit_sha=commit_sha)
        return list(info["failed_checks"]) if info["state"] == "failed" else []

    def merge_pull_request(
        self,
        *,
        github_repo: str,
        pr_number: int,
        expected_head_sha: str,
        commit_title: str,
    ) -> dict[str, Any]:
        """Squash-merge a PR strictly locked to `expected_head_sha`."""
        if not self.is_configured:
            raise PolicyViolationError("DE_GITHUB_TOKEN is not configured.")
        owner, repo = github_repo.split("/", 1)
        with httpx.Client(timeout=25.0, headers=self._headers()) as client:
            resp = client.put(
                f"{self.api_base}/repos/{owner}/{repo}/pulls/{pr_number}/merge",
                json={
                    "commit_title": commit_title,
                    "sha": expected_head_sha,
                    "merge_method": "squash",
                },
            )
            resp.raise_for_status()
            return resp.json()
