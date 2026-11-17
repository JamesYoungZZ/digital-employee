"""Jira Cloud (v3 ADF) & Jira Data Center (v2) Integration — Authoritative Work-Order Client."""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from digital_employee.models.contract import JiraTicketDraft

# Matches `https://.../browse/PROJ-123` or standalone `PROJ-123`
JIRA_BROWSE_URL_RE = re.compile(r"https?://[^\s/]+/browse/([A-Z][A-Z0-9_]{1,9}-\d+)\b")
JIRA_KEY_RE = re.compile(r"(?<![A-Za-z0-9_-])([A-Z][A-Z0-9_]{1,9}-\d+)\b")
NON_JIRA_PREFIXES = {
    "UTF",
    "SHA",
    "CVE",
    "DEC",
    "GPT",
    "PEP",
    "RFC",
    "ISO",
    "HTTP",
    "PR",
}


def extract_jira_issue_key(raw_text: str) -> str | None:
    """Extract an existing Jira issue key (e.g. `KAN-12` or `RAG-42`) from text or URL."""
    text = (raw_text or "").strip()
    if not text:
        return None
    url_match = JIRA_BROWSE_URL_RE.search(text)
    if url_match:
        return url_match.group(1).upper()

    for m in JIRA_KEY_RE.finditer(text):
        candidate = m.group(1).upper()
        prefix = candidate.split("-", 1)[0]
        if prefix in NON_JIRA_PREFIXES:
            continue
        # Exclude compound ledger IDs like `RAG-DEC-001`
        start = m.start(1)
        if start >= 4 and text[start - 4 : start].endswith("-"):
            continue
        return candidate
    return None


def adf_to_markdown(node: Any) -> str:
    """Convert Atlassian Document Format (ADF) JSON tree or plain string into clean Markdown."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node.strip()
    if not isinstance(node, dict):
        return str(node)

    ntype = node.get("type", "")
    if ntype == "text":
        return str(node.get("text") or "")
    if ntype == "hardBreak":
        return "\n"

    children = node.get("content") or []
    rendered_children = [adf_to_markdown(c) for c in children]

    if ntype == "doc":
        return "\n\n".join(x for x in rendered_children if x.strip()).strip()
    if ntype in ("paragraph", "blockquote"):
        return "".join(rendered_children).strip()
    if ntype == "heading":
        lvl = int((node.get("attrs") or {}).get("level") or 3)
        return f"{'#' * min(6, max(1, lvl))} {''.join(rendered_children).strip()}"
    if ntype == "codeBlock":
        return f"```\n{''.join(rendered_children)}\n```"
    if ntype == "bulletList":
        return "\n".join(f"- {c.strip()}" for c in rendered_children if c.strip())
    if ntype == "orderedList":
        return "\n".join(f"{idx}. {c.strip()}" for idx, c in enumerate(rendered_children, 1) if c.strip())
    if ntype == "listItem":
        return " ".join(c.strip() for c in rendered_children if c.strip())

    return " ".join(x for x in rendered_children if x).strip()


def markdown_to_adf(text: str) -> dict[str, Any]:
    """Convert plain/markdown paragraphs & bullet lines into valid Jira Cloud v3 ADF JSON."""
    blocks: list[dict[str, Any]] = []
    bullet_buf: list[str] = []

    def flush_bullets() -> None:
        nonlocal bullet_buf
        if not bullet_buf:
            return
        blocks.append(
            {
                "type": "bulletList",
                "content": [
                    {
                        "type": "listItem",
                        "content": [
                            {"type": "paragraph", "content": [{"type": "text", "text": b}]}
                        ],
                    }
                    for b in bullet_buf
                ],
            }
        )
        bullet_buf = []

    for raw_ln in (text or "").splitlines():
        ln = raw_ln.strip()
        if not ln:
            flush_bullets()
            continue
        if ln.startswith(("- ", "* ", "• ")):
            bullet_buf.append(ln[2:].strip())
            continue
        flush_bullets()
        if ln.startswith("### "):
            blocks.append(
                {
                    "type": "heading",
                    "attrs": {"level": 3},
                    "content": [{"type": "text", "text": ln[4:].strip()}],
                }
            )
        elif ln.startswith("## "):
            blocks.append(
                {
                    "type": "heading",
                    "attrs": {"level": 2},
                    "content": [{"type": "text", "text": ln[3:].strip()}],
                }
            )
        else:
            blocks.append(
                {"type": "paragraph", "content": [{"type": "text", "text": ln}]}
            )

    flush_bullets()
    if not blocks:
        blocks.append({"type": "paragraph", "content": [{"type": "text", "text": text or "-"}]})
    return {"type": "doc", "version": 1, "content": blocks}


@dataclass
class JiraIssueData:
    """Normalized authoritative Jira ticket fetched from Jira REST API."""

    key: str
    url: str
    project_key: str
    issue_type: str
    summary: str
    description: str
    status: str
    priority: str = "Medium"
    labels: list[str] = field(default_factory=list)
    comments: list[str] = field(default_factory=list)

    def render_authoritative_spec(self) -> str:
        """Render the Jira ticket as the binding 100%-authority work-order specification."""
        lines = [
            f"Jira Issue Key: {self.key}",
            f"Jira URL: {self.url}",
            f"Type: {self.issue_type} | Priority: {self.priority} | Status: {self.status}",
            f"Labels: {', '.join(self.labels) if self.labels else '(none)'}",
            f"Summary: {self.summary}",
            "",
            "### Authoritative Description & Acceptance Criteria",
            self.description or "(No description body provided on Jira issue)",
        ]
        if self.comments:
            lines.append("\n### Latest Jira Comments / Clarifications")
            for cmt in self.comments[-5:]:
                lines.append(f"- {cmt}")
        return "\n".join(lines)


class JiraIntegration:
    """Client supporting both Jira Cloud (`/rest/api/3`) and Jira Server/DC (`/rest/api/2`)."""

    def __init__(
        self,
        *,
        base_url: str = "",
        email: str = "",
        api_token: str = "",
        default_project: str = "",
    ) -> None:
        self.base_url = base_url.strip().rstrip("/")
        self.email = email.strip()
        self.api_token = api_token.strip()
        self.default_project = default_project.strip().upper()

    @property
    def is_configured(self) -> bool:
        return bool(self.base_url and self.api_token)

    @property
    def is_cloud_v3(self) -> bool:
        return ".atlassian.net" in self.base_url.lower() or bool(self.email)

    @property
    def api_prefix(self) -> str:
        return f"{self.base_url}/rest/api/{'3' if self.is_cloud_v3 else '2'}"

    def browse_url(self, issue_key: str) -> str:
        return f"{self.base_url}/browse/{issue_key}"

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.email:
            raw = f"{self.email}:{self.api_token}".encode()
            b64 = base64.b64encode(raw).decode("ascii")
            headers["Authorization"] = f"Basic {b64}"
        elif self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        return headers

    def verify_connection(self) -> dict[str, Any]:
        """Verify Jira credentials and return user + accessible projects."""
        if not self.is_configured:
            raise ValueError("Missing Jira Base URL or API Token")
        with httpx.Client(timeout=15.0, headers=self._headers()) as client:
            me_resp = client.get(f"{self.api_prefix}/myself")
            if me_resp.status_code >= 400 and self.api_prefix.endswith("/3"):
                # Fallback probe on /rest/api/2 for self-hosted instances using email+password
                me_resp = client.get(f"{self.base_url}/rest/api/2/myself")
            me_resp.raise_for_status()
            me = me_resp.json()
            projects = self.list_projects(client=client)
            return {
                "display_name": str(me.get("displayName") or me.get("name") or "Jira User"),
                "account_id": str(me.get("accountId") or me.get("key") or ""),
                "projects": projects,
            }

    def list_projects(self, *, client: httpx.Client | None = None) -> list[dict[str, str]]:
        if not self.is_configured:
            return []
        own_client = client is None
        http = client or httpx.Client(timeout=15.0, headers=self._headers())
        try:
            resp = http.get(f"{self.api_prefix}/project")
            if resp.status_code >= 400:
                return []
            raw = resp.json()
            items = raw if isinstance(raw, list) else (raw.get("values") or [])
            return [
                {"key": str(p.get("key", "")), "name": str(p.get("name", ""))}
                for p in items[:30]
                if isinstance(p, dict) and p.get("key")
            ]
        finally:
            if own_client:
                http.close()

    def get_issue(self, issue_key: str) -> JiraIssueData:
        """Fetch full authoritative Jira issue by key (`PROJ-123`)."""
        if not self.is_configured:
            raise RuntimeError(
                f"检测到 Jira 工单号 `{issue_key}`，但尚未配置 Jira 连接！请先在控制台「Jira 工单集成」填入 Jira URL 与 API Token。"
            )
        clean_key = issue_key.strip().upper()
        with httpx.Client(timeout=20.0, headers=self._headers()) as client:
            resp = client.get(f"{self.api_prefix}/issue/{clean_key}")
            if resp.status_code == 404:
                raise ValueError(f"Jira 工单 `{clean_key}` 不存在或当前 Token 无权访问。")
            resp.raise_for_status()
            data = resp.json()

        fields = data.get("fields") or {}
        desc_md = adf_to_markdown(fields.get("description"))
        proj = str((fields.get("project") or {}).get("key") or clean_key.split("-", 1)[0])
        itype = str((fields.get("issuetype") or {}).get("name") or "Task")
        status_name = str((fields.get("status") or {}).get("name") or "To Do")
        prio = str((fields.get("priority") or {}).get("name") or "Medium")
        labels = [str(x) for x in (fields.get("labels") or []) if str(x).strip()]

        comments_raw = ((fields.get("comment") or {}).get("comments") or [])[-5:]
        comments = [
            f"{(c.get('author') or {}).get('displayName', 'user')}: {adf_to_markdown(c.get('body'))}"
            for c in comments_raw
            if isinstance(c, dict)
        ]

        return JiraIssueData(
            key=str(data.get("key") or clean_key),
            url=self.browse_url(str(data.get("key") or clean_key)),
            project_key=proj,
            issue_type=itype,
            summary=str(fields.get("summary") or clean_key),
            description=desc_md,
            status=status_name,
            priority=prio,
            labels=labels,
            comments=comments,
        )

    def _resolve_valid_issue_type(
        self, client: httpx.Client, project_key: str, preferred_name: str
    ) -> dict[str, str]:
        """Resolve valid issue type for `project_key` (handles localized CN/EN Jira projects)."""
        resp = client.get(f"{self.api_prefix}/project/{project_key}")
        if resp.status_code >= 400:
            return {"name": preferred_name or "Task"}
        itypes = [
            t
            for t in (resp.json().get("issueTypes") or [])
            if isinstance(t, dict) and not t.get("subtask", False)
        ]
        if not itypes:
            return {"name": preferred_name or "Task"}

        pref_lower = (preferred_name or "Task").lower()
        synonyms = {
            "bug": {"bug", "缺陷", "故障", "问题"},
            "task": {"task", "任务", "研发任务", "story", "故事"},
            "story": {"story", "故事", "需求", "task", "任务"},
        }.get(pref_lower, {pref_lower})

        for t in itypes:
            if str(t.get("name", "")).lower() in synonyms:
                return {"id": str(t["id"])} if t.get("id") else {"name": str(t["name"])}
        first = itypes[0]
        return {"id": str(first["id"])} if first.get("id") else {"name": str(first["name"])}

    def create_issue(self, draft: JiraTicketDraft) -> JiraIssueData:
        """Create a real Jira ticket after human approval of `JiraTicketDraft`."""
        if not self.is_configured:
            raise RuntimeError(
                "尚未配置 Jira 连接凭证，无法在真实 Jira 创建工单！请先前往控制台「Jira 工单集成」配置。"
            )
        project_key = (draft.project_key or self.default_project).strip().upper()
        if not project_key:
            raise ValueError("Missing Jira `project_key` (e.g. KAN or RAG).")

        full_desc_lines = [draft.description.strip()]
        if draft.acceptance_criteria:
            full_desc_lines.append("\n### 验收标准 (Acceptance Criteria)")
            for ac in draft.acceptance_criteria:
                full_desc_lines.append(f"- {ac}")
        full_desc = "\n".join(full_desc_lines).strip()

        with httpx.Client(timeout=20.0, headers=self._headers()) as client:
            itype_spec = self._resolve_valid_issue_type(client, project_key, draft.issue_type)
            desc_payload: Any = markdown_to_adf(full_desc) if self.is_cloud_v3 else full_desc

            fields: dict[str, Any] = {
                "project": {"key": project_key},
                "summary": draft.summary.strip()[:240],
                "issuetype": itype_spec,
                "description": desc_payload,
            }
            if draft.labels:
                fields["labels"] = [lbl.replace(" ", "-") for lbl in draft.labels[:6] if lbl.strip()]
            if draft.priority:
                fields["priority"] = {"name": draft.priority}

            resp = client.post(f"{self.api_prefix}/issue", json={"fields": fields})
            # Auto-strip optional screen-restricted fields (`priority` / `labels`) on 400
            if resp.status_code == 400 and ("priority" in fields or "labels" in fields):
                err_body = resp.json() if resp.content else {}
                bad_fields = (err_body.get("errors") or {}) if isinstance(err_body, dict) else {}
                for optional_k in ("priority", "labels"):
                    if optional_k in bad_fields or not bad_fields:
                        fields.pop(optional_k, None)
                resp = client.post(f"{self.api_prefix}/issue", json={"fields": fields})

            if resp.status_code >= 400:
                raise RuntimeError(f"Jira create issue HTTP {resp.status_code}: {resp.text[:350]}")
            created_key = str(resp.json()["key"])

        return self.get_issue(created_key)

    def add_comment(self, issue_key: str, markdown_body: str) -> bool:
        """Post a progress/PR/merge update comment onto the Jira ticket."""
        if not self.is_configured or not issue_key:
            return False
        payload_body: Any = markdown_to_adf(markdown_body) if self.is_cloud_v3 else markdown_body
        with httpx.Client(timeout=15.0, headers=self._headers()) as client:
            resp = client.post(
                f"{self.api_prefix}/issue/{issue_key.strip().upper()}/comment",
                json={"body": payload_body},
            )
            return resp.status_code < 400

    def transition_issue_by_keywords(self, issue_key: str, target_keywords: set[str]) -> str | None:
        """Best-effort transition Jira ticket status (e.g. In Progress / In Review / Done)."""
        if not self.is_configured or not issue_key:
            return None
        clean_key = issue_key.strip().upper()
        with httpx.Client(timeout=15.0, headers=self._headers()) as client:
            resp = client.get(f"{self.api_prefix}/issue/{clean_key}/transitions")
            if resp.status_code >= 400:
                return None
            transitions = resp.json().get("transitions") or []
            lowered_kw = {k.lower() for k in target_keywords}
            chosen = None
            for tr in transitions:
                t_name = str(tr.get("name") or "").lower()
                to_name = str((tr.get("to") or {}).get("name") or "").lower()
                cat_key = str(((tr.get("to") or {}).get("statusCategory") or {}).get("key") or "").lower()
                if t_name in lowered_kw or to_name in lowered_kw or cat_key in lowered_kw:
                    chosen = tr
                    break
            if not chosen:
                return None
            post_resp = client.post(
                f"{self.api_prefix}/issue/{clean_key}/transitions",
                json={"transition": {"id": str(chosen["id"])}},
            )
            if post_resp.status_code < 400:
                return str((chosen.get("to") or {}).get("name") or chosen.get("name") or "")
        return None

    def search_issues(self, jql: str, max_results: int = 10) -> list[str]:
        """Search Jira issue keys matching JQL (compatible with Cloud `/search/jql` & v2 `/search`)."""
        if not self.is_configured or not jql.strip():
            return []
        with httpx.Client(timeout=20.0, headers=self._headers()) as client:
            if self.is_cloud_v3:
                resp = client.get(
                    f"{self.api_prefix}/search/jql",
                    params={"jql": jql, "maxResults": max_results, "fields": "key"},
                )
                if resp.status_code == 404:
                    resp = client.get(
                        f"{self.api_prefix}/search",
                        params={"jql": jql, "maxResults": max_results, "fields": "key"},
                    )
            else:
                resp = client.get(
                    f"{self.api_prefix}/search",
                    params={"jql": jql, "maxResults": max_results, "fields": "key"},
                )
            if resp.status_code >= 400:
                return []
            issues = resp.json().get("issues") or []
            return [str(i["key"]) for i in issues if isinstance(i, dict) and i.get("key")]
