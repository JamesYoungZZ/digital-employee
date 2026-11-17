import React from "react";
import {
  AlertTriangle,
  ArrowUpRight,
  BookMarked,
  CheckCircle2,
  Clock,
  Copy,
  FileDiff,
  GitBranch,
  GitPullRequestDraft,
  Loader2,
  MessageSquarePlus,
  Send,
  ShieldAlert,
  ShieldCheck,
  Sparkles,
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

export function TaskInspectorSheet({ detail, onClose, onMutated }: Props) {
  const [answers, setAnswers] = React.useState<Record<string, string>>({});
  const [followupText, setFollowupText] = React.useState("");
  const [ruleScope, setRuleScope] = React.useState("");
  const [ruleText, setRuleText] = React.useState("");
  const [showLedgerForm, setShowLedgerForm] = React.useState(false);
  const [busyAction, setBusyAction] = React.useState<string | null>(null);

  const taskId = detail?.task.task_id;

  React.useEffect(() => {
    if (!detail) return;
    const c = detail.task.contract;
    setRuleScope(c.permitted_scopes?.[0] ?? "*");
    const defaults: Record<string, string> = {};
    (c.unresolved_decisions ?? []).forEach((d, idx) => {
      defaults[String(idx)] = d.options?.[0] ?? d.default_assumption;
    });
    setAnswers(defaults);
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

  const runControl = async (action: string) => {
    setBusyAction(action);
    try {
      await apiFetch(`/api/tasks/${encodeURIComponent(t.task_id)}/control`, {
        method: "POST",
        body: JSON.stringify({ action }),
      });
      toast.success(`已执行操作：${action}`);
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
      toast.success("已确认契约决策，隔离沙箱已唤醒执行");
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
      toast.success("增量追改指令已下发，沙箱正在更新分支与 PR");
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
      const entry = await apiFetch<{ id: string }>(
        `/api/tasks/${encodeURIComponent(t.task_id)}/promote-decision`,
        {
          method: "POST",
          body: JSON.stringify({ scope: ruleScope, rule: ruleText.trim() }),
        }
      );
      setRuleText("");
      setShowLedgerForm(false);
      toast.success(`已沉淀团队规则 ${entry.id} 至 .ai/DECISIONS.yaml`);
    } catch (err: any) {
      toast.error(err.message);
    } finally {
      setBusyAction(null);
    }
  };

  return (
    <div className="fixed inset-0 z-40 flex justify-end bg-black/35 backdrop-blur-[1px]" onClick={onClose}>
      <div
        className="flex h-full w-full max-w-4xl flex-col border-l border-zinc-200 bg-white shadow-2xl dark:border-zinc-800 dark:bg-zinc-950"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Sticky Top Header */}
        <div className="flex items-center justify-between border-b border-zinc-200 dark:border-zinc-800 px-6 py-4">
          <div className="flex items-center gap-3">
            <span className="font-mono text-sm font-semibold text-zinc-900 dark:text-zinc-100">
              {t.task_id}
            </span>
            <StatusBadge status={t.lifecycle_status} />
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

          <div className="flex items-center gap-2">
            {t.deliverable_pr_url ? (
              <a
                href={t.deliverable_pr_url}
                target="_blank"
                rel="noreferrer"
                className="inline-flex h-8 items-center gap-1.5 rounded-lg bg-blue-600 px-3 text-xs font-medium text-white hover:bg-blue-500 shadow-xs"
              >
                <GitPullRequestDraft className="h-3.5 w-3.5" />
                {t.deliverable_pr_number ? `Draft PR #${t.deliverable_pr_number}` : "前往 GitHub 建单"}
                <ArrowUpRight className="h-3.5 w-3.5" />
              </a>
            ) : (
              ev?.repro_test_failed_on_base &&
              ev?.repro_test_passed_on_head && (
                <Button
                  variant="primary"
                  disabled={!!busyAction}
                  onClick={() => runControl("publish_pr")}
                >
                  <GitPullRequestDraft className="h-3.5 w-3.5" />
                  推送创建 Draft PR
                </Button>
              )
            )}

            {t.lifecycle_status === "WAITING_MERGE_APPROVAL" ? (
              <Button
                variant="emerald"
                disabled={!!busyAction}
                onClick={() => runControl("approve_merge")}
              >
                <CheckCircle2 className="h-3.5 w-3.5" />
                批准并自动 Squash 合入
              </Button>
            ) : (
              <Button
                variant="emerald"
                disabled={!!busyAction || t.lifecycle_status === "ACCEPTED_AND_CLOSED"}
                onClick={() => runControl("accept")}
              >
                <CheckCircle2 className="h-3.5 w-3.5" />
                标记已采纳
              </Button>
            )}

            <Button
              variant="outline"
              disabled={!!busyAction || t.lifecycle_status === "HANDED_OFF_INCOMPLETE"}
              onClick={() => runControl("cancel")}
            >
              中断交接
            </Button>

            <Button variant="ghost" size="icon" onClick={onClose}>
              <X className="h-4 w-4" />
            </Button>
          </div>
        </div>

        {/* Scrollable Body */}
        <div className="flex-1 overflow-y-auto px-6 py-5 space-y-5">
          {/* Active Running Banner */}
          {t.lifecycle_status === "WAITING_MERGE_APPROVAL" && (
            <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-emerald-500/40 bg-emerald-500/10 px-4 py-3.5 text-xs text-emerald-800 dark:text-emerald-200">
              <div>
                <div className="font-semibold text-sm">
                  🛎️ 全部 4 道前置门禁与线上 GitHub Actions CI 已通过，等待你的合入审批
                </div>
                <div className="mt-0.5 opacity-85">
                  点击右侧按钮（或在飞书审批卡上点批准）将立即执行 SHA 锁定的自动 Squash Merge。
                </div>
              </div>
              <Button
                variant="emerald"
                disabled={!!busyAction}
                onClick={() => runControl("approve_merge")}
              >
                <CheckCircle2 className="h-3.5 w-3.5" />
                批准并自动 Squash 合入
              </Button>
            </div>
          )}

          {isRunning && (
            <div className="flex items-center gap-3 rounded-xl border border-blue-500/30 bg-blue-500/8 px-4 py-3 text-xs text-blue-700 dark:text-blue-300">
              <Loader2 className="h-4 w-4 animate-spin shrink-0 text-blue-500" />
              <div>
                <span className="font-semibold">
                  隔离沙箱正在异步执行（第 {t.sandbox_attempt_count || 1} 轮尝试）
                </span>
                <span className="ml-2 opacity-80">
                  状态流每 3 秒自动无感同步，跑过 Fail-to-Pass 门禁后自动创建线上 GitHub Draft PR。
                </span>
              </div>
            </div>
          )}

          {/* 5-Second Micro Clarification Interactive Module */}
          {t.lifecycle_status === "WAITING_CLARIFICATION" &&
            c.unresolved_decisions?.length > 0 && (
              <div className="rounded-xl border border-amber-500/40 bg-amber-500/8 p-4 space-y-3.5">
                <div className="flex items-center gap-2 text-amber-700 dark:text-amber-300 text-xs font-semibold">
                  <AlertTriangle className="h-4 w-4 shrink-0" />
                  需你 5 秒微确认（发现影响行为边界的关键未决问题）
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
                    <div className="text-[11px] text-zinc-500">
                      默认假设：<code className="text-amber-700 dark:text-amber-300">{dec.default_assumption}</code>
                    </div>
                  </div>
                ))}

                <div className="flex items-center gap-2.5">
                  <Button
                    variant="primary"
                    disabled={busyAction === "clarify"}
                    onClick={() => submitClarify(false)}
                  >
                    提交所选方案并启动沙箱
                  </Button>
                  <Button
                    variant="outline"
                    disabled={busyAction === "clarify"}
                    onClick={() => submitClarify(true)}
                  >
                    直接按默认假设继续
                  </Button>
                </div>
              </div>
            )}

          {/* Verified No-Op Banner */}
          {ev?.noop_verified && (
            <div className="rounded-xl border border-emerald-500/35 bg-emerald-500/8 p-4 space-y-1.5 text-xs">
              <div className="flex items-center gap-2 font-semibold text-emerald-700 dark:text-emerald-300">
                <CheckCircle2 className="h-4 w-4" />
                沙箱独立实测已全绿 · 无需代码改动 (No-Op Clean Exit)
              </div>
              <p className="text-zinc-600 dark:text-zinc-300 leading-relaxed">
                {ev.noop_summary || "目标测试集已在远端基准分支全部通过，未产生多余改动或无效 PR。"}
              </p>
            </div>
          )}

          {/* Handoff Bundle Alert */}
          {t.handoff_bundle && (
            <div className="rounded-xl border border-red-500/35 bg-red-500/8 p-4 space-y-2 text-xs">
              <div className="flex items-center gap-2 font-semibold text-red-700 dark:text-red-300">
                <ShieldAlert className="h-4 w-4" />
                结构化交接包 ({t.handoff_bundle.failure_class})：{t.handoff_bundle.reason}
              </div>
              <p className="text-zinc-600 dark:text-zinc-400">
                建议下一步：{t.handoff_bundle.recommended_next_step}
              </p>
            </div>
          )}

          {/* Main 4 Radix Tabs */}
          <Tabs defaultValue="contract">
            <TabsList className="w-full justify-start">
              <TabsTrigger value="contract">
                <Sparkles className="h-3.5 w-3.5" />
                1. 契约与边界
              </TabsTrigger>
              <TabsTrigger value="evidence">
                <ShieldCheck className="h-3.5 w-3.5" />
                2. 证据门禁与盲区
              </TabsTrigger>
              <TabsTrigger value="diff">
                <FileDiff className="h-3.5 w-3.5" />
                3. 逐行补丁与日志
              </TabsTrigger>
              <TabsTrigger value="feishu">
                <Clock className="h-3.5 w-3.5" />
                4. 飞书卡片与状态轨迹
              </TabsTrigger>
            </TabsList>

            {/* TAB 1: CONTRACT */}
            <TabsContent value="contract" className="space-y-4">
              <div className="rounded-xl border border-zinc-200 dark:border-zinc-800 p-4 space-y-3">
                <div className="text-[11px] font-semibold uppercase tracking-wider text-zinc-400">
                  推断契约目标 (TaskContract v{c.contract_version})
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
                    <span className="text-zinc-400 block text-[11px]">基线 Commit</span>
                    <span className="font-mono">{c.base_branch}@{c.base_commit_sha.slice(0, 8)}</span>
                  </div>
                  <div>
                    <span className="text-zinc-400 block text-[11px]">真实模型耗时 / 成本</span>
                    <span className="font-mono">
                      {t.agent_elapsed_seconds.toFixed(1)}s · ${t.total_cost_usd.toFixed(4)}
                    </span>
                  </div>
                  <div>
                    <span className="text-zinc-400 block text-[11px]">净节省人工时间</span>
                    <span className="font-mono text-emerald-600 dark:text-emerald-400">
                      {t.estimated_net_minutes_saved != null ? `${t.estimated_net_minutes_saved} min` : "—"}
                    </span>
                  </div>
                </div>
              </div>

              <div className="grid sm:grid-cols-2 gap-4">
                <div className="rounded-xl border border-zinc-200 dark:border-zinc-800 p-4 space-y-2">
                  <div className="text-xs font-semibold text-zinc-700 dark:text-zinc-300">
                    允许修改白名单 (Permitted Scopes)
                  </div>
                  <div className="flex flex-wrap gap-1.5">
                    {c.permitted_scopes.map((s) => (
                      <code
                        key={s}
                        className="rounded-md bg-blue-500/10 px-2 py-0.5 text-[11.5px] text-blue-700 dark:text-blue-300"
                      >
                        {s}
                      </code>
                    ))}
                  </div>
                </div>

                <div className="rounded-xl border border-zinc-200 dark:border-zinc-800 p-4 space-y-2">
                  <div className="text-xs font-semibold text-zinc-700 dark:text-zinc-300">
                    明确禁止触碰边界 (Out of Scope)
                  </div>
                  <div className="flex flex-wrap gap-1.5">
                    {c.out_of_scope.slice(0, 6).map((s) => (
                      <code
                        key={s}
                        className="rounded-md bg-zinc-100 dark:bg-zinc-800 px-2 py-0.5 text-[11px] text-zinc-600 dark:text-zinc-400"
                      >
                        {s.slice(0, 48)}
                      </code>
                    ))}
                  </div>
                </div>
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

            {/* TAB 2: EVIDENCE */}
            <TabsContent value="evidence" className="space-y-4">
              {!ev ? (
                <div className="rounded-xl border border-dashed border-zinc-200 dark:border-zinc-800 p-10 text-center text-xs text-zinc-500">
                  沙箱尚未执行到门禁阶段，完成后在此展示硬性回归证据单
                </div>
              ) : (
                <>
                  <div className="grid grid-cols-2 sm:grid-cols-5 gap-2.5">
                    {[
                      { label: "基线复现失败 (Fail)", ok: ev.repro_test_failed_on_base },
                      { label: "修复后通过 (Pass)", ok: ev.repro_test_passed_on_head },
                      { label: "原有测试套件全绿", ok: ev.existing_test_suite_passed },
                      { label: "反作弊扫描 (零删改)", ok: ev.tests_weakened_check },
                      { label: "范围与 Linter 合规", ok: ev.scope_compliance_check && ev.static_analysis_passed },
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
                    <div className="rounded-xl border border-zinc-200 dark:border-zinc-800 p-4 space-y-2.5 text-xs">
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <span className="font-semibold text-zinc-800 dark:text-zinc-200">
                          独立跨模型 PR Review Agent 报告 (Skill: <code className="font-mono">{ev.reviewer_report.skill_used ?? "pr-code-review"}</code>)
                        </span>
                        <div className="flex items-center gap-2">
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
                            {ev.reviewer_report.verdict ?? "APPROVE"} · {(ev.reviewer_report.blocking_issues ?? []).length} Blocking
                          </span>
                        </div>
                      </div>
                      <p className="leading-relaxed text-zinc-600 dark:text-zinc-300">
                        {ev.reviewer_report.summary}
                      </p>
                      {(ev.reviewer_report.blocking_issues ?? []).length > 0 && (
                        <div className="rounded-lg border border-red-500/30 bg-red-500/8 p-3 space-y-1">
                          <div className="font-semibold text-red-600 dark:text-red-400">
                            严重阻断项 (Blocking Only)：
                          </div>
                          {(ev.reviewer_report.blocking_issues ?? []).map((b, idx) => (
                            <div key={idx} className="text-red-700 dark:text-red-300">
                              <b>[{b.severity} · {b.category}]</b>{" "}
                              {b.path ? <code className="font-mono">{b.path}{b.line ? `:${b.line}` : ""}</code> : null}{" "}
                              — {b.message}
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  )}

                  <div className="rounded-xl border border-amber-500/30 bg-amber-500/6 p-4 space-y-2 text-xs">
                    <div className="flex items-center gap-1.5 font-semibold text-amber-700 dark:text-amber-300">
                      <AlertTriangle className="h-4 w-4" />
                      未验证风险盲区诚实披露 (Unverified Risks — 人类 Review 只需重点关注以下项)
                    </div>
                    <ul className="list-disc pl-5 space-y-1 text-zinc-700 dark:text-zinc-300">
                      {(ev.reviewer_report?.unverified_risks ?? ev.unverified_risks ?? []).map(
                        (r, i) => (
                          <li key={i}>{r}</li>
                        )
                      )}
                    </ul>
                  </div>
                </>
              )}
            </TabsContent>

            {/* TAB 3: DIFF & LOGS */}
            <TabsContent value="diff">
              <DiffViewer diff={detail.diff} verificationLog={detail.verification_log} />
            </TabsContent>

            {/* TAB 4: FEISHU CARD & CHECKPOINTS */}
            <TabsContent value="feishu">
              <div className="grid lg:grid-cols-2 gap-5">
                <div className="space-y-2">
                  <div className="text-xs font-semibold text-zinc-600 dark:text-zinc-400">
                    系统推送给用户的飞书交互卡片 1:1 渲染：
                  </div>
                  <FeishuCardPreview payload={detail.feishu_card_preview} />
                </div>

                <div className="space-y-2">
                  <div className="text-xs font-semibold text-zinc-600 dark:text-zinc-400">
                    SQLite WAL 持久化检查点轨迹 ({detail.checkpoints.length} 步)：
                  </div>
                  <div className="rounded-xl border border-zinc-200 dark:border-zinc-800 p-4 max-h-[420px] overflow-y-auto">
                    <ol className="relative border-l border-zinc-200 dark:border-zinc-800 ml-2 space-y-3.5">
                      {detail.checkpoints.map((cp) => (
                        <li key={cp.id} className="ml-4 text-xs">
                          <span className="absolute -left-1.5 mt-1 h-2.5 w-2.5 rounded-full border border-white bg-blue-500 dark:border-zinc-950" />
                          <div className="flex items-center gap-2 font-mono text-[11px] text-zinc-400 tabular-nums">
                            <span>{cp.created_at.slice(11, 19)}</span>
                            <span className="font-semibold text-zinc-700 dark:text-zinc-200">
                              {cp.step_name}
                            </span>
                          </div>
                          {cp.note && (
                            <p className="mt-0.5 text-[11.5px] text-zinc-500 dark:text-zinc-400 break-all">
                              {cp.note.slice(0, 140)}
                            </p>
                          )}
                        </li>
                      ))}
                    </ol>
                  </div>
                </div>
              </div>
            </TabsContent>
          </Tabs>
        </div>

        {/* Sticky Bottom Follow-Up & Decision Ledger Bar */}
        <div className="border-t border-zinc-200 dark:border-zinc-800 bg-zinc-50/80 dark:bg-zinc-900/50 px-6 py-3.5 space-y-3">
          <form onSubmit={submitFollowup} className="flex items-center gap-2">
            <div className="relative flex-1">
              <MessageSquarePlus className="absolute left-3 top-2.5 h-4 w-4 text-zinc-400" />
              <input
                type="text"
                value={followupText}
                onChange={(e) => setFollowupText(e.target.value)}
                placeholder="追加修改意见（唤醒原沙箱增量修改并同步更新 GitHub Draft PR）..."
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
              沉淀规则
            </Button>
          </form>

          {showLedgerForm && (
            <form
              onSubmit={submitRule}
              className="flex flex-col sm:flex-row items-center gap-2 pt-1 animate-in fade-in"
            >
              <input
                type="text"
                value={ruleScope}
                onChange={(e) => setRuleScope(e.target.value)}
                placeholder="目录范围 src/..."
                className="h-8 w-full sm:w-44 rounded-lg border border-zinc-200 bg-white px-2.5 font-mono text-xs dark:border-zinc-800 dark:bg-zinc-950"
              />
              <input
                type="text"
                value={ruleText}
                onChange={(e) => setRuleText(e.target.value)}
                placeholder="输入需写入 .ai/DECISIONS.yaml 的长期架构/业务约束..."
                className="h-8 flex-1 w-full rounded-lg border border-zinc-200 bg-white px-2.5 text-xs dark:border-zinc-800 dark:bg-zinc-950"
              />
              <Button type="submit" variant="emerald" size="sm" disabled={busyAction === "rule"}>
                写入决策账本
              </Button>
            </form>
          )}
        </div>
      </div>
    </div>
  );
}
