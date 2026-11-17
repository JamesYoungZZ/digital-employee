import React from "react";
import {
  ArrowRight,
  CheckCircle2,
  Copy,
  Cpu,
  ExternalLink,
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

            <div className="rounded-lg bg-zinc-50 dark:bg-zinc-950 p-3 space-y-1 text-[11.5px] text-zinc-600 dark:text-zinc-400">
              <div>
                长连接状态：{" "}
                <b className={status?.ws_active ? "text-emerald-600 dark:text-emerald-400" : ""}>
                  {status?.ws_active ? "🟢 WebSocket 已在线（收消息 + 点卡片按钮均免公网）" : "⚪ 待填入 App ID & Secret"}
                </b>
              </div>
              <div>
                群 Webhook：<b>{status?.webhook_configured ? "已连通" : "可选（未配也可走应用直推）"}</b>
                {" · "}默认推送会话：
                <b>{status?.default_chat_id ? `已绑定 (${status.default_chat_id.slice(0, 12)}…)` : "给机器人发句「连接」自动绑定"}</b>
              </div>
            </div>

            <div className="rounded-xl border border-blue-500/25 bg-blue-500/5 p-3.5 space-y-2 text-[11.5px] text-zinc-700 dark:text-zinc-300 leading-relaxed">
              <div className="flex items-center justify-between font-semibold text-blue-700 dark:text-blue-300">
                <span>⚡ 3 分钟零公网接入飞书自建机器人指南</span>
                <a
                  href="https://open.feishu.cn/app"
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-1 underline hover:opacity-80"
                >
                  打开飞书开放平台 <ExternalLink className="h-3 w-3" />
                </a>
              </div>
              <ol className="list-decimal pl-4 space-y-1">
                <li>
                  创建<b>企业自建应用</b> → 左侧「添加应用能力」开启<b>机器人</b> → 在「凭证与基础信息」复制 <code>App ID</code> 和 <code>App Secret</code> 先填到上方保存（<b>先让本地长连接跑起来</b>，飞书后台下一步才能校验通过！）。
                </li>
                <li>
                  左侧「权限管理」→ 点<b>批量导入/导出权限</b>，粘贴下方权限 JSON 并申请开通：
                  <button
                    type="button"
                    onClick={() => {
                      navigator.clipboard.writeText(FEISHU_PERMISSIONS_JSON);
                      toast.success("已复制飞书权限 JSON，直接去「批量导入权限」粘贴即可！");
                    }}
                    className="ml-2 inline-flex items-center gap-1 rounded bg-blue-600 px-2 py-0.5 text-[11px] font-medium text-white hover:bg-blue-500 cursor-pointer"
                  >
                    <Copy className="h-2.5 w-2.5" /> 一键复制权限 JSON
                  </button>
                </li>
                <li>
                  左侧「事件与回调」→ <b>事件配置</b>选<b>「使用长连接接收事件」</b>添加 <code>im.message.receive_v1</code>（接收消息）；<b>回调配置</b>同样选<b>「使用长连接接收回调」</b>添加 <code>card.action.trigger</code>（卡片交互）→ 点顶部「创建版本并发布」。
                </li>
                <li>
                  打开手机/电脑飞书搜到该机器人，发一句 <code>连接</code>，即可自动绑定会话并收到绿色欢迎卡！
                </li>
              </ol>
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
