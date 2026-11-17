import React from "react";
import { BookMarked, FolderGit2, GitBranch, Loader2, Plus, Terminal } from "lucide-react";
import { toast } from "sonner";
import type { RepositoryItem } from "../types/api";
import { apiFetch } from "../lib/utils";
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

export function RepositoriesView({ repos, onRefresh }: Props) {
  const [open, setOpen] = React.useState(false);
  const [source, setSource] = React.useState("");
  const [repoId, setRepoId] = React.useState("");
  const [branch, setBranch] = React.useState("");
  const [loading, setLoading] = React.useState(false);

  const handleOnboard = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!source.trim()) return;
    setLoading(true);
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
      setOpen(false);
      await onRefresh();
    } catch (err: any) {
      toast.error(err.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h2 className="text-lg font-semibold text-zinc-900 dark:text-zinc-100">
            受管业务代码仓库与决策账本
          </h2>
          <p className="text-xs text-zinc-500 dark:text-zinc-400 mt-0.5">
            零代码侵入挂载本地或远端 Git 仓库 · 自动解析 .ai/DECISIONS.yaml 中的团队架构红线
          </p>
        </div>

        <Dialog open={open} onOpenChange={setOpen}>
          <DialogTrigger asChild>
            <Button variant="primary" size="lg">
              <Plus className="h-4 w-4" />
              接入新代码仓库
            </Button>
          </DialogTrigger>
          <DialogContent>
            <h3 className="text-base font-semibold text-zinc-900 dark:text-zinc-100">
              一键接入新业务仓库 (Auto-Onboarding)
            </h3>
            <p className="mt-1 text-xs text-zinc-500 dark:text-zinc-400">
              支持填入 GitHub <code className="font-mono">owner/repo</code>、SSH/HTTPS 克隆地址或宿主机本地目录，自动探测 Python / Node / Go 测试运行器。
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
                <Button type="button" variant="outline" onClick={() => setOpen(false)}>
                  取消
                </Button>
                <Button type="submit" variant="primary" disabled={loading}>
                  {loading && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                  确认接入
                </Button>
              </div>
            </form>
          </DialogContent>
        </Dialog>
      </div>

      <div className="grid gap-4">
        {repos.map((r) => (
          <SurfaceCard key={r.repo_id} className="p-5 space-y-4">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="flex items-center gap-3">
                <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-blue-500/10 text-blue-600 dark:text-blue-400">
                  <FolderGit2 className="h-5 w-5" />
                </div>
                <div>
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-sm font-semibold text-zinc-900 dark:text-zinc-100">
                      {r.repo_id}
                    </span>
                    <span className="inline-flex items-center gap-1 rounded-md bg-zinc-100 dark:bg-zinc-800 px-2 py-0.5 font-mono text-[11px] text-zinc-600 dark:text-zinc-300">
                      <GitBranch className="h-3 w-3" />
                      {r.default_base_branch}
                    </span>
                  </div>
                  <div className="text-xs text-zinc-500 mt-0.5">
                    {r.github_repo} · <code className="text-[11px]">{r.local_path}</code>
                  </div>
                </div>
              </div>

              <div className="inline-flex items-center gap-1.5 rounded-lg border border-zinc-200/80 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-950 px-3 py-1.5 font-mono text-[11px] text-zinc-600 dark:text-zinc-400">
                <Terminal className="h-3.5 w-3.5 text-zinc-400" />
                {r.pytest_cmd.join(" ")}
              </div>
            </div>

            <div className="rounded-xl border border-zinc-200/70 dark:border-zinc-800/80 bg-zinc-50/60 dark:bg-zinc-950/50 p-4 space-y-2.5">
              <div className="flex items-center gap-1.5 text-xs font-semibold text-zinc-700 dark:text-zinc-300">
                <BookMarked className="h-3.5 w-3.5 text-teal-500" />
                生效中的组织决策账本 (.ai/DECISIONS.yaml · {r.active_policies?.length ?? 0} 条正式策略)
              </div>
              {!r.active_policies?.length ? (
                <p className="text-xs text-zinc-400">暂无已登记策略</p>
              ) : (
                <div className="divide-y divide-zinc-200/60 dark:divide-zinc-800/70 text-xs">
                  {r.active_policies.map((p) => (
                    <div key={p.id} className="py-2 first:pt-0 last:pb-0 space-y-0.5">
                      <div className="flex items-center gap-2">
                        <span className="rounded bg-teal-500/10 px-1.5 py-0.2 font-mono text-[11px] font-medium text-teal-700 dark:text-teal-300">
                          {p.id}
                        </span>
                        <code className="text-[11px] text-zinc-500">{p.scope}</code>
                      </div>
                      <p className="text-zinc-800 dark:text-zinc-200 font-medium">{p.rule}</p>
                      {p.reason && <p className="text-[11.5px] text-zinc-500">原因：{p.reason}</p>}
                    </div>
                  ))}
                </div>
              )}
            </div>
          </SurfaceCard>
        ))}
      </div>
    </div>
  );
}
