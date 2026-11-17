import React from "react";
import { ExternalLink, MessageSquare } from "lucide-react";
import type { FeishuCardPayload } from "../types/api";
import { cn } from "../lib/utils";

const HEADER_GRADIENTS: Record<string, string> = {
  green: "from-emerald-600 to-teal-500",
  orange: "from-amber-500 to-orange-500",
  blue: "from-blue-600 to-indigo-500",
  wathet: "from-cyan-600 to-sky-500",
};

function renderFormattedLine(line: string, idx: number) {
  // Split bold **...** and inline code `...` cleanly without raw dangerouslySetInnerHTML
  const tokens = line.split(/(\*\*[^*]+\*\*|`[^`]+`)/g);
  return (
    <div key={idx} className="min-h-[1.25rem]">
      {tokens.map((tok, i) => {
        if (tok.startsWith("**") && tok.endsWith("**")) {
          return (
            <strong key={i} className="font-semibold text-zinc-900 dark:text-zinc-100">
              {tok.slice(2, -2)}
            </strong>
          );
        }
        if (tok.startsWith("`") && tok.endsWith("`")) {
          return (
            <code
              key={i}
              className="rounded bg-zinc-100 dark:bg-zinc-800 px-1.5 py-0.5 font-mono text-[11.5px] text-blue-600 dark:text-blue-300"
            >
              {tok.slice(1, -1)}
            </code>
          );
        }
        return <React.Fragment key={i}>{tok}</React.Fragment>;
      })}
    </div>
  );
}

export function FeishuCardPreview({ payload }: { payload?: FeishuCardPayload | null }) {
  const card = payload?.card;
  if (!card) return null;

  const tpl = card.header?.template ?? "blue";
  const title = card.header?.title?.content ?? "飞书交互消息卡片";
  const gradient = HEADER_GRADIENTS[tpl] ?? HEADER_GRADIENTS.blue;

  return (
    <div className="w-full max-w-lg overflow-hidden rounded-xl border border-zinc-200/90 bg-white shadow-md dark:border-zinc-800 dark:bg-zinc-900">
      {/* Authentic Feishu Bot Sender Bar */}
      <div className="flex items-center gap-2 border-b border-zinc-100 dark:border-zinc-800/80 bg-zinc-50/80 dark:bg-zinc-950/50 px-3.5 py-2 text-[11px] text-zinc-500">
        <span className="flex h-4 w-4 items-center justify-center rounded bg-blue-600 text-white">
          <MessageSquare className="h-2.5 w-2.5" />
        </span>
        <span className="font-medium text-zinc-700 dark:text-zinc-300">AI 研发数字员工</span>
        <span className="rounded bg-blue-500/10 px-1.5 py-0.2 text-[10px] font-medium text-blue-600 dark:text-blue-400">
          机器人交互卡片
        </span>
      </div>

      {/* Card Colored Header */}
      <div className={cn("bg-gradient-to-r px-4 py-2.5 text-xs font-semibold text-white", gradient)}>
        {title}
      </div>

      {/* Card Markdown & Action Elements */}
      <div className="space-y-3 p-4 text-[12.5px] leading-relaxed text-zinc-700 dark:text-zinc-300">
        {(card.elements ?? []).map((el, idx) => {
          if (el.tag === "markdown" && el.content) {
            return (
              <div key={idx} className="space-y-0.5">
                {el.content.split("\n").map((ln, lIdx) => renderFormattedLine(ln, lIdx))}
              </div>
            );
          }
          if (el.tag === "action" && el.actions?.length) {
            return (
              <div key={idx} className="flex flex-wrap gap-2 pt-2 border-t border-zinc-100 dark:border-zinc-800">
                {el.actions.map((act, aIdx) => {
                  const label = act.text?.content ?? "操作";
                  const isPrimary = act.type === "primary";
                  const cls = cn(
                    "inline-flex items-center gap-1 rounded-lg px-3 py-1.5 text-xs font-medium transition-colors",
                    isPrimary
                      ? "bg-blue-600 text-white hover:bg-blue-500"
                      : "border border-zinc-200 bg-zinc-50 text-zinc-700 hover:bg-zinc-100 dark:border-zinc-700 dark:bg-zinc-800 dark:text-zinc-200"
                  );
                  if (act.url) {
                    return (
                      <a key={aIdx} href={act.url} target="_blank" rel="noreferrer" className={cls}>
                        {label}
                        <ExternalLink className="h-3 w-3 opacity-80" />
                      </a>
                    );
                  }
                  return (
                    <span key={aIdx} className={cls}>
                      {label}
                    </span>
                  );
                })}
              </div>
            );
          }
          return null;
        })}
      </div>
    </div>
  );
}
