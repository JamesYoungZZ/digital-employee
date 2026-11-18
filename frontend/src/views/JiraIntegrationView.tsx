import React from "react";
import { CheckCircle2, Copy, KeyRound, Loader2 } from "lucide-react";
import { toast } from "sonner";
import type { JiraIntegrationStatus } from "../types/api";
import { apiFetch } from "../lib/utils";
import { Button, SurfaceCard, Switch } from "../components/ui/primitives";

interface Props {
  onConfigSaved: () => Promise<void>;
}

export function JiraIntegrationView({ onConfigSaved }: Props) {
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
          ? `Jira 已连通（账号: ${res.display_name} · 默认项目: ${res.default_project || "未指定"}）`
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

  return (
    <SurfaceCard className="p-5">
      <form onSubmit={handleSaveConfig} className="space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-100 dark:border-zinc-800 pb-3">
          <div>
            <div className="flex items-center gap-2 text-sm font-semibold text-zinc-900 dark:text-zinc-100">
              <KeyRound className="h-4 w-4 text-blue-500" />
              Jira Cloud / Data Center 凭证与自动接单轮询
            </div>
            <p className="mt-0.5 text-xs text-zinc-500 dark:text-zinc-400">
              配置后可在「任务工作台」直接粘贴 Jira 单号开工，或由后台按 JQL 自动拉取带 <code className="font-mono">ai-delegate</code> 标签的工单。
            </p>
          </div>
          {status?.configured && (
            <span className="inline-flex items-center gap-1 rounded-full bg-emerald-500/10 px-2.5 py-0.5 text-[11px] font-medium text-emerald-600 dark:text-emerald-400">
              <CheckCircle2 className="h-3.5 w-3.5" />
              已连通 {status.base_url}
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
              Jira 登录邮箱（Cloud 必填；私有化 PAT 可留空）
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
              API Token / PAT{" "}
              {status?.api_token_configured && (
                <span className="text-emerald-600 dark:text-emerald-400 font-normal">
                  （已保存，留空保持不变）
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
                placeholder="留空则保存时自动探测首个项目"
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
            后台自动接单 JQL（匹配工单将自动拉取进沙箱执行）
          </label>
          <input
            type="text"
            value={pollJql}
            onChange={(e) => setPollJql(e.target.value)}
            className="h-9 w-full rounded-lg border border-zinc-200 bg-white px-3 font-mono text-xs dark:border-zinc-800 dark:bg-zinc-950"
          />
        </div>

        {status?.webhook_url && (
          <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-zinc-50 dark:bg-zinc-950 px-3 py-2 text-[11.5px] text-zinc-500">
            <span>
              可选实时 Webhook 回调地址：<code className="font-mono text-zinc-700 dark:text-zinc-300">{status.webhook_url}</code>
            </span>
            <button
              type="button"
              onClick={() => {
                navigator.clipboard.writeText(status.webhook_url);
                toast.success("已复制 Jira Webhook 地址");
              }}
              className="inline-flex items-center gap-1 text-blue-600 hover:underline dark:text-blue-400 cursor-pointer"
            >
              <Copy className="h-3 w-3" /> 复制地址
            </button>
          </div>
        )}

        <div className="flex flex-wrap items-center justify-between gap-4 pt-2 border-t border-zinc-100 dark:border-zinc-800">
          <label className="flex items-center gap-2 text-xs text-zinc-600 dark:text-zinc-400 cursor-pointer select-none">
            <Switch checked={pollerEnabled} onCheckedChange={setPollerEnabled} />
            <span>启用每 30 秒后台轮询 Jira 自动接单</span>
          </label>

          <Button type="submit" variant="primary" disabled={saving}>
            {saving ? (
              <>
                <Loader2 className="h-4 w-4 animate-spin" />
                正在校验连通性...
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
  );
}
