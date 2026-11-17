export interface HealthInfo {
  status: string;
  inference_mode: string;
  fast_model: string;
  reasoning_model: string;
  reviewer_model?: string;
  reviewer_provider?: string;
  registered_repositories: string[];
  github_configured: boolean;
  github_poller_enabled: boolean;
  feishu_configured: boolean;
  feishu_webhook_configured: boolean;
  feishu_app_configured: boolean;
  feishu_ws_active: boolean;
}

export interface MetricsInfo {
  total_delegated: number;
  contract_confirmed: number;
  evidence_gate_passed: number;
  delivered_ready: number;
  pending_merge_approval?: number;
  accepted_without_rework: number;
  handed_off_incomplete: number;
  abandoned_or_reverted: number;
  total_cost_usd: number;
  estimated_net_human_minutes_saved: number;
}

export interface DecisionPolicy {
  id: string;
  scope: string;
  rule: string;
  reason: string;
  status: string;
  owner: string;
  verified_at: string;
}

export interface RepositoryItem {
  repo_id: string;
  github_repo: string;
  local_path: string;
  default_base_branch: string;
  pytest_cmd: string[];
  lint_cmd: string[];
  active_policies: DecisionPolicy[];
}

export interface UnresolvedDecision {
  question: string;
  options: string[];
  default_assumption: string;
  impact_scope: string;
}

export interface TaskContract {
  contract_version: number;
  objective: string;
  target_repo: string;
  base_branch: string;
  base_commit_sha: string;
  reproduction_evidence: string;
  target_repro_test: string;
  acceptance_criteria: string[];
  permitted_scopes: string[];
  out_of_scope: string[];
  unresolved_decisions: UnresolvedDecision[];
  resolved_decisions: Record<string, string>;
  max_budget_usd: number;
  owner_id: string;
}

export interface BlockingReviewIssue {
  severity: "P0" | "P1";
  category: string;
  path: string;
  line?: number | null;
  message: string;
}

export interface ReviewerReport {
  verdict?: "APPROVE" | "BLOCK";
  reviewer_model?: string;
  skill_used?: string;
  ac_covered: boolean;
  ac_assessment: string[];
  decision_ledger_compliant: boolean;
  ledger_violations: string[];
  blocking_issues?: BlockingReviewIssue[];
  side_effect_findings: string[];
  unverified_risks: string[];
  summary: string;
}

export interface VerificationEvidence {
  verified_commit_sha: string;
  repro_test_target: string;
  repro_test_failed_on_base: boolean;
  repro_test_passed_on_head: boolean;
  existing_test_suite_passed: boolean;
  tests_weakened_check: boolean;
  scope_compliance_check: boolean;
  static_analysis_passed: boolean;
  anti_tampering_violations: string[];
  scope_violations: string[];
  unverified_risks: string[];
  reviewer_report?: ReviewerReport | null;
}

export interface MergeApprovalCardInfo {
  pr_number: number;
  pr_url: string;
  verified_commit_sha: string;
  ci_checks_count: number;
  reviewer_model: string;
  reviewer_verdict: string;
  reviewer_summary: string;
  repro_test_target: string;
  residual_risk: string;
  ready_at: string;
  approved_by?: string | null;
  approved_at?: string | null;
}

export interface HandoffBundle {
  reason: string;
  failure_class: string;
  root_cause_clues: string[];
  attempted_branch: string;
  worktree_path: string;
  modified_files: string[];
  failing_test_summary: string;
  recommended_next_step: string;
}

export interface TaskItem {
  task_id: string;
  trace_id: string;
  raw_request: string;
  source_channel: string;
  contract: TaskContract;
  lifecycle_status: string;
  auto_publish_pr: boolean;
  auto_merge?: boolean;
  branch_name: string;
  worktree_path: string;
  head_commit_sha?: string | null;
  verification_evidence?: VerificationEvidence | null;
  merge_approval_card?: MergeApprovalCardInfo | null;
  handoff_bundle?: HandoffBundle | null;
  deliverable_pr_url?: string | null;
  deliverable_pr_number?: number | null;
  pr_review_comment_url?: string | null;
  merged_commit_sha?: string | null;
  failure_class: string;
  failure_reason?: string | null;
  sandbox_attempt_count: number;
  total_cost_usd: number;
  agent_elapsed_seconds: number;
  estimated_net_minutes_saved?: number | null;
  created_at: string;
  updated_at: string;
}

export interface CheckpointItem {
  id: number;
  step_name: string;
  lifecycle_status: string;
  note: string;
  created_at: string;
}

export interface FeishuCardPayload {
  msg_type?: string;
  card?: {
    header?: {
      title?: { content?: string };
      template?: string;
    };
    elements?: Array<{
      tag: string;
      content?: string;
      actions?: Array<{
        tag: string;
        type?: string;
        url?: string;
        text?: { content?: string };
      }>;
    }>;
  };
}

export interface TaskDetailResponse {
  task: TaskItem;
  checkpoints: CheckpointItem[];
  diff: string;
  verification_log: string;
  feishu_card_preview: FeishuCardPayload;
}

export interface FeishuIntegrationStatus {
  webhook_configured: boolean;
  webhook_masked: string;
  app_id: string;
  app_secret_configured: boolean;
  ws_active: boolean;
  callback_webhook_path: string;
}
