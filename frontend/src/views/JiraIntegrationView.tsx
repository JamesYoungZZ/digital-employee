import React from "react";
import {
  ArrowUpRight,
  CheckCircle2,
  ClipboardCheck,
  KeyRound,
  Loader2,
  Play,
  Search,
  Sparkles,
  TicketCheck,
} from "lucide-react";
import { toast } from "sonner";
import type {
  JiraIntegrationStatus,
  JiraIssuePreview,
  RepositoryItem,
  TaskItem,
} from "../types/api";
import { apiFetch } from "../lib/utils";
import { Button, SurfaceCard, Switch } from "../components/ui/primitives";

interface Props {
  repos: RepositoryItem[];
  onDispatchedTask: (taskId: string) => Promise<void>;
  onConfigSaved: () => Promise<void>;
}

function normalizeJiraKeyInput(raw: string): string {
  const txt = raw.trim();
  const urlMatch = txt.match(/\/browse\/([A-Za-z][A-Za-z0-9_]{1,9}-\d+)\b/);
  if (urlMatch) return urlMatch[1].toUpperCase();
  const keyMatch = txt.match(/\b([A-Za-z][A-Za-z0-9_]{1,9}-\d+)\b/);
  return keyMatch ? keyMatch[1].toUpperCase() : txt.toUpperCase();
}

export function JiraIntegrationView({ repos, onDispatchedTask, onConfigSaved }: Props) {
  const [status, setStatus] = React.useState<JiraIntegrationStatus | null>(null);
  const [baseUrl, setBaseUrl] = React.useState("");
  const [email, setEmail] = React.useState("");
  const [apiToken, setApiToken] = React.useState("");
  const [defaultProject, setDefaultProject] = React.useState("");
  const [pollJql, setPollJql] = React.useState(
    'labels = "ai-delegate" AND statusCategory = "To Do" ORDER BY created DESC'
  );
  const [pollerEnabled, setPollerEnabled] = React.useState(true);
  const [saving, setSaving] = React.useState(false);

  const [issueInput, setIssueInput] = React.useState("");
  const [targetRepo, setTargetRepo] = React.useState("");
  const [preview, setPreview] = React.useState<JiraIssuePreview | null>(null);
  const [loadingPreview, setLoadingPreview] = React.useState(false);
  const [dispatching, setDispatching] = React.useState(false);

  React.useEffect(() => {
    if (!targetRepo && repos.length > 0) {
      setTargetRepo(repos[0].repo_id);
    }
  }, [repos, targetRepo]);

  const loadConfig = React.useCallback(async () => {
    try {
      const s = await apiFetch<JiraIntegrationStatus>("/api/integrations/jira");
      setStatus(s);
      setBaseUrl(s.base_url || "");
      setEmail(s.email || "");
      setDefaultProject(s.default_project || "");
      if (s.poll_jql) setPollJql(s.poll_jql);
      setPollerEnabled(s.poller_enabled);
    } catch (err: any) {
      toast.error(err.message);
    }
  }, []);

  React.useEffect(() => {
    loadConfig();
  }, [loadConfig]);

  const handleSaveConfig = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    try {
      const res = await apiFetch<{
        ok: boolean;
        configured: boolean;
        default_project: string;
        display_name: string;
      }>("/api/integrations/jira/config", {
        method: "POST",
        body: JSON.stringify({
          base_url: baseUrl.trim(),
          email: email.trim(),
          api_token: apiToken.trim(),
          default_project: defaultProject.trim().toUpperCase(),
          poll_jql: pollJql.trim(),
          poller_enabled: pollerEnabled,
        }),
      });
      setApiToken("");
      toast.success(
        res.display_name
          ? `✅ Jira 已连通（账号: ${res.display_name} · 默认项目: ${res.default_project || "未指定"}）`
          : "Jira 配置已保存"
      );
      await loadConfig();
      await onConfigSaved();
    } catch (err: any) {
      toast.error(err.message);
    } finally {
      setSaving(false);
    }
  };

  const handlePreviewIssue = async () => {
    const key = normalizeJiraKeyInput(issueInput);
    if (!key) return;
    setLoadingPreview(true);
    try {
      const p = await apiFetch<JiraIssuePreview>(
        `/api/integrations/jira/issue/${encodeURIComponent(key)}`
      );
      setPreview(p);
    } catch (err: any) {
      toast.error(err.message);
    } finally {
      setLoadingPreview(false);
    }
  };

  const handleDispatchExistingJira = async () => {
    const key = normalizeJiraKeyInput(issueInput);
    if (!key || !targetRepo) return;
    setDispatching(true);
    try {
      const created = await apiFetch<TaskItem>("/api/tasks", {
        method: "POST",
        body: JSON.stringify({
          raw_request: key,
          jira_issue_key: key,
          target_repo: targetRepo,
          publish_pr: true,
          accept_defaults: true,
          auto_execute: true,
        }),
      });
      toast.success(`🎫 已拉取权威工单 ${key}，沙箱直接开工 (${created.task_id})！`);
      setIssueInput("");
      setPreview(null);
      await onDispatchedTask(created.task_id);
    } catch (err: any) {
      toast.error(err.message);
    } finally {
      setDispatching(false);
    }
  };

  return (
    <div className="space-y-6">
      {/* Dual-Path Authoritative Work-Order Architecture Banner */}
      <div className="grid md:grid-cols-2 gap-4">
        <SurfaceCard className="p-5 border-blue-500/30 bg-gradient-to-br from-blue-500/6 via-white to-white dark:from-blue-500/10 dark:via-zinc-900 dark:to-zinc-900">
          <div className="flex items-center gap-2 text-xs font-semibold text-blue-600 dark:text-blue-400">
            <TicketCheck className="h-4 w-4" />
            路径一：已有 Jira 工单 → 直接按 Jira 开工（零重复审卡）
          </div>
          <p className="mt-2 text-xs leading-relaxed text-zinc-600 dark:text-zinc-300">
            在飞书发 <code className="rounded bg-zinc-100 dark:bg-zinc-800 px-1.5 py-0.5">[modular-rag] KAN-12</code>、在控制台粘贴 Jira 链接，或在 Jira 给工单打上{" "}
            <code className="rounded bg-zinc-100 dark:bg-zinc-800 px-1.5 py-0.5">ai-delegate</code> 标签：系统自动拉取 Jira 标题/正文/评论作为<strong>唯一权威需求源</strong>，流转状态为{" "}
            <code className="rounded bg-zinc-100 dark:bg-zinc-800 px-1 py-0.5">In Progress</code> 并立即启动沙箱编码。
          </p>
        </SurfaceCard>

        <SurfaceCard className="p-5 border-amber-500/30 bg-gradient-to-br from-amber-500/6 via-white to-white dark:from-amber-500/10 dark:via-zinc-900 dark:to-zinc-900">
          <div className="flex items-center gap-2 text-xs font-semibold text-amber-600 dark:text-amber-400">
            <ClipboardCheck className="h-4 w-4" />
            路径二：自然语言建卡场景 → 先人工审核草稿 → 建卡后以 Jira 为准
          </div>
          <p className="mt-2 text-xs leading-relaxed text-zinc-600 dark:text-zinc-300">
            口头提出新需求而无现成工单时，Agent 先起草结构化 Jira 工单草稿并暂停在{" "}
            <code className="rounded bg-zinc-100 dark:bg-zinc-800 px-1 py-0.5">WAITING_JIRA_APPROVAL</code>，同步推送飞书与控制台供你审核/修改；你点<strong>「批准创建 Jira 并开工」</strong>后才正式建卡，后续编码、Review、PR 合入全程以该 Jira 工单为准。
          </p>
        </SurfaceCard>
      </div>

      {/* Quick Launcher for Existing Jira Ticket */}
      <SurfaceCard className="p-5 space-y-4">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2 text-xs font-semibold text-zinc-800 dark:text-zinc-200">
            <Sparkles className="h-4 w-4 text-emerald-500" />
            按已有 Jira 单号 / 链接直接开工
          </div>
          {status?.configured ? (
            <span className="inline-flex items-center gap-1 rounded-full bg-emerald-500/10 px-2.5 py-0.5 text-[11px] font-medium text-emerald-600 dark:text-emerald-400">
              <CheckCircle2 className="h-3.5 w-3.5" />
              已连接 {status.base_url}
            </span>
          ) : (
            <span className="text-[11px] text-amber-600 dark:text-amber-400">
              请先在下方填写并保存 Jira 连接凭证
            </span>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-2.5">
          <input
            type="text"
            value={issueInput}
            onChange={(e) => setIssueInput(e.target.value)}
            placeholder="输入已有 Jira 单号（如 KAN-12）或完整链接 https://.../browse/KAN-12"
            className="h-9 flex-1 min-w-[260px] rounded-lg border border-zinc-200 bg-zinc-50/60 px-3 text-xs font-mono text-zinc-900 placeholder:font-sans placeholder:text-zinc-400 focus:border-blue-500 focus:bg-white focus:outline-none dark:border-zinc-800 dark:bg-zinc-950 dark:text-zinc-100"
          />
          <select
            value={targetRepo}
            onChange={(e) => setTargetRepo(e.target.value)}
            className="h-9 rounded-lg border border-zinc-200 bg-white px-2.5 font-mono text-xs text-zinc-800 dark:border-zinc-800 dark:bg-zinc-950 dark:text-zinc-200"
          >
            {repos.map((r) => (
              <option key={r.repo_id} value={r.repo_id}>
                {r.repo_id} ({r.default_base_branch})
              </option>
            ))}
          </select>
          <Button
            variant="outline"
            disabled={!issueInput.trim() || loadingPreview || !status?.configured}
            onClick={handlePreviewIssue}
          >
            {loadingPreview ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Search className="h-3.5 w-3.5" />
            )}
            拉取预览 Jira
          </Button>
          <Button
            variant="emerald"
            disabled={!issueInput.trim() || dispatching || !status?.configured}
            onClick={handleDispatchExistingJira}
          >
            {dispatching ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Play className="h-3.5 w-3.5" />
            )}
            直接按此 Jira 工单开工
          </Button>
        </div>

        {preview && (
          <div className="rounded-xl border border-blue-500/30 bg-blue-500/5 p-4 space-y-2 text-xs">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex items-center gap-2">
                <a
                  href={preview.url}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-1 rounded bg-blue-600 px-2 py-0.5 font-mono text-[11px] font-semibold text-white hover:bg-blue-500"
                >
                  {preview.key}
                  <ArrowUpRight className="h-3 w-3" />
                </a>
                <span className="rounded bg-zinc-200/70 dark:bg-zinc-800 px-2 py-0.5 text-[11px]">
                  {preview.issue_type} · {preview.priority} · {preview.status}
                </span>
              </div>
              {preview.labels.length > 0 && (
                <div className="flex flex-wrap gap-1">
                  {preview.labels.map((l) => (
                    <code
                      key={l}
                      className="rounded bg-zinc-100 dark:bg-zinc-800 px-1.5 py-0.5 text-[10.5px]"
                    >
                      #{l}
                    </code>
                  ))}
                </div>
              )}
            </div>
            <div className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
              {preview.summary}
            </div>
            {preview.description && (
              <pre className="max-h-44 overflow-y-auto whitespace-pre-wrap rounded-lg bg-white/80 dark:bg-zinc-950/70 p-3 font-sans text-xs text-zinc-700 dark:text-zinc-300 border border-zinc-200/70 dark:border-zinc-800">
                {preview.description}
              </pre>
            )}
          </div>
        )}
      </SurfaceCard>

      {/* Jira Credentials & Auto-Poller Configuration */}
      <SurfaceCard className="p-5">
        <form onSubmit={handleSaveConfig} className="space-y-4">
          <div className="flex items-center justify-between border-b border-zinc-100 dark:border-zinc-800 pb-3">
            <div className="flex items-center gap-2 text-xs font-semibold text-zinc-800 dark:text-zinc-200">
              <KeyRound className="h-4 w-4 text-blue-500" />
              Jira Cloud (v3 ADF) / Jira Data Center (v2) 连接配置
            </div>
            {status?.webhook_url && (
              <span className="text-[11px] text-zinc-400">
                可选 Webhook 回调地址：<code className="text-zinc-600 dark:text-zinc-300">{status.webhook_url}</code>
              </span>
            )}
          </div>

          <div className="grid sm:grid-cols-2 gap-4 text-xs">
            <div className="space-y-1.5">
              <label className="font-medium text-zinc-700 dark:text-zinc-300">
                Jira Base URL <span className="text-red-500">*</span>
              </label>
              <input
                type="url"
                required
                value={baseUrl}
                onChange={(e) => setBaseUrl(e.target.value)}
                placeholder="https://your-domain.atlassian.net"
                className="h-9 w-full rounded-lg border border-zinc-200 bg-white px-3 font-mono text-xs dark:border-zinc-800 dark:bg-zinc-950"
              />
            </div>

            <div className="space-y-1.5">
              <label className="font-medium text-zinc-700 dark:text-zinc-300">
                Jira 登录邮箱（Atlassian Cloud 必填；私有部署用 PAT 可留空）
              </label>
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@company.com"
                className="h-9 w-full rounded-lg border border-zinc-200 bg-white px-3 font-mono text-xs dark:border-zinc-800 dark:bg-zinc-950"
              />
            </div>

            <div className="space-y-1.5">
              <label className="font-medium text-zinc-700 dark:text-zinc-300">
                Jira API Token / Personal Access Token{" "}
                {status?.api_token_configured && (
                  <span className="text-emerald-600 dark:text-emerald-400 font-normal">
                    （已加密保存，留空保持不变）
                  </span>
                )}
              </label>
              <input
                type="password"
                value={apiToken}
                onChange={(e) => setApiToken(e.target.value)}
                placeholder={
                  status?.api_token_configured
                    ? "••••••••••••••••（如需更换请输入新 Token）"
                    : "在 id.atlassian.com/manage-profile/security/api-tokens 生成"
                }
                className="h-9 w-full rounded-lg border border-zinc-200 bg-white px-3 font-mono text-xs dark:border-zinc-800 dark:bg-zinc-950"
              />
            </div>

            <div className="space-y-1.5">
              <label className="font-medium text-zinc-700 dark:text-zinc-300">
                默认建卡 Project Key（如 KAN / RAG）
              </label>
              <div className="flex gap-2">
                <input
                  type="text"
                  value={defaultProject}
                  onChange={(e) => setDefaultProject(e.target.value.toUpperCase())}
                  placeholder="留空则保存时自动探测首个可访问项目"
                  className="h-9 flex-1 rounded-lg border border-zinc-200 bg-white px-3 font-mono text-xs uppercase dark:border-zinc-800 dark:bg-zinc-950"
                />
                {status?.projects && status.projects.length > 0 && (
                  <select
                    value={defaultProject}
                    onChange={(e) => setDefaultProject(e.target.value)}
                    className="h-9 rounded-lg border border-zinc-200 bg-zinc-50 px-2 font-mono text-xs dark:border-zinc-800 dark:bg-zinc-900"
                  >
                    {status.projects.map((p) => (
                      <option key={p.key} value={p.key}>
                        {p.key} ({p.name})
                      </option>
                    ))}
                  </select>
                )}
              </div>
            </div>
          </div>

          <div className="space-y-1.5 text-xs">
            <label className="font-medium text-zinc-700 dark:text-zinc-300">
              后台自动接单 JQL（匹配到的已有工单将跳过建卡审核直接进沙箱执行）
            </label>
            <input
              type="text"
              value={pollJql}
              onChange={(e) => setPollJql(e.target.value)}
              className="h-9 w-full rounded-lg border border-zinc-200 bg-white px-3 font-mono text-xs dark:border-zinc-800 dark:bg-zinc-950"
            />
          </div>

          <div className="flex flex-wrap items-center justify-between gap-4 pt-2 border-t border-zinc-100 dark:border-zinc-800">
            <label className="flex items-center gap-2 text-xs text-zinc-600 dark:text-zinc-400 cursor-pointer select-none">
              <Switch checked={pollerEnabled} onCheckedChange={setPollerEnabled} />
              <span>启用每 30 秒后台轮询 Jira 自动接单</span>
            </label>

            <Button type="submit" variant="primary" size="lg" disabled={saving}>
              {saving ? (
                <>
                  <Loader2 className="h-4 w-4 animate-spin" />
                  正在验证 Jira 凭证与项目列表...
                </>
              ) : (
                <>
                  <CheckCircle2 className="h-4 w-4" />
                  保存并校验 Jira 连通性
                </>
              )}
            </Button>
          </div>
        </form>
      </SurfaceCard>
    </div>
  );
}
