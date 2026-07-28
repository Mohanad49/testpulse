import { api, useAsync } from "../api/client";
import {
  Badge,
  Card,
  EmptyState,
  ErrorState,
  LoadingBlock,
  SectionHeading,
  Trend,
  formatMs,
} from "../components/primitives";

/**
 * Sorted by p95 rather than mean.
 *
 * The mean hides the case people actually care about. A test that usually takes
 * 200ms and occasionally takes 40 seconds has an unremarkable mean and is the
 * reason CI is slow, because the suite waits for the slow case every time it
 * happens. p95 surfaces it; the mean is shown alongside so the gap between the
 * two is visible, and a wide gap is itself the interesting signal.
 */
export function Slowest({ suite }: { suite: string }) {
  const { data, error, loading, reload } = useAsync(
    (signal) => api.tests(suite, { sortBy: "p95_duration_ms", order: "desc", limit: 40 }, signal),
    [suite],
  );

  if (error) return <ErrorState message={error} onRetry={reload} />;

  const items = data?.items ?? [];
  const slowest = items[0]?.p95_duration_ms ?? 1;

  return (
    <div className="animate-fade-rise space-y-4">
      <SectionHeading
        title="Slowest tests"
        hint="Ranked by p95, not mean. A test that is usually fast and occasionally terrible has an unremarkable mean and is why CI is slow."
      />

      {loading ? (
        <LoadingBlock rows={8} label="Loading slowest tests" />
      ) : items.length === 0 ? (
        <EmptyState title="No tests in the window" detail="Ingest a run to populate this view." />
      ) : (
        <Card className="overflow-x-auto p-0">
          <table className="w-full min-w-[820px] text-sm">
            <caption className="sr-only">Slowest tests in {suite}, by p95 duration</caption>
            <thead>
              <tr
                className="text-left text-xs"
                style={{ color: "var(--fg-muted)", borderBottom: "1px solid var(--border)" }}
              >
                <th scope="col" className="px-3 py-2 font-medium">Test</th>
                <th scope="col" className="px-3 py-2 font-medium">Relative</th>
                <th scope="col" className="px-3 py-2 text-right font-medium">p95</th>
                <th scope="col" className="px-3 py-2 text-right font-medium">Mean</th>
                <th scope="col" className="px-3 py-2 font-medium">Trend</th>
                <th scope="col" className="px-3 py-2 text-right font-medium">Runs</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => {
                // A wide p95/mean gap means the test is bimodal: mostly fine,
                // sometimes awful. That is a different problem from "slow" and
                // deserves calling out by name.
                const spread = item.mean_duration_ms > 0
                  ? item.p95_duration_ms / item.mean_duration_ms
                  : 1;
                return (
                  <tr key={item.test_id} style={{ borderBottom: "1px solid var(--border)" }}>
                    <td className="max-w-[24rem] px-3 py-2">
                      <span className="block truncate font-medium" title={item.display_name}>
                        {item.display_name}
                      </span>
                    </td>
                    <td className="px-3 py-2">
                      {/* A bar is faster to scan than a column of numbers, and the
                          numbers are right there anyway, so it carries no meaning
                          on its own. */}
                      <div
                        className="h-1.5 w-32 overflow-hidden rounded-full"
                        style={{ backgroundColor: "var(--bg-subtle)" }}
                        aria-hidden="true"
                      >
                        <div
                          className="h-full rounded-full"
                          style={{
                            width: `${Math.max((item.p95_duration_ms / slowest) * 100, 2)}%`,
                            backgroundColor: "var(--accent)",
                          }}
                        />
                      </div>
                    </td>
                    <td className="tnum px-3 py-2 text-right font-medium">
                      {formatMs(item.p95_duration_ms)}
                    </td>
                    <td className="tnum px-3 py-2 text-right" style={{ color: "var(--fg-muted)" }}>
                      {formatMs(item.mean_duration_ms)}
                      {spread >= 2 && item.scored_runs >= 3 && (
                        <span className="ml-2">
                          <Badge tone="warn" title="p95 is at least twice the mean: mostly fast, sometimes very slow.">
                            spiky
                          </Badge>
                        </span>
                      )}
                    </td>
                    <td className="px-3 py-2">
                      <Trend msPerRun={item.duration_trend_ms_per_run} />
                    </td>
                    <td className="tnum px-3 py-2 text-right" style={{ color: "var(--fg-muted)" }}>
                      {item.scored_runs}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </Card>
      )}
    </div>
  );
}
