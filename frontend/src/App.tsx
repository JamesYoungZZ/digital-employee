import React from "react";
import {
  Activity,
  Bot,
  FolderGit2,
  LayoutDashboard,
  Moon,
  RefreshCw,
  Settings,
  Sun,
  WifiOff,
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
import {
  SettingsCenterView,
  type SettingsSubTab,
} from "./views/SettingsCenterView";
import { TaskInspectorSheet } from "./components/TaskInspectorSheet";

type ActiveTab = "workbench" | "repos" | "settings";

export function App() {
  const [tab, setTab] = React.useState<ActiveTab>("workbench");
  const [settingsSubTab, setSettingsSubTab] = React.useState<SettingsSubTab>("models");
  const [dark, setDark] = React.useState<boolean>(() => {
    const saved = localStorage.getItem("de_theme");
    return saved ? saved === "dark" : true;
  });

  const [health, setHealth] = React.useState<HealthInfo | null>(null);
  const [metrics, setMetrics] = React.useState<MetricsInfo | null>(null);
  const [repos, setRepos] = React.useState<RepositoryItem[]>([]);
  const [tasks, setTasks] = React.useState<TaskItem[]>([]);
  const [backendOffline, setBackendOffline] = React.useState<boolean>(false);

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

  const openSettings = React.useCallback((sub: SettingsSubTab = "models") => {
    setSettingsSubTab(sub);
    setTab("settings");
  }, []);

  const syncRepos = React.useCallback(async () => {
    const r = await apiFetch<RepositoryItem[]>("/api/repositories");
    setRepos(r);
  }, []);

  const syncRuntime = React.useCallback(
    async (includeRepos = false) => {
      try {
        const reqs: Promise<any>[] = [
          apiFetch<HealthInfo>("/api/health"),
          apiFetch<MetricsInfo>("/api/metrics"),
          apiFetch<TaskItem[]>("/api/tasks?limit=150"),
        ];
        if (includeRepos) {
          reqs.push(apiFetch<RepositoryItem[]>("/api/repositories"));
        }
        const [h, m, t, r] = await Promise.all(reqs);
        setBackendOffline(false);
        setHealth(h);
        setMetrics(m);
        setTasks(t);
        if (r) setRepos(r);

        // Push toast notification when a new Merge Approval Card arrives
        const currentApprovals = (t as TaskItem[]).filter(
          (x) => x.lifecycle_status === "WAITING_MERGE_APPROVAL"
        );
        if (!initializedRef.current) {
          currentApprovals.forEach((x) => notifiedApprovalsRef.current.add(x.task_id));
          initializedRef.current = true;
        } else {
          currentApprovals.forEach((x) => {
            if (!notifiedApprovalsRef.current.has(x.task_id)) {
              notifiedApprovalsRef.current.add(x.task_id);
              toast.success(
                `PR #${x.deliverable_pr_number ?? ""} 单测、独立审查与线上 CI 已全绿，可审批合入！`,
                { duration: 8000 }
              );
            }
          });
        }
      } catch (err) {
        setBackendOffline(true);
        throw err;
      }
    },
    []
  );

  const syncAll = React.useCallback(async () => {
    await syncRuntime(true);
  }, [syncRuntime]);

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

  // Initial load + visibility wake-up
  React.useEffect(() => {
    syncRuntime(true).catch(() => {});
    const onVisibility = () => {
      if (!document.hidden) {
        syncRuntime(tab === "repos").catch(() => {});
      }
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
  }, [syncRuntime, tab]);

  // Refresh repos when switching to the repos tab
  React.useEffect(() => {
    if (tab === "repos") {
      syncRepos().catch(() => {});
    }
  }, [tab, syncRepos]);

  // Adaptive background polling: 2.2s when tasks are actively running, 10s when idle
  const hasActiveTasks = React.useMemo(
    () =>
      tasks.some(
        (t) =>
          t.worker_alive ||
          ["DRAFTING_CONTRACT", "QUEUED", "RUNNING_IN_SANDBOX", "VERIFYING_EVIDENCE"].includes(
            t.lifecycle_status
          )
      ),
    [tasks]
  );

  React.useEffect(() => {
    const intervalMs = backendOffline ? 5000 : hasActiveTasks ? 2200 : 10000;
    const timer = setInterval(() => {
      if (document.hidden) return;
      syncRuntime(false).catch(() => {});
    }, intervalMs);
    return () => clearInterval(timer);
  }, [hasActiveTasks, backendOffline, syncRuntime]);

  // Selected task detail load + live polling ONLY while selected task is running
  const selectedTaskRunning = React.useMemo(() => {
    if (!selectedTaskId) return false;
    const match = tasks.find((t) => t.task_id === selectedTaskId);
    if (!match) return true;
    return (
      Boolean(match.worker_alive) ||
      ["DRAFTING_CONTRACT", "QUEUED", "RUNNING_IN_SANDBOX", "VERIFYING_EVIDENCE"].includes(
        match.lifecycle_status
      )
    );
  }, [selectedTaskId, tasks]);

  React.useEffect(() => {
    if (!selectedTaskId) {
      if (window.location.hash.startsWith("#task=")) {
        history.replaceState(null, "", window.location.pathname);
      }
      setTaskDetail(null);
      return;
    }
    window.location.hash = `task=${selectedTaskId}`;
    syncSelectedDetail(selectedTaskId);

    if (!selectedTaskRunning) return;
    const timer = setInterval(() => {
      if (!document.hidden) {
        syncSelectedDetail(selectedTaskId);
      }
    }, 2000);
    return () => clearInterval(timer);
  }, [selectedTaskId, selectedTaskRunning, syncSelectedDetail]);

  const manualRefresh = async () => {
    setSyncing(true);
    try {
      await syncAll();
      if (selectedTaskId) await syncSelectedDetail(selectedTaskId);
    } catch (e: any) {
      toast.error(`同步失败：${e.message}`);
    } finally {
      setSyncing(false);
    }
  };

  const pendingActionCount = tasks.filter((x) =>
    ["WAITING_MERGE_APPROVAL", "WAITING_JIRA_APPROVAL", "WAITING_CLARIFICATION"].includes(
      x.lifecycle_status
    )
  ).length;

  const unconfiguredCount =
    (health && health.llm_api_key_configured === false ? 1 : 0) +
    (health && !health.github_configured ? 1 : 0);

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
            <div className="text-[11px] text-zinc-400">Console v3.0 · mewcode</div>
          </div>
        </div>

        <nav className="mt-5 space-y-1 flex-1">
          {[
            {
              id: "workbench" as const,
              label: "任务工作台",
              icon: LayoutDashboard,
              badge: pendingActionCount,
              badgeTone: "emerald" as const,
            },
            {
              id: "repos" as const,
              label: "代码仓库与规范",
              icon: FolderGit2,
              badge: repos.reduce((acc, r) => acc + (r.policy_summary?.unhealthy ?? 0), 0),
              badgeTone: "amber" as const,
            },
            {
              id: "settings" as const,
              label: "系统设置与集成",
              icon: Settings,
              badge: unconfiguredCount,
              badgeTone: "amber" as const,
            },
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
                  <span
                    className={cn(
                      "rounded-full px-1.5 py-0.2 text-[10.5px] font-semibold text-white tabular-nums",
                      item.badgeTone === "amber" ? "bg-amber-500" : "bg-emerald-500"
                    )}
                  >
                    {item.badge}
                  </span>
                )}
              </button>
            );
          })}
        </nav>

        {/* Clickable Runtime Status Telemetry Footer */}
        <div className="rounded-xl border border-zinc-200/80 bg-zinc-50/80 p-3 text-[11.5px] space-y-2 dark:border-zinc-800/80 dark:bg-zinc-900/50">
          <div className="flex items-center justify-between font-semibold text-zinc-700 dark:text-zinc-300">
            <span className="flex items-center gap-1.5">
              {backendOffline ? (
                <WifiOff className="h-3.5 w-3.5 text-rose-500" />
              ) : (
                <Activity className="h-3.5 w-3.5 text-emerald-500" />
              )}
              {backendOffline ? "守护进程离线" : "运行态监控"}
            </span>
          </div>
          <div className="space-y-1 text-zinc-500 dark:text-zinc-400">
            <button
              type="button"
              onClick={() => openSettings("models")}
              className="flex w-full items-center justify-between hover:text-zinc-900 dark:hover:text-zinc-200 cursor-pointer"
              title="点击配置主编码模型"
            >
              <span>主编码模型</span>
              <code className="text-[10.5px] text-zinc-800 dark:text-zinc-200 truncate max-w-[115px]">
                {health?.reasoning_model ?? "..."}
              </code>
            </button>
            <button
              type="button"
              onClick={() => openSettings("models")}
              className="flex w-full items-center justify-between hover:text-zinc-900 dark:hover:text-zinc-200 cursor-pointer"
              title="点击配置独立审查模型"
            >
              <span>独立审查模型</span>
              <code className="text-[10.5px] text-purple-600 dark:text-purple-400 truncate max-w-[115px]">
                {health?.reviewer_model ?? "..."}
              </code>
            </button>
            <button
              type="button"
              onClick={() => openSettings("models")}
              className="flex w-full items-center justify-between hover:text-zinc-900 dark:hover:text-zinc-200 cursor-pointer"
              title="点击配置 GitHub 凭证"
            >
              <span>GitHub 巡检</span>
              <span
                className={cn(
                  "font-medium",
                  health?.github_poller_enabled ? "text-emerald-500" : "text-amber-500"
                )}
              >
                {health?.github_poller_enabled ? "15s 在线" : "待配置"}
              </span>
            </button>
            <button
              type="button"
              onClick={() => openSettings("jira")}
              className="flex w-full items-center justify-between hover:text-zinc-900 dark:hover:text-zinc-200 cursor-pointer"
              title="点击配置 Jira 集成"
            >
              <span>Jira 工单</span>
              <span
                className={cn(
                  "font-medium",
                  health?.jira_configured ? "text-emerald-500" : "text-zinc-400"
                )}
              >
                {health?.jira_configured
                  ? `已接 ${health.jira_default_project || "Jira"}`
                  : "可选"}
              </span>
            </button>
            <button
              type="button"
              onClick={() => openSettings("feishu")}
              className="flex w-full items-center justify-between hover:text-zinc-900 dark:hover:text-zinc-200 cursor-pointer"
              title="点击配置飞书通道"
            >
              <span>飞书通道</span>
              <span
                className={cn(
                  "font-medium",
                  health?.feishu_configured ? "text-emerald-500" : "text-zinc-400"
                )}
              >
                {health?.feishu_configured ? "已连通" : "可选"}
              </span>
            </button>
          </div>
        </div>
      </aside>

      {/* Main Content Area */}
      <div className="pl-60 flex-1 flex flex-col min-w-0">
        {/* Top Utility Header */}
        <header className="sticky top-0 z-20 flex h-14 items-center justify-between border-b border-zinc-200/80 bg-white/80 px-7 backdrop-blur-md dark:border-zinc-800/80 dark:bg-zinc-950/80">
          <div className="flex items-center gap-2 text-xs text-zinc-500 dark:text-zinc-400">
            <span>可信研发委托系统</span>
            <span>·</span>
            <span className="font-medium text-zinc-800 dark:text-zinc-200">
              契约先行 / 沙箱隔离 / 证据门禁
            </span>
          </div>

          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" onClick={manualRefresh}>
              <RefreshCw className={cn("h-3.5 w-3.5", syncing && "animate-spin")} />
              刷新
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

        {/* Explicit Offline Alert Banner (Never swallow network outages silently) */}
        {backendOffline && (
          <div className="bg-rose-600 px-7 py-2 text-xs font-medium text-white flex items-center justify-between">
            <span className="flex items-center gap-2">
              <WifiOff className="h-3.5 w-3.5" />
              无法连接到后台守护进程，请确认已运行 <code className="font-mono">./dectl.sh start</code>（正在每 5s 自动重连…）
            </span>
            <button
              type="button"
              onClick={manualRefresh}
              className="underline hover:opacity-85 cursor-pointer"
            >
              立即重试
            </button>
          </div>
        )}

        <main className="flex-1 p-7 max-w-[1440px] w-full mx-auto">
          {tab === "workbench" && (
            <WorkbenchView
              health={health}
              metrics={metrics}
              repos={repos}
              tasks={tasks}
              selectedTaskId={selectedTaskId}
              onSelectTask={(tid) => setSelectedTaskId(tid)}
              onCreatedTask={async (tid) => {
                await syncRuntime(false);
                setSelectedTaskId(tid);
              }}
              onMutated={syncAll}
              onOpenSettings={openSettings}
              onOpenRepos={() => setTab("repos")}
            />
          )}

          {tab === "repos" && (
            <RepositoriesView repos={repos} onRefresh={syncRepos} />
          )}

          {tab === "settings" && (
            <SettingsCenterView
              initialSubTab={settingsSubTab}
              repos={repos}
              onMutated={syncAll}
              onSelectTask={(tid) => {
                setTab("workbench");
                setSelectedTaskId(tid);
              }}
            />
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
