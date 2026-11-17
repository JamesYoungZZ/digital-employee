import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export async function apiFetch<T>(url: string, options?: RequestInit): Promise<T> {
  const res = await fetch(url, {
    headers: {
      "Content-Type": "application/json",
      ...(options?.headers ?? {}),
    },
    ...options,
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(body?.detail || `请求失败 (HTTP ${res.status})`);
  }
  return body as T;
}

export const STATUS_META: Record<
  string,
  { label: string; tone: "amber" | "blue" | "purple" | "emerald" | "red" | "zinc"; pulse?: boolean }
> = {
  DRAFTING_CONTRACT: { label: "起草契约中", tone: "blue", pulse: true },
  WAITING_CLARIFICATION: { label: "待你拍板", tone: "amber", pulse: true },
  QUEUED: { label: "排队启动", tone: "blue", pulse: true },
  RUNNING_IN_SANDBOX: { label: "沙箱执行中", tone: "blue", pulse: true },
  VERIFYING_EVIDENCE: { label: "门禁验证中", tone: "purple", pulse: true },
  DELIVERED_LOCAL_READY: { label: "本地分支就绪", tone: "emerald" },
  PR_OPEN_FOLLOWING_UP: { label: "Draft PR 跟进中", tone: "emerald" },
  ACCEPTED_AND_CLOSED: { label: "已采纳闭环", tone: "emerald" },
  HANDED_OFF_INCOMPLETE: { label: "已交接人工", tone: "red" },
  ABANDONED_OR_REVERTED: { label: "已废弃", tone: "zinc" },
};
