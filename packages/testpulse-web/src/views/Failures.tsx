import { api, useAsync } from "../api/client";
import {
  Badge,
  Card,
  EmptyState,
  ErrorState,
  LoadingBlock,
  SectionHeading,
} from "../components/primitives";

/**
 * Failure messages grouped by root cause.
 *
 * The template is shown as the heading and one real message underneath. Showing
 * only the template would be undebuggable — nobody can work from
 * "TimeoutError: Timeout <duration> exceeded" — and showing only a real message
 * would hide that the grouping happened at all. Both, every time.
 */
export function Failures({ suite }: { suite: string }) {
  const { data, error, loading, reload } = useAsync(
    (signal) => api.failures(suite, signal),
    [suite],
  );

  if (error) return <ErrorState message={error} onRetry={reload} />;

  const clusters = data ?? [];
  const totalFailures = clusters.reduce((sum, cluster) => sum + cluster.count, 0);

  return (
    <div className="animate-fade-rise space-y-4">
      <SectionHeading
        title="Failure clusters"
        hint={
          clusters.length > 0
            ? `${totalFailures} failures in ${clusters.length} distinct group${clusters.length === 1 ? "" : "s"}.`
            : "Failures with the varying parts removed, grouped by what is left."
        }
      />

      {loading ? (
        <LoadingBlock rows={4} label="Loading failure clusters" />
      ) : clusters.length === 0 ? (
        <EmptyState
          title="No failures in the window"
          detail="Nothing failed with a message attached in the runs currently being analysed."
        />
      ) : (
        <ul className="space-y-3">
          {clusters.map((cluster) => (
            <li key={cluster.template}>
              <Card>
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <p className="font-mono text-xs leading-relaxed break-all" style={{ color: "var(--fg)" }}>
                    {cluster.template}
                  </p>
                  <Badge tone={cluster.count > 1 ? "danger" : "neutral"}>
                    {cluster.count} failure{cluster.count === 1 ? "" : "s"}
                  </Badge>
                </div>

                <details className="mt-3 group">
                  <summary
                    className="cursor-pointer text-xs font-medium"
                    style={{ color: "var(--accent)" }}
                  >
                    Example message and affected tests
                  </summary>
                  <pre
                    className="mt-2 max-h-52 overflow-auto rounded p-2.5 font-mono text-[11px] leading-relaxed whitespace-pre-wrap"
                    style={{ backgroundColor: "var(--bg-subtle)", color: "var(--fg-muted)" }}
                  >
                    {cluster.representative}
                  </pre>
                  <ul className="mt-2 space-y-1">
                    {cluster.test_ids.map((testId) => (
                      <li
                        key={testId}
                        className="font-mono text-[11px] break-all"
                        style={{ color: "var(--fg-subtle)" }}
                      >
                        {testId}
                      </li>
                    ))}
                  </ul>
                </details>

                {cluster.test_ids.length > 1 && (
                  <p className="mt-2 text-xs" style={{ color: "var(--fg-muted)" }}>
                    Spans {cluster.test_ids.length} tests, which usually points at shared setup
                    or infrastructure rather than any one test.
                  </p>
                )}
              </Card>
            </li>
          ))}
        </ul>
      )}

      {!loading && clusters.length > 0 && (
        <p className="max-w-3xl text-xs leading-relaxed" style={{ color: "var(--fg-subtle)" }}>
          <strong style={{ color: "var(--fg-muted)" }}>How grouping works.</strong> Durations,
          UUIDs, source locations, quoted values and bare numbers are replaced with placeholders,
          then messages with identical templates are grouped. It is exact-match on the template
          rather than fuzzy similarity, because a false merge hides a second bug inside a cluster
          that already looks explained, while a false split just shows two rows you can see are
          the same.
        </p>
      )}
    </div>
  );
}
