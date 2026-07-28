import { Link, useParams } from "react-router-dom";
import { api, useAsync } from "../api/client";
import { TestDurationChart } from "../components/charts";
import { StatusLegend, StatusTimeline } from "../components/StatusTimeline";
import {
  Badge,
  Card,
  ErrorState,
  LoadingBlock,
  SectionHeading,
  Trend,
  formatMs,
  formatPercent,
  formatWhen,
  runAxisLabels,
} from "../components/primitives";

export function TestDetail({ suite }: { suite: string }) {
  const params = useParams();
  const testId = params["*"] ?? "";
  const { data, error, loading, reload } = useAsync(
    (signal) => api.testDetail(suite, testId, signal),
    [suite, testId],
  );

  if (error) return <ErrorState message={error} onRetry={reload} />;
  if (loading) return <LoadingBlock rows={6} label="Loading test detail" />;
  if (!data) return null;

  const { metrics, timeline, attachments } = data;
  const durationLabels = runAxisLabels(timeline.map((point) => point.started_at));
  const durations = timeline.map((point, index) => ({
    label: durationLabels[index],
    durationMs: point.duration_ms,
  }));

  // Distinct failure messages, most recent first. Not clustered here: on one
  // test the useful question is "what did it say the last few times", and the
  // cross-test grouping lives on the Failures view.
  const messages = Array.from(
    new Map(
      timeline
        .filter((point) => point.failure_message)
        .reverse()
        .map((point) => [point.failure_message as string, point]),
    ).entries(),
  ).slice(0, 5);

  return (
    <div className="animate-fade-rise space-y-5">
      <div>
        <Link
          to={`/suites/${encodeURIComponent(suite)}/flaky`}
          className="text-xs underline underline-offset-2"
          style={{ color: "var(--accent)" }}
        >
          ← Back to flakiness
        </Link>
        <h1 className="mt-2 text-lg font-semibold tracking-tight">{metrics.display_name}</h1>
        <p className="mt-1 font-mono text-[11px] break-all" style={{ color: "var(--fg-subtle)" }}>
          {metrics.test_id}
        </p>
        <div className="mt-2 flex flex-wrap gap-1.5">
          {metrics.is_flaky &&
            metrics.flake_evidence.map((evidence) => (
              <Badge key={evidence} tone={evidence === "same-commit" ? "danger" : "warn"}>
                {evidence}
              </Badge>
            ))}
          {metrics.is_newly_failing && <Badge tone="danger">newly failing</Badge>}
          {metrics.is_quarantined && <Badge tone="accent">quarantined</Badge>}
          {!metrics.is_flaky && !metrics.is_newly_failing && <Badge tone="ok">healthy</Badge>}
        </div>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        {[
          { label: "Pass rate", value: formatPercent(metrics.pass_rate) },
          { label: "Flip rate", value: metrics.flip_rate.toFixed(2) },
          { label: "p95 duration", value: formatMs(metrics.p95_duration_ms) },
          { label: "Scored runs", value: String(metrics.scored_runs) },
        ].map((stat) => (
          <Card key={stat.label}>
            <p className="text-xs font-medium" style={{ color: "var(--fg-muted)" }}>
              {stat.label}
            </p>
            <p className="tnum mt-1 text-2xl font-semibold tracking-tight">{stat.value}</p>
          </Card>
        ))}
      </div>

      <Card as="section">
        <SectionHeading
          title="Run history"
          hint="Oldest first. Outlined cells passed only after a retry."
          actions={<StatusLegend />}
        />
        <StatusTimeline points={timeline} size="full" />
      </Card>

      <div className="grid gap-3 lg:grid-cols-2">
        <Card as="section">
          <SectionHeading
            title="Duration"
            hint={`Mean ${formatMs(metrics.mean_duration_ms)}, p95 ${formatMs(metrics.p95_duration_ms)}.`}
            actions={<Trend msPerRun={metrics.duration_trend_ms_per_run} />}
          />
          <TestDurationChart data={durations} />
        </Card>

        <Card as="section">
          <SectionHeading title="Facts" />
          <dl className="grid grid-cols-2 gap-y-2 text-xs">
            {[
              ["First seen", formatWhen(metrics.first_seen_at)],
              ["Last failed", formatWhen(metrics.last_failed_at)],
              ["Consecutive failures", String(metrics.consecutive_failures)],
              ["Runs in window", String(metrics.runs_in_window)],
            ].map(([term, value]) => (
              <div key={term} className="contents">
                <dt style={{ color: "var(--fg-muted)" }}>{term}</dt>
                <dd className="tnum">{value}</dd>
              </div>
            ))}
          </dl>

          {attachments.length > 0 && (
            <div className="mt-4">
              <p className="mb-1.5 text-xs font-medium" style={{ color: "var(--fg-muted)" }}>
                Attachments from the latest run
              </p>
              <ul className="space-y-1">
                {attachments.map((attachment) => (
                  <li
                    key={attachment}
                    className="font-mono text-[11px] break-all"
                    style={{ color: "var(--fg-subtle)" }}
                  >
                    {attachment}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </Card>
      </div>

      {messages.length > 0 && (
        <Card as="section">
          <SectionHeading title="Recent failure messages" hint="Distinct messages, newest first." />
          <ul className="space-y-2">
            {messages.map(([message, point]) => (
              <li key={message}>
                <p className="mb-1 text-xs tnum" style={{ color: "var(--fg-subtle)" }}>
                  {formatWhen(point.started_at)}
                  {point.commit_sha && ` · ${point.commit_sha.slice(0, 7)}`}
                </p>
                <pre
                  className="max-h-40 overflow-auto rounded p-2.5 font-mono text-[11px] leading-relaxed whitespace-pre-wrap"
                  style={{ backgroundColor: "var(--bg-subtle)", color: "var(--fg-muted)" }}
                >
                  {message}
                </pre>
              </li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  );
}
