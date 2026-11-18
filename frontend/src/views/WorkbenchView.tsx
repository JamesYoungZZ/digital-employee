import React from "react";
import {
  ArrowUpRight,
  BellRing,
  CheckCircle2,
  Clock,
  Coins,
  Command,
  FileDiff,
  GitMerge,
  GitPullRequestDraft,
  Layers,
  Loader2,
  MessageSquare,
  Monitor,
  ShieldCheck,
  Sparkles,
  Terminal,
  TicketCheck,
  XCircle,
} from "lucide-react";
import { toast } from "sonner";
import type { HealthInfo, MetricsInfo, RepositoryItem, TaskItem } from "../types/api";
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
  onOpenJiraTab?: () => void;
}

const FILTERS = [
  { id: "ALL", label: "全部任务" },
  { id: "WAITING_JIRA_APPROVAL", label: "待审建 Jira" },
  { id: "WAITING_MERGE_APPROVAL", label: "待审批合入" },
  { id: "WAITING_CLARIFICATION", label: "待你拍板" },
  { id: "ACTIVE", label: "沙箱运行中" },
  { id: "READY", label: "等待 CI 中" },
  { id: "ACCEPTED_AND_CLOSED", label: "已合入闭环" },
  { id: "HANDED_OFF_INCOMPLETE", label: "已交接现场" },
] as const;

const SAMPLE_PROMPTS = [
  {
    repo: "livestream-copilot",
    text: "直播间常有观众在公屏诱导私加微信/VX/WhatsApp或套话让助播承诺『给您免单/已补发』，极易触发平台违规处罚，需拦截此类回复并引导走官方客服",
  },
  {
    repo: "livestream-copilot",
    text: "大促连续开播数小时、公屏弹幕持续高频涌入时，助播服务内存随开播时长不断上涨存在卡顿崩溃风险，需控制历史弹幕保留范围保障长稳运行",
  },
  {
    repo: "modular-rag",
    text: "导购客服检索商品知识库时经常把不同品类的尺码/售后规则混在一起，希望支持按商品类目筛选检索结果提升回答准确度",
  },
  {
    repo: "modular-rag",
    text: "买家搜索带连字符的户外商品科技词（如 Gore-Tex、 Dri-FIT）时经常搜不到明明在售的商品，请排查修复漏召回问题",
  },
];

function matchesFilter(t: TaskItem, filter: string): boolean {
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
  project_key: string;
  issue_type: string;
  priority: string;
  summary: string;
  description: string;
  ac_text: string;
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
  onOpenJiraTab,
}: Props) {
  const [targetRepo, setTargetRepo] = React.useState("");
  const [delegateMode, setDelegateMode] = React.useState<"raw" | "existing_jira">("raw");
  const [prompt, setPrompt] = React.useState("");
  const [existingJiraKey, setExistingJiraKey] = React.useState("");
  const [requireJiraApproval, setRequireJiraApproval] = React.useState<boolean>(true);
  const [publishPr, setPublishPr] = React.useState(true);
  const [acceptDefaults, setAcceptDefaults] = React.useState(false);
  const [submitting, setSubmitting] = React.useState(false);
  const [mergingId, setMergingId] = React.useState<string | null>(null);
  const [filter, setFilter] = React.useState<string>("ALL");
  const [draftEdits, setDraftEdits] = React.useState<Record<string, EditableJiraDraftState>>({});

  React.useEffect(() => {
    if (health) {
      setRequireJiraApproval(Boolean(health.jira_configured));
    }
  }, [health?.jira_configured]);

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
            project_key: d?.project_key || health?.jira_default_project || "PROJ",
            issue_type: d?.issue_type || "Task",
            priority: d?.priority || "Medium",
            summary: d?.summary || t.contract.objective,
            description: d?.description || t.contract.reproduction_evidence || t.raw_request,
            ac_text: (d?.acceptance_criteria || t.contract.acceptance_criteria || []).join("\n"),
          };
        }
      }
      return next;
    });
  }, [pendingJiraTasks, health?.jira_default_project]);

  const handleApproveJiraDraft = async (taskId: string) => {
    const edit = draftEdits[taskId];
    setMergingId(taskId);
    try {
      const created = await apiFetch<TaskItem>(
        `/api/tasks/${encodeURIComponent(taskId)}/approve-jira`,
        {
          method: "POST",
          body: JSON.stringify({
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
        `✅ 已创建真实 Jira 工单 ${created.jira_issue_key || ""} 并锁定为唯一权威需求源，沙箱开始执行！`
      );
      await onMutated();
      onSelectTask(taskId);
    } catch (err: any) {
      toast.error(err.message);
    } finally {
      setMergingId(null);
    }
  };

  const pendingApprovalTasks = React.useMemo(
    () => tasks.filter((t) => t.lifecycle_status === "WAITING_MERGE_APPROVAL"),
    [tasks]
  );

  const handleApproveMerge = async (taskId: string) => {
    setMergingId(taskId);
    try {
      await apiFetch(`/api/tasks/${encodeURIComponent(taskId)}/control`, {
        method: "POST",
        body: JSON.stringify({ action: "approve_merge", note: "console_approval_card" }),
      });
      toast.success("已批准！系统已在 GitHub 自动完成 Squash Merge 合入");
      await onMutated();
    } catch (err: any) {
      toast.error(`合入失败: ${err.message}`);
    } finally {
      setMergingId(null);
    }
  };

  const handleRejectMerge = async (taskId: string) => {
    setMergingId(taskId);
    try {
      await apiFetch(`/api/tasks/${encodeURIComponent(taskId)}/control`, {
        method: "POST",
        body: JSON.stringify({ action: "abandon", note: "rejected_on_console_card" }),
      });
      toast.info("已拒绝该合入请求");
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
      toast.success(`🔧 已唤醒沙箱！正在按 Review 意见自动修复 ${taskId}…`);
      onSelectTask(taskId);
      await onMutated();
    } catch (err: any) {
      toast.error(err.message);
    } finally {
      setMergingId(null);
    }
  };

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

  React.useEffect(() => {
    if (!targetRepo && repos.length > 0) {
      setTargetRepo(repos[0].repo_id);
    }
  }, [repos, targetRepo]);

  const handleSubmit = async (e?: React.FormEvent) => {
    e?.preventDefault();
    if (!targetRepo) return;
    if (delegateMode === "existing_jira" && !existingJiraKey.trim()) return;
    if (delegateMode === "raw" && !prompt.trim()) return;

    setSubmitting(true);
    try {
      const body =
        delegateMode === "existing_jira"
          ? {
              target_repo: targetRepo,
              raw_request: existingJiraKey.trim(),
              jira_issue_key: existingJiraKey.trim(),
              publish_pr: publishPr,
              auto_merge: false,
              accept_defaults: acceptDefaults,
              auto_execute: true,
            }
          : {
              target_repo: targetRepo,
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
      if (created.lifecycle_status === "WAITING_JIRA_APPROVAL") {
        toast.success(`📋 已起草 Jira 工单草稿 (${created.task_id})，请审核确认后建卡开工！`);
      } else if (created.jira_issue_key) {
        toast.success(`🎫 已拉取权威工单 ${created.jira_issue_key}，沙箱直接开工 (${created.task_id})！`);
      } else {
        toast.success(`已生成契约 ${created.task_id}`);
      }
      await onCreatedTask(created.task_id);
    } catch (err: any) {
      toast.error(err.message);
    } finally {
      setSubmitting(false);
    }
  };

  const filteredTasks = React.useMemo(
    () => tasks.filter((t) => matchesFilter(t, filter)),
    [tasks, filter]
  );

  return (
    <div className="space-y-6">
      {/* Bento KPI Strip */}
      <div className="grid grid-cols-2 lg:grid-cols-5 gap-3">
        {[
          {
            label: "累计委托任务",
            val: metrics?.total_delegated ?? 0,
            icon: Layers,
            accent: "text-blue-500",
          },
          {
            label: "证据门禁通过",
            val: metrics?.evidence_gate_passed ?? 0,
            icon: ShieldCheck,
            accent: "text-emerald-500",
          },
          {
            label: "已采纳合入闭环",
            val: metrics?.accepted_without_rework ?? 0,
            icon: GitMerge,
            accent: "text-teal-500",
          },
          {
            label: "预计净省人工耗时",
            val: `${metrics?.estimated_net_human_minutes_saved ?? 0} min`,
            icon: Clock,
            accent: "text-indigo-500",
          },
          {
            label: "累计模型费用 (KV折后)",
            val: `$${(metrics?.total_cost_usd ?? 0).toFixed(4)}`,
            icon: Coins,
            accent: "text-amber-500",
          },
        ].map((k) => {
          const Icon = k.icon;
          return (
            <SurfaceCard key={k.label} className="p-4">
              <div className="flex items-center justify-between text-xs text-zinc-500 dark:text-zinc-400">
                <span>{k.label}</span>
                <Icon className={cn("h-4 w-4", k.accent)} />
              </div>
              <div className="mt-2 text-2xl font-semibold tracking-tight tabular-nums text-zinc-900 dark:text-zinc-50">
                {k.val}
              </div>
            </SurfaceCard>
          );
        })}
      </div>

      {/* Interactive Jira Ticket Draft Review Cards (WAITING_JIRA_APPROVAL) */}
      {pendingJiraTasks.length > 0 && (
        <div className="space-y-3">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2 text-xs font-semibold text-amber-600 dark:text-amber-400">
              <TicketCheck className="h-4 w-4 animate-pulse" />
              <span>
                待你审核 Jira 工单草稿 ({pendingJiraTasks.length} 张 — 审核批准后在真实 Jira 建卡，后续全程以 Jira 任务为准)
              </span>
            </div>
            {!health?.jira_configured && onOpenJiraTab && (
              <Button variant="outline" size="sm" onClick={onOpenJiraTab}>
                前往配置 Jira 连接凭证
              </Button>
            )}
          </div>

          <div className="grid gap-3">
            {pendingJiraTasks.map((t) => {
              const edit = draftEdits[t.task_id] || {
                project_key: t.jira_draft?.project_key || "PROJ",
                issue_type: t.jira_draft?.issue_type || "Task",
                priority: t.jira_draft?.priority || "Medium",
                summary: t.jira_draft?.summary || t.contract.objective,
                description: t.jira_draft?.description || t.raw_request,
                ac_text: (t.jira_draft?.acceptance_criteria || t.contract.acceptance_criteria).join("\n"),
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
                  className="rounded-xl border border-amber-500/45 bg-gradient-to-r from-amber-500/8 via-white to-white p-5 shadow-md dark:from-amber-500/12 dark:via-zinc-900 dark:to-zinc-900 space-y-3.5"
                >
                  <div className="flex flex-wrap items-center justify-between gap-3">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="inline-flex items-center gap-1 rounded-md bg-amber-600 px-2 py-0.5 text-[11px] font-semibold text-white">
                        📋 待审建 Jira 工单
                      </span>
                      <span className="font-mono text-xs font-semibold text-zinc-800 dark:text-zinc-200">
                        {t.task_id}
                      </span>
                      <code className="rounded bg-zinc-100 dark:bg-zinc-800 px-1.5 py-0.5 text-[11px] text-zinc-600 dark:text-zinc-300">
                        目标仓库: {t.contract.target_repo} ({t.contract.base_branch})
                      </code>
                    </div>

                    <div className="flex flex-wrap items-center gap-2">
                      <Button
                        variant="emerald"
                        size="lg"
                        disabled={busy}
                        onClick={() => handleApproveJiraDraft(t.task_id)}
                      >
                        {busy ? (
                          <Loader2 className="h-4 w-4 animate-spin" />
                        ) : (
                          <TicketCheck className="h-4 w-4" />
                        )}
                        ✅ 批准创建真实 Jira 工单并开工
                      </Button>
                      <Button
                        variant="ghost"
                        size="default"
                        disabled={busy}
                        onClick={() => handleRejectMerge(t.task_id)}
                      >
                        放弃（不建卡）
                      </Button>
                    </div>
                  </div>

                  <div className="grid sm:grid-cols-4 gap-3 text-xs">
                    <div>
                      <label className="text-[11px] text-zinc-500 block mb-1">Jira Project Key</label>
                      <input
                        type="text"
                        value={edit.project_key}
                        onChange={(e) => updateField({ project_key: e.target.value.toUpperCase() })}
                        className="h-8 w-full rounded-lg border border-zinc-200 bg-white px-2.5 font-mono text-xs uppercase dark:border-zinc-800 dark:bg-zinc-950"
                      />
                    </div>
                    <div>
                      <label className="text-[11px] text-zinc-500 block mb-1">工单类型 (Issue Type)</label>
                      <select
                        value={edit.issue_type}
                        onChange={(e) => updateField({ issue_type: e.target.value })}
                        className="h-8 w-full rounded-lg border border-zinc-200 bg-white px-2 text-xs dark:border-zinc-800 dark:bg-zinc-950"
                      >
                        <option value="Bug">Bug (缺陷)</option>
                        <option value="Task">Task (研发任务)</option>
                        <option value="Story">Story (需求故事)</option>
                      </select>
                    </div>
                    <div>
                      <label className="text-[11px] text-zinc-500 block mb-1">优先级 (Priority)</label>
                      <select
                        value={edit.priority}
                        onChange={(e) => updateField({ priority: e.target.value })}
                        className="h-8 w-full rounded-lg border border-zinc-200 bg-white px-2 text-xs dark:border-zinc-800 dark:bg-zinc-950"
                      >
                        <option value="Highest">Highest</option>
                        <option value="High">High</option>
                        <option value="Medium">Medium</option>
                        <option value="Low">Low</option>
                      </select>
                    </div>
                    <div className="flex items-end">
                      <span className="text-[11px] text-zinc-500">
                        提示：飞书也已同步收到此草稿卡片，可直接回复 <code className="font-mono">建单</code>
                      </span>
                    </div>
                  </div>

                  <div className="space-y-2 text-xs">
                    <div>
                      <label className="text-[11px] text-zinc-500 block mb-1">Jira 工单标题 (Summary)</label>
                      <input
                        type="text"
                        value={edit.summary}
                        onChange={(e) => updateField({ summary: e.target.value })}
                        className="h-8 w-full rounded-lg border border-zinc-200 bg-white px-3 text-xs font-medium dark:border-zinc-800 dark:bg-zinc-950"
                      />
                    </div>
                    <div className="grid sm:grid-cols-2 gap-3">
                      <div>
                        <label className="text-[11px] text-zinc-500 block mb-1">
                          权威需求描述 (Description)
                        </label>
                        <textarea
                          rows={3}
                          value={edit.description}
                          onChange={(e) => updateField({ description: e.target.value })}
                          className="w-full rounded-lg border border-zinc-200 bg-white p-2.5 text-xs dark:border-zinc-800 dark:bg-zinc-950"
                        />
                      </div>
                      <div>
                        <label className="text-[11px] text-zinc-500 block mb-1">
                          验收标准 (Acceptance Criteria · 每行一条)
                        </label>
                        <textarea
                          rows={3}
                          value={edit.ac_text}
                          onChange={(e) => updateField({ ac_text: e.target.value })}
                          className="w-full rounded-lg border border-zinc-200 bg-white p-2.5 text-xs dark:border-zinc-800 dark:bg-zinc-950"
                        />
                      </div>
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Interactive Merge Approval Notification Cards (Console + Feishu Sync) */}
      {pendingApprovalTasks.length > 0 && (
        <div className="space-y-3">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2 text-xs font-semibold text-emerald-600 dark:text-emerald-400">
              <BellRing className="h-4 w-4 animate-bounce" />
              <span>
                待你审批自动合入 ({pendingApprovalTasks.length} 张审批卡 — 接入飞书后同步推送至手机飞书审批)
              </span>
            </div>
          </div>

          <div className="grid gap-3">
            {pendingApprovalTasks.map((t) => {
              const mc = t.merge_approval_card;
              const ev = t.verification_evidence;
              const busy = mergingId === t.task_id;
              return (
                <div
                  key={t.task_id}
                  className="rounded-xl border border-emerald-500/45 bg-gradient-to-r from-emerald-500/8 via-white to-white p-5 shadow-md dark:from-emerald-500/12 dark:via-zinc-900 dark:to-zinc-900 space-y-3.5"
                >
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="space-y-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="inline-flex items-center gap-1 rounded-md bg-emerald-600 px-2 py-0.5 text-[11px] font-semibold text-white">
                          <ShieldCheck className="h-3 w-3" />
                          审查与线上 CI 已全绿 · 待审批合入
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
                      </div>
                      <h4 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
                        {t.contract.objective}
                      </h4>
                    </div>

                    <div className="flex flex-wrap items-center gap-2">
                      <Button
                        variant="emerald"
                        size="lg"
                        disabled={busy}
                        onClick={() => handleApproveMerge(t.task_id)}
                      >
                        {busy ? (
                          <Loader2 className="h-4 w-4 animate-spin" />
                        ) : (
                          <GitMerge className="h-4 w-4" />
                        )}
                        批准并自动 Squash 合入
                      </Button>
                      <Button
                        variant="outline"
                        size="default"
                        onClick={() => onSelectTask(t.task_id)}
                      >
                        <FileDiff className="h-3.5 w-3.5" />
                        审阅 Diff / 追改
                      </Button>
                      <Button
                        variant="ghost"
                        size="default"
                        disabled={busy}
                        onClick={() => handleRejectMerge(t.task_id)}
                      >
                        拒绝
                      </Button>
                    </div>
                  </div>

                  {/* Evidence & Independent PR Review Digest */}
                  <div className="grid sm:grid-cols-3 gap-2.5 text-xs">
                    <div className="rounded-lg border border-zinc-200/80 dark:border-zinc-800 bg-white/70 dark:bg-zinc-950/60 px-3 py-2">
                      <span className="text-zinc-400 block text-[11px]">沙箱回归测试 (Fail → Pass)</span>
                      <span className="font-mono text-[11.5px] text-emerald-600 dark:text-emerald-400 font-medium">
                        ✅ {mc?.repro_test_target || ev?.repro_test_target}
                      </span>
                    </div>
                    <div className="rounded-lg border border-zinc-200/80 dark:border-zinc-800 bg-white/70 dark:bg-zinc-950/60 px-3 py-2">
                      <span className="text-zinc-400 block text-[11px]">独立跨模型 PR Review Agent</span>
                      <span className="font-medium text-emerald-600 dark:text-emerald-400">
                        ✅ APPROVE · 0 Blocking ({mc?.reviewer_model || ev?.reviewer_report?.reviewer_model})
                      </span>
                    </div>
                    <div className="rounded-lg border border-zinc-200/80 dark:border-zinc-800 bg-white/70 dark:bg-zinc-950/60 px-3 py-2">
                      <span className="text-zinc-400 block text-[11px]">线上 GitHub Actions CI</span>
                      <span className="font-medium text-emerald-600 dark:text-emerald-400">
                        ✅ {mc?.ci_checks_count ?? 2} 项流水线检查全绿
                      </span>
                    </div>
                  </div>

                  {(mc?.reviewer_summary || ev?.reviewer_report?.summary) && (
                    <p className="text-xs text-zinc-600 dark:text-zinc-300 leading-relaxed">
                      <b className="text-zinc-800 dark:text-zinc-200">PR Review 简报：</b>
                      {mc?.reviewer_summary || ev?.reviewer_report?.summary}
                    </p>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Interactive Review Blocked -> 1-Click Auto-Fix Cards */}
      {pendingReviewFixTasks.length > 0 && (
        <div className="space-y-3">
          <div className="flex items-center gap-2 text-xs font-semibold text-amber-600 dark:text-amber-400">
            <BellRing className="h-4 w-4" />
            <span>
              Code Review 待修复 ({pendingReviewFixTasks.length} 张卡片 — 飞书与控制台均支持一键唤醒自动修复)
            </span>
          </div>

          <div className="grid gap-3">
            {pendingReviewFixTasks.map((t) => {
              const rep = t.verification_evidence?.reviewer_report;
              const issues = rep?.blocking_issues ?? [];
              const busy = mergingId === t.task_id;
              return (
                <div
                  key={t.task_id}
                  className="rounded-xl border border-amber-500/45 bg-gradient-to-r from-amber-500/8 via-white to-white p-5 shadow-md dark:from-amber-500/12 dark:via-zinc-900 dark:to-zinc-900 space-y-3"
                >
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="space-y-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="inline-flex items-center gap-1 rounded-md bg-amber-600 px-2 py-0.5 text-[11px] font-semibold text-white">
                          🛑 Code Review 发现 {issues.length} 项阻断问题
                        </span>
                        <span className="font-mono text-xs font-semibold text-zinc-800 dark:text-zinc-200">
                          {t.task_id}
                        </span>
                        {t.deliverable_pr_url && (
                          <a
                            href={t.pr_review_comment_url || t.deliverable_pr_url}
                            target="_blank"
                            rel="noreferrer"
                            className="inline-flex items-center gap-1 text-xs font-medium text-blue-600 hover:underline dark:text-blue-400"
                          >
                            PR #{t.deliverable_pr_number} 审查评论
                            <ArrowUpRight className="h-3.5 w-3.5" />
                          </a>
                        )}
                      </div>
                      <h4 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
                        {t.contract.objective}
                      </h4>
                    </div>

                    <div className="flex flex-wrap items-center gap-2">
                      <Button
                        variant="primary"
                        size="lg"
                        disabled={busy}
                        onClick={() => handleAutoFixReview(t.task_id)}
                      >
                        {busy ? (
                          <Loader2 className="h-4 w-4 animate-spin" />
                        ) : (
                          <Sparkles className="h-4 w-4" />
                        )}
                        🔧 一键按 Review 意见自动修复
                      </Button>
                      <a
                        href={`/reports/${encodeURIComponent(t.task_id)}/review`}
                        target="_blank"
                        rel="noreferrer"
                        className="inline-flex h-8 items-center gap-1 rounded-lg border border-zinc-200 bg-white px-3 text-xs font-medium text-zinc-700 hover:bg-zinc-50 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-200"
                      >
                        📄 全量报告
                        <ArrowUpRight className="h-3 w-3" />
                      </a>
                    </div>
                  </div>

                  <div className="space-y-1 rounded-lg border border-amber-500/20 bg-white/80 dark:bg-zinc-950/60 p-3 text-xs">
                    {issues.slice(0, 3).map((b, idx) => (
                      <div key={idx} className="text-zinc-700 dark:text-zinc-300">
                        <b className="text-red-600 dark:text-red-400">[{b.severity}]</b>{" "}
                        {b.path ? (
                          <code className="font-mono mr-1">
                            {b.path}
                            {b.line ? `:${b.line}` : ""}
                          </code>
                        ) : null}
                        {b.message}
                      </div>
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Command-Bar Delegation Composer */}
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
                新需求委托（先审 Jira 草稿再建卡）
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
                已有 Jira 工单（直接按 Jira 开工）
              </button>
            </div>

            <div className="flex items-center gap-1.5 text-[11px] text-zinc-400">
              <Command className="h-3 w-3" />
              <span>快捷键 ⌘ + Enter 直接派发</span>
            </div>
          </div>

          {delegateMode === "existing_jira" ? (
            <div className="space-y-2">
              <input
                type="text"
                value={existingJiraKey}
                onChange={(e) => setExistingJiraKey(e.target.value)}
                placeholder="输入已有 Jira 工单号（如 KAN-12 / RAG-42）或粘贴完整链接 https://.../browse/KAN-12"
                className="h-11 w-full rounded-xl border border-emerald-500/40 bg-emerald-500/5 px-4 font-mono text-sm text-zinc-900 placeholder:font-sans placeholder:text-zinc-400 focus:border-emerald-500 focus:bg-white focus:outline-none dark:bg-zinc-950 dark:text-zinc-100"
                required
              />
              <p className="text-[11.5px] text-zinc-500">
                已有真实 Jira 工单时将<strong>跳过建卡审核</strong>，直接拉取 Jira 标题、正文与最新评论作为唯一权威需求源进沙箱编码。
              </p>
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
                placeholder="输入自然语言需求、单测失败路径或直接包含已有 Jira 号（如 KAN-12）..."
                className="w-full resize-none rounded-xl border border-zinc-200 bg-zinc-50/50 p-3.5 text-sm text-zinc-900 placeholder:text-zinc-400 focus:border-blue-500 focus:bg-white focus:outline-none focus:ring-3 focus:ring-blue-500/12 dark:border-zinc-800 dark:bg-zinc-950/70 dark:text-zinc-100 dark:focus:bg-zinc-950"
                required
              />

              {/* Quick-fill Chips */}
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="text-[11px] text-zinc-400 mr-1">产品 / 运营 / QA 业务诉求示例：</span>
                {SAMPLE_PROMPTS.map((sp) => (
                  <button
                    key={sp.text}
                    type="button"
                    onClick={() => {
                      setPrompt(sp.text);
                      setTargetRepo(sp.repo);
                    }}
                    className="rounded-md border border-zinc-200/80 bg-zinc-50 px-2.5 py-1 text-[11.5px] text-zinc-600 hover:border-zinc-300 hover:text-zinc-900 dark:border-zinc-800 dark:bg-zinc-900 dark:text-zinc-400 dark:hover:border-zinc-700 dark:hover:text-zinc-200 cursor-pointer transition-colors"
                  >
                    {sp.text.slice(0, 42)}…
                  </button>
                ))}
              </div>
            </>
          )}

          {/* Bottom Controls Bar */}
          <div className="flex flex-wrap items-center justify-between gap-4 pt-2 border-t border-zinc-100 dark:border-zinc-800/80">
            <div className="flex flex-wrap items-center gap-5">
              <div className="flex items-center gap-2">
                <span className="text-xs font-medium text-zinc-500">仓库</span>
                <select
                  value={targetRepo}
                  onChange={(e) => setTargetRepo(e.target.value)}
                  className="h-8 rounded-lg border border-zinc-200 bg-white px-2.5 font-mono text-xs text-zinc-800 focus:border-blue-500 focus:outline-none dark:border-zinc-800 dark:bg-zinc-950 dark:text-zinc-200"
                >
                  {repos.map((r) => (
                    <option key={r.repo_id} value={r.repo_id}>
                      {r.repo_id} ({r.default_base_branch})
                    </option>
                  ))}
                </select>
              </div>

              {delegateMode === "raw" && (
                <label className="flex items-center gap-2 text-xs text-amber-700 dark:text-amber-300 font-medium cursor-pointer select-none">
                  <Switch
                    checked={requireJiraApproval}
                    onCheckedChange={setRequireJiraApproval}
                  />
                  <span>先生成 Jira 工单草稿供我审核建卡（建卡后以 Jira 为准）</span>
                </label>
              )}

              <label className="flex items-center gap-2 text-xs text-zinc-600 dark:text-zinc-400 cursor-pointer select-none">
                <Switch checked={publishPr} onCheckedChange={setPublishPr} />
                <span>自动创建正式 PR（过审+CI全绿后推合入审批卡）</span>
              </label>

              <label className="flex items-center gap-2 text-xs text-zinc-600 dark:text-zinc-400 cursor-pointer select-none">
                <Switch checked={acceptDefaults} onCheckedChange={setAcceptDefaults} />
                <span>轻微歧义按默认继续</span>
              </label>
            </div>

            <Button type="submit" variant="primary" size="lg" disabled={submitting || !repos.length}>
              {submitting ? (
                <>
                  <Loader2 className="h-4 w-4 animate-spin" />
                  {delegateMode === "existing_jira"
                    ? "正在拉取权威 Jira 工单并启动沙箱..."
                    : "正在探测仓库起草工单与契约..."}
                </>
              ) : delegateMode === "existing_jira" ? (
                <>
                  <TicketCheck className="h-4 w-4" />
                  拉取 Jira 工单并直接开工
                </>
              ) : requireJiraApproval ? (
                <>
                  <TicketCheck className="h-4 w-4" />
                  起草 Jira 工单供我审核
                </>
              ) : (
                <>
                  <Sparkles className="h-4 w-4" />
                  起草契约并执行委托
                </>
              )}
            </Button>
          </div>
        </form>
      </SurfaceCard>

      {/* Work Queue Table */}
      <SurfaceCard className="overflow-hidden">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-zinc-200/80 dark:border-zinc-800/80 px-5 py-3.5">
          <div className="flex flex-wrap items-center gap-1">
            {FILTERS.map((f) => {
              const count = tasks.filter((t) => matchesFilter(t, f.id)).length;
              const active = filter === f.id;
              return (
                <button
                  key={f.id}
                  type="button"
                  onClick={() => setFilter(f.id)}
                  className={cn(
                    "inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs font-medium transition-colors cursor-pointer",
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

          <span className="text-[11.5px] text-zinc-400">
            点击任意任务行在右侧抽屉查看完整契约、逐行 Diff 与飞书卡片
          </span>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-left text-xs">
            <thead>
              <tr className="border-b border-zinc-200/70 bg-zinc-50/70 text-[11px] font-semibold uppercase tracking-wider text-zinc-500 dark:border-zinc-800/80 dark:bg-zinc-900/40 dark:text-zinc-400">
                <th className="py-3 pl-5 pr-3">Task ID</th>
                <th className="px-3 py-3">入口</th>
                <th className="px-3 py-3">目标仓库</th>
                <th className="px-3 py-3">契约目标</th>
                <th className="px-3 py-3">当前状态</th>
                <th className="px-3 py-3">回归验证</th>
                <th className="px-3 py-3">交付 PR</th>
                <th className="px-3 py-3 text-right">费用</th>
                <th className="py-3 pl-3 pr-5 text-right">净省工时</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-100 dark:divide-zinc-800/60">
              {filteredTasks.length === 0 ? (
                <tr>
                  <td colSpan={9} className="py-12 text-center text-xs text-zinc-400">
                    当前筛选条件下暂无委托记录
                  </td>
                </tr>
              ) : (
                filteredTasks.map((t) => {
                  const ev = t.verification_evidence;
                  const f2pPassed =
                    ev?.repro_test_failed_on_base && ev?.repro_test_passed_on_head;
                  const ChannelIcon = t.source_channel.startsWith("feishu")
                    ? MessageSquare
                    : t.source_channel === "cli"
                    ? Terminal
                    : Monitor;

                  return (
                    <tr
                      key={t.task_id}
                      onClick={() => onSelectTask(t.task_id)}
                      className={cn(
                        "group cursor-pointer transition-colors hover:bg-zinc-50/90 dark:hover:bg-zinc-800/40",
                        selectedTaskId === t.task_id &&
                          "bg-blue-50/70 dark:bg-blue-500/10"
                      )}
                    >
                      <td className="py-3.5 pl-5 pr-3 font-mono font-medium text-blue-600 dark:text-blue-400 whitespace-nowrap">
                        {t.task_id}
                      </td>
                      <td className="px-3 py-3.5 whitespace-nowrap text-zinc-500 dark:text-zinc-400">
                        <span className="inline-flex items-center gap-1.5">
                          <ChannelIcon className="h-3.5 w-3.5 opacity-75" />
                          <span>
                            {t.source_channel.startsWith("feishu")
                              ? "飞书"
                              : t.source_channel.toUpperCase()}
                          </span>
                        </span>
                      </td>
                      <td className="px-3 py-3.5 whitespace-nowrap">
                        <code className="rounded bg-zinc-100 dark:bg-zinc-800 px-1.5 py-0.5 text-[11px] text-zinc-700 dark:text-zinc-300">
                          {t.contract.target_repo}
                        </code>
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
                                  : "后台执行线程已离线（点开可一键唤醒）")}
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
                            No-Op · 全绿无需改
                          </span>
                        ) : f2pPassed ? (
                          <span className="inline-flex items-center gap-1 text-emerald-600 dark:text-emerald-400 font-medium">
                            <CheckCircle2 className="h-3.5 w-3.5" />
                            Fail → Pass
                          </span>
                        ) : (
                          <span className="inline-flex items-center gap-1 text-red-500 font-medium">
                            <XCircle className="h-3.5 w-3.5" />
                            未通过
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
                            <GitPullRequestDraft className="h-3 w-3" />
                            {t.deliverable_pr_number ? `PR #${t.deliverable_pr_number}` : "建单链接"}
                            <ArrowUpRight className="h-3 w-3" />
                          </a>
                        ) : (
                          <span className="text-zinc-400">—</span>
                        )}
                      </td>
                      <td className="px-3 py-3.5 text-right font-mono tabular-nums text-zinc-600 dark:text-zinc-300 whitespace-nowrap">
                        ${t.total_cost_usd.toFixed(4)}
                      </td>
                      <td className="py-3.5 pl-3 pr-5 text-right font-mono tabular-nums text-emerald-600 dark:text-emerald-400 whitespace-nowrap">
                        {t.estimated_net_minutes_saved != null
                          ? `${t.estimated_net_minutes_saved}m`
                          : "—"}
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </SurfaceCard>
    </div>
  );
}
