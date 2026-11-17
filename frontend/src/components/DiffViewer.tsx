import React from "react";
import { Check, Copy, FileCode2, Terminal } from "lucide-react";
import { toast } from "sonner";
import { Button } from "./ui/primitives";
import { cn } from "../lib/utils";

interface ParsedDiffLine {
  type: "add" | "del" | "hunk" | "ctx";
  oldNo: string;
  newNo: string;
  content: string;
}

interface ParsedDiffFile {
  filename: string;
  adds: number;
  dels: number;
  lines: ParsedDiffLine[];
}

function parseUnifiedDiff(diff: string): ParsedDiffFile[] {
  if (!diff || !diff.trim()) return [];
  const rawFiles = diff.split(/^diff --git /m).filter(Boolean);
  return rawFiles.map((chunk) => {
    const rawLines = chunk.split("\n");
    const firstHeader = rawLines[0] || "";
    const matchB = firstHeader.match(/b\/(.+)$/);
    const filename = matchB ? matchB[1].trim() : firstHeader.trim();

    let adds = 0;
    let dels = 0;
    let oldCursor = 0;
    let newCursor = 0;
    const lines: ParsedDiffLine[] = [];

    for (let i = 1; i < rawLines.length; i++) {
      const line = rawLines[i];
      if (
        line.startsWith("index ") ||
        line.startsWith("--- ") ||
        line.startsWith("+++ ") ||
        line.startsWith("new file mode") ||
        line.startsWith("deleted file mode")
      ) {
        continue;
      }
      const hunkMatch = line.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)$/);
      if (hunkMatch) {
        oldCursor = parseInt(hunkMatch[1], 10);
        newCursor = parseInt(hunkMatch[2], 10);
        lines.push({ type: "hunk", oldNo: "…", newNo: "…", content: line });
        continue;
      }
      if (line.startsWith("+")) {
        adds++;
        lines.push({
          type: "add",
          oldNo: "",
          newNo: String(newCursor++),
          content: line.slice(1),
        });
      } else if (line.startsWith("-")) {
        dels++;
        lines.push({
          type: "del",
          oldNo: String(oldCursor++),
          newNo: "",
          content: line.slice(1),
        });
      } else if (line.length > 0 || i < rawLines.length - 1) {
        lines.push({
          type: "ctx",
          oldNo: oldCursor ? String(oldCursor++) : "",
          newNo: newCursor ? String(newCursor++) : "",
          content: line.startsWith(" ") ? line.slice(1) : line,
        });
      }
    }
    return { filename, adds, dels, lines };
  });
}

export function DiffViewer({
  diff,
  verificationLog,
}: {
  diff: string;
  verificationLog?: string;
}) {
  const [copied, setCopied] = React.useState(false);
  const [showLogs, setShowLogs] = React.useState(false);
  const files = React.useMemo(() => parseUnifiedDiff(diff), [diff]);

  const copyPatch = async () => {
    if (!diff) return;
    await navigator.clipboard.writeText(diff);
    setCopied(true);
    toast.success("Git 补丁已复制到剪贴板");
    setTimeout(() => setCopied(false), 1800);
  };

  return (
    <div className="space-y-4">
      {!files.length ? (
        <div className="rounded-xl border border-dashed border-zinc-200 dark:border-zinc-800 p-8 text-center text-xs text-zinc-500">
          暂无代码变更补丁（沙箱提交后自动展示逐行 Diff）
        </div>
      ) : (
        <div className="space-y-3">
          <div className="flex items-center justify-between">
            <span className="text-xs text-zinc-500 dark:text-zinc-400">
              共修改 <b className="text-zinc-900 dark:text-zinc-100">{files.length}</b> 个文件
            </span>
            <Button variant="outline" size="sm" onClick={copyPatch}>
              {copied ? <Check className="h-3.5 w-3.5 text-emerald-500" /> : <Copy className="h-3.5 w-3.5" />}
              复制完整 Patch
            </Button>
          </div>

          {files.map((file) => (
            <div
              key={file.filename}
              className="overflow-hidden rounded-xl border border-zinc-200 dark:border-zinc-800 bg-zinc-950 text-zinc-200"
            >
              <div className="flex items-center justify-between border-b border-zinc-800 bg-zinc-900/90 px-3.5 py-2 text-xs font-mono">
                <div className="flex items-center gap-2 text-zinc-200">
                  <FileCode2 className="h-3.5 w-3.5 text-blue-400" />
                  <span>{file.filename}</span>
                </div>
                <div className="flex items-center gap-2 tabular-nums text-[11px]">
                  <span className="text-emerald-400">+{file.adds}</span>
                  <span className="text-red-400">-{file.dels}</span>
                </div>
              </div>

              <div className="max-h-[420px] overflow-auto font-mono text-[12px] leading-5">
                <table className="w-full border-collapse">
                  <tbody>
                    {file.lines.map((ln, idx) => (
                      <tr
                        key={idx}
                        className={cn(
                          ln.type === "add" && "bg-emerald-500/12 text-emerald-300",
                          ln.type === "del" && "bg-red-500/12 text-red-300",
                          ln.type === "hunk" && "bg-blue-500/10 text-blue-300"
                        )}
                      >
                        <td className="w-10 select-none border-r border-zinc-800/80 px-2 text-right text-[11px] text-zinc-600 tabular-nums">
                          {ln.oldNo}
                        </td>
                        <td className="w-10 select-none border-r border-zinc-800/80 px-2 text-right text-[11px] text-zinc-600 tabular-nums">
                          {ln.newNo}
                        </td>
                        <td className="w-5 select-none px-1.5 text-center text-zinc-500">
                          {ln.type === "add" ? "+" : ln.type === "del" ? "-" : ""}
                        </td>
                        <td className="whitespace-pre pr-4">{ln.content}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ))}
        </div>
      )}

      {verificationLog && (
        <div className="rounded-xl border border-zinc-200 dark:border-zinc-800 overflow-hidden">
          <button
            type="button"
            onClick={() => setShowLogs((v) => !v)}
            className="flex w-full items-center justify-between bg-zinc-100/80 dark:bg-zinc-900 px-3.5 py-2.5 text-xs font-medium text-zinc-700 dark:text-zinc-300 hover:bg-zinc-200/50 dark:hover:bg-zinc-800/80 cursor-pointer"
          >
            <span className="flex items-center gap-2">
              <Terminal className="h-3.5 w-3.5 text-zinc-500" />
              沙箱工具调用轨迹与 Pytest 原始验证日志
            </span>
            <span className="text-[11px] text-blue-600 dark:text-blue-400">
              {showLogs ? "收起日志" : "展开查看"}
            </span>
          </button>
          {showLogs && (
            <pre className="max-h-80 overflow-auto bg-zinc-950 p-3.5 text-[11.5px] leading-relaxed text-zinc-300 font-mono">
              {verificationLog}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}
