import React from "react";
import {
  ChevronDown,
  Copy,
  ExternalLink,
  Play,
  Send,
  Webhook,
} from "lucide-react";
import { toast } from "sonner";
import type { FeishuCardPayload, FeishuIntegrationStatus } from "../types/api";
import { apiFetch } from "../lib/utils";
import { Button, SurfaceCard } from "../components/ui/primitives";
import { FeishuCardPreview } from "../components/FeishuCardPreview";

const FEISHU_PERMISSIONS_JSON = JSON.stringify(
  {
    scopes: {
      tenant: [
        "im:message",
        "im:message:send_as_bot",
        "im:message.p2p_msg:readonly",
        "im:message.group_at_msg:readonly",
        "im:chat:readonly",
      ],
      user: [],
    },
  },
  null,
  2
);

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
      await onTaskDispatched();
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
        toast.success(`已通过模拟消息创建任务 ${res.task.task_id}`);
        await onTaskDispatched();
      }
    } catch (err: any) {
      toast.error(err.message);
    }
  };

  return (
    <div className="space-y-4">
      <SurfaceCard className="p-5 space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-100 dark:border-zinc-800 pb-3">
          <div>
            <div className="flex items-center gap-2 text-sm font-semibold text-zinc-900 dark:text-zinc-100">
              <Webhook className="h-4 w-4 text-blue-500" />
              飞书免公网双向通道配置
            </div>
            <p className="mt-0.5 text-xs text-zinc-500 dark:text-zinc-400">
              支持单向群机器人 Webhook 推送，或通过飞书自建应用 WebSocket 长连接实现免公网收发消息与卡片按钮审批。
            </p>
          </div>
          <span className="text-xs font-medium">
            {status?.ws_active ? (
              <span className="text-emerald-600 dark:text-emerald-400">
                🟢 长连接在线
              </span>
            ) : status?.webhook_configured ? (
              <span className="text-emerald-600 dark:text-emerald-400">
                🟢 Webhook 已配置
              </span>
            ) : (
              <span className="text-zinc-400">⚪ 未配置</span>
            )}
          </span>
        </div>

        <form onSubmit={handleSave} className="space-y-4 text-xs">
          <div className="grid md:grid-cols-2 gap-4">
            <div>
              <label className="block font-medium text-zinc-700 dark:text-zinc-300 mb-1">
                方案 A（仅推送通知）：飞书群「自定义机器人」Webhook URL
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
                方案 B（双向对话 + 卡片按钮审批）：自建应用 App ID & Secret
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
                  placeholder={status?.app_secret_configured ? "已保存 Secret（留空不变）" : "App Secret"}
                  className="h-9 rounded-lg border border-zinc-200 bg-white px-3 font-mono text-xs dark:border-zinc-800 dark:bg-zinc-950"
                />
              </div>
            </div>
          </div>

          <div className="rounded-lg bg-zinc-50 dark:bg-zinc-950 px-3.5 py-2.5 text-[11.5px] text-zinc-600 dark:text-zinc-400 flex flex-wrap items-center justify-between gap-2">
            <div>
              默认推送会话：
              <b className="ml-1 text-zinc-800 dark:text-zinc-200">
                {status?.default_chat_id
                  ? `已绑定 (${status.default_chat_id.slice(0, 14)}…)`
                  : "配置应用后在飞书给机器人发一句「连接」即可自动绑定"}
              </b>
            </div>
            <button
              type="button"
              onClick={() => {
                navigator.clipboard.writeText(FEISHU_PERMISSIONS_JSON);
                toast.success("已复制飞书权限批量导入 JSON");
              }}
              className="inline-flex items-center gap-1 text-blue-600 hover:underline dark:text-blue-400 cursor-pointer"
            >
              <Copy className="h-3 w-3" /> 复制飞书应用所需权限 JSON
            </button>
          </div>

          <details className="rounded-lg border border-zinc-200/80 dark:border-zinc-800 bg-zinc-50/40 dark:bg-zinc-900/40 px-3.5 py-2.5 text-[11.5px] text-zinc-600 dark:text-zinc-400">
            <summary className="cursor-pointer font-medium text-zinc-700 dark:text-zinc-300 flex items-center justify-between select-none">
              <span>查看 3 步飞书自建应用接入说明</span>
              <ChevronDown className="h-3.5 w-3.5 text-zinc-400" />
            </summary>
            <ol className="list-decimal pl-4 space-y-1.5 mt-2.5 leading-relaxed">
              <li>
                前往{" "}
                <a
                  href="https://open.feishu.cn/app"
                  target="_blank"
                  rel="noreferrer"
                  className="text-blue-600 underline dark:text-blue-400 inline-flex items-center gap-0.5"
                >
                  飞书开放平台 <ExternalLink className="h-2.5 w-2.5" />
                </a>{" "}
                创建企业自建应用 → 开启<b>机器人</b>能力 → 将 <code>App ID</code> 与 <code>App Secret</code> 填入上方保存。
              </li>
              <li>
                在飞书后台「权限管理 → 批量导入权限」粘贴上方复制的权限 JSON；在「事件与回调」将<b>事件配置</b>（添加 <code>im.message.receive_v1</code>）与<b>回调配置</b>（添加 <code>card.action.trigger</code>）均设为<b>使用长连接</b>，发布版本。
              </li>
              <li>
                打开飞书向该机器人发送 <code>连接</code> 即可绑定默认会话；群聊派活格式为 <code>@机器人 [仓库ID] 需求描述</code>。
              </li>
            </ol>
          </details>

          <div className="flex flex-wrap gap-2 pt-1">
            <Button type="submit" variant="primary">
              保存飞书配置
            </Button>
            <Button type="button" variant="outline" onClick={handleTestPush}>
              <Send className="h-3.5 w-3.5" />
              发送测试卡片到飞书
            </Button>
          </div>
        </form>
      </SurfaceCard>

      {/* Collapsible Developer Simulator */}
      <details className="group rounded-xl border border-zinc-200/80 bg-white dark:border-zinc-800/80 dark:bg-zinc-900/60 p-4">
        <summary className="cursor-pointer text-xs font-semibold text-zinc-700 dark:text-zinc-300 flex items-center justify-between select-none">
          <span>开发者调试工具：飞书指令路由与卡片渲染仿真</span>
          <ChevronDown className="h-4 w-4 text-zinc-400 transition-transform group-open:rotate-180" />
        </summary>
        <div className="mt-4 space-y-3 text-xs">
          <div className="flex flex-wrap gap-2">
            <input
              type="text"
              value={simInput}
              onChange={(e) => setSimInput(e.target.value)}
              className="h-9 flex-1 min-w-[260px] rounded-lg border border-zinc-200 bg-white px-3 text-xs dark:border-zinc-800 dark:bg-zinc-950"
            />
            <Button type="button" variant="outline" onClick={() => runSimulation(false)}>
              解析意图 JSON
            </Button>
            <Button type="button" variant="primary" onClick={() => runSimulation(true)}>
              <Play className="h-3.5 w-3.5" />
              模拟真实飞书派单
            </Button>
          </div>

          {parsedIntent && (
            <pre className="rounded-lg bg-zinc-950 p-3 font-mono text-[11.5px] text-zinc-200 overflow-x-auto">
              {JSON.stringify(parsedIntent, null, 2)}
            </pre>
          )}

          {previewCard && <FeishuCardPreview payload={previewCard} />}
        </div>
      </details>
    </div>
  );
}
