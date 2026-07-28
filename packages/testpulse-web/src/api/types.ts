/**
 * Mirrors the response models the API publishes.
 *
 * Hand-written rather than generated from openapi.json. A generator would be
 * better with more endpoints, but there are ten, and a generated file nobody
 * reads is a worse place for the two comments below than a file somebody edits.
 *
 * Two nullable fields carry meaning and must not be defaulted away on this side:
 * `passRate` is null (not 0) when nothing scored, and `retryCount` is null (not
 * 0) when the source format cannot report retries.
 */

export type TestStatus = "passed" | "failed" | "skipped" | "error";

export interface Suite {
  name: string;
}

export interface RunSummary {
  id: number;
  started_at: string;
  finished_at: string | null;
  commit_sha: string | null;
  branch: string | null;
  environment: string | null;
  source_format: string;
  total: number;
  passed: number;
  failed: number;
  skipped: number;
  errored: number;
  duration_ms: number;
}

export interface SuiteHealth {
  suite_name: string;
  runs_in_window: number;
  total_tests: number;
  flaky_count: number;
  newly_failing_count: number;
  /** Null when nothing in the window was scored. Not the same as zero. */
  pass_rate: number | null;
  mean_run_duration_ms: number;
  run_duration_trend_ms_per_run: number;
  recent_runs: RunSummary[];
}

export interface TestMetrics {
  test_id: string;
  display_name: string;
  runs_in_window: number;
  scored_runs: number;
  pass_rate: number | null;
  flip_rate: number;
  /** Ranks the rolling-flip strategy only. A same-commit finding can score 0. */
  flakiness_score: number;
  mean_duration_ms: number;
  p95_duration_ms: number;
  duration_trend_ms_per_run: number;
  first_seen_at: string;
  last_failed_at: string | null;
  consecutive_failures: number;
  is_newly_failing: boolean;
  is_flaky: boolean;
  flake_evidence: string[];
  is_quarantined: boolean;
}

export interface PagedTests {
  suite_name: string;
  total: number;
  limit: number;
  offset: number;
  sort_by: string;
  order: string;
  items: TestMetrics[];
}

export interface TimelinePoint {
  run_id: number;
  started_at: string;
  commit_sha: string | null;
  branch: string | null;
  status: TestStatus;
  raw_status: string;
  duration_ms: number;
  /** Null means the format cannot report retries, not that there were none. */
  retry_count: number | null;
  failure_message: string | null;
}

export interface TestDetail {
  metrics: TestMetrics;
  timeline: TimelinePoint[];
  attachments: string[];
}

export interface FailureCluster {
  template: string;
  count: number;
  representative: string;
  test_ids: string[];
}

export interface QuarantineEntry {
  suite_name: string;
  test_id: string;
  quarantined_at: string;
  expires_at: string;
  expires_after_days: number;
  /** Negative once expired, so the UI can say how far overdue it is. */
  days_remaining: number;
  is_expired: boolean;
  reason: string | null;
  quarantined_by: string | null;
}

export interface QuarantineList {
  suite_name: string;
  entries: QuarantineEntry[];
  debt_count: number;
}
