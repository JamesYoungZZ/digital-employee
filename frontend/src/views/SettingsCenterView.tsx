import React from "react";
import {
  BellElectric,
  CheckCircle2,
  Copy,
  Cpu,
  GitBranch,
  Loader2,
  MessageSquareShare,
  Play,
  ShieldCheck,
  TicketCheck,
} from "lucide-react";
import { toast } from "sonner";
import type { CoreEngineStatus, RepositoryItem, TaskItem } from "../types/api";
import { apiFetch } from "../lib/utils";
import {
  Button,
  SurfaceCard,
  Switch,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "../components/ui/primitives";
import { JiraIntegrationView } from "./JiraIntegrationView";
import { FeishuIntegrationView } from "./FeishuIntegrationView";

export type SettingsSubTab = "models" | "jira" | "feishu" | "ops";

interface Props {
  initialSubTab?: SettingsSubTab;
  repos: RepositoryItem[];
  onMutated: () => Promise<void>;
  onSelectTask?: (taskId: string) => void;
}

export function SettingsCenterView({
  initialSubTab = "models",
  repos,
  onMutated,
  onSelectTask,
}: Props) {
  const [subTab, setSubTab] = React.useState<SettingsSubTab>(initialSubTab);

  React.useEffect(() => {
    setSubTab(initialSubTab);
  }, [initialSubTab]);

  // Core Coding Engine & GitHub state
  const [core, setCore] = React.useState<CoreEngineStatus | null>(null);
  const [llmBaseUrl, setLlmBaseUrl] = React.useState("");
  const [llmApiKey, setLlmApiKey] = React.useState("");
  const [reasoningModel, setReasoningModel] = React.useState("");
  const [fastModel, setFastModel] = React.useState("");
  const [githubToken, setGithubToken] = React.useState("");
  const [defaultBudget, setDefaultBudget] = React.useState("1.5");
  const [enableOpsMcp, setEnableOpsMcp] = React.useState(true);
  const [savingCore, setSavingCore] = React.useState(false);

  // Independent Reviewer state
  const [revProvider, setRevProvider] = React.useState("gemini_cli");
  const [revModel, setRevModel] = React.useState("gemini-3.8-flash");
  const [revBaseUrl, setRevBaseUrl] = React.useState("");
  const [revApiKey, setRevApiKey] = React.useState("");
  const [revCurrent, setRevCurrent] = React.useState<{
    provider: string;
    model: string;
    api_key_configured?: boolean;
  } | null>(null);
  const [savingRev, setSavingRev] = React.useState(false);

  // Ops Alert Simulation state
  const [alertRepo, setAlertRepo] = React.useState("");
  const [alertName, setAlertName] = React.useState("HighLatencyOrUnhandledException");
  const [alertSeverity, setAlertSeverity] = React.useState("critical");
  const [alertError, setAlertError] = React.useState(
    "KeyError: 'stage_timings' in observability middleware"
  );
  const [alertStack, setAlertStack] = React.useState("");
  const [sendingAlert, setSendingAlert] = React.useState(false);

  React.useEffect(() => {
    if (!alertRepo && repos.length > 0) {
      setAlertRepo(repos[0].repo_id);
    }
  }, [repos, alertRepo]);

  const loadAllConfigs = React.useCallback(async () => {
    const [c, rev] = await Promise.all([
      apiFetch<CoreEngineStatus>("/api/integrations/core"),
      apiFetch<{ provider: string; model: string; base_url?: string; api_key_configured?: boolean }>(
        "/api/integrations/reviewer"
      ),
    ]);
    setCore(c);
    setLlmBaseUrl(c.llm_base_url || "https://api.deepseek.com");
    setReasoningModel(c.reasoning_model || "deepseek-v4-pro");
    setFastModel(c.fast_model || "deepseek-flash");
    setDefaultBudget(String(c.default_max_budget_usd ?? 1.5));
    setEnableOpsMcp(c.enable_ops_mcp);

    setRevCurrent(rev);
    setRevProvider(rev.provider);
    setRevModel(rev.model);
    setRevBaseUrl(rev.base_url || "");
  }, []);

  React.useEffect(() => {
    loadAllConfigs().catch((e) => toast.error(e.message));
  }, [loadAllConfigs]);

  const handleSaveCore = async (e: React.FormEvent) => {
    e.preventDefault();
    setSavingCore(true);
    try {
      await apiFetch("/api/integrations/core/config", {
        method: "POST",
        body: JSON.stringify({
          llm_base_url: llmBaseUrl.trim(),
          llm_api_key: llmApiKey.trim(),
          reasoning_model: reasoningModel.trim(),
          fast_model: fastModel.trim(),
          github_token: githubToken.trim(),
          default_max_budget_usd: parseFloat(defaultBudget) || 1.5,
          enable_ops_mcp: enableOpsMcp,
        }),
      });
      setLlmApiKey("");
      setGithubToken("");
      toast.success("主编码模型、预算与 GitHub 凭证已保存并实时热生效");
      await loadAllConfigs();
      await onMutated();
    } catch (err: any) {
      toast.error(err.message);
    } finally {
      setSavingCore(false);
    }
  };

  const handleSaveReviewer = async (e: React.FormEvent) => {
    e.preventDefault();
    setSavingRev(true);
    try {
      await apiFetch("/api/integrations/reviewer/config", {
        method: "POST",
        body: JSON.stringify({
          provider: revProvider,
          model: revModel.trim(),
          base_url: revBaseUrl.trim(),
          api_key: revApiKey.trim(),
        }),
      });
      setRevApiKey("");
      toast.success(`独立 Code Review 引擎已切换为 ${revProvider} · ${revModel}`);
      await loadAllConfigs();
      await onMutated();
    } catch (err: any) {
      toast.error(err.message);
    } finally {
      setSavingRev(false);
    }
  };

  const handleTriggerAlert = async (autoExecute: boolean) => {
    setSendingAlert(true);
    try {
      const created = await apiFetch<TaskItem>("/api/integrations/ops/simulate-alert", {
        method: "POST",
        body: JSON.stringify({
          target_repo: alertRepo,
          alert_name: alertName.trim(),
          severity: alertSeverity,
          error_message: alertError.trim(),
          stack_trace: alertStack.trim(),
          auto_execute: autoExecute,
        }),
      });
      toast.success(
        autoExecute
          ? `已派发告警排障任务 ${created.task_id} 并启动沙箱诊断`
          : `已生成告警排障契约草稿 ${created.task_id}`
      );
      await onMutated();
      onSelectTask?.(created.task_id);
    } catch (err: any) {
      toast.error(err.message);
    } finally {
      setSendingAlert(false);
    }
  };

  const sampleAlertCurl = `curl -X POST "${core?.ops_alert_webhook_url || "http://127.0.0.1:8020/webhooks/alert"}" \\
  -H "Content-Type: application/json" \\
  -d '{
    "target_repo": "${alertRepo || "modular-rag"}",
    "alert_name": "HighErrorRate5xx",
    "severity": "critical",
    "error_message": "KeyError in request pipeline",
    "stack_trace": "File app/main.py, line 84...",
    "auto_execute": true
  }'`;

  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-lg font-semibold text-zinc-900 dark:text-zinc-100">
          系统设置与外部集成
        </h2>
        <p className="mt-0.5 text-xs text-zinc-500 dark:text-zinc-400">
          集中管理主编码模型、异构 Code Review 模型、GitHub 凭证、Jira 工单流、飞书通道与 7×24 告警 Webhook（所有修改自动持久化至本地 <code className="font-mono">.env</code> 并热生效）。
        </p>
      </div>

      <Tabs value={subTab} onValueChange={(v) => setSubTab(v as SettingsSubTab)}>
        <TabsList className="flex-wrap h-auto">
          <TabsTrigger value="models">
            <Cpu className="h-3.5 w-3.5" />
            模型与 GitHub
          </TabsTrigger>
          <TabsTrigger value="jira">
            <TicketCheck className="h-3.5 w-3.5" />
            Jira 工单集成
          </TabsTrigger>
          <TabsTrigger value="feishu">
            <MessageSquareShare className="h-3.5 w-3.5" />
            飞书双向通道
          </TabsTrigger>
          <TabsTrigger value="ops">
            <BellElectric className="h-3.5 w-3.5" />
            生产告警接入 (Ops)
          </TabsTrigger>
        </TabsList>

        {/* SUB-TAB 1: MODELS & GITHUB */}
        <TabsContent value="models" className="space-y-5">
          <SurfaceCard className="p-5">
            <form onSubmit={handleSaveCore} className="space-y-4">
              <div className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-100 dark:border-zinc-800 pb-3">
                <div>
                  <div className="flex items-center gap-2 text-sm font-semibold text-zinc-900 dark:text-zinc-100">
                    <Cpu className="h-4 w-4 text-blue-500" />
                    主编码内核模型与 GitHub 交付凭证
                  </div>
                  <p className="mt-0.5 text-xs text-zinc-500 dark:text-zinc-400">
                    用于起草任务契约、驱动 <code className="font-mono">mewcode</code> 沙箱读写代码，以及自动推送分支创建 PR / Squash Merge。
                  </p>
                </div>
                <div className="flex items-center gap-2 text-[11.5px]">
                  <span
                    className={
                      core?.llm_api_key_configured
                        ? "text-emerald-600 dark:text-emerald-400 font-medium"
                        : "text-amber-600 dark:text-amber-400 font-medium"
                    }
                  >
                    {core?.llm_api_key_configured ? "✓ 编码 API Key 已就绪" : "⚠ 待配置编码 API Key"}
                  </span>
                  <span className="text-zinc-300 dark:text-zinc-700">|</span>
                  <span
                    className={
                      core?.github_configured
                        ? "text-emerald-600 dark:text-emerald-400 font-medium"
                        : "text-amber-600 dark:text-amber-400 font-medium"
                    }
                  >
                    {core?.github_configured ? "✓ GitHub Token 已就绪" : "⚠ 待配置 GitHub Token"}
                  </span>
                </div>
              </div>

              <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-4 text-xs">
                <div className="space-y-1.5">
                  <label className="font-medium text-zinc-700 dark:text-zinc-300">
                    LLM API Base URL
                  </label>
                  <input
                    type="url"
                    required
                    value={llmBaseUrl}
                    onChange={(e) => setLlmBaseUrl(e.target.value)}
                    placeholder="https://api.deepseek.com"
                    className="h-9 w-full rounded-lg border border-zinc-200 bg-white px-3 font-mono text-xs dark:border-zinc-800 dark:bg-zinc-950"
                  />
                </div>

                <div className="space-y-1.5">
                  <label className="font-medium text-zinc-700 dark:text-zinc-300">
                    主编码模型 ID (Reasoning / Coding)
                  </label>
                  <input
                    type="text"
                    required
                    value={reasoningModel}
                    onChange={(e) => setReasoningModel(e.target.value)}
                    placeholder="deepseek-v4-pro / claude-sonnet-4-20250514"
                    className="h-9 w-full rounded-lg border border-zinc-200 bg-white px-3 font-mono text-xs dark:border-zinc-800 dark:bg-zinc-950"
                  />
                </div>

                <div className="space-y-1.5">
                  <label className="font-medium text-zinc-700 dark:text-zinc-300">
                    轻量路由模型 ID (Fast Model)
                  </label>
                  <input
                    type="text"
                    required
                    value={fastModel}
                    onChange={(e) => setFastModel(e.target.value)}
                    placeholder="deepseek-flash"
                    className="h-9 w-full rounded-lg border border-zinc-200 bg-white px-3 font-mono text-xs dark:border-zinc-800 dark:bg-zinc-950"
                  />
                </div>

                <div className="space-y-1.5">
                  <label className="font-medium text-zinc-700 dark:text-zinc-300">
                    主模型 API Key{" "}
                    {core?.llm_api_key_configured && (
                      <span className="text-emerald-600 dark:text-emerald-400 font-normal">
                        （已保存，留空不变）
                      </span>
                    )}
                  </label>
                  <input
                    type="password"
                    value={llmApiKey}
                    onChange={(e) => setLlmApiKey(e.target.value)}
                    placeholder={
                      core?.llm_api_key_configured
                        ? "••••••••••••••••"
                        : "sk-..."
                    }
                    className="h-9 w-full rounded-lg border border-zinc-200 bg-white px-3 font-mono text-xs dark:border-zinc-800 dark:bg-zinc-950"
                  />
                </div>

                <div className="space-y-1.5">
                  <label className="font-medium text-zinc-700 dark:text-zinc-300">
                    GitHub Personal Access Token{" "}
                    {core?.github_configured && (
                      <span className="text-emerald-600 dark:text-emerald-400 font-normal">
                        （已保存，留空不变）
                      </span>
                    )}
                  </label>
                  <input
                    type="password"
                    value={githubToken}
                    onChange={(e) => setGithubToken(e.target.value)}
                    placeholder={
                      core?.github_configured
                        ? "••••••••••••••••"
                        : "ghp_... 或 github_pat_... (需 repo 权限)"
                    }
                    className="h-9 w-full rounded-lg border border-zinc-200 bg-white px-3 font-mono text-xs dark:border-zinc-800 dark:bg-zinc-950"
                  />
                </div>

                <div className="space-y-1.5">
                  <label className="font-medium text-zinc-700 dark:text-zinc-300">
                    单任务默认费用上限 (USD)
                  </label>
                  <input
                    type="number"
                    step="0.1"
                    min="0.1"
                    max="50"
                    value={defaultBudget}
                    onChange={(e) => setDefaultBudget(e.target.value)}
                    className="h-9 w-full rounded-lg border border-zinc-200 bg-white px-3 font-mono text-xs dark:border-zinc-800 dark:bg-zinc-950"
                  />
                </div>
              </div>

              {core?.github_webhook_url && (
                <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-zinc-50 dark:bg-zinc-950 px-3 py-2 text-[11.5px] text-zinc-500">
                  <span className="inline-flex items-center gap-1.5">
                    <GitBranch className="h-3.5 w-3.5 text-zinc-400" />
                    GitHub PR 评论自动监听：已内置每 15s 免公网轮询；亦可配置实时 Webhook{" "}
                    <code className="font-mono text-zinc-700 dark:text-zinc-300">
                      {core.github_webhook_url}
                    </code>
                  </span>
                  <button
                    type="button"
                    onClick={() => {
                      navigator.clipboard.writeText(core.github_webhook_url);
                      toast.success("已复制 GitHub Webhook 地址");
                    }}
                    className="inline-flex items-center gap-1 text-blue-600 hover:underline dark:text-blue-400 cursor-pointer"
                  >
                    <Copy className="h-3 w-3" /> 复制
                  </button>
                </div>
              )}

              <div className="flex justify-end pt-1">
                <Button type="submit" variant="primary" disabled={savingCore}>
                  {savingCore ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : (
                    <CheckCircle2 className="h-4 w-4" />
                  )}
                  保存主引擎与 GitHub 配置
                </Button>
              </div>
            </form>
          </SurfaceCard>

          <SurfaceCard className="p-5">
            <form onSubmit={handleSaveReviewer} className="space-y-4">
              <div className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-100 dark:border-zinc-800 pb-3">
                <div>
                  <div className="flex items-center gap-2 text-sm font-semibold text-zinc-900 dark:text-zinc-100">
                    <ShieldCheck className="h-4 w-4 text-purple-500" />
                    独立跨模型 Code Review 引擎
                  </div>
                  <p className="mt-0.5 text-xs text-zinc-500 dark:text-zinc-400">
                    建议选用与主编码模型不同厂商的模型进行互审，避免同源模型盲区（仅拦截 P0/P1 实质缺陷与工程红线违例）。
                  </p>
                </div>
                {revCurrent && (
                  <code className="rounded-md bg-purple-500/10 px-2.5 py-1 font-mono text-xs text-purple-700 dark:text-purple-300">
                    当前生效：{revCurrent.provider} · {revCurrent.model}
                  </code>
                )}
              </div>

              <div className="grid sm:grid-cols-3 gap-4 text-xs">
                <div className="space-y-1.5">
                  <label className="font-medium text-zinc-700 dark:text-zinc-300">
                    Review 供应商通道
                  </label>
                  <select
                    value={revProvider}
                    onChange={(e) => {
                      const p = e.target.value;
                      setRevProvider(p);
                      if (p === "gemini_cli" || p === "gemini") setRevModel("gemini-3.8-flash");
                      else if (p === "anthropic") setRevModel("claude-sonnet-4-20250514");
                      else setRevModel("deepseek-flash");
                    }}
                    className="h-9 w-full rounded-lg border border-zinc-200 bg-white px-2.5 font-mono text-xs dark:border-zinc-800 dark:bg-zinc-950"
                  >
                    <option value="gemini_cli">Antigravity CLI (本机免 Key)</option>
                    <option value="gemini">Google Gemini API</option>
                    <option value="anthropic">Anthropic Claude API</option>
                    <option value="openai_compatible">OpenAI / DeepSeek 兼容接口</option>
                  </select>
                </div>

                <div className="space-y-1.5">
                  <label className="font-medium text-zinc-700 dark:text-zinc-300">
                    Review 模型 ID
                  </label>
                  <input
                    type="text"
                    required
                    value={revModel}
                    onChange={(e) => setRevModel(e.target.value)}
                    placeholder="gemini-3.8-flash"
                    className="h-9 w-full rounded-lg border border-zinc-200 bg-white px-3 font-mono text-xs dark:border-zinc-800 dark:bg-zinc-950"
                  />
                </div>

                <div className="space-y-1.5">
                  <label className="font-medium text-zinc-700 dark:text-zinc-300">
                    Review API Key（选 CLI 时免填）
                  </label>
                  <input
                    type="password"
                    value={revApiKey}
                    onChange={(e) => setRevApiKey(e.target.value)}
                    disabled={revProvider === "gemini_cli"}
                    placeholder={
                      revProvider === "gemini_cli"
                        ? "使用本机 CLI 登录态"
                        : revCurrent?.api_key_configured
                          ? "••••••••••••••••（留空不变）"
                          : "AIza... / sk-ant-..."
                    }
                    className="h-9 w-full rounded-lg border border-zinc-200 bg-white px-3 font-mono text-xs disabled:opacity-50 dark:border-zinc-800 dark:bg-zinc-950"
                  />
                </div>
              </div>

              <div className="flex justify-end pt-1">
                <Button type="submit" variant="primary" disabled={savingRev}>
                  {savingRev ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : (
                    <CheckCircle2 className="h-4 w-4" />
                  )}
                  保存审查模型配置
                </Button>
              </div>
            </form>
          </SurfaceCard>
        </TabsContent>

        {/* SUB-TAB 2: JIRA */}
        <TabsContent value="jira">
          <JiraIntegrationView onConfigSaved={onMutated} />
        </TabsContent>

        {/* SUB-TAB 3: FEISHU */}
        <TabsContent value="feishu">
          <FeishuIntegrationView onTaskDispatched={onMutated} />
        </TabsContent>

        {/* SUB-TAB 4: OPS ALERT WEBHOOK */}
        <TabsContent value="ops" className="space-y-5">
          <SurfaceCard className="p-5 space-y-4">
            <div className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-100 dark:border-zinc-800 pb-3">
              <div>
                <div className="flex items-center gap-2 text-sm font-semibold text-zinc-900 dark:text-zinc-100">
                  <BellElectric className="h-4 w-4 text-amber-500" />
                  7×24 生产告警自动排障 Webhook
                </div>
                <p className="mt-0.5 text-xs text-zinc-500 dark:text-zinc-400">
                  将 Prometheus Alertmanager、Sentry 或日志告警推入以下地址，数字员工会自动调用只读诊断工具（近期变更对比、DLP 脱敏日志检索）定位根因：若是代码缺陷则生成回归补丁，若基线正常则出具 <code className="font-mono">No-Op</code> 诊断报告。
                </p>
              </div>

              <label className="flex items-center gap-2 text-xs font-medium text-zinc-700 dark:text-zinc-300 cursor-pointer">
                <Switch
                  checked={enableOpsMcp}
                  onCheckedChange={async (checked) => {
                    setEnableOpsMcp(checked);
                    try {
                      await apiFetch("/api/integrations/core/config", {
                        method: "POST",
                        body: JSON.stringify({ enable_ops_mcp: checked }),
                      });
                      toast.success(checked ? "已启用 Ops 只读诊断 MCP" : "已关闭 Ops 诊断 MCP");
                      await onMutated();
                    } catch (err: any) {
                      toast.error(err.message);
                    }
                  }}
                />
                <span>启用 Ops 诊断 MCP</span>
              </label>
            </div>

            <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-950 px-3.5 py-2.5 text-xs">
              <div>
                <span className="text-zinc-500 mr-2">告警接入端点 (POST):</span>
                <code className="font-mono font-semibold text-zinc-900 dark:text-zinc-100">
                  {core?.ops_alert_webhook_url || "http://127.0.0.1:8020/webhooks/alert"}
                </code>
              </div>
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => {
                  navigator.clipboard.writeText(
                    core?.ops_alert_webhook_url || "http://127.0.0.1:8020/webhooks/alert"
                  );
                  toast.success("已复制告警 Webhook 地址");
                }}
              >
                <Copy className="h-3 w-3" />
                复制 Webhook URL
              </Button>
            </div>

            <div className="space-y-1.5 text-xs">
              <div className="flex items-center justify-between">
                <span className="font-medium text-zinc-700 dark:text-zinc-300">
                  标准调用示例 (Alertmanager / Sentry / 自定义脚本)：
                </span>
                <button
                  type="button"
                  onClick={() => {
                    navigator.clipboard.writeText(sampleAlertCurl);
                    toast.success("已复制 curl 示例命令");
                  }}
                  className="inline-flex items-center gap-1 text-blue-600 hover:underline dark:text-blue-400 cursor-pointer"
                >
                  <Copy className="h-3 w-3" /> 复制 curl
                </button>
              </div>
              <pre className="rounded-lg bg-zinc-950 p-3.5 font-mono text-[11.5px] text-zinc-200 overflow-x-auto">
                {sampleAlertCurl}
              </pre>
            </div>
          </SurfaceCard>

          <SurfaceCard className="p-5 space-y-4">
            <div className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
              在线触发告警排障演练
            </div>

            <div className="grid sm:grid-cols-3 gap-3 text-xs">
              <div>
                <label className="block font-medium text-zinc-700 dark:text-zinc-300 mb-1">
                  目标服务仓库
                </label>
                <select
                  value={alertRepo}
                  onChange={(e) => setAlertRepo(e.target.value)}
                  className="h-9 w-full rounded-lg border border-zinc-200 bg-white px-2.5 font-mono text-xs dark:border-zinc-800 dark:bg-zinc-950"
                >
                  {repos.map((r) => (
                    <option key={r.repo_id} value={r.repo_id}>
                      {r.repo_id}
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label className="block font-medium text-zinc-700 dark:text-zinc-300 mb-1">
                  告警名称
                </label>
                <input
                  type="text"
                  value={alertName}
                  onChange={(e) => setAlertName(e.target.value)}
                  className="h-9 w-full rounded-lg border border-zinc-200 bg-white px-3 font-mono text-xs dark:border-zinc-800 dark:bg-zinc-950"
                />
              </div>
              <div>
                <label className="block font-medium text-zinc-700 dark:text-zinc-300 mb-1">
                  告警级别
                </label>
                <select
                  value={alertSeverity}
                  onChange={(e) => setAlertSeverity(e.target.value)}
                  className="h-9 w-full rounded-lg border border-zinc-200 bg-white px-2.5 text-xs dark:border-zinc-800 dark:bg-zinc-950"
                >
                  <option value="critical">CRITICAL (P0)</option>
                  <option value="warning">WARNING (P1)</option>
                </select>
              </div>
            </div>

            <div className="grid sm:grid-cols-2 gap-3 text-xs">
              <div>
                <label className="block font-medium text-zinc-700 dark:text-zinc-300 mb-1">
                  异常摘要 (Error Message)
                </label>
                <input
                  type="text"
                  value={alertError}
                  onChange={(e) => setAlertError(e.target.value)}
                  className="h-9 w-full rounded-lg border border-zinc-200 bg-white px-3 text-xs dark:border-zinc-800 dark:bg-zinc-950"
                />
              </div>
              <div>
                <label className="block font-medium text-zinc-700 dark:text-zinc-300 mb-1">
                  堆栈或补充上下文 (可选)
                </label>
                <input
                  type="text"
                  value={alertStack}
                  onChange={(e) => setAlertStack(e.target.value)}
                  placeholder="Traceback / Request ID / 最近变更线索..."
                  className="h-9 w-full rounded-lg border border-zinc-200 bg-white px-3 font-mono text-xs dark:border-zinc-800 dark:bg-zinc-950"
                />
              </div>
            </div>

            <div className="flex flex-wrap gap-2">
              <Button
                type="button"
                variant="outline"
                disabled={sendingAlert || !repos.length}
                onClick={() => handleTriggerAlert(false)}
              >
                仅生成排障契约（不自动执行）
              </Button>
              <Button
                type="button"
                variant="primary"
                disabled={sendingAlert || !repos.length}
                onClick={() => handleTriggerAlert(true)}
              >
                {sendingAlert ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <Play className="h-3.5 w-3.5" />
                )}
                派发告警并启动自动排障
              </Button>
            </div>
          </SurfaceCard>
        </TabsContent>
      </Tabs>
    </div>
  );
}
