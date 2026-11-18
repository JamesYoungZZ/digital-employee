import React from "react";
import {
  AlertCircle,
  ArrowUpRight,
  BellElectric,
  BellRing,
  CheckCircle2,
  Coins,
  Command,
  FileDiff,
  FolderGit2,
  GitBranch,
  GitMerge,
  GitPullRequest,
  Loader2,
  MessageSquare,
  Monitor,
  Search,
  ShieldCheck,
  Sparkles,
  Terminal,
  TicketCheck,
  Trash2,
  XCircle,
} from "lucide-react";
import { toast } from "sonner";
import type {
  HealthInfo,
  JiraIssuePreview,
  MetricsInfo,
  RepositoryItem,
  TaskItem,
} from "../types/api";
import type { SettingsSubTab } from "./SettingsCenterView";
import { apiFetch, cn } from "../lib/utils";
import { Button, StatusBadge, SurfaceCard, Switch } from "../components/ui/primitives";

interface Props {
  health?: HealthInfo | null;
  metrics: MetricsInfo | null;
  repos: RepositoryItem[];
  tasks: TaskItem[];
  selectedTaskId: string | null;
  onSelectTask: (taskId: string) => void;
  onCreatedTask: (taskId: string) => Promise<void>;
  onMutated: () => Promise<void>;
  onOpenSettings?: (subTab?: SettingsSubTab) => void;
  onOpenRepos?: () => void;
}

const FILTERS = [
  { id: "ALL", label: "全部" },
  { id: "WAITING_MERGE_APPROVAL", label: "待合入" },
  { id: "WAITING_JIRA_APPROVAL", label: "待建 Jira" },
  { id: "WAITING_CLARIFICATION", label: "待确认" },
  { id: "ACTIVE", label: "运行中" },
  { id: "READY", label: "等待 CI" },
  { id: "ACCEPTED_AND_CLOSED", label: "已完成" },
  { id: "HANDED_OFF_INCOMPLETE", label: "已交接" },
] as const;

const SAMPLE_PROMPTS = [
  {
    repo: "livestream-copilot",
    text: "直播间观众在公屏诱导私加微信或套话让助播承诺免单时，需拦截此类回复并引导联系官方客服",
  },
  {
    repo: "modular-rag",
    text: "搜索带连字符的商品科技词（如 Gore-Tex、Dri-FIT）时漏召回在售商品，请排查分词检索逻辑并补齐单测",
  },
];

function normalizeJiraKeyInput(raw: string): string {
  const txt = raw.trim();
  const urlMatch = txt.match(/\/browse\/([A-Za-z][A-Za-z0-9_]{1,9}-\d+)\b/);
  if (urlMatch) return urlMatch[1].toUpperCase();
  const keyMatch = txt.match(/\b([A-Za-z][A-Za-z0-9_]{1,9}-\d+)\b/);
  return keyMatch ? keyMatch[1].toUpperCase() : txt.toUpperCase();
}

function matchesStatusFilter(t: TaskItem, filter: string): boolean {
  if (filter === "ALL") return true;
  if (filter === "ACTIVE") {
    return [
      "DRAFTING_CONTRACT",
      "QUEUED",
      "RUNNING_IN_SANDBOX",
      "VERIFYING_EVIDENCE",
    ].includes(t.lifecycle_status);
  }
  if (filter === "READY") {
    return ["PR_OPEN_FOLLOWING_UP", "DELIVERED_LOCAL_READY"].includes(t.lifecycle_status);
  }
  return t.lifecycle_status === filter;
}

interface EditableJiraDraftState {
  target_repo: string;
  project_key: string;
  issue_type: string;
  priority: string;
  summary: string;
  description: string;
  ac_text: string;
  refine_instruction: string;
}

export function WorkbenchView({
  health,
  metrics,
  repos,
  tasks,
  selectedTaskId,
  onSelectTask,
  onCreatedTask,
  onMutated,
  onOpenSettings,
  onOpenRepos,
}: Props) {
  // Composer state (`targetRepo === ""` means Smart Auto Repo Routing!)
  const [targetRepo, setTargetRepo] = React.useState("");
  const [baseBranch, setBaseBranch] = React.useState("");
  const [repoBranches, setRepoBranches] = React.useState<string[]>([]);
  const [maxBudget, setMaxBudget] = React.useState<string>("1.5");
  const [delegateMode, setDelegateMode] = React.useState<"raw" | "existing_jira">("raw");
  const [prompt, setPrompt] = React.useState("");
  const [existingJiraKey, setExistingJiraKey] = React.useState("");
  const [jiraPreview, setJiraPreview] = React.useState<JiraIssuePreview | null>(null);
  const [loadingJiraPreview, setLoadingJiraPreview] = React.useState(false);

  const [requireJiraApproval, setRequireJiraApproval] = React.useState<boolean>(() => {
    return localStorage.getItem("de_require_jira_draft") === "true";
  });
  const [publishPr, setPublishPr] = React.useState(true);
  const [acceptDefaults, setAcceptDefaults] = React.useState(false);
  const [submitting, setSubmitting] = React.useState(false);

  // Queue filter, search & pagination state
  const [statusFilter, setStatusFilter] = React.useState<string>("ALL");
  const [repoFilter, setRepoFilter] = React.useState<string>("ALL");
  const [channelFilter, setChannelFilter] = React.useState<string>("ALL");
  const [searchQuery, setSearchQuery] = React.useState<string>("");
  const [visibleCount, setVisibleCount] = React.useState<number>(25);

  // Action busy state
  const [mergingId, setMergingId] = React.useState<string | null>(null);
  const [deletingId, setDeletingId] = React.useState<string | null>(null);
  const [draftEdits, setDraftEdits] = React.useState<Record<string, EditableJiraDraftState>>({});

  React.useEffect(() => {
    if (health?.default_max_budget_usd) {
      setMaxBudget(String(health.default_max_budget_usd));
    }
  }, [health?.default_max_budget_usd]);

  // Load branches whenever an explicit targetRepo is selected
  React.useEffect(() => {
    if (!targetRepo) {
      setRepoBranches([]);
      setBaseBranch("");
      return;
    }
    const repoObj = repos.find((r) => r.repo_id === targetRepo);
    if (repoObj) {
      setBaseBranch(repoObj.default_base_branch);
      setRepoBranches([repoObj.default_base_branch]);
    }
    let cancelled = false;
    apiFetch<{ default_base_branch: string; branches: string[] }>(
      `/api/repositories/${encodeURIComponent(targetRepo)}/branches`
    )
      .then((res) => {
        if (cancelled) return;
        setRepoBranches(res.branches.length ? res.branches : [res.default_base_branch]);
        setBaseBranch(res.default_base_branch);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [targetRepo, repos]);

  const pendingJiraTasks = React.useMemo(
    () => tasks.filter((t) => t.lifecycle_status === "WAITING_JIRA_APPROVAL"),
    [tasks]
  );

  React.useEffect(() => {
    setDraftEdits((prev) => {
      const next = { ...prev };
      for (const t of pendingJiraTasks) {
        if (!next[t.task_id]) {
          const d = t.jira_draft;
          next[t.task_id] = {
            target_repo: t.contract.target_repo,
            project_key: d?.project_key || health?.jira_default_project || "PROJ",
            issue_type: d?.issue_type || "Task",
            priority: d?.priority || "Medium",
            summary: d?.summary || t.contract.objective,
            description: d?.description || t.contract.reproduction_evidence || t.raw_request,
            ac_text: (d?.acceptance_criteria || t.contract.acceptance_criteria || []).join("\n"),
            refine_instruction: "",
          };
        }
      }
      return next;
    });
  }, [pendingJiraTasks, health?.jira_default_project]);

  const pendingApprovalTasks = React.useMemo(
    () => tasks.filter((t) => t.lifecycle_status === "WAITING_MERGE_APPROVAL"),
    [tasks]
  );

  const pendingReviewFixTasks = React.useMemo(
    () =>
      tasks.filter(
        (t) =>
          !t.worker_alive &&
          !["ACCEPTED_AND_CLOSED", "ABANDONED_OR_REVERTED", "WAITING_MERGE_APPROVAL"].includes(
            t.lifecycle_status
          ) &&
          t.verification_evidence?.reviewer_report?.verdict === "BLOCK"
      ),
    [tasks]
  );

  const handlePreviewJira = async () => {
    const key = normalizeJiraKeyInput(existingJiraKey);
    if (!key) return;
    setLoadingJiraPreview(true);
    try {
      const p = await apiFetch<JiraIssuePreview>(
        `/api/integrations/jira/issue/${encodeURIComponent(key)}`
      );
      setJiraPreview(p);
    } catch (err: any) {
      toast.error(err.message);
    } finally {
      setLoadingJiraPreview(false);
    }
  };

  const handleSubmit = async (e?: React.FormEvent) => {
    e?.preventDefault();
    if (!repos.length) return;
    if (delegateMode === "existing_jira" && !existingJiraKey.trim()) return;
    if (delegateMode === "raw" && !prompt.trim()) return;

    setSubmitting(true);
    try {
      const parsedBudget = parseFloat(maxBudget);
      const budgetVal = Number.isFinite(parsedBudget) && parsedBudget > 0 ? parsedBudget : undefined;
      const normalizedKey =
        delegateMode === "existing_jira" ? normalizeJiraKeyInput(existingJiraKey) : "";

      const body =
        delegateMode === "existing_jira"
          ? {
              target_repo: targetRepo,
              base_branch: targetRepo && baseBranch ? baseBranch : null,
              max_budget_usd: budgetVal,
              raw_request: normalizedKey,
              jira_issue_key: normalizedKey,
              publish_pr: publishPr,
              auto_merge: false,
              accept_defaults: acceptDefaults,
              auto_execute: true,
            }
          : {
              target_repo: targetRepo,
              base_branch: targetRepo && baseBranch ? baseBranch : null,
              max_budget_usd: budgetVal,
              raw_request: prompt.trim(),
              require_jira_approval: requireJiraApproval,
              publish_pr: publishPr,
              auto_merge: false,
              accept_defaults: acceptDefaults,
              auto_execute: true,
            };

      const created = await apiFetch<TaskItem>("/api/tasks", {
        method: "POST",
        body: JSON.stringify(body),
      });
      setPrompt("");
      setExistingJiraKey("");
      setJiraPreview(null);
      if (created.lifecycle_status === "WAITING_JIRA_APPROVAL") {
        toast.success(`已起草 Jira 工单草稿 (${created.task_id})，请在下方审核确认后建卡开工`);
      } else if (created.jira_issue_key) {
        toast.success(`已拉取工单 ${created.jira_issue_key}，沙箱开始执行 (${created.task_id})`);
      } else {
        toast.success(`已生成任务契约 ${created.task_id} (${created.contract.target_repo})`);
      }
      await onCreatedTask(created.task_id);
    } catch (err: any) {
      toast.error(err.message);
    } finally {
      setSubmitting(false);
    }
  };

  const handleRefineJiraDraft = async (taskId: string) => {
    const edit = draftEdits[taskId];
    if (!edit?.refine_instruction.trim()) return;
    setMergingId(taskId);
    try {
      const updated = await apiFetch<TaskItem>(
        `/api/tasks/${encodeURIComponent(taskId)}/refine-jira`,
        {
          method: "POST",
          body: JSON.stringify({ instruction: edit.refine_instruction.trim() }),
        }
      );
      if (updated.jira_draft) {
        setDraftEdits((prev) => ({
          ...prev,
          [taskId]: {
            target_repo: updated.contract.target_repo,
            project_key: updated.jira_draft?.project_key || edit.project_key,
            issue_type: updated.jira_draft?.issue_type || edit.issue_type,
            priority: updated.jira_draft?.priority || edit.priority,
            summary: updated.jira_draft?.summary || edit.summary,
            description: updated.jira_draft?.description || edit.description,
            ac_text: (updated.jira_draft?.acceptance_criteria || []).join("\n") || edit.ac_text,
            refine_instruction: "",
          },
        }));
      }
      toast.success("AI 已按修改意见重拟工单草稿");
      await onMutated();
    } catch (err: any) {
      toast.error(err.message);
    } finally {
      setMergingId(null);
    }
  };

  const handleApproveJiraDraft = async (taskId: string) => {
    const edit = draftEdits[taskId];
    setMergingId(taskId);
    try {
      const created = await apiFetch<TaskItem>(
        `/api/tasks/${encodeURIComponent(taskId)}/approve-jira`,
        {
          method: "POST",
          body: JSON.stringify({
            target_repo: edit?.target_repo || "",
            project_key: edit?.project_key || "",
            issue_type: edit?.issue_type || "Task",
            priority: edit?.priority || "Medium",
            summary: edit?.summary || "",
            description: edit?.description || "",
            acceptance_criteria: edit
              ? edit.ac_text
                  .split("\n")
                  .map((s) => s.trim())
                  .filter(Boolean)
              : undefined,
            auto_execute: true,
          }),
        }
      );
      toast.success(
        `已创建 Jira 工单 ${created.jira_issue_key || ""} 并启动沙箱执行`
      );
      await onMutated();
      onSelectTask(taskId);
    } catch (err: any) {
      toast.error(err.message);
    } finally {
      setMergingId(null);
    }
  };

  const handleApproveMerge = async (t: TaskItem) => {
    const prLabel = t.deliverable_pr_number ? `PR #${t.deliverable_pr_number}` : t.branch_name;
    if (
      !window.confirm(
        `确认批准并将 ${prLabel} 自动 Squash Merge 合入 ${t.contract.target_repo} (${t.contract.base_branch}) 主干吗？`
      )
    ) {
      return;
    }
    setMergingId(t.task_id);
    try {
      await apiFetch(`/api/tasks/${encodeURIComponent(t.task_id)}/control`, {
        method: "POST",
        body: JSON.stringify({ action: "approve_merge", note: "console_approval_card" }),
      });
      toast.success("已批准并在 GitHub 完成 Squash Merge 合入");
      await onMutated();
    } catch (err: any) {
      toast.error(`合入失败: ${err.message}`);
    } finally {
      setMergingId(null);
    }
  };

  const handleRejectTask = async (taskId: string, actionLabel: string) => {
    if (!window.confirm(`确认${actionLabel}并将任务 ${taskId} 标记为已废弃吗？`)) {
      return;
    }
    setMergingId(taskId);
    try {
      await apiFetch(`/api/tasks/${encodeURIComponent(taskId)}/control`, {
        method: "POST",
        body: JSON.stringify({ action: "abandon", note: "rejected_on_console_card" }),
      });
      toast.info(`已废弃任务 ${taskId}`);
      await onMutated();
    } catch (err: any) {
      toast.error(err.message);
    } finally {
      setMergingId(null);
    }
  };

  const handleAutoFixReview = async (taskId: string) => {
    setMergingId(taskId);
    try {
      await apiFetch(`/api/tasks/${encodeURIComponent(taskId)}/control`, {
        method: "POST",
        body: JSON.stringify({ action: "autofix_review" }),
      });
      toast.success(`已唤醒沙箱按 Review 意见自动修复 ${taskId}`);
      onSelectTask(taskId);
      await onMutated();
    } catch (err: any) {
      toast.error(err.message);
    } finally {
      setMergingId(null);
    }
  };

  const handleDeleteTask = async (e: React.MouseEvent, t: TaskItem) => {
    e.stopPropagation();
    if (!window.confirm(`确认永久删除任务记录 ${t.task_id} 及其本地日志产物吗？`)) {
      return;
    }
    setDeletingId(t.task_id);
    try {
      await apiFetch(`/api/tasks/${encodeURIComponent(t.task_id)}`, {
        method: "DELETE",
      });
      toast.success(`已删除任务记录 ${t.task_id}`);
      await onMutated();
    } catch (err: any) {
      toast.error(err.message);
    } finally {
      setDeletingId(null);
    }
  };

  const filteredTasks = React.useMemo(() => {
    const qLower = searchQuery.trim().toLowerCase();
    return tasks.filter((t) => {
      if (!matchesStatusFilter(t, statusFilter)) return false;
      if (repoFilter !== "ALL" && t.contract.target_repo !== repoFilter) return false;
      if (channelFilter !== "ALL") {
        if (channelFilter === "feishu" && !t.source_channel.startsWith("feishu")) return false;
        if (channelFilter === "jira" && !t.source_channel.startsWith("jira") && !t.jira_issue_key)
          return false;
        if (channelFilter === "alert" && t.source_channel !== "alert_webhook") return false;
        if (channelFilter === "web" && !["web", "cli"].includes(t.source_channel)) return false;
      }
      if (qLower) {
        const hay = [
          t.task_id,
          t.jira_issue_key || "",
          t.deliverable_pr_number ? `pr #${t.deliverable_pr_number} #${t.deliverable_pr_number}` : "",
          t.branch_name,
          t.contract.target_repo,
          t.contract.objective,
          t.raw_request,
        ]
          .join(" ")
          .toLowerCase();
        if (!hay.includes(qLower)) return false;
      }
      return true;
    });
  }, [tasks, statusFilter, repoFilter, channelFilter, searchQuery]);

  const displayedTasks = filteredTasks.slice(0, visibleCount);

  return (
    <div className="space-y-5">
      {/* Missing Essential Setup Prompts */}
      {health && health.llm_api_key_configured === false && (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-amber-500/40 bg-amber-500/10 px-4 py-3 text-xs text-amber-800 dark:text-amber-200">
          <div className="flex items-center gap-2">
            <AlertCircle className="h-4 w-4 shrink-0" />
            <span>尚未配置主编码大模型 API Key，任务派发后将无法执行推理。</span>
          </div>
          {onOpenSettings && (
            <Button variant="primary" size="sm" onClick={() => onOpenSettings("models")}>
              前往配置模型与密钥
            </Button>
          )}
        </div>
      )}

      {repos.length === 0 && (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-blue-500/35 bg-blue-500/8 px-4 py-3 text-xs text-blue-800 dark:text-blue-200">
          <div className="flex items-center gap-2">
            <FolderGit2 className="h-4 w-4 shrink-0" />
            <span>尚未接入任何业务代码仓库，请先挂载本地或 GitHub 仓库再派发任务。</span>
          </div>
          {onOpenRepos && (
            <Button variant="primary" size="sm" onClick={onOpenRepos}>
              前往接入代码仓库
            </Button>
          )}
        </div>
      )}

      {/* 1. TOP-OF-PAGE DELEGATION COMPOSER */}
      <SurfaceCard className="p-5">
        <form onSubmit={handleSubmit} className="space-y-3.5">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="inline-flex items-center gap-1 rounded-lg bg-zinc-100 p-1 dark:bg-zinc-800/80">
              <button
                type="button"
                onClick={() => setDelegateMode("raw")}
                className={cn(
                  "inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-medium transition-all cursor-pointer",
                  delegateMode === "raw"
                    ? "bg-white text-zinc-900 shadow-2xs dark:bg-zinc-950 dark:text-zinc-100"
                    : "text-zinc-500 hover:text-zinc-800 dark:text-zinc-400 dark:hover:text-zinc-200"
                )}
              >
                <Sparkles className="h-3.5 w-3.5 text-blue-500" />
                描述需求 / Bug 派发
              </button>
              <button
                type="button"
                onClick={() => setDelegateMode("existing_jira")}
                className={cn(
                  "inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-medium transition-all cursor-pointer",
                  delegateMode === "existing_jira"
                    ? "bg-white text-zinc-900 shadow-2xs dark:bg-zinc-950 dark:text-zinc-100"
                    : "text-zinc-500 hover:text-zinc-800 dark:text-zinc-400 dark:hover:text-zinc-200"
                )}
              >
                <TicketCheck className="h-3.5 w-3.5 text-emerald-500" />
                按已有 Jira 工单开工
              </button>
            </div>

            <div className="flex items-center gap-1.5 text-[11px] text-zinc-400">
              <Command className="h-3 w-3" />
              <span>⌘ + Enter 快速派发</span>
            </div>
          </div>

          {delegateMode === "existing_jira" ? (
            <div className="space-y-2.5">
              <div className="flex flex-wrap gap-2">
                <input
                  type="text"
                  value={existingJiraKey}
                  onChange={(e) => {
                    setExistingJiraKey(e.target.value);
                    if (jiraPreview) setJiraPreview(null);
                  }}
                  placeholder="输入已有 Jira 单号（如 KAN-12）或粘贴完整链接 https://.../browse/KAN-12"
                  className="h-10 flex-1 min-w-[240px] rounded-xl border border-zinc-200 bg-zinc-50/50 px-3.5 font-mono text-xs text-zinc-900 placeholder:font-sans placeholder:text-zinc-400 focus:border-blue-500 focus:bg-white focus:outline-none dark:border-zinc-800 dark:bg-zinc-950 dark:text-zinc-100"
                  required
                />
                <Button
                  type="button"
                  variant="outline"
                  disabled={!existingJiraKey.trim() || loadingJiraPreview}
                  onClick={handlePreviewJira}
                >
                  {loadingJiraPreview ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <Search className="h-3.5 w-3.5" />
                  )}
                  预览工单内容
                </Button>
                {!health?.jira_configured && onOpenSettings && (
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    onClick={() => onOpenSettings("jira")}
                  >
                    配置 Jira 凭证 →
                  </Button>
                )}
              </div>

              {jiraPreview && (
                <div className="rounded-xl border border-blue-500/30 bg-blue-500/5 p-3.5 space-y-1.5 text-xs">
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-2">
                      <a
                        href={jiraPreview.url}
                        target="_blank"
                        rel="noreferrer"
                        className="rounded bg-blue-600 px-2 py-0.5 font-mono text-[11px] font-semibold text-white hover:bg-blue-500 inline-flex items-center gap-1"
                      >
                        {jiraPreview.key} <ArrowUpRight className="h-3 w-3" />
                      </a>
                      <span className="text-[11px] text-zinc-500">
                        {jiraPreview.issue_type} · {jiraPreview.priority} · {jiraPreview.status}
                      </span>
                    </div>
                  </div>
                  <div className="font-semibold text-zinc-900 dark:text-zinc-100">
                    {jiraPreview.summary}
                  </div>
                  {jiraPreview.description && (
                    <p className="line-clamp-3 text-zinc-600 dark:text-zinc-300 whitespace-pre-wrap">
                      {jiraPreview.description}
                    </p>
                  )}
                </div>
              )}
            </div>
          ) : (
            <>
              <textarea
                rows={3}
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                onKeyDown={(e) => {
                  if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
                    e.preventDefault();
                    handleSubmit();
                  }
                }}
                placeholder="描述需要实现的研发改动、报错堆栈或回归测试路径..."
                className="w-full resize-none rounded-xl border border-zinc-200 bg-zinc-50/50 p-3.5 text-sm text-zinc-900 placeholder:text-zinc-400 focus:border-blue-500 focus:bg-white focus:outline-none focus:ring-3 focus:ring-blue-500/12 dark:border-zinc-800 dark:bg-zinc-950/70 dark:text-zinc-100 dark:focus:bg-zinc-950"
                required
              />

              {tasks.length === 0 && (
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="text-[11px] text-zinc-400 mr-1">示例体验：</span>
                  {SAMPLE_PROMPTS.map((sp) => (
                    <button
                      key={sp.text}
                      type="button"
                      onClick={() => {
                        setPrompt(sp.text);
                        if (repos.some((r) => r.repo_id === sp.repo)) {
                          setTargetRepo(sp.repo);
                        }
                      }}
                      className="rounded-md border border-zinc-200/80 bg-zinc-50 px-2.5 py-1 text-[11.5px] text-zinc-600 hover:border-zinc-300 hover:text-zinc-900 dark:border-zinc-800 dark:bg-zinc-900 dark:text-zinc-400 dark:hover:text-zinc-200 cursor-pointer"
                    >
                      {sp.text.slice(0, 36)}…
                    </button>
                  ))}
                </div>
              )}
            </>
          )}

          {/* Composer Controls Bar */}
          <div className="flex flex-wrap items-center justify-between gap-3 pt-2 border-t border-zinc-100 dark:border-zinc-800/80">
            <div className="flex flex-wrap items-center gap-3.5 text-xs">
              <div className="flex items-center gap-1.5">
                <span className="text-zinc-500">仓库</span>
                <select
                  value={targetRepo}
                  onChange={(e) => setTargetRepo(e.target.value)}
                  className="h-8 rounded-lg border border-zinc-200 bg-white px-2.5 font-mono text-xs text-zinc-800 dark:border-zinc-800 dark:bg-zinc-950 dark:text-zinc-200"
                >
                  <option value="">✨ 智能自动路由仓库</option>
                  {repos.map((r) => (
                    <option key={r.repo_id} value={r.repo_id}>
                      {r.repo_id} ({r.default_base_branch})
                    </option>
                  ))}
                </select>
              </div>

              {targetRepo && repoBranches.length > 0 && (
                <div className="flex items-center gap-1.5">
                  <GitBranch className="h-3.5 w-3.5 text-zinc-400" />
                  <select
                    value={baseBranch}
                    onChange={(e) => setBaseBranch(e.target.value)}
                    title="选择任务拉取的基线分支（PR 也将合入该分支）"
                    className="h-8 rounded-lg border border-zinc-200 bg-white px-2 font-mono text-xs text-zinc-800 dark:border-zinc-800 dark:bg-zinc-950 dark:text-zinc-200"
                  >
                    {repoBranches.map((b) => (
                      <option key={b} value={b}>
                        {b}
                      </option>
                    ))}
                  </select>
                </div>
              )}

              <div className="flex items-center gap-1.5">
                <Coins className="h-3.5 w-3.5 text-zinc-400" />
                <select
                  value={maxBudget}
                  onChange={(e) => setMaxBudget(e.target.value)}
                  title="单任务模型推理预算上限"
                  className="h-8 rounded-lg border border-zinc-200 bg-white px-2 font-mono text-xs text-zinc-800 dark:border-zinc-800 dark:bg-zinc-950 dark:text-zinc-200"
                >
                  <option value="0.5">$0.50 上限</option>
                  <option value="1.5">$1.50 上限</option>
                  <option value="3.0">$3.00 上限</option>
                  <option value="5.0">$5.00 上限</option>
                </select>
              </div>

              <label className="flex items-center gap-1.5 text-zinc-600 dark:text-zinc-400 cursor-pointer select-none">
                <Switch checked={publishPr} onCheckedChange={setPublishPr} />
                <span>验证通过后推送 PR</span>
              </label>

              {delegateMode === "raw" && (
                <label className="flex items-center gap-1.5 text-zinc-600 dark:text-zinc-400 cursor-pointer select-none">
                  <Switch
                    checked={requireJiraApproval}
                    onCheckedChange={(v) => {
                      setRequireJiraApproval(v);
                      localStorage.setItem("de_require_jira_draft", v ? "true" : "false");
                    }}
                  />
                  <span>先审建 Jira 卡</span>
                </label>
              )}

              <label className="flex items-center gap-1.5 text-zinc-600 dark:text-zinc-400 cursor-pointer select-none">
                <Switch checked={acceptDefaults} onCheckedChange={setAcceptDefaults} />
                <span>轻微歧义默认继续</span>
              </label>
            </div>

            <Button type="submit" variant="primary" size="lg" disabled={submitting || !repos.length}>
              {submitting ? (
                <>
                  <Loader2 className="h-4 w-4 animate-spin" />
                  正在启动任务...
                </>
              ) : delegateMode === "existing_jira" ? (
                <>
                  <TicketCheck className="h-4 w-4" />
                  拉取 Jira 并开工
                </>
              ) : requireJiraApproval ? (
                <>
                  <TicketCheck className="h-4 w-4" />
                  起草 Jira 草稿
                </>
              ) : (
                <>
                  <Sparkles className="h-4 w-4" />
                  派发编码任务
                </>
              )}
            </Button>
          </div>
        </form>
      </SurfaceCard>

      {/* 2. ACTIONABLE PENDING INBOX */}
      {(pendingApprovalTasks.length > 0 ||
        pendingReviewFixTasks.length > 0 ||
        pendingJiraTasks.length > 0) && (
        <div className="space-y-3">
          {pendingApprovalTasks.map((t) => {
            const mc = t.merge_approval_card;
            const ev = t.verification_evidence;
            const busy = mergingId === t.task_id;
            return (
              <div
                key={t.task_id}
                className="rounded-xl border border-emerald-500/45 bg-gradient-to-r from-emerald-500/8 via-white to-white p-4 shadow-xs dark:from-emerald-500/12 dark:via-zinc-900 dark:to-zinc-900 space-y-2.5"
              >
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div className="space-y-0.5 min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="inline-flex items-center gap-1 rounded-md bg-emerald-600 px-2 py-0.5 text-[11px] font-semibold text-white">
                        <ShieldCheck className="h-3 w-3" />
                        待审批合入
                      </span>
                      <span className="font-mono text-xs font-semibold text-zinc-800 dark:text-zinc-200">
                        {t.task_id}
                      </span>
                      <code className="rounded bg-zinc-100 dark:bg-zinc-800 px-1.5 py-0.5 text-[11px] text-zinc-600 dark:text-zinc-300">
                        {t.contract.target_repo} ({t.branch_name} → {t.contract.base_branch})
                      </code>
                      {t.deliverable_pr_url && (
                        <a
                          href={t.deliverable_pr_url}
                          target="_blank"
                          rel="noreferrer"
                          className="inline-flex items-center gap-1 text-xs font-medium text-blue-600 hover:underline dark:text-blue-400"
                        >
                          PR #{t.deliverable_pr_number}
                          <ArrowUpRight className="h-3.5 w-3.5" />
                        </a>
                      )}
                      <span className="text-[11px] text-emerald-600 dark:text-emerald-400">
                        ✓ 单测通过 · Review {mc?.reviewer_model || ev?.reviewer_report?.reviewer_model} 通过 · CI 全绿
                      </span>
                    </div>
                    <div className="text-sm font-semibold text-zinc-900 dark:text-zinc-100 truncate">
                      {t.contract.objective}
                    </div>
                  </div>

                  <div className="flex flex-wrap items-center gap-2 shrink-0">
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => onSelectTask(t.task_id)}
                    >
                      <FileDiff className="h-3.5 w-3.5" />
                      审阅 Review 与 Diff
                    </Button>
                    <Button
                      variant="emerald"
                      size="sm"
                      disabled={busy}
                      onClick={() => handleApproveMerge(t)}
                    >
                      {busy ? (
                        <Loader2 className="h-3.5 w-3.5 animate-spin" />
                      ) : (
                        <GitMerge className="h-3.5 w-3.5" />
                      )}
                      批准 Squash 合入
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      disabled={busy}
                      onClick={() => handleRejectTask(t.task_id, "拒绝合入")}
                    >
                      拒绝
                    </Button>
                  </div>
                </div>
              </div>
            );
          })}

          {pendingReviewFixTasks.map((t) => {
            const rep = t.verification_evidence?.reviewer_report;
            const issues = rep?.blocking_issues ?? [];
            const busy = mergingId === t.task_id;
            return (
              <div
                key={t.task_id}
                className="rounded-xl border border-amber-500/45 bg-gradient-to-r from-amber-500/8 via-white to-white p-4 shadow-xs dark:from-amber-500/12 dark:via-zinc-900 dark:to-zinc-900 space-y-2"
              >
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div className="space-y-0.5 min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="inline-flex items-center gap-1 rounded-md bg-amber-600 px-2 py-0.5 text-[11px] font-semibold text-white">
                        <BellRing className="h-3 w-3" />
                        Review 发现 {issues.length} 项阻断问题
                      </span>
                      <span className="font-mono text-xs font-semibold">{t.task_id}</span>
                      <code className="rounded bg-zinc-100 dark:bg-zinc-800 px-1.5 py-0.5 text-[11px]">
                        {t.contract.target_repo}
                      </code>
                    </div>
                    <div className="text-sm font-semibold text-zinc-900 dark:text-zinc-100 truncate">
                      {t.contract.objective}
                    </div>
                  </div>

                  <div className="flex items-center gap-2 shrink-0">
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => onSelectTask(t.task_id)}
                    >
                      查看问题详情
                    </Button>
                    <Button
                      variant="primary"
                      size="sm"
                      disabled={busy}
                      onClick={() => handleAutoFixReview(t.task_id)}
                    >
                      {busy ? (
                        <Loader2 className="h-3.5 w-3.5 animate-spin" />
                      ) : (
                        <Sparkles className="h-3.5 w-3.5" />
                      )}
                      一键按审查意见自动修复
                    </Button>
                  </div>
                </div>
              </div>
            );
          })}

          {pendingJiraTasks.map((t) => {
            const edit = draftEdits[t.task_id] || {
              target_repo: t.contract.target_repo,
              project_key: t.jira_draft?.project_key || "PROJ",
              issue_type: t.jira_draft?.issue_type || "Task",
              priority: t.jira_draft?.priority || "Medium",
              summary: t.jira_draft?.summary || t.contract.objective,
              description: t.jira_draft?.description || t.raw_request,
              ac_text: (t.jira_draft?.acceptance_criteria || t.contract.acceptance_criteria).join("\n"),
              refine_instruction: "",
            };
            const busy = mergingId === t.task_id;
            const updateField = (patch: Partial<EditableJiraDraftState>) =>
              setDraftEdits((prev) => ({
                ...prev,
                [t.task_id]: { ...edit, ...patch },
              }));

            return (
              <div
                key={t.task_id}
                className="rounded-xl border border-amber-500/45 bg-gradient-to-r from-amber-500/8 via-white to-white p-4 shadow-xs dark:from-amber-500/12 dark:via-zinc-900 dark:to-zinc-900 space-y-3"
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="inline-flex items-center gap-1 rounded-md bg-amber-600 px-2 py-0.5 text-[11px] font-semibold text-white">
                      待审建 Jira 工单
                    </span>
                    <span className="font-mono text-xs font-semibold">{t.task_id}</span>
                  </div>

                  <div className="flex items-center gap-2">
                    {!health?.jira_configured && onOpenSettings && (
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => onOpenSettings("jira")}
                      >
                        配置 Jira 凭证
                      </Button>
                    )}
                    <Button
                      variant="emerald"
                      size="sm"
                      disabled={busy}
                      onClick={() => handleApproveJiraDraft(t.task_id)}
                    >
                      {busy ? (
                        <Loader2 className="h-3.5 w-3.5 animate-spin" />
                      ) : (
                        <TicketCheck className="h-3.5 w-3.5" />
                      )}
                      批准建卡并开工
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      disabled={busy}
                      onClick={() => handleRejectTask(t.task_id, "放弃建卡")}
                    >
                      放弃
                    </Button>
                  </div>
                </div>

                <div className="grid sm:grid-cols-4 gap-2.5 text-xs">
                  <select
                    value={edit.target_repo}
                    onChange={(e) => updateField({ target_repo: e.target.value })}
                    className="h-8 rounded-lg border border-zinc-200 bg-white px-2 font-mono text-xs dark:border-zinc-800 dark:bg-zinc-950"
                  >
                    {repos.map((r) => (
                      <option key={r.repo_id} value={r.repo_id}>
                        {r.repo_id}
                      </option>
                    ))}
                  </select>
                  <input
                    type="text"
                    value={edit.project_key}
                    onChange={(e) => updateField({ project_key: e.target.value.toUpperCase() })}
                    placeholder="Project Key"
                    className="h-8 rounded-lg border border-zinc-200 bg-white px-2.5 font-mono text-xs uppercase dark:border-zinc-800 dark:bg-zinc-950"
                  />
                  <select
                    value={edit.issue_type}
                    onChange={(e) => updateField({ issue_type: e.target.value })}
                    className="h-8 rounded-lg border border-zinc-200 bg-white px-2 text-xs dark:border-zinc-800 dark:bg-zinc-950"
                  >
                    <option value="Bug">Bug</option>
                    <option value="Task">Task</option>
                    <option value="Story">Story</option>
                  </select>
                  <input
                    type="text"
                    value={edit.summary}
                    onChange={(e) => updateField({ summary: e.target.value })}
                    placeholder="工单标题"
                    className="h-8 rounded-lg border border-zinc-200 bg-white px-3 text-xs font-medium dark:border-zinc-800 dark:bg-zinc-950"
                  />
                </div>

                <div className="flex items-center gap-2 text-xs">
                  <input
                    type="text"
                    value={edit.refine_instruction}
                    onChange={(e) => updateField({ refine_instruction: e.target.value })}
                    placeholder="需要调整草稿？输入一句修改意见让 AI 重拟草稿…"
                    className="h-8 flex-1 rounded-lg border border-amber-500/35 bg-white px-3 text-xs dark:bg-zinc-950"
                  />
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    disabled={busy || !edit.refine_instruction.trim()}
                    onClick={() => handleRefineJiraDraft(t.task_id)}
                  >
                    ✨ AI 重拟
                  </Button>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* 3. TASK QUEUE WITH SEARCH, REPO/SOURCE FILTERS, COMPACT TELEMETRY & CLEANUP */}
      <SurfaceCard className="overflow-hidden">
        <div className="border-b border-zinc-200/80 dark:border-zinc-800/80 px-5 py-3.5 space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex flex-wrap items-center gap-1">
              {FILTERS.map((f) => {
                const count = tasks.filter((t) => matchesStatusFilter(t, f.id)).length;
                const active = statusFilter === f.id;
                return (
                  <button
                    key={f.id}
                    type="button"
                    onClick={() => {
                      setStatusFilter(f.id);
                      setVisibleCount(25);
                    }}
                    className={cn(
                      "inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-xs font-medium transition-colors cursor-pointer",
                      active
                        ? "bg-zinc-900 text-white dark:bg-zinc-100 dark:text-zinc-900"
                        : "text-zinc-600 hover:bg-zinc-100 dark:text-zinc-400 dark:hover:bg-zinc-800/70"
                    )}
                  >
                    <span>{f.label}</span>
                    <span
                      className={cn(
                        "rounded-full px-1.5 py-0.2 text-[10.5px] tabular-nums",
                        active
                          ? "bg-white/20 text-white dark:bg-zinc-900/15 dark:text-zinc-900"
                          : "bg-zinc-200/70 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-400"
                      )}
                    >
                      {count}
                    </span>
                  </button>
                );
              })}
            </div>

            {metrics && (
              <div className="flex flex-wrap items-center gap-3 text-[11.5px] text-zinc-500 dark:text-zinc-400 tabular-nums">
                <span>
                  累计 <b className="text-zinc-800 dark:text-zinc-200">{metrics.total_delegated}</b>
                </span>
                <span>·</span>
                <span>
                  门禁通过{" "}
                  <b className="text-emerald-600 dark:text-emerald-400">
                    {metrics.evidence_gate_passed}
                  </b>
                </span>
                <span>·</span>
                <span>
                  已闭环{" "}
                  <b className="text-zinc-800 dark:text-zinc-200">
                    {metrics.accepted_without_rework}
                  </b>
                </span>
                <span>·</span>
                <span>
                  总消耗{" "}
                  <b className="font-mono text-zinc-800 dark:text-zinc-200">
                    ${metrics.total_cost_usd.toFixed(4)}
                  </b>
                </span>
              </div>
            )}
          </div>

          <div className="flex flex-wrap items-center gap-2.5 text-xs">
            <div className="relative flex-1 min-w-[220px]">
              <Search className="absolute left-2.5 top-2 h-3.5 w-3.5 text-zinc-400" />
              <input
                type="text"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                placeholder="搜索任务 ID、Jira 单号、PR 编号、分支或需求关键词..."
                className="h-8 w-full rounded-lg border border-zinc-200 bg-zinc-50/70 pl-8 pr-3 text-xs text-zinc-900 placeholder:text-zinc-400 focus:border-blue-500 focus:bg-white focus:outline-none dark:border-zinc-800 dark:bg-zinc-950 dark:text-zinc-100"
              />
            </div>

            <select
              value={repoFilter}
              onChange={(e) => setRepoFilter(e.target.value)}
              className="h-8 rounded-lg border border-zinc-200 bg-white px-2.5 font-mono text-xs text-zinc-700 dark:border-zinc-800 dark:bg-zinc-950 dark:text-zinc-300"
            >
              <option value="ALL">全部仓库</option>
              {repos.map((r) => (
                <option key={r.repo_id} value={r.repo_id}>
                  {r.repo_id}
                </option>
              ))}
            </select>

            <select
              value={channelFilter}
              onChange={(e) => setChannelFilter(e.target.value)}
              className="h-8 rounded-lg border border-zinc-200 bg-white px-2.5 text-xs text-zinc-700 dark:border-zinc-800 dark:bg-zinc-950 dark:text-zinc-300"
            >
              <option value="ALL">全部来源</option>
              <option value="web">控制台 / CLI</option>
              <option value="jira">Jira 工单</option>
              <option value="feishu">飞书消息</option>
              <option value="alert">生产告警 (Ops)</option>
            </select>
          </div>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-left text-xs">
            <thead>
              <tr className="border-b border-zinc-200/70 bg-zinc-50/70 text-[11px] font-semibold uppercase tracking-wider text-zinc-500 dark:border-zinc-800/80 dark:bg-zinc-900/40 dark:text-zinc-400">
                <th className="py-3 pl-5 pr-3">Task ID</th>
                <th className="px-3 py-3">来源</th>
                <th className="px-3 py-3">仓库 / 分支</th>
                <th className="px-3 py-3">任务目标与实时进度</th>
                <th className="px-3 py-3">状态</th>
                <th className="px-3 py-3">门禁与审查</th>
                <th className="px-3 py-3">交付 PR</th>
                <th className="px-3 py-3 text-right">费用</th>
                <th className="py-3 pl-2 pr-4 text-right">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-100 dark:divide-zinc-800/60">
              {displayedTasks.length === 0 ? (
                <tr>
                  <td colSpan={9} className="py-12 text-center text-xs text-zinc-400">
                    暂无匹配的委托任务记录
                  </td>
                </tr>
              ) : (
                displayedTasks.map((t) => {
                  const ev = t.verification_evidence;
                  const f2pPassed =
                    ev?.repro_test_failed_on_base && ev?.repro_test_passed_on_head;
                  const regBuildPassed =
                    ev?.verification_mode_used === "regression_and_build" &&
                    ev?.repro_test_passed_on_head;
                  const gatePassed = Boolean(f2pPassed || regBuildPassed);
                  const revVerdict = ev?.reviewer_report?.verdict;

                  const ChannelIcon = t.source_channel.startsWith("feishu")
                    ? MessageSquare
                    : t.source_channel === "alert_webhook"
                      ? BellElectric
                      : t.source_channel === "cli"
                        ? Terminal
                        : Monitor;
                  const channelLabel = t.source_channel.startsWith("feishu")
                    ? "飞书"
                    : t.source_channel === "alert_webhook"
                      ? "告警"
                      : t.source_channel.startsWith("jira")
                        ? "Jira"
                        : t.source_channel.toUpperCase();

                  return (
                    <tr
                      key={t.task_id}
                      onClick={() => onSelectTask(t.task_id)}
                      className={cn(
                        "group cursor-pointer transition-colors hover:bg-zinc-50/90 dark:hover:bg-zinc-800/40",
                        selectedTaskId === t.task_id && "bg-blue-50/70 dark:bg-blue-500/10"
                      )}
                    >
                      <td className="py-3.5 pl-5 pr-3 font-mono font-medium text-blue-600 dark:text-blue-400 whitespace-nowrap">
                        {t.task_id}
                      </td>
                      <td className="px-3 py-3.5 whitespace-nowrap text-zinc-500 dark:text-zinc-400">
                        <span className="inline-flex items-center gap-1.5">
                          <ChannelIcon className="h-3.5 w-3.5 opacity-75" />
                          <span>{channelLabel}</span>
                        </span>
                      </td>
                      <td className="px-3 py-3.5 whitespace-nowrap">
                        <div className="flex items-center gap-1">
                          <code className="rounded bg-zinc-100 dark:bg-zinc-800 px-1.5 py-0.5 text-[11px] text-zinc-700 dark:text-zinc-300">
                            {t.contract.target_repo}
                          </code>
                          <span className="font-mono text-[10.5px] text-zinc-400">
                            :{t.contract.base_branch}
                          </span>
                        </div>
                      </td>
                      <td className="px-3 py-3.5 max-w-md text-zinc-800 dark:text-zinc-200 font-medium">
                        <div className="flex items-center gap-1.5 truncate">
                          {t.jira_issue_key && (
                            <a
                              href={t.jira_issue_url || "#"}
                              target="_blank"
                              rel="noreferrer"
                              onClick={(e) => e.stopPropagation()}
                              className="inline-flex shrink-0 items-center gap-0.5 rounded bg-indigo-500/12 px-1.5 py-0.5 font-mono text-[10.5px] font-semibold text-indigo-600 hover:bg-indigo-500/20 dark:text-indigo-300"
                            >
                              {t.jira_issue_key}
                              <ArrowUpRight className="h-2.5 w-2.5" />
                            </a>
                          )}
                          <span className="truncate">{t.contract.objective}</span>
                        </div>
                        {["QUEUED", "RUNNING_IN_SANDBOX", "VERIFYING_EVIDENCE"].includes(
                          t.lifecycle_status
                        ) && (
                          <div className="mt-1 flex items-center gap-1.5 truncate text-[11px] font-normal text-blue-600 dark:text-blue-400">
                            {t.worker_alive ? (
                              <Loader2 className="h-3 w-3 animate-spin shrink-0" />
                            ) : (
                              <span className="h-2 w-2 rounded-full bg-amber-500 shrink-0" />
                            )}
                            <span className="truncate">
                              {t.current_activity ||
                                (t.worker_alive
                                  ? `第 ${t.sandbox_attempt_count || 1} 轮沙箱执行中…`
                                  : "后台执行线程已离线（点开可唤醒）")}
                            </span>
                          </div>
                        )}
                      </td>
                      <td className="px-3 py-3.5 whitespace-nowrap">
                        <StatusBadge status={t.lifecycle_status} />
                      </td>
                      <td className="px-3 py-3.5 whitespace-nowrap">
                        {!ev ? (
                          <span className="text-zinc-400">—</span>
                        ) : ev.noop_verified ? (
                          <span className="inline-flex items-center gap-1 text-emerald-600 dark:text-emerald-400 font-medium">
                            <CheckCircle2 className="h-3.5 w-3.5" />
                            No-Op 验证通过
                          </span>
                        ) : gatePassed && revVerdict !== "BLOCK" ? (
                          <span className="inline-flex items-center gap-1 text-emerald-600 dark:text-emerald-400 font-medium">
                            <CheckCircle2 className="h-3.5 w-3.5" />
                            {regBuildPassed ? "构建 + 审查通过" : "单测 + 审查通过"}
                          </span>
                        ) : gatePassed && revVerdict === "BLOCK" ? (
                          <span className="inline-flex items-center gap-1 text-amber-600 dark:text-amber-400 font-medium">
                            <AlertCircle className="h-3.5 w-3.5" />
                            审查阻断待修
                          </span>
                        ) : (
                          <span className="inline-flex items-center gap-1 text-red-500 font-medium">
                            <XCircle className="h-3.5 w-3.5" />
                            门禁未通过
                          </span>
                        )}
                      </td>
                      <td className="px-3 py-3.5 whitespace-nowrap">
                        {t.deliverable_pr_url ? (
                          <a
                            href={t.deliverable_pr_url}
                            target="_blank"
                            rel="noreferrer"
                            onClick={(e) => e.stopPropagation()}
                            className="inline-flex items-center gap-1 rounded-md bg-blue-500/10 px-2 py-0.5 font-mono text-[11px] font-medium text-blue-600 hover:bg-blue-500/20 dark:text-blue-300"
                          >
                            <GitPullRequest className="h-3 w-3" />
                            {t.deliverable_pr_number ? `PR #${t.deliverable_pr_number}` : "查看 PR"}
                            <ArrowUpRight className="h-3 w-3" />
                          </a>
                        ) : (
                          <span className="text-zinc-400">—</span>
                        )}
                      </td>
                      <td className="px-3 py-3.5 text-right font-mono tabular-nums text-zinc-600 dark:text-zinc-300 whitespace-nowrap">
                        ${t.total_cost_usd.toFixed(4)}
                      </td>
                      <td className="py-3.5 pl-2 pr-4 text-right whitespace-nowrap">
                        {!t.worker_alive && (
                          <button
                            type="button"
                            disabled={deletingId === t.task_id}
                            onClick={(e) => handleDeleteTask(e, t)}
                            title="删除此任务记录"
                            className="rounded p-1 text-zinc-400 opacity-0 group-hover:opacity-100 hover:bg-rose-500/10 hover:text-rose-600 dark:hover:text-rose-400 transition-all cursor-pointer"
                          >
                            <Trash2 className="h-3.5 w-3.5" />
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>

        {filteredTasks.length > visibleCount && (
          <div className="border-t border-zinc-100 dark:border-zinc-800/80 px-5 py-3 text-center">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => setVisibleCount((c) => c + 30)}
            >
              加载更多（当前展示 {displayedTasks.length} / {filteredTasks.length} 条）
            </Button>
          </div>
        )}
      </SurfaceCard>
    </div>
  );
}
