import React from "react";
import {
  AlertTriangle,
  ArrowUpRight,
  BookMarked,
  CheckCircle2,
  ChevronDown,
  Clock,
  Copy,
  FileDiff,
  FolderPlus,
  GitBranch,
  GitMerge,
  GitPullRequest,
  Loader2,
  MessageSquarePlus,
  Play,
  RefreshCw,
  Send,
  ShieldAlert,
  ShieldCheck,
  Sparkles,
  TicketCheck,
  X,
  XCircle,
} from "lucide-react";
import { toast } from "sonner";
import type { TaskDetailResponse } from "../types/api";
import { apiFetch, cn } from "../lib/utils";
import {
  Button,
  StatusBadge,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "./ui/primitives";
import { DiffViewer } from "./DiffViewer";
import { FeishuCardPreview } from "./FeishuCardPreview";

interface Props {
  detail: TaskDetailResponse | null;
  onClose: () => void;
  onMutated: () => Promise<void>;
}

const STEP_LABEL_MAP: Record<string, string> = {
  queued: "任务已入队等待执行",
  jira_draft_created: "已生成 Jira 工单草稿等待审核",
  jira_ticket_created: "已在真实 Jira 创建工单",
  jira_synced: "已同步最新 Jira 需求内容",
  clarification_resolved: "人工已确认未决问题",
  running_in_sandbox: "沙箱开始执行编码",
  verifying_evidence: "开始执行单测与静态门禁",
  scope_updated: "人工已更新允许修改目录范围",
  scope_expanded_and_resumed: "人工已批准扩权并恢复沙箱执行",
  decision_promoted: "已沉淀研发工程红线",
  accepted_by_human: "人工确认采纳结单",
  abandoned_by_human: "人工标记废弃任务",
  merged_and_closed: "PR 已完成 Squash Merge 合入主干",
  noop_verified_and_closed: "基线测试已全绿，No-Op 正常结单",
};

function formatStepLabel(raw: string): string {
  if (STEP_LABEL_MAP[raw]) return STEP_LABEL_MAP[raw];
  if (raw.startsWith("verifying_evidence_")) return `执行第 ${raw.split("_").pop()} 轮单测证据门禁`;
  if (raw.startsWith("sandbox_attempt_")) return `启动第 ${raw.split("_").pop()} 轮沙箱推理`;
  if (raw.startsWith("idempotent:push_branch")) return "推送代码分支至远端仓库";
  if (raw.startsWith("idempotent:create_pr") || raw.startsWith("idempotent:ensure_pr"))
    return "创建线上 GitHub Pull Request";
  if (raw.startsWith("idempotent:notify_")) return "推送飞书状态通知卡片";
  return raw;
}

export function TaskInspectorSheet({ detail, onClose, onMutated }: Props) {
  const [activeTab, setActiveTab] = React.useState<string>("contract");
  const [replayExpanded, setReplayExpanded] = React.useState<boolean>(true);
  const [answers, setAnswers] = React.useState<Record<string, string>>({});
  const [followupText, setFollowupText] = React.useState("");

  // Editable scopes state
  const [editableScopes, setEditableScopes] = React.useState<string[]>([]);
  const [newScopeInput, setNewScopeInput] = React.useState("");

  // Rule promotion state
  const [ruleTitle, setRuleTitle] = React.useState("");
  const [ruleScope, setRuleScope] = React.useState("");
  const [ruleText, setRuleText] = React.useState("");
  const [showLedgerForm, setShowLedgerForm] = React.useState(false);

  // Jira draft state
  const [jiraProject, setJiraProject] = React.useState("");
  const [jiraType, setJiraType] = React.useState("Task");
  const [jiraSummary, setJiraSummary] = React.useState("");
  const [jiraDesc, setJiraDesc] = React.useState("");

  const [busyAction, setBusyAction] = React.useState<string | null>(null);
  const liveContainerRef = React.useRef<HTMLDivElement | null>(null);

  const taskId = detail?.task.task_id;
  const liveCount = detail?.live_events?.length ?? 0;

  React.useEffect(() => {
    const el = liveContainerRef.current;
    if (el && replayExpanded) {
      el.scrollTop = el.scrollHeight;
    }
  }, [liveCount, taskId, replayExpanded]);

  // Context-aware initialization when switching to a task
  React.useEffect(() => {
    if (!detail) return;
    const t = detail.task;
    const c = t.contract;
    const ev = t.verification_evidence;
    const running = [
      "DRAFTING_CONTRACT",
      "QUEUED",
      "RUNNING_IN_SANDBOX",
      "VERIFYING_EVIDENCE",
    ].includes(t.lifecycle_status);

    setReplayExpanded(running);
    setEditableScopes(c.permitted_scopes ?? []);
    setNewScopeInput("");
    setRuleScope(c.permitted_scopes?.[0] ?? "*");

    // Smart default tab based on lifecycle stage
    if (
      t.lifecycle_status === "WAITING_MERGE_APPROVAL" ||
      ev?.reviewer_report != null
    ) {
      setActiveTab("evidence");
    } else if (
      ["PR_OPEN_FOLLOWING_UP", "DELIVERED_LOCAL_READY"].includes(t.lifecycle_status) &&
      detail.diff
    ) {
      setActiveTab("diff");
    } else {
      setActiveTab("contract");
    }

    const defaults: Record<string, string> = {};
    (c.unresolved_decisions ?? []).forEach((d, idx) => {
      defaults[String(idx)] = d.options?.[0] ?? d.default_assumption;
    });
    setAnswers(defaults);

    if (t.jira_draft) {
      setJiraProject(t.jira_draft.project_key || "PROJ");
      setJiraType(t.jira_draft.issue_type || "Task");
      setJiraSummary(t.jira_draft.summary || c.objective);
      setJiraDesc(t.jira_draft.description || c.reproduction_evidence);
    }
  }, [taskId]);

  React.useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  if (!detail) return null;

  const t = detail.task;
  const c = t.contract;
  const ev = t.verification_evidence;
  const isRunning = [
    "DRAFTING_CONTRACT",
    "QUEUED",
    "RUNNING_IN_SANDBOX",
    "VERIFYING_EVIDENCE",
  ].includes(t.lifecycle_status);

  const scopesModified =
    JSON.stringify(editableScopes) !== JSON.stringify(c.permitted_scopes ?? []);

  const runControl = async (
    action: string,
    extra?: { permitted_scopes?: string[]; note?: string; confirmMsg?: string }
  ) => {
    if (extra?.confirmMsg && !window.confirm(extra.confirmMsg)) {
      return;
    }
    setBusyAction(action);
    try {
      await apiFetch(`/api/tasks/${encodeURIComponent(t.task_id)}/control`, {
        method: "POST",
        body: JSON.stringify({
          action,
          permitted_scopes: extra?.permitted_scopes,
          note: extra?.note || "",
        }),
      });
      if (action === "approve_merge") {
        toast.success("已批准并在 GitHub 完成 Squash Merge 合入主干");
      } else if (action === "update_scope_and_retry") {
        toast.success("已扩充允许修改范围并唤醒沙箱继续执行");
        setReplayExpanded(true);
      } else if (action === "update_scope") {
        toast.success("已保存新的允许修改目录范围");
      } else if (action === "cancel") {
        toast.info("已中断任务并保留工作区现场");
      } else {
        toast.success("操作已生效");
      }
      await onMutated();
    } catch (err: any) {
      toast.error(err.message);
    } finally {
      setBusyAction(null);
    }
  };

  const addScopeItem = (rawScope?: string) => {
    const val = (rawScope ?? newScopeInput).trim();
    if (!val) return;
    if (!editableScopes.includes(val)) {
      setEditableScopes([...editableScopes, val]);
    }
    if (!rawScope) setNewScopeInput("");
  };

  const removeScopeItem = (target: string) => {
    if (editableScopes.length <= 1) {
      toast.error("至少需保留一个允许修改目录");
      return;
    }
    setEditableScopes(editableScopes.filter((s) => s !== target));
  };

  const submitApproveJira = async () => {
    setBusyAction("approve_jira");
    try {
      const updated = await apiFetch<{ jira_issue_key?: string }>(
        `/api/tasks/${encodeURIComponent(t.task_id)}/approve-jira`,
        {
          method: "POST",
          body: JSON.stringify({
            project_key: jiraProject,
            issue_type: jiraType,
            summary: jiraSummary,
            description: jiraDesc,
            auto_execute: true,
          }),
        }
      );
      toast.success(`已创建 Jira 工单 ${updated.jira_issue_key || ""} 并启动沙箱执行`);
      setReplayExpanded(true);
      await onMutated();
    } catch (err: any) {
      toast.error(err.message);
    } finally {
      setBusyAction(null);
    }
  };

  const syncFromJira = async () => {
    setBusyAction("sync_jira");
    try {
      await apiFetch(`/api/tasks/${encodeURIComponent(t.task_id)}/sync-jira`, {
        method: "POST",
      });
      toast.success(`已从 Jira 工单 ${t.jira_issue_key} 同步最新需求与评论`);
      await onMutated();
    } catch (err: any) {
      toast.error(err.message);
    } finally {
      setBusyAction(null);
    }
  };

  const submitClarify = async (useDefaults: boolean) => {
    setBusyAction("clarify");
    try {
      await apiFetch(`/api/tasks/${encodeURIComponent(t.task_id)}/clarify`, {
        method: "POST",
        body: JSON.stringify({
          answers: useDefaults ? {} : answers,
          use_defaults: useDefaults,
          auto_execute: true,
          publish_pr: true,
        }),
      });
      toast.success("已确认方案，沙箱开始执行");
      setReplayExpanded(true);
      await onMutated();
    } catch (err: any) {
      toast.error(err.message);
    } finally {
      setBusyAction(null);
    }
  };

  const submitFollowup = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!followupText.trim()) return;
    setBusyAction("followup");
    try {
      await apiFetch(`/api/tasks/${encodeURIComponent(t.task_id)}/followup`, {
        method: "POST",
        body: JSON.stringify({ comment_body: followupText.trim(), publish_pr: true }),
      });
      setFollowupText("");
      setReplayExpanded(true);
      toast.success("追改指令已下发，沙箱正在增量修改并更新 PR");
      await onMutated();
    } catch (err: any) {
      toast.error(err.message);
    } finally {
      setBusyAction(null);
    }
  };

  const submitRule = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!ruleText.trim()) return;
    setBusyAction("rule");
    try {
      const entry = await apiFetch<{ id: string; health_status?: string }>(
        `/api/tasks/${encodeURIComponent(t.task_id)}/promote-decision`,
        {
          method: "POST",
          body: JSON.stringify({
            title: ruleTitle.trim(),
            scope: ruleScope.trim() || "*",
            rule: ruleText.trim(),
          }),
        }
      );
      setRuleTitle("");
      setRuleText("");
      setShowLedgerForm(false);
      if (entry.health_status === "INVALID_SCOPE") {
        toast.warning(
          `已沉淀规则 ${entry.id}，但作用路径在仓库中未找到，请前往「代码仓库与规范」核对路径`
        );
      } else {
        toast.success(`已沉淀研发红线 ${entry.id}（可在「代码仓库与规范」随时编辑）`);
      }
      await onMutated();
    } catch (err: any) {
      toast.error(err.message);
    } finally {
      setBusyAction(null);
    }
  };

  // Extract violated paths from scope_violations or handoff_bundle to offer 1-click scope chips
  const suggestedScopeDirs = React.useMemo(() => {
    const candidates = new Set<string>();
    const files = [
      ...(ev?.scope_violations ?? []),
      ...(ev?.out_of_scope_files ?? []),
      ...(t.handoff_bundle?.modified_files ?? []),
    ];
    for (const f of files) {
      const clean = f.trim().replace(/^\.\//, "");
      if (!clean) continue;
      const parts = clean.split("/");
      const dir = parts.length > 1 ? `${parts.slice(0, 2).join("/")}/*` : clean;
      if (!editableScopes.includes(dir) && !dir.startsWith(".env") && !dir.startsWith(".github")) {
        candidates.add(dir);
      }
    }
    return Array.from(candidates).slice(0, 4);
  }, [ev?.scope_violations, t.handoff_bundle?.modified_files, editableScopes]);

  return (
    <div
      className="fixed inset-0 z-40 flex justify-end bg-black/35 backdrop-blur-[1px]"
      onClick={onClose}
    >
      <div
        className="flex h-full w-full max-w-4xl flex-col border-l border-zinc-200 bg-white shadow-2xl dark:border-zinc-800 dark:bg-zinc-950"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Sticky Top Header (Clean & Context-Accurate Actions Only) */}
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-200 dark:border-zinc-800 px-6 py-3.5">
          <div className="flex flex-wrap items-center gap-2.5">
            <span className="font-mono text-sm font-semibold text-zinc-900 dark:text-zinc-100">
              {t.task_id}
            </span>
            <StatusBadge status={t.lifecycle_status} />
            {t.jira_issue_key && (
              <div className="inline-flex items-center gap-1">
                <a
                  href={t.jira_issue_url || "#"}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-1 rounded-md bg-indigo-500/15 px-2 py-0.5 font-mono text-[11px] font-semibold text-indigo-700 hover:bg-indigo-500/25 dark:text-indigo-300"
                >
                  <TicketCheck className="h-3 w-3" />
                  {t.jira_issue_key}
                  {t.jira_status ? ` · ${t.jira_status}` : ""}
                  <ArrowUpRight className="h-3 w-3" />
                </a>
                <button
                  type="button"
                  title="从 Jira 拉取最新需求与评论"
                  disabled={!!busyAction}
                  onClick={syncFromJira}
                  className="rounded p-1 text-zinc-500 hover:bg-zinc-100 hover:text-indigo-600 dark:hover:bg-zinc-800 cursor-pointer"
                >
                  <RefreshCw
                    className={cn("h-3 w-3", busyAction === "sync_jira" && "animate-spin")}
                  />
                </button>
              </div>
            )}
            <button
              type="button"
              onClick={() => {
                navigator.clipboard.writeText(t.branch_name);
                toast.success(`已复制分支名 ${t.branch_name}`);
              }}
              className="hidden sm:inline-flex items-center gap-1 rounded-md bg-zinc-100 dark:bg-zinc-900 px-2 py-1 font-mono text-[11px] text-zinc-600 dark:text-zinc-400 hover:text-zinc-900 dark:hover:text-zinc-200 cursor-pointer"
            >
              <GitBranch className="h-3 w-3" />
              {t.branch_name}
              <Copy className="h-2.5 w-2.5 ml-0.5 opacity-60" />
            </button>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            {t.deliverable_pr_url ? (
              <a
                href={t.deliverable_pr_url}
                target="_blank"
                rel="noreferrer"
                className="inline-flex h-8 items-center gap-1.5 rounded-lg bg-blue-600 px-3 text-xs font-medium text-white hover:bg-blue-500 shadow-xs"
              >
                <GitPullRequest className="h-3.5 w-3.5" />
                {t.deliverable_pr_number ? `PR #${t.deliverable_pr_number}` : "打开 GitHub PR"}
                <ArrowUpRight className="h-3.5 w-3.5" />
              </a>
            ) : (
              ev?.repro_test_failed_on_base &&
              ev?.repro_test_passed_on_head &&
              t.lifecycle_status === "DELIVERED_LOCAL_READY" && (
                <Button
                  variant="primary"
                  size="sm"
                  disabled={!!busyAction}
                  onClick={() => runControl("publish_pr")}
                >
                  <GitPullRequest className="h-3.5 w-3.5" />
                  推送创建 PR
                </Button>
              )
            )}

            {t.lifecycle_status === "WAITING_MERGE_APPROVAL" && (
              <Button
                variant="emerald"
                size="sm"
                disabled={!!busyAction}
                onClick={() =>
                  runControl("approve_merge", {
                    confirmMsg: `确认将 PR #${t.deliverable_pr_number ?? ""} Squash Merge 合入 ${c.target_repo} (${c.base_branch}) 主干吗？`,
                  })
                }
              >
                <GitMerge className="h-3.5 w-3.5" />
                批准 Squash 合入
              </Button>
            )}

            {t.lifecycle_status === "DELIVERED_LOCAL_READY" && (
              <Button
                variant="emerald"
                size="sm"
                disabled={!!busyAction}
                onClick={() => runControl("accept")}
              >
                <CheckCircle2 className="h-3.5 w-3.5" />
                标记本地已采纳
              </Button>
            )}

            {((isRunning && !t.worker_alive) ||
              t.lifecycle_status === "HANDED_OFF_INCOMPLETE" ||
              (ev?.reviewer_report?.verdict === "BLOCK" && !t.worker_alive)) && (
              <Button
                variant="primary"
                size="sm"
                disabled={!!busyAction}
                onClick={() => {
                  setReplayExpanded(true);
                  runControl("retry");
                }}
              >
                <Sparkles className="h-3.5 w-3.5" />
                {ev?.reviewer_report?.verdict === "BLOCK"
                  ? "按 Review 意见自动修复"
                  : "恢复执行"}
              </Button>
            )}

            {((isRunning && t.worker_alive) ||
              t.lifecycle_status === "WAITING_CLARIFICATION") && (
              <Button
                variant="outline"
                size="sm"
                disabled={!!busyAction}
                onClick={() =>
                  runControl("cancel", {
                    confirmMsg: `确认中断当前运行中的任务 ${t.task_id} 并转为人工交接态吗？`,
                  })
                }
              >
                中断任务
              </Button>
            )}

            <Button variant="ghost" size="icon" onClick={onClose}>
              <X className="h-4 w-4" />
            </Button>
          </div>
        </div>

        {/* Scrollable Body */}
        <div className="flex-1 overflow-y-auto px-6 py-5 space-y-4">
          {/* Jira Draft Review Banner */}
          {t.lifecycle_status === "WAITING_JIRA_APPROVAL" && (
            <div className="rounded-xl border border-amber-500/45 bg-amber-500/8 p-4 space-y-3 text-xs">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex items-center gap-2 font-semibold text-amber-800 dark:text-amber-200 text-sm">
                  <TicketCheck className="h-4 w-4" />
                  待审核 Jira 工单草稿
                </div>
                <div className="flex items-center gap-2">
                  <Button
                    variant="emerald"
                    size="sm"
                    disabled={!!busyAction}
                    onClick={submitApproveJira}
                  >
                    {busyAction === "approve_jira" ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <TicketCheck className="h-3.5 w-3.5" />
                    )}
                    批准建卡并开工
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={!!busyAction}
                    onClick={() =>
                      runControl("abandon", {
                        confirmMsg: `确认放弃建卡并废弃任务 ${t.task_id} 吗？`,
                      })
                    }
                  >
                    放弃
                  </Button>
                </div>
              </div>

              <div className="grid sm:grid-cols-3 gap-2.5">
                <div>
                  <span className="text-[11px] text-zinc-500 block mb-1">Project Key</span>
                  <input
                    type="text"
                    value={jiraProject}
                    onChange={(e) => setJiraProject(e.target.value.toUpperCase())}
                    className="h-8 w-full rounded-lg border border-zinc-200 bg-white px-2.5 font-mono text-xs uppercase dark:border-zinc-800 dark:bg-zinc-950"
                  />
                </div>
                <div>
                  <span className="text-[11px] text-zinc-500 block mb-1">Issue Type</span>
                  <select
                    value={jiraType}
                    onChange={(e) => setJiraType(e.target.value)}
                    className="h-8 w-full rounded-lg border border-zinc-200 bg-white px-2 text-xs dark:border-zinc-800 dark:bg-zinc-950"
                  >
                    <option value="Bug">Bug</option>
                    <option value="Task">Task</option>
                    <option value="Story">Story</option>
                  </select>
                </div>
              </div>

              <div className="space-y-2">
                <input
                  type="text"
                  value={jiraSummary}
                  onChange={(e) => setJiraSummary(e.target.value)}
                  className="h-8 w-full rounded-lg border border-zinc-200 bg-white px-3 text-xs font-medium dark:border-zinc-800 dark:bg-zinc-950"
                />
                <textarea
                  rows={3}
                  value={jiraDesc}
                  onChange={(e) => setJiraDesc(e.target.value)}
                  className="w-full rounded-lg border border-zinc-200 bg-white p-2.5 text-xs dark:border-zinc-800 dark:bg-zinc-950"
                />
              </div>
            </div>
          )}

          {/* Merge Approval Banner */}
          {t.lifecycle_status === "WAITING_MERGE_APPROVAL" && (
            <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-emerald-500/40 bg-emerald-500/10 px-4 py-3 text-xs text-emerald-800 dark:text-emerald-200">
              <div>
                <div className="flex flex-wrap items-center gap-2 font-semibold text-sm">
                  <span>单测门禁、独立审查与线上 CI 已全部通过</span>
                  {ev?.reviewer_report?.quality_score != null && (
                    <span className="rounded-full bg-emerald-600 px-2 py-0.5 text-[11px] font-bold text-white">
                      {ev.reviewer_report.quality_score}/100 ·{" "}
                      {ev.reviewer_report.merge_recommendation_label || "可合入"}
                    </span>
                  )}
                </div>
                <div className="mt-0.5 opacity-85">
                  请核对下方审查报告与代码 Diff 后点击批准合入主干。
                </div>
              </div>
              <div className="flex items-center gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setActiveTab("diff")}
                >
                  <FileDiff className="h-3.5 w-3.5" />
                  查看代码 Diff
                </Button>
                <Button
                  variant="emerald"
                  size="sm"
                  disabled={!!busyAction}
                  onClick={() =>
                    runControl("approve_merge", {
                      confirmMsg: `确认将 PR #${t.deliverable_pr_number ?? ""} Squash Merge 合入 ${c.target_repo} (${c.base_branch}) 主干吗？`,
                    })
                  }
                >
                  <GitMerge className="h-3.5 w-3.5" />
                  批准 Squash 合入
                </Button>
              </div>
            </div>
          )}

          {/* Real-Time Agent Loop Terminal (Auto-expanded when running, collapsible when finished) */}
          {(isRunning || (detail.live_events && detail.live_events.length > 0)) && (
            <div className="overflow-hidden rounded-xl border border-zinc-800 bg-zinc-950 text-zinc-100 shadow-xs">
              <button
                type="button"
                onClick={() => setReplayExpanded((v) => !v)}
                className="flex w-full flex-wrap items-center justify-between gap-2 bg-zinc-900/90 px-4 py-2.5 text-xs hover:bg-zinc-900 cursor-pointer"
              >
                <div className="flex items-center gap-2 font-medium">
                  {isRunning && t.worker_alive ? (
                    <>
                      <Loader2 className="h-3.5 w-3.5 animate-spin text-emerald-400" />
                      <span className="text-emerald-400">Agent 内核实时执行中</span>
                    </>
                  ) : isRunning && !t.worker_alive ? (
                    <>
                      <span className="h-2 w-2 rounded-full bg-amber-400" />
                      <span className="text-amber-300">后台线程已暂停（点击右上角「恢复执行」继续）</span>
                    </>
                  ) : (
                    <>
                      <CheckCircle2 className="h-3.5 w-3.5 text-zinc-400" />
                      <span className="text-zinc-300">
                        Agent 执行过程回放（共 {detail.live_events?.length ?? 0} 条记录）
                      </span>
                    </>
                  )}
                  <span className="font-mono text-[11px] text-zinc-400">
                    · 第 {t.sandbox_attempt_count || 1} 轮 · Step {t.current_step || 0}/
                    {t.max_steps || 32} · ${t.total_cost_usd.toFixed(4)}
                  </span>
                </div>

                <div className="flex items-center gap-2 text-[11px] text-zinc-400">
                  <span>{replayExpanded ? "收起日志" : "展开查看"}</span>
                  <ChevronDown
                    className={cn(
                      "h-3.5 w-3.5 transition-transform",
                      replayExpanded && "rotate-180"
                    )}
                  />
                </div>
              </button>

              {replayExpanded && (
                <div
                  ref={liveContainerRef}
                  className="max-h-56 overflow-y-auto border-t border-zinc-800/90 px-4 py-3 space-y-2 font-mono text-[11.5px] leading-relaxed"
                >
                  {!detail.live_events || detail.live_events.length === 0 ? (
                    <div className="text-zinc-500">正在等待首个沙箱动作事件写入…</div>
                  ) : (
                    detail.live_events.map((evItem, idx) => {
                      const timeStr = evItem.ts ? evItem.ts.slice(11, 19) : "";
                      return (
                        <div key={idx} className="space-y-0.5 border-l-2 border-zinc-800 pl-2.5">
                          <div className="flex items-start gap-2">
                            <span className="text-zinc-500 shrink-0">{timeStr}</span>
                            <span className="shrink-0">{evItem.icon}</span>
                            <span
                              className={cn(
                                "text-zinc-200",
                                evItem.kind === "thinking" && "text-blue-300",
                                evItem.kind === "thought" && "text-zinc-400 italic",
                                evItem.kind === "gate_rejected" && "text-amber-300",
                                evItem.kind === "error" && "text-red-400",
                                (evItem.kind === "finish_task" || evItem.kind === "deliver") &&
                                  "text-emerald-300 font-semibold"
                              )}
                            >
                              {evItem.title}
                            </span>
                          </div>
                          {evItem.detail && (
                            <div className="pl-16 text-[11px] text-zinc-400 whitespace-pre-wrap break-words">
                              {evItem.detail}
                            </div>
                          )}
                        </div>
                      );
                    })
                  )}
                </div>
              )}
            </div>
          )}

          {/* Clarification Module */}
          {t.lifecycle_status === "WAITING_CLARIFICATION" &&
            c.unresolved_decisions?.length > 0 && (
              <div className="rounded-xl border border-amber-500/40 bg-amber-500/8 p-4 space-y-3.5">
                <div className="flex items-center gap-2 text-amber-700 dark:text-amber-300 text-xs font-semibold">
                  <AlertTriangle className="h-4 w-4 shrink-0" />
                  存在关键行为边界歧义，请确认实现方案
                </div>

                {c.unresolved_decisions.map((dec, qIdx) => (
                  <div
                    key={qIdx}
                    className="rounded-lg border border-amber-500/20 bg-white/90 dark:bg-zinc-900/90 p-3.5 space-y-2"
                  >
                    <div className="text-xs font-medium text-zinc-900 dark:text-zinc-100">
                      <span className="mr-1.5 rounded bg-amber-500/15 px-1.5 py-0.5 font-mono text-[10.5px] text-amber-700 dark:text-amber-300">
                        {dec.impact_scope}
                      </span>
                      {dec.question}
                    </div>
                    <div className="grid gap-1.5 pt-1">
                      {(dec.options ?? []).map((opt) => {
                        const selected = answers[String(qIdx)] === opt;
                        return (
                          <label
                            key={opt}
                            className={cn(
                              "flex items-start gap-2.5 rounded-lg border px-3 py-2 text-xs cursor-pointer transition-colors",
                              selected
                                ? "border-blue-500 bg-blue-500/8 text-zinc-900 dark:text-zinc-100"
                                : "border-zinc-200 dark:border-zinc-800 text-zinc-600 dark:text-zinc-400 hover:bg-zinc-50 dark:hover:bg-zinc-800/50"
                            )}
                          >
                            <input
                              type="radio"
                              name={`q_${qIdx}`}
                              checked={selected}
                              onChange={() =>
                                setAnswers((prev) => ({ ...prev, [String(qIdx)]: opt }))
                              }
                              className="mt-0.5 accent-blue-600"
                            />
                            <span>{opt}</span>
                          </label>
                        );
                      })}
                    </div>
                  </div>
                ))}

                <div className="flex items-center gap-2.5">
                  <Button
                    variant="primary"
                    disabled={busyAction === "clarify"}
                    onClick={() => submitClarify(false)}
                  >
                    按所选方案执行
                  </Button>
                  <Button
                    variant="outline"
                    disabled={busyAction === "clarify"}
                    onClick={() => submitClarify(true)}
                  >
                    按默认假设继续
                  </Button>
                </div>
              </div>
            )}

          {/* Handoff Bundle Alert + One-Click Scope Expansion Recovery */}
          {t.handoff_bundle && (
            <div className="rounded-xl border border-red-500/35 bg-red-500/8 p-4 space-y-2.5 text-xs">
              <div className="flex items-center justify-between gap-2">
                <div className="flex items-center gap-2 font-semibold text-red-700 dark:text-red-300">
                  <ShieldAlert className="h-4 w-4 shrink-0" />
                  任务暂停交接 ({t.handoff_bundle.failure_class})：{t.handoff_bundle.reason}
                </div>
                {t.handoff_bundle.failure_class === "SECURITY_OR_SCOPE_VIOLATION" && (
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={() => setActiveTab("contract")}
                  >
                    <FolderPlus className="h-3.5 w-3.5" />
                    在下方扩充目录范围并续跑
                  </Button>
                )}
              </div>
              <p className="text-zinc-600 dark:text-zinc-400">
                建议下一步：{t.handoff_bundle.recommended_next_step}
              </p>
            </div>
          )}

          {/* Verified No-Op Banner */}
          {ev?.noop_verified && (
            <div className="rounded-xl border border-emerald-500/35 bg-emerald-500/8 p-4 space-y-1.5 text-xs">
              <div className="flex items-center gap-2 font-semibold text-emerald-700 dark:text-emerald-300">
                <CheckCircle2 className="h-4 w-4" />
                沙箱独立实测已通过 · 无需修改代码 (No-Op)
              </div>
              <p className="text-zinc-600 dark:text-zinc-300 leading-relaxed">
                {ev.noop_summary || "目标测试集已在基线分支全部通过，未产生多余改动。"}
              </p>
            </div>
          )}

          {/* Controlled Tabs */}
          <Tabs value={activeTab} onValueChange={setActiveTab}>
            <TabsList className="w-full justify-start flex-wrap h-auto">
              <TabsTrigger value="contract">
                <Sparkles className="h-3.5 w-3.5" />
                契约与权限范围
              </TabsTrigger>
              <TabsTrigger value="evidence">
                <ShieldCheck className="h-3.5 w-3.5" />
                门禁与审查报告
              </TabsTrigger>
              <TabsTrigger value="diff">
                <FileDiff className="h-3.5 w-3.5" />
                代码补丁与日志
              </TabsTrigger>
              <TabsTrigger value="timeline">
                <Clock className="h-3.5 w-3.5" />
                状态轨迹与通知
              </TabsTrigger>
            </TabsList>

            {/* TAB 1: CONTRACT & INTERACTIVE SCOPE MANAGEMENT */}
            <TabsContent value="contract" className="space-y-4">
              <div className="rounded-xl border border-zinc-200 dark:border-zinc-800 p-4 space-y-3">
                <div className="text-[11px] font-semibold uppercase tracking-wider text-zinc-400">
                  任务契约目标 (Contract v{c.contract_version})
                </div>
                <p className="text-sm font-medium leading-relaxed text-zinc-900 dark:text-zinc-100">
                  {c.objective}
                </p>
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 pt-2 border-t border-zinc-100 dark:border-zinc-800/80 text-xs tabular-nums">
                  <div>
                    <span className="text-zinc-400 block text-[11px]">目标仓库</span>
                    <span className="font-mono font-medium">{c.target_repo}</span>
                  </div>
                  <div>
                    <span className="text-zinc-400 block text-[11px]">基线分支 @ Commit</span>
                    <span className="font-mono">
                      {c.base_branch}@{c.base_commit_sha.slice(0, 8)}
                    </span>
                  </div>
                  <div>
                    <span className="text-zinc-400 block text-[11px]">耗时 / 模型费用</span>
                    <span className="font-mono">
                      {t.agent_elapsed_seconds.toFixed(1)}s · ${t.total_cost_usd.toFixed(4)}
                    </span>
                  </div>
                  <div>
                    <span className="text-zinc-400 block text-[11px]">预算上限</span>
                    <span className="font-mono">${c.max_budget_usd.toFixed(2)}</span>
                  </div>
                </div>
              </div>

              {/* Interactive Permitted Scopes Editor */}
              <div className="rounded-xl border border-zinc-200 dark:border-zinc-800 p-4 space-y-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <div className="text-xs font-semibold text-zinc-800 dark:text-zinc-200">
                      允许修改目录白名单 (Permitted Scopes)
                    </div>
                    <p className="text-[11px] text-zinc-500">
                      沙箱写入受限于以下路径前缀；若因跨模块修改被拦截，可在此添加目录并一键恢复执行。
                    </p>
                  </div>
                </div>

                <div className="flex flex-wrap gap-1.5">
                  {editableScopes.map((s) => (
                    <span
                      key={s}
                      className="inline-flex items-center gap-1 rounded-md bg-blue-500/10 px-2 py-1 font-mono text-xs text-blue-700 dark:text-blue-300"
                    >
                      {s}
                      {t.lifecycle_status !== "ACCEPTED_AND_CLOSED" && (
                        <button
                          type="button"
                          onClick={() => removeScopeItem(s)}
                          className="ml-0.5 rounded hover:bg-blue-500/20 p-0.5 cursor-pointer"
                          title="移除此目录"
                        >
                          <X className="h-3 w-3" />
                        </button>
                      )}
                    </span>
                  ))}
                </div>

                {suggestedScopeDirs.length > 0 && (
                  <div className="flex flex-wrap items-center gap-1.5 text-[11px]">
                    <span className="text-amber-600 dark:text-amber-400 font-medium">
                      检测到被拦截路径，点击快速加入白名单：
                    </span>
                    {suggestedScopeDirs.map((dir) => (
                      <button
                        key={dir}
                        type="button"
                        onClick={() => addScopeItem(dir)}
                        className="rounded border border-amber-500/40 bg-amber-500/10 px-2 py-0.5 font-mono text-amber-700 dark:text-amber-300 hover:bg-amber-500/20 cursor-pointer"
                      >
                        + {dir}
                      </button>
                    ))}
                  </div>
                )}

                {t.lifecycle_status !== "ACCEPTED_AND_CLOSED" && (
                  <div className="flex flex-wrap items-center gap-2 pt-1">
                    <input
                      type="text"
                      value={newScopeInput}
                      onChange={(e) => setNewScopeInput(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") {
                          e.preventDefault();
                          addScopeItem();
                        }
                      }}
                      placeholder="输入允许修改的目录前缀（如 src/common/* 或 scripts/*）回车添加"
                      className="h-8 flex-1 min-w-[220px] rounded-lg border border-zinc-200 bg-white px-2.5 font-mono text-xs dark:border-zinc-800 dark:bg-zinc-950"
                    />
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={() => addScopeItem()}
                    >
                      添加目录
                    </Button>

                    {scopesModified && (
                      <>
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          disabled={!!busyAction}
                          onClick={() =>
                            runControl("update_scope", {
                              permitted_scopes: editableScopes,
                            })
                          }
                        >
                          仅保存范围
                        </Button>
                        <Button
                          type="button"
                          variant="primary"
                          size="sm"
                          disabled={!!busyAction}
                          onClick={() =>
                            runControl("update_scope_and_retry", {
                              permitted_scopes: editableScopes,
                              note: "Human expanded permitted_scopes; please complete task.",
                            })
                          }
                        >
                          <Play className="h-3.5 w-3.5" />
                          保存范围并恢复执行
                        </Button>
                      </>
                    )}
                  </div>
                )}
              </div>

              <div className="rounded-xl border border-zinc-200 dark:border-zinc-800 p-4 space-y-2">
                <div className="text-xs font-semibold text-zinc-700 dark:text-zinc-300">
                  验收标准清单 (Acceptance Criteria)
                </div>
                <ul className="space-y-1.5 text-xs text-zinc-600 dark:text-zinc-300">
                  {c.acceptance_criteria.map((ac, idx) => (
                    <li key={idx} className="flex items-start gap-2">
                      <CheckCircle2 className="h-3.5 w-3.5 mt-0.5 shrink-0 text-emerald-500" />
                      <span>{ac}</span>
                    </li>
                  ))}
                </ul>
              </div>
            </TabsContent>

            {/* TAB 2: EVIDENCE & CODE REVIEW */}
            <TabsContent value="evidence" className="space-y-4">
              {!ev ? (
                <div className="rounded-xl border border-dashed border-zinc-200 dark:border-zinc-800 p-10 text-center text-xs text-zinc-500">
                  沙箱尚未运行到门禁阶段，完成后在此展示回归单测证据与独立审查报告
                </div>
              ) : (
                <>
                  <div className="grid grid-cols-2 sm:grid-cols-5 gap-2.5">
                    {[
                      ev.verification_mode_used === "regression_and_build"
                        ? { label: "构建与存量模式适配", ok: ev.repro_test_passed_on_head }
                        : { label: "基线复现失败 (Fail)", ok: ev.repro_test_failed_on_base },
                      { label: "修改后通过 (Pass)", ok: ev.repro_test_passed_on_head },
                      { label: "存量单测全绿", ok: ev.existing_test_suite_passed },
                      { label: "测试零弱化作弊", ok: ev.tests_weakened_check },
                      {
                        label: "目录与静态检查",
                        ok: ev.scope_compliance_check && ev.static_analysis_passed,
                      },
                    ].map((item) => (
                      <div
                        key={item.label}
                        className={cn(
                          "rounded-xl border p-3 text-xs",
                          item.ok
                            ? "border-emerald-500/25 bg-emerald-500/6"
                            : "border-red-500/30 bg-red-500/8"
                        )}
                      >
                        <div className="flex items-center gap-1.5 font-semibold">
                          {item.ok ? (
                            <CheckCircle2 className="h-4 w-4 text-emerald-500" />
                          ) : (
                            <XCircle className="h-4 w-4 text-red-500" />
                          )}
                          <span>{item.ok ? "通过" : "未通过"}</span>
                        </div>
                        <div className="mt-1 text-[11px] text-zinc-500 dark:text-zinc-400">
                          {item.label}
                        </div>
                      </div>
                    ))}
                  </div>

                  {ev.reviewer_report && (
                    <div className="rounded-xl border border-zinc-200 dark:border-zinc-800 p-4 space-y-3 text-xs">
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <span className="font-semibold text-zinc-800 dark:text-zinc-200">
                          独立跨模型 Code Review 报告
                        </span>
                        <div className="flex flex-wrap items-center gap-2">
                          <Button
                            type="button"
                            variant="outline"
                            size="sm"
                            onClick={() => setActiveTab("diff")}
                          >
                            <FileDiff className="h-3 w-3" />
                            对照代码 Diff
                          </Button>
                          <a
                            href={`/reports/${encodeURIComponent(t.task_id)}/review`}
                            target="_blank"
                            rel="noreferrer"
                            className="inline-flex items-center gap-1 rounded-md bg-blue-500/10 px-2 py-1 font-medium text-blue-600 hover:bg-blue-500/20 dark:text-blue-300"
                          >
                            独立全屏报告
                            <ArrowUpRight className="h-3 w-3" />
                          </a>
                          {ev.reviewer_report.reviewer_model && (
                            <code className="rounded bg-purple-500/10 px-2 py-0.5 text-[11px] text-purple-600 dark:text-purple-300">
                              {ev.reviewer_report.reviewer_model}
                            </code>
                          )}
                          <span
                            className={cn(
                              "rounded-full px-2 py-0.5 text-[11px] font-semibold",
                              ev.reviewer_report.verdict === "BLOCK"
                                ? "bg-red-500/15 text-red-600 dark:text-red-400"
                                : "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400"
                            )}
                          >
                            {ev.reviewer_report.verdict ?? "APPROVE"} ·{" "}
                            {(ev.reviewer_report.blocking_issues ?? []).length} 阻断
                          </span>
                        </div>
                      </div>

                      <p className="leading-relaxed text-zinc-700 dark:text-zinc-200 font-medium">
                        {ev.reviewer_report.summary}
                      </p>

                      {ev.reviewer_report.reviewer_fallback_note && (
                        <div className="rounded-lg border border-amber-500/35 bg-amber-500/10 px-3 py-2 text-[11.5px] text-amber-800 dark:text-amber-200">
                          <b>⚠️ 审查模型降级提示：</b>
                          {ev.reviewer_report.reviewer_fallback_note}
                        </div>
                      )}

                      {(ev.reviewer_report.change_overview ?? []).length > 0 && (
                        <div className="rounded-lg bg-zinc-50 dark:bg-zinc-900 p-3 space-y-1">
                          <div className="font-semibold text-zinc-700 dark:text-zinc-300">
                            改动概览：
                          </div>
                          <ul className="list-disc pl-4 space-y-0.5 text-zinc-600 dark:text-zinc-400">
                            {(ev.reviewer_report.change_overview ?? []).map((ln, idx) => (
                              <li key={idx}>{ln}</li>
                            ))}
                          </ul>
                        </div>
                      )}

                      {(ev.reviewer_report.blocking_issues ?? []).length > 0 && (
                        <div className="rounded-lg border border-red-500/30 bg-red-500/8 p-3 space-y-2">
                          <div className="font-semibold text-red-600 dark:text-red-400">
                            阻断级审查问题：
                          </div>
                          {(ev.reviewer_report.blocking_issues ?? []).map((b, idx) => (
                            <div key={idx} className="space-y-0.5 text-red-700 dark:text-red-300">
                              <div>
                                <b>
                                  [{b.severity} · {b.category}]
                                </b>{" "}
                                {b.path ? (
                                  <code className="font-mono">
                                    {b.path}
                                    {b.line ? `:${b.line}` : ""}
                                  </code>
                                ) : null}
                                {" — "}
                                {b.message}
                              </div>
                              {b.suggestion && (
                                <div className="pl-3 text-emerald-700 dark:text-emerald-300">
                                  <b>建议改法：</b>
                                  {b.suggestion}
                                </div>
                              )}
                            </div>
                          ))}
                        </div>
                      )}

                      {(ev.reviewer_report.audit_checklist ?? []).length > 0 && (
                        <div className="overflow-hidden rounded-lg border border-zinc-200 dark:border-zinc-800">
                          <table className="w-full text-left text-[11.5px]">
                            <thead className="bg-zinc-50 dark:bg-zinc-900 text-zinc-500">
                              <tr>
                                <th className="px-3 py-1.5">审查维度</th>
                                <th className="px-3 py-1.5">结论</th>
                                <th className="px-3 py-1.5">核查依据</th>
                              </tr>
                            </thead>
                            <tbody className="divide-y divide-zinc-100 dark:divide-zinc-800/80">
                              {(ev.reviewer_report.audit_checklist ?? []).map((row, idx) => (
                                <tr key={idx}>
                                  <td className="px-3 py-1.5 font-medium">{row.dimension}</td>
                                  <td className="px-3 py-1.5 font-mono font-semibold">
                                    {row.status === "PASS"
                                      ? "PASS"
                                      : row.status === "WARN"
                                        ? "WARN"
                                        : "FAIL"}
                                  </td>
                                  <td className="px-3 py-1.5 text-zinc-600 dark:text-zinc-400">
                                    {row.detail}
                                  </td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </div>
                      )}
                    </div>
                  )}

                  {(ev.reviewer_report?.unverified_risks ?? ev.unverified_risks ?? []).length >
                    0 && (
                    <div className="rounded-xl border border-amber-500/30 bg-amber-500/6 p-4 space-y-2 text-xs">
                      <div className="flex items-center gap-1.5 font-semibold text-amber-700 dark:text-amber-300">
                        <AlertTriangle className="h-4 w-4" />
                        人工审阅重点关注项 (Unverified Risks)
                      </div>
                      <ul className="list-disc pl-5 space-y-1 text-zinc-700 dark:text-zinc-300">
                        {(ev.reviewer_report?.unverified_risks ?? ev.unverified_risks ?? []).map(
                          (r, i) => (
                            <li key={i}>{r}</li>
                          )
                        )}
                      </ul>
                    </div>
                  )}
                </>
              )}
            </TabsContent>

            {/* TAB 3: DIFF & LOGS */}
            <TabsContent value="diff">
              <DiffViewer diff={detail.diff} verificationLog={detail.verification_log} />
            </TabsContent>

            {/* TAB 4: HUMAN-READABLE TIMELINE & COLLAPSIBLE CARD DEBUG */}
            <TabsContent value="timeline" className="space-y-4">
              <div className="rounded-xl border border-zinc-200 dark:border-zinc-800 p-4 max-h-[380px] overflow-y-auto">
                <div className="text-xs font-semibold text-zinc-700 dark:text-zinc-300 mb-3">
                  任务状态流转时间线 ({detail.checkpoints.length} 个节点)
                </div>
                <ol className="relative border-l border-zinc-200 dark:border-zinc-800 ml-2 space-y-3">
                  {detail.checkpoints.map((cp) => (
                    <li key={cp.id} className="ml-4 text-xs">
                      <span className="absolute -left-1.5 mt-1 h-2.5 w-2.5 rounded-full border border-white bg-blue-500 dark:border-zinc-950" />
                      <div className="flex flex-wrap items-center gap-2 text-[11.5px]">
                        <span className="font-mono text-zinc-400 tabular-nums">
                          {cp.created_at.slice(11, 19)}
                        </span>
                        <span className="font-medium text-zinc-800 dark:text-zinc-200">
                          {formatStepLabel(cp.step_name)}
                        </span>
                        <code className="text-[10px] text-zinc-400">{cp.step_name}</code>
                      </div>
                      {cp.note && (
                        <p className="mt-0.5 text-[11px] text-zinc-500 dark:text-zinc-400 break-all">
                          {cp.note.slice(0, 160)}
                        </p>
                      )}
                    </li>
                  ))}
                </ol>
              </div>

              <details className="group rounded-xl border border-zinc-200 dark:border-zinc-800 p-4">
                <summary className="cursor-pointer text-xs font-semibold text-zinc-600 dark:text-zinc-400 flex items-center justify-between select-none">
                  <span>查看当前阶段推送的飞书交互卡片预览</span>
                  <ChevronDown className="h-4 w-4 text-zinc-400 transition-transform group-open:rotate-180" />
                </summary>
                <div className="mt-3">
                  <FeishuCardPreview payload={detail.feishu_card_preview} />
                </div>
              </details>
            </TabsContent>
          </Tabs>
        </div>

        {/* Sticky Bottom Follow-Up & Engineering Guardrail Bar */}
        <div className="border-t border-zinc-200 dark:border-zinc-800 bg-zinc-50/80 dark:bg-zinc-900/50 px-6 py-3.5 space-y-3">
          <form onSubmit={submitFollowup} className="flex items-center gap-2">
            <div className="relative flex-1">
              <MessageSquarePlus className="absolute left-3 top-2.5 h-4 w-4 text-zinc-400" />
              <input
                type="text"
                value={followupText}
                onChange={(e) => setFollowupText(e.target.value)}
                placeholder="输入追加修改意见（唤醒原沙箱增量修改代码并同步更新 PR）..."
                className="h-9 w-full rounded-lg border border-zinc-200 bg-white pl-9 pr-3 text-xs text-zinc-900 placeholder:text-zinc-400 focus:border-blue-500 focus:outline-none dark:border-zinc-800 dark:bg-zinc-950 dark:text-zinc-100"
              />
            </div>
            <Button type="submit" variant="primary" disabled={busyAction === "followup"}>
              <Send className="h-3.5 w-3.5" />
              发送追改
            </Button>
            <Button
              type="button"
              variant="outline"
              onClick={() => setShowLedgerForm((v) => !v)}
            >
              <BookMarked className="h-3.5 w-3.5" />
              沉淀工程红线
            </Button>
          </form>

          {showLedgerForm && (
            <form
              onSubmit={submitRule}
              className="flex flex-col sm:flex-row items-center gap-2 pt-1 animate-in fade-in"
            >
              <input
                type="text"
                value={ruleTitle}
                onChange={(e) => setRuleTitle(e.target.value)}
                placeholder="规则标题（如：禁止导入 legacy）"
                className="h-8 w-full sm:w-48 rounded-lg border border-zinc-200 bg-white px-2.5 text-xs dark:border-zinc-800 dark:bg-zinc-950"
              />
              <input
                type="text"
                value={ruleScope}
                onChange={(e) => setRuleScope(e.target.value)}
                placeholder="目录范围 src/..."
                className="h-8 w-full sm:w-36 rounded-lg border border-zinc-200 bg-white px-2.5 font-mono text-xs dark:border-zinc-800 dark:bg-zinc-950"
              />
              <input
                type="text"
                value={ruleText}
                onChange={(e) => setRuleText(e.target.value)}
                placeholder="输入长期研发工程红线（如 import 禁令、构建必跑脚本；请勿填写单次产品需求）..."
                className="h-8 flex-1 w-full rounded-lg border border-zinc-200 bg-white px-2.5 text-xs dark:border-zinc-800 dark:bg-zinc-950"
              />
              <Button type="submit" variant="emerald" size="sm" disabled={busyAction === "rule"}>
                保存红线
              </Button>
            </form>
          )}
        </div>
      </div>
    </div>
  );
}
