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
  coding_runtime?: string;
  jira_configured?: boolean;
  jira_base_url?: string;
  jira_default_project?: string;
  jira_poller_enabled?: boolean;
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

export type PolicyCategory = "ARCHITECTURE" | "SECURITY" | "TESTING" | "CODE_QUALITY";
export type PolicySourceType = "CONSOLE_MANUAL" | "TASK_PROMOTED" | "REPO_SYNC";
export type PolicyHealthStatus =
  | "HEALTHY"
  | "WARN_STALE_REF"
  | "WARN_NEEDS_REVIEW"
  | "INVALID_SCOPE";

export interface DecisionPolicy {
  id: string;
  title: string;
  category: PolicyCategory;
  scope: string;
  rule: string;
  reason: string;
  status: "APPROVED_POLICY" | "PAUSED" | "ONE_OFF_WORKAROUND" | "DEPRECATED" | string;
  source_type: PolicySourceType | string;
  source_ref: string;
  owner: string;
  verified_at: string;
  updated_at?: string;
  health_status?: PolicyHealthStatus;
  health_message?: string;
  matched_files_count?: number | null;
  stale_references?: string[];
  effective_for_ai?: boolean;
}

export interface RepositoryPolicySummary {
  total: number;
  active_effective: number;
  paused: number;
  unhealthy: number;
}

export interface RepositoryItem {
  repo_id: string;
  github_repo: string;
  local_path: string;
  default_base_branch: string;
  pytest_cmd: string[];
  lint_cmd: string[];
  policies?: DecisionPolicy[];
  active_policies: DecisionPolicy[];
  policy_summary?: RepositoryPolicySummary;
}

export interface UnresolvedDecision {
  question: string;
  options: string[];
  default_assumption: string;
  impact_scope: string;
}

export interface JiraTicketDraft {
  project_key: string;
  issue_type: string;
  summary: string;
  description: string;
  acceptance_criteria: string[];
  priority: string;
  labels: string[];
  approved_by?: string | null;
  approved_at?: string | null;
}

export interface TaskContract {
  contract_version: number;
  jira_issue_key?: string;
  jira_issue_url?: string;
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
  suggestion?: string;
}

export interface ReviewAuditItem {
  dimension: string;
  status: "PASS" | "WARN" | "FAIL";
  detail: string;
}

export interface ReviewObservation {
  path: string;
  line?: number | null;
  note: string;
}

export interface ReviewerReport {
  verdict?: "APPROVE" | "BLOCK";
  quality_score?: number | null;
  merge_recommendation?: "MERGE_READY" | "MERGE_WITH_CAUTION" | "DO_NOT_MERGE" | null;
  merge_recommendation_label?: string;
  reviewer_model?: string;
  skill_used?: string;
  ac_covered: boolean;
  ac_assessment: string[];
  decision_ledger_compliant: boolean;
  ledger_violations: string[];
  change_overview?: string[];
  audit_checklist?: ReviewAuditItem[];
  blocking_issues?: BlockingReviewIssue[];
  notable_observations?: ReviewObservation[];
  side_effect_findings: string[];
  unverified_risks: string[];
  summary: string;
  full_report_url?: string;
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
  noop_verified?: boolean;
  noop_summary?: string;
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
  quality_score?: number;
  merge_recommendation?: string;
  merge_recommendation_label?: string;
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
  jira_draft?: JiraTicketDraft | null;
  jira_issue_key?: string;
  jira_issue_url?: string;
  jira_status?: string;
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
  current_step?: number;
  max_steps?: number;
  current_activity?: string;
  worker_alive?: boolean;
  total_cost_usd: number;
  agent_elapsed_seconds: number;
  estimated_net_minutes_saved?: number | null;
  created_at: string;
  updated_at: string;
}

export interface LiveStreamEvent {
  ts: string;
  attempt: number;
  step: number;
  max_steps: number;
  kind: string;
  icon: string;
  title: string;
  detail?: string;
  cost_usd?: number;
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
      fields?: Array<{
        is_short?: boolean;
        text?: { tag?: string; content?: string };
      }>;
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
  live_events?: LiveStreamEvent[];
  diff: string;
  verification_log: string;
  feishu_card_preview: FeishuCardPayload;
}

export interface FeishuIntegrationStatus {
  webhook_configured: boolean;
  webhook_masked: string;
  app_id: string;
  app_secret_configured: boolean;
  default_chat_id?: string;
  ws_active: boolean;
  callback_webhook_path: string;
}

export interface JiraProjectOption {
  key: string;
  name: string;
}

export interface JiraIntegrationStatus {
  configured: boolean;
  base_url: string;
  email: string;
  api_token_configured: boolean;
  default_project: string;
  poll_jql: string;
  poller_enabled: boolean;
  webhook_url: string;
  projects: JiraProjectOption[];
}

export interface JiraIssuePreview {
  key: string;
  url: string;
  project_key: string;
  issue_type: string;
  summary: string;
  description: string;
  status: string;
  priority: string;
  labels: string[];
  comments: string[];
}
