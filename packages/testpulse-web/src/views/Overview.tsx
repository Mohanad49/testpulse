import { Link } from "react-router-dom";
import { api, useAsync } from "../api/client";
import { DurationTrendChart, PassRateChart, type RunPoint } from "../components/charts";
import {
  Badge,
  Card,
  EmptyState,
  ErrorState,
  LoadingBlock,
  SectionHeading,
  Skeleton,
  Trend,
  formatMs,
  runAxisLabels,
  formatPercent,
  formatWhen,
} from "../components/primitives";

/**
 * Headline numbers, two trends, and the recent runs table.
 *
 * The four cards are the four questions someone opens this page to answer, in
 * the order they ask them: is it green, is it lying to me, is it slow, how big
 * is it.
 */

function StatCard({
  label,
  value,
  sub,
  tone,
  loading,
}: {
  label: string;
  value: string;
  sub?: React.ReactNode;
  tone?: "danger" | "warn" | "ok";
  loading?: boolean;
}) {
  const colour =
    tone === "danger"
      ? "var(--status-failed)"
      : tone === "warn"
        ? "var(--status-error)"
        : tone === "ok"
          ? "var(--status-passed)"
          : "var(--fg)";
  return (
    <Card>
      <p className="text-xs font-medium" style={{ color: "var(--fg-muted)" }}>
        {label}
      </p>
      {loading ? (
        <Skeleton className="mt-2 h-8 w-20" />
      ) : (
        <p className="tnum mt-1 text-3xl font-semibold tracking-tight" style={{ color: colour }}>
          {value}
        </p>
      )}
      <div className="mt-1 min-h-[1rem] text-xs" style={{ color: "var(--fg-subtle)" }}>
        {loading ? <Skeleton className="h-3 w-28" /> : sub}
      </div>
    </Card>
  );
}

export function Overview({ suite }: { suite: string }) {
  const { data, error, loading, reload } = useAsync(
    (signal) => api.health(suite, signal),
    [suite],
  );

  if (error) return <ErrorState message={error} onRetry={reload} />;

  // Runs arrive newest first for the table; charts need oldest first or every
  // trend reads backwards.
  const chronological = (data?.recent_runs ?? []).slice().reverse();
  const labels = runAxisLabels(chronological.map((run) => run.started_at));
  const points: RunPoint[] = chronological.map((run, index) => {
    const scored = run.passed + run.failed + run.errored;
    return {
      label: labels[index],
      passRate: scored ? run.passed / scored : 0,
      durationMs: run.duration_ms,
      total: run.total,
      failed: run.failed + run.errored,
    };
  });

  const flaky = data?.flaky_count ?? 0;
  const newlyFailing = data?.newly_failing_count ?? 0;

  return (
    <div className="animate-fade-rise space-y-5">
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard
          loading={loading}
          label="Pass rate"
          value={formatPercent(data?.pass_rate ?? null)}
          sub={
            data ? `across ${data.runs_in_window} run${data.runs_in_window === 1 ? "" : "s"}` : ""
          }
          tone={
            data?.pass_rate == null ? undefined : data.pass_rate >= 0.95 ? "ok" : "danger"
          }
        />
        <StatCard
          loading={loading}
          label="Flaky tests"
          value={String(flaky)}
          sub={flaky > 0 ? "same-commit or rolling-flip evidence" : "no evidence of flakiness"}
          tone={flaky > 0 ? "warn" : "ok"}
        />
        <StatCard
          loading={loading}
          label="Newly failing"
          value={String(newlyFailing)}
          sub={newlyFailing > 0 ? "clean history, now failing" : "no new regressions"}
          tone={newlyFailing > 0 ? "danger" : "ok"}
        />
        <StatCard
          loading={loading}
          label="Suite duration"
          value={data ? formatMs(data.mean_run_duration_ms) : "—"}
          sub={data ? <Trend msPerRun={data.run_duration_trend_ms_per_run} /> : ""}
        />
      </div>

      <div className="grid gap-3 lg:grid-cols-2">
        <Card as="section">
          <SectionHeading
            title="Pass rate over time"
            hint="One point per run, oldest first."
          />
          {loading ? <Skeleton className="h-[168px] w-full" /> : <PassRateChart data={points} />}
        </Card>
        <Card as="section">
          <SectionHeading
            title="Suite duration over time"
            hint="Wall-clock per run. Estimated for formats that omit an end time."
          />
          {loading ? (
            <Skeleton className="h-[168px] w-full" />
          ) : (
            <DurationTrendChart data={points} />
          )}
        </Card>
      </div>

      <section>
        <SectionHeading title="Recent runs" hint="Newest first." />
        {loading ? (
          <LoadingBlock rows={5} label="Loading recent runs" />
        ) : !data || data.recent_runs.length === 0 ? (
          <EmptyState
            title="No runs stored for this suite"
            detail="Ingest a report with `testpulse ingest` or POST it to /api/ingest."
          />
        ) : (
          <Card className="overflow-x-auto p-0">
            <table className="w-full min-w-[720px] text-sm">
              <caption className="sr-only">
                Recent runs for {suite}, newest first
              </caption>
              <thead>
                <tr
                  className="text-left text-xs"
                  style={{ color: "var(--fg-muted)", borderBottom: "1px solid var(--border)" }}
                >
                  <th scope="col" className="px-3 py-2 font-medium">Started</th>
                  <th scope="col" className="px-3 py-2 font-medium">Commit</th>
                  <th scope="col" className="px-3 py-2 font-medium">Branch</th>
                  <th scope="col" className="px-3 py-2 text-right font-medium">Pass</th>
                  <th scope="col" className="px-3 py-2 text-right font-medium">Failed</th>
                  <th scope="col" className="px-3 py-2 text-right font-medium">Total</th>
                  <th scope="col" className="px-3 py-2 text-right font-medium">Duration</th>
                </tr>
              </thead>
              <tbody>
                {data.recent_runs.map((run) => {
                  const scored = run.passed + run.failed + run.errored;
                  const rate = scored ? run.passed / scored : null;
                  const broken = run.failed + run.errored;
                  return (
                    <tr
                      key={run.id}
                      className="transition-colors duration-150"
                      style={{ borderBottom: "1px solid var(--border)" }}
                    >
                      <td className="tnum px-3 py-2 whitespace-nowrap">
                        {formatWhen(run.started_at)}
                      </td>
                      <td className="px-3 py-2 font-mono text-xs" style={{ color: "var(--fg-muted)" }}>
                        {run.commit_sha ? run.commit_sha.slice(0, 7) : "—"}
                      </td>
                      <td className="px-3 py-2 text-xs" style={{ color: "var(--fg-muted)" }}>
                        {run.branch ?? "—"}
                      </td>
                      <td className="tnum px-3 py-2 text-right">{formatPercent(rate)}</td>
                      <td className="tnum px-3 py-2 text-right">
                        {broken > 0 ? (
                          <Badge tone="danger">{broken}</Badge>
                        ) : (
                          <span style={{ color: "var(--fg-subtle)" }}>0</span>
                        )}
                      </td>
                      <td className="tnum px-3 py-2 text-right" style={{ color: "var(--fg-muted)" }}>
                        {run.total}
                      </td>
                      <td className="tnum px-3 py-2 text-right">{formatMs(run.duration_ms)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </Card>
        )}
      </section>

      {flaky > 0 && (
        <p className="text-xs" style={{ color: "var(--fg-muted)" }}>
          <Link
            to={`/suites/${encodeURIComponent(suite)}/flaky`}
            className="underline underline-offset-2"
            style={{ color: "var(--accent)" }}
          >
            Review the {flaky} flaky test{flaky === 1 ? "" : "s"}
          </Link>{" "}
          before trusting this pass rate.
        </p>
      )}
    </div>
  );
}
