import React from "react";
import {
  AlertTriangle,
  BookMarked,
  CheckCircle2,
  Edit3,
  FolderGit2,
  GitBranch,
  HelpCircle,
  Loader2,
  PauseCircle,
  PlayCircle,
  Plus,
  RefreshCw,
  Settings2,
  ShieldAlert,
  ShieldCheck,
  Terminal,
  Trash2,
  Unplug,
  UserCheck,
} from "lucide-react";
import { toast } from "sonner";
import type {
  DecisionPolicy,
  PolicyCategory,
  RepositoryItem,
} from "../types/api";
import { apiFetch, cn } from "../lib/utils";
import {
  Button,
  Dialog,
  DialogContent,
  DialogTrigger,
  SurfaceCard,
} from "../components/ui/primitives";

interface Props {
  repos: RepositoryItem[];
  onRefresh: () => Promise<void>;
}

const CATEGORY_META: Record<
  PolicyCategory,
  { label: string; badgeClass: string }
> = {
  ARCHITECTURE: {
    label: "架构与依赖禁令",
    badgeClass:
      "bg-purple-500/10 text-purple-700 ring-purple-500/25 dark:bg-purple-500/15 dark:text-purple-300 dark:ring-purple-400/30",
  },
  SECURITY: {
    label: "安全与鉴权规范",
    badgeClass:
      "bg-rose-500/10 text-rose-700 ring-rose-500/25 dark:bg-rose-500/15 dark:text-rose-300 dark:ring-rose-400/30",
  },
  TESTING: {
    label: "测试与 CI 必跑步骤",
    badgeClass:
      "bg-blue-500/10 text-blue-700 ring-blue-500/25 dark:bg-blue-500/15 dark:text-blue-300 dark:ring-blue-400/30",
  },
  CODE_QUALITY: {
    label: "代码质量与异常处理",
    badgeClass:
      "bg-teal-500/10 text-teal-700 ring-teal-500/25 dark:bg-teal-500/15 dark:text-teal-300 dark:ring-teal-400/30",
  },
};

function renderSourceLabel(p: DecisionPolicy): string {
  if (p.source_type === "TASK_PROMOTED") {
    return p.source_ref ? `任务沉淀 (${p.source_ref})` : "历史任务复盘沉淀";
  }
  if (p.source_type === "REPO_SYNC") {
    return `仓库内置导入 (${p.source_ref || ".ai/DECISIONS.yaml"})`;
  }
  return "控制台人工配置";
}

interface PolicyEditorState {
  repoId: string;
  mode: "create" | "edit";
  policyId?: string;
  title: string;
  category: PolicyCategory;
  scope: string;
  rule: string;
  reason: string;
  owner: string;
  status: "APPROVED_POLICY" | "PAUSED";
}

interface RepoConfigEditorState {
  repoId: string;
  defaultBaseBranch: string;
  pytestCmdText: string;
  lintCmdText: string;
  defaultJiraProject: string;
}

export function RepositoriesView({ repos, onRefresh }: Props) {
  const [showHelp, setShowHelp] = React.useState(false);

  // Onboard repository dialog state
  const [onboardOpen, setOnboardOpen] = React.useState(false);
  const [source, setSource] = React.useState("");
  const [repoId, setRepoId] = React.useState("");
  const [branch, setBranch] = React.useState("");
  const [onboardLoading, setOnboardLoading] = React.useState(false);

  // Repo settings edit dialog state
  const [repoEditor, setRepoEditor] = React.useState<RepoConfigEditorState | null>(null);
  const [savingRepo, setSavingRepo] = React.useState(false);

  // Policy create/edit dialog state
  const [editor, setEditor] = React.useState<PolicyEditorState | null>(null);
  const [savingPolicy, setSavingPolicy] = React.useState(false);
  const [busyKey, setBusyKey] = React.useState<string | null>(null);

  const handleOnboard = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!source.trim()) return;
    setOnboardLoading(true);
    try {
      const p = await apiFetch<{ repo_id: string }>("/api/repositories/onboard", {
        method: "POST",
        body: JSON.stringify({
          source: source.trim(),
          repo_id: repoId.trim() || null,
          base_branch: branch.trim() || null,
        }),
      });
      toast.success(`仓库 ${p.repo_id} 已成功接入并识别测试命令`);
      setSource("");
      setRepoId("");
      setBranch("");
      setOnboardOpen(false);
      await onRefresh();
    } catch (err: any) {
      toast.error(err.message);
    } finally {
      setOnboardLoading(false);
    }
  };

  const openRepoConfigEditor = (r: RepositoryItem) => {
    setRepoEditor({
      repoId: r.repo_id,
      defaultBaseBranch: r.default_base_branch || "main",
      pytestCmdText: (r.pytest_cmd ?? []).join(" "),
      lintCmdText: (r.lint_cmd ?? []).join(" "),
      defaultJiraProject: r.default_jira_project || "",
    });
  };

  const handleSaveRepoConfig = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!repoEditor) return;
    const pytestParts = repoEditor.pytestCmdText
      .trim()
      .split(/\s+/)
      .filter(Boolean);
    if (pytestParts.length === 0) {
      toast.error("测试门禁命令不能为空");
      return;
    }
    const lintParts = repoEditor.lintCmdText
      .trim()
      .split(/\s+/)
      .filter(Boolean);

    setSavingRepo(true);
    try {
      await apiFetch(`/api/repositories/${encodeURIComponent(repoEditor.repoId)}`, {
        method: "PUT",
        body: JSON.stringify({
          default_base_branch: repoEditor.defaultBaseBranch.trim() || "main",
          pytest_cmd: pytestParts,
          lint_cmd: lintParts,
          default_jira_project: repoEditor.defaultJiraProject.trim().toUpperCase(),
        }),
      });
      toast.success(`已更新仓库 ${repoEditor.repoId} 的基线与测试门禁配置`);
      setRepoEditor(null);
      await onRefresh();
    } catch (err: any) {
      toast.error(err.message);
    } finally {
      setSavingRepo(false);
    }
  };

  const handleUnbindRepo = async (r: RepositoryItem) => {
    if (
      !window.confirm(
        `确认从控制台解绑仓库「${r.repo_id}」吗？\n\n注：仅移除控制台托管配置与规则，不会删除宿主机目录 ${r.local_path} 中的任何代码。`
      )
    ) {
      return;
    }
    const key = `unbind:${r.repo_id}`;
    setBusyKey(key);
    try {
      await apiFetch(`/api/repositories/${encodeURIComponent(r.repo_id)}`, {
        method: "DELETE",
      });
      toast.success(`已解绑仓库 ${r.repo_id}`);
      await onRefresh();
    } catch (err: any) {
      toast.error(err.message);
    } finally {
      setBusyKey(null);
    }
  };

  const openCreatePolicy = (targetRepoId: string) => {
    setEditor({
      repoId: targetRepoId,
      mode: "create",
      title: "",
      category: "ARCHITECTURE",
      scope: "*",
      rule: "",
      reason: "",
      owner: "jamesyang",
      status: "APPROVED_POLICY",
    });
  };

  const openEditPolicy = (targetRepoId: string, p: DecisionPolicy) => {
    const cat = (
      p.category in CATEGORY_META ? p.category : "CODE_QUALITY"
    ) as PolicyCategory;
    setEditor({
      repoId: targetRepoId,
      mode: "edit",
      policyId: p.id,
      title: p.title || "",
      category: cat,
      scope: p.scope || "*",
      rule: p.rule || "",
      reason: p.reason || "",
      owner: p.owner || "jamesyang",
      status: p.status === "APPROVED_POLICY" ? "APPROVED_POLICY" : "PAUSED",
    });
  };

  const handleSavePolicy = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!editor || !editor.rule.trim()) return;
    setSavingPolicy(true);
    try {
      const url =
        editor.mode === "create"
          ? `/api/repositories/${encodeURIComponent(editor.repoId)}/policies`
          : `/api/repositories/${encodeURIComponent(editor.repoId)}/policies/${encodeURIComponent(editor.policyId!)}`;
      const method = editor.mode === "create" ? "POST" : "PUT";
      const saved = await apiFetch<DecisionPolicy>(url, {
        method,
        body: JSON.stringify({
          title: editor.title.trim(),
          category: editor.category,
          scope: editor.scope.trim() || "*",
          rule: editor.rule.trim(),
          reason: editor.reason.trim(),
          owner: editor.owner.trim() || "jamesyang",
          status: editor.status,
        }),
      });
      if (saved.health_status === "INVALID_SCOPE") {
        toast.warning(
          `规则 ${saved.id} 已保存，但作用路径在仓库中不存在，已自动暂停向 AI 注入，请检查路径`
        );
      } else {
        toast.success(
          editor.mode === "create"
            ? `已新增规则 ${saved.id}（实时路径校验通过）`
            : `已更新规则 ${saved.id}`
        );
      }
      setEditor(null);
      await onRefresh();
    } catch (err: any) {
      toast.error(err.message);
    } finally {
      setSavingPolicy(false);
    }
  };

  const handleTogglePolicy = async (targetRepoId: string, p: DecisionPolicy) => {
    const key = `toggle:${targetRepoId}:${p.id}`;
    setBusyKey(key);
    try {
      const updated = await apiFetch<DecisionPolicy>(
        `/api/repositories/${encodeURIComponent(targetRepoId)}/policies/${encodeURIComponent(p.id)}/toggle`,
        { method: "POST" }
      );
      toast.success(
        updated.status === "APPROVED_POLICY"
          ? `规则 ${p.id} 已启用`
          : `规则 ${p.id} 已停用（不再注入 AI）`
      );
      await onRefresh();
    } catch (err: any) {
      toast.error(err.message);
    } finally {
      setBusyKey(null);
    }
  };

  const handleVerifyPolicy = async (targetRepoId: string, p: DecisionPolicy) => {
    const key = `verify:${targetRepoId}:${p.id}`;
    setBusyKey(key);
    try {
      await apiFetch(
        `/api/repositories/${encodeURIComponent(targetRepoId)}/policies/${encodeURIComponent(p.id)}/verify`,
        {
          method: "POST",
          body: JSON.stringify({ owner: p.owner || "jamesyang" }),
        }
      );
      toast.success(`已确认规则 ${p.id} 有效，核验日期已更新为今天`);
      await onRefresh();
    } catch (err: any) {
      toast.error(err.message);
    } finally {
      setBusyKey(null);
    }
  };

  const handleDeletePolicy = async (targetRepoId: string, p: DecisionPolicy) => {
    if (!window.confirm(`确认删除规则 ${p.id}「${p.title || p.rule.slice(0, 24)}」吗？`)) {
      return;
    }
    const key = `del:${targetRepoId}:${p.id}`;
    setBusyKey(key);
    try {
      await apiFetch(
        `/api/repositories/${encodeURIComponent(targetRepoId)}/policies/${encodeURIComponent(p.id)}`,
        { method: "DELETE" }
      );
      toast.success(`规则 ${p.id} 已删除`);
      await onRefresh();
    } catch (err: any) {
      toast.error(err.message);
    } finally {
      setBusyKey(null);
    }
  };

  const handleSyncRepoPolicies = async (targetRepoId: string) => {
    const key = `sync:${targetRepoId}`;
    setBusyKey(key);
    try {
      const res = await apiFetch<{ added: number }>(
        `/api/repositories/${encodeURIComponent(targetRepoId)}/policies/sync-repo`,
        { method: "POST" }
      );
      if (res.added > 0) {
        toast.success(`已从仓库 .ai/DECISIONS.yaml 增量导入 ${res.added} 条新规则`);
      } else {
        toast.info("仓库内置 .ai/DECISIONS.yaml 暂无新增工程规则");
      }
      await onRefresh();
    } catch (err: any) {
      toast.error(err.message);
    } finally {
      setBusyKey(null);
    }
  };

  return (
    <div className="space-y-5">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <div className="flex items-center gap-2">
            <h2 className="text-lg font-semibold text-zinc-900 dark:text-zinc-100">
              代码仓库与研发工程规范
            </h2>
            <button
              type="button"
              onClick={() => setShowHelp((v) => !v)}
              className="inline-flex items-center gap-1 rounded-md border border-zinc-200 dark:border-zinc-800 px-2 py-0.5 text-[11px] text-zinc-500 hover:text-zinc-800 dark:hover:text-zinc-200 cursor-pointer"
            >
              <HelpCircle className="h-3 w-3" />
              {showHelp ? "收起说明" : "运作机制"}
            </button>
          </div>
          <p className="text-xs text-zinc-500 dark:text-zinc-400 mt-0.5">
            管理受控 Git 仓库、测试/Lint 门禁命令与纯研发工程红线（产品需求请通过工单派发）
          </p>
        </div>

        <Dialog open={onboardOpen} onOpenChange={setOnboardOpen}>
          <DialogTrigger asChild>
            <Button variant="primary">
              <Plus className="h-4 w-4" />
              接入新代码仓库
            </Button>
          </DialogTrigger>
          <DialogContent>
            <h3 className="text-base font-semibold text-zinc-900 dark:text-zinc-100">
              接入业务代码仓库
            </h3>
            <p className="mt-1 text-xs text-zinc-500 dark:text-zinc-400">
              支持 GitHub <code className="font-mono">owner/repo</code>、SSH/HTTPS 克隆地址或宿主机本地目录，自动探测测试运行器。
            </p>
            <form onSubmit={handleOnboard} className="mt-4 space-y-3.5">
              <div>
                <label className="block text-xs font-medium text-zinc-700 dark:text-zinc-300 mb-1">
                  仓库源地址或本地绝对路径 *
                </label>
                <input
                  type="text"
                  required
                  value={source}
                  onChange={(e) => setSource(e.target.value)}
                  placeholder="git@github.com:org/service.git 或 /path/to/repo"
                  className="h-9 w-full rounded-lg border border-zinc-200 bg-white px-3 text-xs font-mono dark:border-zinc-800 dark:bg-zinc-950"
                />
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs font-medium text-zinc-700 dark:text-zinc-300 mb-1">
                    自定义仓库 ID (可选)
                  </label>
                  <input
                    type="text"
                    value={repoId}
                    onChange={(e) => setRepoId(e.target.value)}
                    placeholder="自动推断"
                    className="h-9 w-full rounded-lg border border-zinc-200 bg-white px-3 text-xs font-mono dark:border-zinc-800 dark:bg-zinc-950"
                  />
                </div>
                <div>
                  <label className="block text-xs font-medium text-zinc-700 dark:text-zinc-300 mb-1">
                    默认基线分支 (可选)
                  </label>
                  <input
                    type="text"
                    value={branch}
                    onChange={(e) => setBranch(e.target.value)}
                    placeholder="自动探测 main/dev"
                    className="h-9 w-full rounded-lg border border-zinc-200 bg-white px-3 text-xs font-mono dark:border-zinc-800 dark:bg-zinc-950"
                  />
                </div>
              </div>

              <div className="flex justify-end gap-2 pt-2">
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => setOnboardOpen(false)}
                >
                  取消
                </Button>
                <Button type="submit" variant="primary" disabled={onboardLoading}>
                  {onboardLoading && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                  确认接入
                </Button>
              </div>
            </form>
          </DialogContent>
        </Dialog>
      </div>

      {/* On-demand Help Panel (hidden by default so screen stays clean) */}
      {showHelp && (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3 animate-in fade-in">
          <div className="rounded-xl border border-zinc-200/80 bg-white/90 p-3.5 dark:border-zinc-800/80 dark:bg-zinc-900/50 text-xs">
            <div className="font-semibold text-zinc-800 dark:text-zinc-100">1. 零侵入独立托管</div>
            <p className="mt-1 text-[11.5px] text-zinc-500 dark:text-zinc-400">
              规则存储在数字员工本地 SQLite 数据库中，标明来源与维护人，不污染业务代码仓库。
            </p>
          </div>
          <div className="rounded-xl border border-zinc-200/80 bg-white/90 p-3.5 dark:border-zinc-800/80 dark:bg-zinc-900/50 text-xs">
            <div className="font-semibold text-zinc-800 dark:text-zinc-100">2. 只管工程不管需求</div>
            <p className="mt-1 text-[11.5px] text-zinc-500 dark:text-zinc-400">
              仅收录依赖分层禁令、安全规范、本地构建/CI 必跑命令与代码质量规范。
            </p>
          </div>
          <div className="rounded-xl border border-zinc-200/80 bg-white/90 p-3.5 dark:border-zinc-800/80 dark:bg-zinc-900/50 text-xs">
            <div className="font-semibold text-zinc-800 dark:text-zinc-100">3. 路径失效自动熔断</div>
            <p className="mt-1 text-[11.5px] text-zinc-500 dark:text-zinc-400">
              实时校验作用目录与引用脚本在仓库中是否存在；代码重构后旧规则自动熔断停止注入 AI。
            </p>
          </div>
          <div className="rounded-xl border border-zinc-200/80 bg-white/90 p-3.5 dark:border-zinc-800/80 dark:bg-zinc-900/50 text-xs">
            <div className="font-semibold text-zinc-800 dark:text-zinc-100">4. 编码+审查双门禁</div>
            <p className="mt-1 text-[11.5px] text-zinc-500 dark:text-zinc-400">
              有效规则按修改文件匹配注入编码内核，并在 PR 审查阶段由异构审查模型逐条验真。
            </p>
          </div>
        </div>
      )}

      {/* Empty state if 0 repos */}
      {repos.length === 0 ? (
        <SurfaceCard className="p-12 text-center space-y-3">
          <FolderGit2 className="h-8 w-8 mx-auto text-zinc-400" />
          <div className="text-sm font-medium text-zinc-800 dark:text-zinc-200">
            尚未接入任何业务代码仓库
          </div>
          <p className="text-xs text-zinc-500 max-w-md mx-auto">
            点击右上角「接入新代码仓库」填入本地 Git 仓库路径或 GitHub 地址，即可开始派发编码任务。
          </p>
        </SurfaceCard>
      ) : (
        <div className="grid gap-5">
          {repos.map((r) => {
            const allPolicies = r.policies ?? r.active_policies ?? [];
            const activeCount =
              r.policy_summary?.active_effective ??
              allPolicies.filter(
                (p) => p.effective_for_ai !== false && p.status === "APPROVED_POLICY"
              ).length;
            const pausedCount =
              r.policy_summary?.paused ??
              allPolicies.filter((p) => p.status !== "APPROVED_POLICY").length;
            const unhealthyCount =
              r.policy_summary?.unhealthy ??
              allPolicies.filter(
                (p) => p.health_status && p.health_status !== "HEALTHY"
              ).length;

            return (
              <SurfaceCard key={r.repo_id} className="p-5 space-y-4">
                {/* Repo Top Metadata & Config Controls */}
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="flex items-center gap-3">
                    <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-blue-500/10 text-blue-600 dark:text-blue-400">
                      <FolderGit2 className="h-5 w-5" />
                    </div>
                    <div>
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-mono text-sm font-semibold text-zinc-900 dark:text-zinc-100">
                          {r.repo_id}
                        </span>
                        <span className="inline-flex items-center gap-1 rounded-md bg-zinc-100 dark:bg-zinc-800 px-2 py-0.5 font-mono text-[11px] text-zinc-600 dark:text-zinc-300">
                          <GitBranch className="h-3 w-3" />
                          默认分支: {r.default_base_branch}
                        </span>
                        {r.default_jira_project && (
                          <span className="inline-flex items-center rounded-md bg-indigo-500/10 px-2 py-0.5 font-mono text-[11px] text-indigo-600 dark:text-indigo-300">
                            Jira: {r.default_jira_project}
                          </span>
                        )}
                      </div>
                      <div className="text-xs text-zinc-500 mt-0.5">
                        {r.github_repo ? `${r.github_repo} · ` : ""}
                        <code className="text-[11px]">{r.local_path}</code>
                      </div>
                    </div>
                  </div>

                  <div className="flex flex-wrap items-center gap-2">
                    <div
                      className="inline-flex items-center gap-1.5 rounded-lg border border-zinc-200/80 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-950 px-2.5 py-1 font-mono text-[11px] text-zinc-600 dark:text-zinc-400 max-w-md truncate"
                      title={r.pytest_cmd.join(" ")}
                    >
                      <Terminal className="h-3.5 w-3.5 text-zinc-400 shrink-0" />
                      <span className="truncate">{r.pytest_cmd.join(" ")}</span>
                    </div>

                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={() => openRepoConfigEditor(r)}
                      title="修改默认分支、测试运行器命令、Lint 命令或默认 Jira 项目"
                    >
                      <Settings2 className="h-3.5 w-3.5" />
                      配置仓库
                    </Button>

                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      className="text-zinc-500 hover:text-rose-600 dark:hover:text-rose-400"
                      disabled={busyKey === `unbind:${r.repo_id}`}
                      onClick={() => handleUnbindRepo(r)}
                      title="从控制台解绑此仓库（不删除宿主机代码）"
                    >
                      <Unplug className="h-3.5 w-3.5" />
                      解绑
                    </Button>
                  </div>
                </div>

                {/* Governance Ledger Panel */}
                <div className="rounded-xl border border-zinc-200/70 dark:border-zinc-800/80 bg-zinc-50/60 dark:bg-zinc-950/50 p-4 space-y-3.5">
                  <div className="flex flex-wrap items-center justify-between gap-2 pb-2 border-b border-zinc-200/70 dark:border-zinc-800/80">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="flex items-center gap-1.5 text-xs font-semibold text-zinc-800 dark:text-zinc-200">
                        <BookMarked className="h-3.5 w-3.5 text-teal-500" />
                        研发工程红线与构建避坑规范
                      </span>
                      <span className="inline-flex items-center gap-1 rounded-full bg-emerald-500/10 px-2 py-0.5 text-[11px] font-medium text-emerald-700 dark:text-emerald-300">
                        <ShieldCheck className="h-3 w-3" />
                        {activeCount} 条生效
                      </span>
                      {unhealthyCount > 0 && (
                        <span className="inline-flex items-center gap-1 rounded-full bg-amber-500/15 px-2 py-0.5 text-[11px] font-medium text-amber-700 dark:text-amber-300">
                          <AlertTriangle className="h-3 w-3" />
                          {unhealthyCount} 条路径异常/待复核
                        </span>
                      )}
                      {pausedCount > 0 && (
                        <span className="inline-flex items-center gap-1 rounded-full bg-zinc-500/10 px-2 py-0.5 text-[11px] font-medium text-zinc-600 dark:text-zinc-400">
                          {pausedCount} 条已停用
                        </span>
                      )}
                    </div>

                    <div className="flex items-center gap-2">
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        disabled={busyKey === `sync:${r.repo_id}`}
                        onClick={() => handleSyncRepoPolicies(r.repo_id)}
                        title="从仓库本地 .ai/DECISIONS.yaml 增量导入新规则（不覆盖已有修改）"
                      >
                        <RefreshCw
                          className={cn(
                            "h-3 w-3",
                            busyKey === `sync:${r.repo_id}` && "animate-spin"
                          )}
                        />
                        同步仓库文件
                      </Button>
                      <Button
                        type="button"
                        variant="primary"
                        size="sm"
                        onClick={() => openCreatePolicy(r.repo_id)}
                      >
                        <Plus className="h-3.5 w-3.5" />
                        新增规则
                      </Button>
                    </div>
                  </div>

                  {allPolicies.length === 0 ? (
                    <div className="py-5 text-center space-y-2">
                      <p className="text-xs text-zinc-500 dark:text-zinc-400">
                        暂无额外工程红线，AI 默认遵守仓库现有代码结构与测试套件。
                      </p>
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        onClick={() => openCreatePolicy(r.repo_id)}
                      >
                        <Plus className="h-3.5 w-3.5" />
                        添加第一条研发工程红线
                      </Button>
                    </div>
                  ) : (
                    <div className="space-y-2.5">
                      {allPolicies.map((p) => {
                        const catKey = (
                          p.category in CATEGORY_META ? p.category : "CODE_QUALITY"
                        ) as PolicyCategory;
                        const catMeta = CATEGORY_META[catKey];
                        const isActive = p.status === "APPROVED_POLICY";
                        const isInvalidScope = p.health_status === "INVALID_SCOPE";
                        const isWarn =
                          p.health_status === "WARN_STALE_REF" ||
                          p.health_status === "WARN_NEEDS_REVIEW";
                        const effective = isActive && !isInvalidScope;

                        return (
                          <div
                            key={p.id}
                            className={cn(
                              "rounded-lg border p-3.5 transition-colors space-y-2",
                              !isActive
                                ? "border-zinc-200/60 bg-zinc-100/50 opacity-75 dark:border-zinc-800/60 dark:bg-zinc-900/30"
                                : isInvalidScope
                                  ? "border-rose-300/80 bg-rose-50/40 dark:border-rose-900/70 dark:bg-rose-950/20"
                                  : isWarn
                                    ? "border-amber-300/80 bg-amber-50/30 dark:border-amber-900/60 dark:bg-amber-950/15"
                                    : "border-zinc-200/80 bg-white dark:border-zinc-800/80 dark:bg-zinc-900/70"
                            )}
                          >
                            <div className="flex flex-wrap items-start justify-between gap-2">
                              <div className="flex flex-wrap items-center gap-2 min-w-0">
                                <span
                                  className={cn(
                                    "inline-flex items-center rounded-md px-2 py-0.5 text-[11px] font-semibold ring-1 ring-inset",
                                    catMeta.badgeClass
                                  )}
                                >
                                  {catMeta.label}
                                </span>
                                <span className="text-xs sm:text-[13px] font-semibold text-zinc-900 dark:text-zinc-100">
                                  {p.title || p.rule}
                                </span>
                                <span className="rounded bg-zinc-100 dark:bg-zinc-800 px-1.5 py-0.5 font-mono text-[10.5px] text-zinc-500 dark:text-zinc-400">
                                  {p.id}
                                </span>

                                {effective ? (
                                  <span className="inline-flex items-center gap-1 rounded-full bg-emerald-500/10 px-2 py-0.5 text-[10.5px] font-medium text-emerald-700 dark:text-emerald-300">
                                    <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" />
                                    生效中
                                  </span>
                                ) : !isActive ? (
                                  <span className="inline-flex items-center gap-1 rounded-full bg-zinc-500/15 px-2 py-0.5 text-[10.5px] font-medium text-zinc-600 dark:text-zinc-400">
                                    <span className="h-1.5 w-1.5 rounded-full bg-zinc-400" />
                                    已停用
                                  </span>
                                ) : (
                                  <span className="inline-flex items-center gap-1 rounded-full bg-rose-500/15 px-2 py-0.5 text-[10.5px] font-semibold text-rose-700 dark:text-rose-300">
                                    <span className="h-1.5 w-1.5 rounded-full bg-rose-500" />
                                    已自动熔断（路径不存在）
                                  </span>
                                )}
                              </div>

                              <div className="flex items-center gap-1 shrink-0">
                                {(isWarn || isInvalidScope) && (
                                  <Button
                                    type="button"
                                    variant="outline"
                                    size="sm"
                                    disabled={busyKey === `verify:${r.repo_id}:${p.id}`}
                                    onClick={() => handleVerifyPolicy(r.repo_id, p)}
                                  >
                                    <CheckCircle2 className="h-3 w-3 text-emerald-600" />
                                    确认有效
                                  </Button>
                                )}
                                <Button
                                  type="button"
                                  variant="ghost"
                                  size="sm"
                                  onClick={() => openEditPolicy(r.repo_id, p)}
                                >
                                  <Edit3 className="h-3.5 w-3.5" />
                                  编辑
                                </Button>
                                <Button
                                  type="button"
                                  variant="ghost"
                                  size="sm"
                                  disabled={busyKey === `toggle:${r.repo_id}:${p.id}`}
                                  onClick={() => handleTogglePolicy(r.repo_id, p)}
                                >
                                  {isActive ? (
                                    <>
                                      <PauseCircle className="h-3.5 w-3.5 text-amber-500" />
                                      停用
                                    </>
                                  ) : (
                                    <>
                                      <PlayCircle className="h-3.5 w-3.5 text-emerald-500" />
                                      启用
                                    </>
                                  )}
                                </Button>
                                <Button
                                  type="button"
                                  variant="ghost"
                                  size="sm"
                                  className="text-rose-600 hover:text-rose-700 dark:text-rose-400"
                                  disabled={busyKey === `del:${r.repo_id}:${p.id}`}
                                  onClick={() => handleDeletePolicy(r.repo_id, p)}
                                >
                                  <Trash2 className="h-3.5 w-3.5" />
                                </Button>
                              </div>
                            </div>

                            <div className="space-y-1 text-xs">
                              <p className="text-zinc-800 dark:text-zinc-200 leading-relaxed">
                                <span className="font-medium text-zinc-500 dark:text-zinc-400">
                                  工程约束：
                                </span>
                                {p.rule}
                              </p>
                              {p.reason && (
                                <p className="text-[11.5px] text-zinc-500 dark:text-zinc-400 leading-relaxed">
                                  <span className="font-medium">原因：</span>
                                  {p.reason}
                                </p>
                              )}
                            </div>

                            <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 pt-1.5 border-t border-zinc-100 dark:border-zinc-800/70 text-[11px] text-zinc-500 dark:text-zinc-400">
                              <span className="inline-flex items-center gap-1">
                                <span>范围:</span>
                                <code className="rounded bg-zinc-100 dark:bg-zinc-800 px-1.5 py-0.5 font-mono text-zinc-700 dark:text-zinc-300">
                                  {p.scope || "*"}
                                </code>
                              </span>

                              {p.health_status === "INVALID_SCOPE" ? (
                                <span className="inline-flex items-center gap-1 font-medium text-rose-600 dark:text-rose-400">
                                  <ShieldAlert className="h-3.5 w-3.5" />
                                  {p.health_message}
                                </span>
                              ) : isWarn ? (
                                <span className="inline-flex items-center gap-1 font-medium text-amber-600 dark:text-amber-400">
                                  <AlertTriangle className="h-3.5 w-3.5" />
                                  {p.health_message}
                                </span>
                              ) : (
                                <span className="inline-flex items-center gap-1 text-emerald-600 dark:text-emerald-400">
                                  <CheckCircle2 className="h-3.5 w-3.5" />
                                  {p.health_message || "路径校验通过"}
                                </span>
                              )}

                              <span>来源: {renderSourceLabel(p)}</span>

                              <span className="inline-flex items-center gap-1 ml-auto">
                                <UserCheck className="h-3 w-3 text-zinc-400" />
                                {p.owner || "jamesyang"} · 核验于 {p.verified_at?.slice(0, 10) || "今日"}
                              </span>
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  )}
                </div>
              </SurfaceCard>
            );
          })}
        </div>
      )}

      {/* Edit Repository Config Modal */}
      <Dialog
        open={repoEditor !== null}
        onOpenChange={(open) => {
          if (!open) setRepoEditor(null);
        }}
      >
        <DialogContent className="max-w-lg">
          {repoEditor && (
            <>
              <h3 className="text-base font-semibold text-zinc-900 dark:text-zinc-100">
                配置仓库门禁与基线 · {repoEditor.repoId}
              </h3>
              <p className="mt-1 text-xs text-zinc-500 dark:text-zinc-400">
                调整默认基线分支、沙箱 <code className="font-mono">Fail-to-Pass</code> 测试运行器命令、静态检查命令及默认关联的 Jira Project。
              </p>

              <form onSubmit={handleSaveRepoConfig} className="mt-4 space-y-3.5 text-xs">
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="block font-medium text-zinc-700 dark:text-zinc-300 mb-1">
                      默认基线分支 *
                    </label>
                    <input
                      type="text"
                      required
                      value={repoEditor.defaultBaseBranch}
                      onChange={(e) =>
                        setRepoEditor({
                          ...repoEditor,
                          defaultBaseBranch: e.target.value,
                        })
                      }
                      placeholder="main / dev"
                      className="h-9 w-full rounded-lg border border-zinc-200 bg-white px-3 font-mono text-xs dark:border-zinc-800 dark:bg-zinc-950"
                    />
                  </div>
                  <div>
                    <label className="block font-medium text-zinc-700 dark:text-zinc-300 mb-1">
                      默认关联 Jira Project (可选)
                    </label>
                    <input
                      type="text"
                      value={repoEditor.defaultJiraProject}
                      onChange={(e) =>
                        setRepoEditor({
                          ...repoEditor,
                          defaultJiraProject: e.target.value.toUpperCase(),
                        })
                      }
                      placeholder="如 KAN / RAG"
                      className="h-9 w-full rounded-lg border border-zinc-200 bg-white px-3 font-mono text-xs uppercase dark:border-zinc-800 dark:bg-zinc-950"
                    />
                  </div>
                </div>

                <div>
                  <label className="block font-medium text-zinc-700 dark:text-zinc-300 mb-1">
                    单元测试门禁命令 (Test Runner) *
                  </label>
                  <input
                    type="text"
                    required
                    value={repoEditor.pytestCmdText}
                    onChange={(e) =>
                      setRepoEditor({
                        ...repoEditor,
                        pytestCmdText: e.target.value,
                      })
                    }
                    placeholder="/path/to/.venv/bin/python -m pytest"
                    className="h-9 w-full rounded-lg border border-zinc-200 bg-white px-3 font-mono text-xs dark:border-zinc-800 dark:bg-zinc-950"
                  />
                </div>

                <div>
                  <label className="block font-medium text-zinc-700 dark:text-zinc-300 mb-1">
                    静态代码检查命令 (Linter · 可选)
                  </label>
                  <input
                    type="text"
                    value={repoEditor.lintCmdText}
                    onChange={(e) =>
                      setRepoEditor({
                        ...repoEditor,
                        lintCmdText: e.target.value,
                      })
                    }
                    placeholder="留空则跳过；例如 .venv/bin/ruff check"
                    className="h-9 w-full rounded-lg border border-zinc-200 bg-white px-3 font-mono text-xs dark:border-zinc-800 dark:bg-zinc-950"
                  />
                </div>

                <div className="flex justify-end gap-2 pt-2">
                  <Button
                    type="button"
                    variant="outline"
                    onClick={() => setRepoEditor(null)}
                  >
                    取消
                  </Button>
                  <Button type="submit" variant="primary" disabled={savingRepo}>
                    {savingRepo && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                    保存仓库配置
                  </Button>
                </div>
              </form>
            </>
          )}
        </DialogContent>
      </Dialog>

      {/* Create / Edit Policy Modal */}
      <Dialog
        open={editor !== null}
        onOpenChange={(open) => {
          if (!open) setEditor(null);
        }}
      >
        <DialogContent className="max-w-xl">
          {editor && (
            <>
              <h3 className="text-base font-semibold text-zinc-900 dark:text-zinc-100">
                {editor.mode === "create"
                  ? `新增研发工程红线 · ${editor.repoId}`
                  : `编辑研发工程红线 · ${editor.policyId}`}
              </h3>
              <p className="mt-1 text-xs text-zinc-500 dark:text-zinc-400">
                保存后系统立即自动核验作用目录与引用文件在 <code className="font-mono">{editor.repoId}</code> 中是否存在。
              </p>

              <form onSubmit={handleSavePolicy} className="mt-4 space-y-3.5">
                <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                  <div className="sm:col-span-2">
                    <label className="block text-xs font-medium text-zinc-700 dark:text-zinc-300 mb-1">
                      一句话规则标题 *
                    </label>
                    <input
                      type="text"
                      required
                      value={editor.title}
                      onChange={(e) =>
                        setEditor({ ...editor, title: e.target.value })
                      }
                      placeholder="例如：模块依赖禁令：禁止在线代码 import legacy 目录"
                      className="h-9 w-full rounded-lg border border-zinc-200 bg-white px-3 text-xs dark:border-zinc-800 dark:bg-zinc-950"
                    />
                  </div>
                  <div>
                    <label className="block text-xs font-medium text-zinc-700 dark:text-zinc-300 mb-1">
                      工程分类
                    </label>
                    <select
                      value={editor.category}
                      onChange={(e) =>
                        setEditor({
                          ...editor,
                          category: e.target.value as PolicyCategory,
                        })
                      }
                      className="h-9 w-full rounded-lg border border-zinc-200 bg-white px-2.5 text-xs dark:border-zinc-800 dark:bg-zinc-950"
                    >
                      <option value="ARCHITECTURE">架构与依赖禁令</option>
                      <option value="SECURITY">安全与鉴权规范</option>
                      <option value="TESTING">测试与 CI 必跑步骤</option>
                      <option value="CODE_QUALITY">代码质量与异常处理</option>
                    </select>
                  </div>
                </div>

                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <div>
                    <label className="block text-xs font-medium text-zinc-700 dark:text-zinc-300 mb-1">
                      作用目录或文件路径 (Scope) *
                    </label>
                    <input
                      type="text"
                      required
                      value={editor.scope}
                      onChange={(e) =>
                        setEditor({ ...editor, scope: e.target.value })
                      }
                      placeholder="*（全局）或 src/order/* 或 app/main.py"
                      className="h-9 w-full rounded-lg border border-zinc-200 bg-white px-3 text-xs font-mono dark:border-zinc-800 dark:bg-zinc-950"
                    />
                  </div>
                  <div>
                    <label className="block text-xs font-medium text-zinc-700 dark:text-zinc-300 mb-1">
                      责任维护人 (Owner) *
                    </label>
                    <input
                      type="text"
                      required
                      value={editor.owner}
                      onChange={(e) =>
                        setEditor({ ...editor, owner: e.target.value })
                      }
                      placeholder="例如：jamesyang"
                      className="h-9 w-full rounded-lg border border-zinc-200 bg-white px-3 text-xs dark:border-zinc-800 dark:bg-zinc-950"
                    />
                  </div>
                </div>

                <div>
                  <label className="block text-xs font-medium text-zinc-700 dark:text-zinc-300 mb-1">
                    代码/构建级硬性约束 *
                  </label>
                  <textarea
                    required
                    rows={3}
                    value={editor.rule}
                    onChange={(e) =>
                      setEditor({ ...editor, rule: e.target.value })
                    }
                    placeholder="请写明具体的代码、依赖或提交前必跑命令要求（请勿在此处填写产品业务需求）..."
                    className="w-full rounded-lg border border-zinc-200 bg-white p-3 text-xs leading-relaxed dark:border-zinc-800 dark:bg-zinc-950"
                  />
                </div>

                <div>
                  <label className="block text-xs font-medium text-zinc-700 dark:text-zinc-300 mb-1">
                    制定原因（方便团队后续判断是否仍需保留）
                  </label>
                  <input
                    type="text"
                    value={editor.reason}
                    onChange={(e) =>
                      setEditor({ ...editor, reason: e.target.value })
                    }
                    placeholder="例如：线上 CI 会运行 --check 校验测试统计数，漏跑会导致流水线报错"
                    className="h-9 w-full rounded-lg border border-zinc-200 bg-white px-3 text-xs dark:border-zinc-800 dark:bg-zinc-950"
                  />
                </div>

                <div className="flex justify-end gap-2 pt-2">
                  <Button
                    type="button"
                    variant="outline"
                    onClick={() => setEditor(null)}
                  >
                    取消
                  </Button>
                  <Button type="submit" variant="primary" disabled={savingPolicy}>
                    {savingPolicy && (
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    )}
                    {editor.mode === "create" ? "保存并校验路径" : "更新并刷新核验"}
                  </Button>
                </div>
              </form>
            </>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}
