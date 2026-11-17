import React from "react";
import {
  ArrowRight,
  CheckCircle2,
  Cpu,
  MessageSquareCode,
  Play,
  Send,
  Webhook,
} from "lucide-react";
import { toast } from "sonner";
import type { FeishuCardPayload, FeishuIntegrationStatus } from "../types/api";
import { apiFetch } from "../lib/utils";
import { Button, SurfaceCard } from "../components/ui/primitives";
import { FeishuCardPreview } from "../components/FeishuCardPreview";

interface Props {
  onTaskDispatched: () => Promise<void>;
}

export function FeishuIntegrationView({ onTaskDispatched }: Props) {
  const [status, setStatus] = React.useState<FeishuIntegrationStatus | null>(null);
  const [webhookUrl, setWebhookUrl] = React.useState("");
  const [appId, setAppId] = React.useState("");
  const [appSecret, setAppSecret] = React.useState("");
  const [simInput, setSimInput] = React.useState(
    "@_user_1 [modular-rag] 修复 list_collections 中 include_stats 未透传的问题"
  );
  const [parsedIntent, setParsedIntent] = React.useState<Record<string, string> | null>(null);
  const [previewCard, setPreviewCard] = React.useState<FeishuCardPayload | null>(null);

  const loadConfig = React.useCallback(async () => {
    const res = await apiFetch<FeishuIntegrationStatus>("/api/integrations/feishu");
    setStatus(res);
    setAppId(res.app_id || "");
  }, []);

  React.useEffect(() => {
    loadConfig().catch(() => {});
  }, [loadConfig]);

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      await apiFetch("/api/integrations/feishu/config", {
        method: "POST",
        body: JSON.stringify({
          webhook_url: webhookUrl,
          app_id: appId,
          app_secret: appSecret,
        }),
      });
      setWebhookUrl("");
      setAppSecret("");
      toast.success("飞书连接配置已保存并实时生效");
      await loadConfig();
    } catch (err: any) {
      toast.error(err.message);
    }
  };

  const handleTestPush = async () => {
    try {
      const res = await apiFetch<{
        ok: boolean;
        message: string;
        card_preview: FeishuCardPayload;
      }>("/api/integrations/feishu/test", { method: "POST" });
      setPreviewCard(res.card_preview);
      if (res.ok) toast.success(res.message);
      else toast.info(res.message);
    } catch (err: any) {
      toast.error(err.message);
    }
  };

  const runSimulation = async (dispatchRealTask: boolean) => {
    try {
      const res = await apiFetch<{
        dispatched: boolean;
        parsed_intent: Record<string, string>;
        feishu_reply_card?: FeishuCardPayload;
        sample_test_card?: FeishuCardPayload;
        task?: { task_id: string };
      }>("/api/integrations/feishu/simulate", {
        method: "POST",
        body: JSON.stringify({
          raw_text: simInput,
          dispatch_real_task: dispatchRealTask,
        }),
      });
      setParsedIntent(res.parsed_intent);
      setPreviewCard(res.feishu_reply_card ?? res.sample_test_card ?? null);
      if (res.dispatched && res.task) {
        toast.success(`已模拟飞书派活创建任务 ${res.task.task_id}`);
        await onTaskDispatched();
      }
    } catch (err: any) {
      toast.error(err.message);
    }
  };

  return (
    <div className="space-y-6">
      {/* Input -> Engine -> Output Architecture Blueprint */}
      <div className="grid lg:grid-cols-3 gap-4">
        <SurfaceCard className="p-5 space-y-2.5">
          <div className="flex items-center justify-between">
            <span className="inline-flex items-center gap-1.5 text-xs font-semibold text-blue-600 dark:text-blue-400">
              <MessageSquareCode className="h-4 w-4" />
              1. 系统的输入 (System Inputs)
            </span>
            <ArrowRight className="h-4 w-4 text-zinc-300 hidden lg:block" />
          </div>
          <ul className="space-y-2 text-xs text-zinc-600 dark:text-zinc-300 leading-relaxed">
            <li>
              <b className="text-zinc-900 dark:text-zinc-100">派发新委托</b>：飞书群{" "}
              <code className="rounded bg-zinc-100 dark:bg-zinc-800 px-1 py-0.5">
                @数字员工 [仓库名] 报错或需求
              </code>
            </li>
            <li>
              <b className="text-zinc-900 dark:text-zinc-100">5 秒微确认</b>：直接点飞书卡片选项按钮，或群内回{" "}
              <code className="rounded bg-zinc-100 dark:bg-zinc-800 px-1 py-0.5">默认</code> /{" "}
              <code className="rounded bg-zinc-100 dark:bg-zinc-800 px-1 py-0.5">1</code>
            </li>
            <li>
              <b className="text-zinc-900 dark:text-zinc-100">下半场追改</b>：在 GitHub PR 下直接留评论，或飞书发{" "}
              <code className="rounded bg-zinc-100 dark:bg-zinc-800 px-1 py-0.5">#task_id 修改意见</code>
            </li>
          </ul>
        </SurfaceCard>

        <SurfaceCard className="p-5 space-y-2.5">
          <div className="flex items-center justify-between">
            <span className="inline-flex items-center gap-1.5 text-xs font-semibold text-purple-600 dark:text-purple-400">
              <Cpu className="h-4 w-4" />
              2. 受控沙箱与确定性门禁
            </span>
            <ArrowRight className="h-4 w-4 text-zinc-300 hidden lg:block" />
          </div>
          <ul className="space-y-1.5 text-xs text-zinc-600 dark:text-zinc-300 leading-relaxed">
            <li>• 读取目标仓代码与 <code className="font-mono">.ai/DECISIONS.yaml</code> 自动单模块切片</li>
            <li>• 独立 <code className="font-mono">ai-agent/*</code> Worktree，剥离密钥环境变量</li>
            <li>• 基线跑单测确认 <b>FAIL</b> → 修复后跑单测确认 <b>PASS</b></li>
            <li>• 静态拦截删 <code className="font-mono">assert</code> / 加 <code className="font-mono">@skip</code> 作弊行为</li>
          </ul>
        </SurfaceCard>

        <SurfaceCard className="p-5 space-y-2.5">
          <div className="flex items-center gap-1.5 text-xs font-semibold text-emerald-600 dark:text-emerald-400">
            <CheckCircle2 className="h-4 w-4" />
            3. 给用户的交付输出 (Outputs)
          </div>
          <ul className="space-y-2 text-xs text-zinc-600 dark:text-zinc-300 leading-relaxed">
            <li>
              <b className="text-zinc-900 dark:text-zinc-100">线上 GitHub Draft PR</b>：带线上真实 CI 绿灯，物理禁止自动合入主干
            </li>
            <li>
              <b className="text-zinc-900 dark:text-zinc-100">飞书证据报告卡</b>：展示回归结果、费用与{" "}
              <b>未验证风险盲区 (Unverified Risks)</b>
            </li>
            <li>
              <b className="text-zinc-900 dark:text-zinc-100">兜底 Handoff 包</b>：超步数或缺环境依赖时保留分支现场结构化交接
            </li>
          </ul>
        </SurfaceCard>
      </div>

      {/* Config + Live Simulator */}
      <div className="grid lg:grid-cols-2 gap-5">
        <SurfaceCard className="p-5 space-y-4">
          <div className="flex items-center gap-2 text-sm font-semibold text-zinc-900 dark:text-zinc-100">
            <Webhook className="h-4 w-4 text-blue-500" />
            飞书免公网通道配置
          </div>

          <form onSubmit={handleSave} className="space-y-3.5 text-xs">
            <div>
              <label className="block font-medium text-zinc-700 dark:text-zinc-300 mb-1">
                方案 A（30 秒即用）：飞书群「自定义机器人」Webhook URL
              </label>
              <input
                type="text"
                value={webhookUrl}
                onChange={(e) => setWebhookUrl(e.target.value)}
                placeholder={
                  status?.webhook_configured
                    ? `已配置 (${status.webhook_masked})，填入新值可覆盖`
                    : "https://open.feishu.cn/open-apis/bot/v2/hook/..."
                }
                className="h-9 w-full rounded-lg border border-zinc-200 bg-white px-3 font-mono text-xs dark:border-zinc-800 dark:bg-zinc-950"
              />
            </div>

            <div>
              <label className="block font-medium text-zinc-700 dark:text-zinc-300 mb-1">
                方案 B（手机双向对话）：飞书自建应用 App ID & Secret（配合 lark-oapi 长连接）
              </label>
              <div className="grid grid-cols-2 gap-2.5">
                <input
                  type="text"
                  value={appId}
                  onChange={(e) => setAppId(e.target.value)}
                  placeholder="cli_xxxxxxxxxx"
                  className="h-9 rounded-lg border border-zinc-200 bg-white px-3 font-mono text-xs dark:border-zinc-800 dark:bg-zinc-950"
                />
                <input
                  type="password"
                  value={appSecret}
                  onChange={(e) => setAppSecret(e.target.value)}
                  placeholder={status?.app_secret_configured ? "已配置 Secret" : "App Secret"}
                  className="h-9 rounded-lg border border-zinc-200 bg-white px-3 font-mono text-xs dark:border-zinc-800 dark:bg-zinc-950"
                />
              </div>
            </div>

            <div className="rounded-lg bg-zinc-50 dark:bg-zinc-950 p-3 text-[11.5px] text-zinc-500">
              群 Webhook 状态：<b>{status?.webhook_configured ? "已连通" : "待配置"}</b> · 自建应用：
              <b>{status?.app_secret_configured ? "已配置" : "待配置"}</b>
            </div>

            <div className="flex flex-wrap gap-2 pt-1">
              <Button type="submit" variant="primary">
                保存配置并热生效
              </Button>
              <Button type="button" variant="emerald" onClick={handleTestPush}>
                <Send className="h-3.5 w-3.5" />
                发送测试卡片到飞书
              </Button>
            </div>
          </form>
        </SurfaceCard>

        <SurfaceCard className="p-5 space-y-4">
          <div className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
            飞书消息路由与卡片仿真沙盒
          </div>
          <p className="text-xs text-zinc-500">
            输入任意飞书群聊指令（派新活 / 回 <code className="font-mono">默认</code> / 发 <code className="font-mono">#task_id</code> 追改），实时查看意图解析与回推卡片效果：
          </p>

          <input
            type="text"
            value={simInput}
            onChange={(e) => setSimInput(e.target.value)}
            className="h-9 w-full rounded-lg border border-zinc-200 bg-white px-3 text-xs dark:border-zinc-800 dark:bg-zinc-950"
          />

          <div className="flex flex-wrap gap-2">
            <Button type="button" variant="outline" onClick={() => runSimulation(false)}>
              仅测试意图路由解析
            </Button>
            <Button type="button" variant="primary" onClick={() => runSimulation(true)}>
              <Play className="h-3.5 w-3.5" />
              模拟真实飞书派活并生成回执卡
            </Button>
          </div>

          {parsedIntent && (
            <pre className="rounded-lg bg-zinc-950 p-3 font-mono text-[11.5px] text-zinc-200 overflow-x-auto">
              {JSON.stringify(parsedIntent, null, 2)}
            </pre>
          )}

          {previewCard && <FeishuCardPreview payload={previewCard} />}
        </SurfaceCard>
      </div>
    </div>
  );
}
