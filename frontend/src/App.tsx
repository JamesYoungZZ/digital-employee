import React from "react";
import {
  Activity,
  Bot,
  FolderGit2,
  LayoutDashboard,
  MessageSquareShare,
  Moon,
  RefreshCw,
  Sun,
} from "lucide-react";
import { Toaster, toast } from "sonner";
import type {
  HealthInfo,
  MetricsInfo,
  RepositoryItem,
  TaskDetailResponse,
  TaskItem,
} from "./types/api";
import { apiFetch, cn } from "./lib/utils";
import { Button } from "./components/ui/primitives";
import { WorkbenchView } from "./views/WorkbenchView";
import { RepositoriesView } from "./views/RepositoriesView";
import { FeishuIntegrationView } from "./views/FeishuIntegrationView";
import { TaskInspectorSheet } from "./components/TaskInspectorSheet";

type ActiveTab = "workbench" | "repos" | "feishu";

export function App() {
  const [tab, setTab] = React.useState<ActiveTab>("workbench");
  const [dark, setDark] = React.useState<boolean>(() => {
    const saved = localStorage.getItem("de_theme");
    return saved ? saved === "dark" : true;
  });

  const [health, setHealth] = React.useState<HealthInfo | null>(null);
  const [metrics, setMetrics] = React.useState<MetricsInfo | null>(null);
  const [repos, setRepos] = React.useState<RepositoryItem[]>([]);
  const [tasks, setTasks] = React.useState<TaskItem[]>([]);
  const [selectedTaskId, setSelectedTaskId] = React.useState<string | null>(() => {
    const h = window.location.hash;
    return h.startsWith("#task=") ? h.slice(6) : null;
  });
  const [taskDetail, setTaskDetail] = React.useState<TaskDetailResponse | null>(null);
  const [syncing, setSyncing] = React.useState(false);
  const notifiedApprovalsRef = React.useRef<Set<string>>(new Set());
  const initializedRef = React.useRef<boolean>(false);

  React.useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
    localStorage.setItem("de_theme", dark ? "dark" : "light");
  }, [dark]);

  const syncAll = React.useCallback(async () => {
    const [h, m, r, t] = await Promise.all([
      apiFetch<HealthInfo>("/api/health"),
      apiFetch<MetricsInfo>("/api/metrics"),
      apiFetch<RepositoryItem[]>("/api/repositories"),
      apiFetch<TaskItem[]>("/api/tasks?limit=50"),
    ]);
    setHealth(h);
    setMetrics(m);
    setRepos(r);
    setTasks(t);

    // Push toast notification when a new Merge Approval Card arrives
    const currentApprovals = t.filter((x) => x.lifecycle_status === "WAITING_MERGE_APPROVAL");
    if (!initializedRef.current) {
      currentApprovals.forEach((x) => notifiedApprovalsRef.current.add(x.task_id));
      initializedRef.current = true;
    } else {
      currentApprovals.forEach((x) => {
        if (!notifiedApprovalsRef.current.has(x.task_id)) {
          notifiedApprovalsRef.current.add(x.task_id);
          toast.success(
            `新合入审批卡已送达：PR #${x.deliverable_pr_number ?? ""} 独立审查与线上 CI 已全绿，请审批合入！`,
            { duration: 8000 }
          );
        }
      });
    }
  }, []);

  const syncSelectedDetail = React.useCallback(async (tid: string | null) => {
    if (!tid) {
      setTaskDetail(null);
      return;
    }
    try {
      const d = await apiFetch<TaskDetailResponse>(`/api/tasks/${encodeURIComponent(tid)}`);
      setTaskDetail(d);
    } catch {
      setTaskDetail(null);
    }
  }, []);

  React.useEffect(() => {
    syncAll().catch((e) => toast.error(e.message));
    const timer = setInterval(() => {
      syncAll().catch(() => {});
    }, 3000);
    return () => clearInterval(timer);
  }, [syncAll]);

  React.useEffect(() => {
    if (selectedTaskId) {
      window.location.hash = `task=${selectedTaskId}`;
      syncSelectedDetail(selectedTaskId);
      const timer = setInterval(() => {
        syncSelectedDetail(selectedTaskId);
      }, 3000);
      return () => clearInterval(timer);
    } else {
      if (window.location.hash.startsWith("#task=")) {
        history.replaceState(null, "", window.location.pathname);
      }
      setTaskDetail(null);
    }
  }, [selectedTaskId, syncSelectedDetail]);

  const manualRefresh = async () => {
    setSyncing(true);
    try {
      await syncAll();
      if (selectedTaskId) await syncSelectedDetail(selectedTaskId);
    } finally {
      setSyncing(false);
    }
  };

  return (
    <div className="min-h-screen flex bg-zinc-50 text-zinc-900 dark:bg-zinc-950 dark:text-zinc-100">
      {/* Left Sidebar Rail */}
      <aside className="fixed inset-y-0 left-0 z-30 flex w-60 flex-col border-r border-zinc-200/80 bg-white/95 px-3.5 py-5 dark:border-zinc-800/80 dark:bg-zinc-950/95">
        <div className="flex items-center gap-2.5 px-2.5 pb-5 border-b border-zinc-100 dark:border-zinc-800/80">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-blue-600 text-white shadow-sm shadow-blue-600/30">
            <Bot className="h-4.5 w-4.5" />
          </div>
          <div>
            <div className="text-sm font-semibold tracking-tight">AI 研发数字员工</div>
            <div className="text-[11px] text-zinc-400">Delegation Console v2.0</div>
          </div>
        </div>

        <nav className="mt-5 space-y-1 flex-1">
          {[
            { id: "workbench" as const, label: "任务委托工作台", icon: LayoutDashboard, badge: tasks.filter((x) => x.lifecycle_status === "WAITING_MERGE_APPROVAL").length },
            { id: "repos" as const, label: "仓库与决策账本", icon: FolderGit2, badge: 0 },
            { id: "feishu" as const, label: "飞书交互与通道", icon: MessageSquareShare, badge: 0 },
          ].map((item) => {
            const Icon = item.icon;
            const active = tab === item.id;
            return (
              <button
                key={item.id}
                type="button"
                onClick={() => setTab(item.id)}
                className={cn(
                  "flex w-full items-center justify-between rounded-lg px-3 py-2 text-xs font-medium transition-colors cursor-pointer",
                  active
                    ? "bg-zinc-900 text-white dark:bg-zinc-100 dark:text-zinc-900 shadow-2xs"
                    : "text-zinc-600 hover:bg-zinc-100 hover:text-zinc-900 dark:text-zinc-400 dark:hover:bg-zinc-900 dark:hover:text-zinc-100"
                )}
              >
                <span className="flex items-center gap-2.5">
                  <Icon className="h-4 w-4 shrink-0" />
                  <span>{item.label}</span>
                </span>
                {item.badge > 0 && (
                  <span className="rounded-full bg-emerald-500 px-1.5 py-0.2 text-[10.5px] font-semibold text-white tabular-nums">
                    {item.badge}
                  </span>
                )}
              </button>
            );
          })}
        </nav>

        {/* Runtime Status Telemetry Footer */}
        <div className="rounded-xl border border-zinc-200/80 bg-zinc-50/80 p-3 text-[11.5px] space-y-2 dark:border-zinc-800/80 dark:bg-zinc-900/50">
          <div className="flex items-center gap-1.5 font-semibold text-zinc-700 dark:text-zinc-300">
            <Activity className="h-3.5 w-3.5 text-emerald-500" />
            运行态健康监控
          </div>
          <div className="space-y-1 text-zinc-500 dark:text-zinc-400">
            <div className="flex items-center justify-between">
              <span>编码模型</span>
              <code className="text-[10.5px] text-zinc-800 dark:text-zinc-200">
                {health?.reasoning_model ?? "..."}
              </code>
            </div>
            <div className="flex items-center justify-between">
              <span>独立审查模型</span>
              <code className="text-[10.5px] text-purple-600 dark:text-purple-400">
                {health?.reviewer_model ?? "..."}
              </code>
            </div>
            <div className="flex items-center justify-between">
              <span>PR & 自动合入</span>
              <span
                className={cn(
                  "font-medium",
                  health?.github_poller_enabled ? "text-emerald-500" : "text-amber-500"
                )}
              >
                {health?.github_poller_enabled ? "15s 巡检合入" : "未开启"}
              </span>
            </div>
            <div className="flex items-center justify-between">
              <span>飞书通知通道</span>
              <span
                className={cn(
                  "font-medium",
                  health?.feishu_configured ? "text-emerald-500" : "text-zinc-400"
                )}
              >
                {health?.feishu_configured ? "已连通" : "待配置"}
              </span>
            </div>
          </div>
        </div>
      </aside>

      {/* Main Content Area */}
      <div className="pl-60 flex-1 flex flex-col min-w-0">
        {/* Top Utility Header */}
        <header className="sticky top-0 z-20 flex h-14 items-center justify-between border-b border-zinc-200/80 bg-white/80 px-7 backdrop-blur-md dark:border-zinc-800/80 dark:bg-zinc-950/80">
          <div className="text-xs text-zinc-500 dark:text-zinc-400">
            可信研发委托系统 · <span className="font-medium text-zinc-800 dark:text-zinc-200">契约先行 / 沙箱隔离 / 证据门禁</span>
          </div>

          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" onClick={manualRefresh}>
              <RefreshCw className={cn("h-3.5 w-3.5", syncing && "animate-spin")} />
              同步状态
            </Button>
            <Button
              variant="outline"
              size="icon"
              onClick={() => setDark((d) => !d)}
              title={dark ? "切换浅色模式" : "切换深色模式"}
            >
              {dark ? <Sun className="h-4 w-4 text-amber-400" /> : <Moon className="h-4 w-4" />}
            </Button>
          </div>
        </header>

        <main className="flex-1 p-7 max-w-[1440px] w-full mx-auto">
          {tab === "workbench" && (
            <WorkbenchView
              metrics={metrics}
              repos={repos}
              tasks={tasks}
              selectedTaskId={selectedTaskId}
              onSelectTask={(tid) => setSelectedTaskId(tid)}
              onCreatedTask={async (tid) => {
                await syncAll();
                setSelectedTaskId(tid);
              }}
              onMutated={syncAll}
            />
          )}

          {tab === "repos" && (
            <RepositoriesView repos={repos} onRefresh={syncAll} />
          )}

          {tab === "feishu" && (
            <FeishuIntegrationView onTaskDispatched={syncAll} />
          )}
        </main>
      </div>

      {/* Right Slide-Over Task Inspector */}
      <TaskInspectorSheet
        detail={taskDetail}
        onClose={() => setSelectedTaskId(null)}
        onMutated={async () => {
          await syncAll();
          if (selectedTaskId) await syncSelectedDetail(selectedTaskId);
        }}
      />

      <Toaster theme={dark ? "dark" : "light"} richColors position="bottom-right" />
    </div>
  );
}
