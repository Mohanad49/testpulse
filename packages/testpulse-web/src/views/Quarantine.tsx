import { api, useAsync } from "../api/client";
import {
  Badge,
  Card,
  EmptyState,
  ErrorState,
  LoadingBlock,
  SectionHeading,
  formatWhen,
} from "../components/primitives";

/**
 * Quarantined tests, most overdue first, with the debt stated at the top.
 *
 * The whole reason quarantine entries expire is that a quarantine list without
 * expiry silently becomes a graveyard. So the overdue count is the loudest thing
 * on this page — if it were a small grey number in a corner it would fail to do
 * the one job the expiry mechanism exists for.
 */
export function Quarantine({ suite }: { suite: string }) {
  const { data, error, loading, reload } = useAsync(
    (signal) => api.quarantine(suite, signal),
    [suite],
  );

  if (error) return <ErrorState message={error} onRetry={reload} />;

  const entries = data?.entries ?? [];
  const debt = data?.debt_count ?? 0;

  return (
    <div className="animate-fade-rise space-y-4">
      <SectionHeading
        title="Quarantine"
        hint="Tests somebody decided to stop trusting, and when that decision runs out."
      />

      {loading ? (
        <LoadingBlock rows={4} label="Loading quarantine list" />
      ) : entries.length === 0 ? (
        <EmptyState
          title="Nothing quarantined"
          detail="Quarantine is a deliberate act, not something the classifier does on its own. Add one with `testpulse quarantine add`."
        />
      ) : (
        <>
          {debt > 0 && (
            <Card
              className="border-l-4"
              // Not a toast and not dismissible. Debt that can be dismissed is
              // debt that gets dismissed.
              as="section"
            >
              <p className="text-sm font-semibold" style={{ color: "var(--status-failed)" }}>
                {debt} of {entries.length} quarantine{entries.length === 1 ? "" : "s"} expired
              </p>
              <p className="mt-1 text-xs" style={{ color: "var(--fg-muted)" }}>
                An expired entry does not re-enable the test on its own. Somebody has to decide:
                fix it, delete it, or renew the quarantine with a reason.
              </p>
            </Card>
          )}

          <Card className="overflow-x-auto p-0">
            <table className="w-full min-w-[760px] text-sm">
              <caption className="sr-only">
                Quarantined tests in {suite}, most overdue first
              </caption>
              <thead>
                <tr
                  className="text-left text-xs"
                  style={{ color: "var(--fg-muted)", borderBottom: "1px solid var(--border)" }}
                >
                  <th scope="col" className="px-3 py-2 font-medium">Status</th>
                  <th scope="col" className="px-3 py-2 font-medium">Test</th>
                  <th scope="col" className="px-3 py-2 font-medium">Reason</th>
                  <th scope="col" className="px-3 py-2 font-medium">By</th>
                  <th scope="col" className="px-3 py-2 font-medium">Quarantined</th>
                  <th scope="col" className="px-3 py-2 font-medium">Expires</th>
                </tr>
              </thead>
              <tbody>
                {entries.map((entry) => (
                  <tr key={entry.test_id} style={{ borderBottom: "1px solid var(--border)" }}>
                    <td className="px-3 py-2 whitespace-nowrap">
                      {entry.is_expired ? (
                        <Badge tone="danger">
                          {/* Signed days remaining is why this can say how far
                              overdue rather than just "expired". "Expired" is
                              ignorable; "expired 47 days ago" is not. */}
                          expired {Math.abs(entry.days_remaining)}d ago
                        </Badge>
                      ) : (
                        <Badge tone={entry.days_remaining <= 3 ? "warn" : "neutral"}>
                          {entry.days_remaining}d left
                        </Badge>
                      )}
                    </td>
                    <td className="max-w-[22rem] px-3 py-2">
                      <span className="block truncate font-mono text-xs" title={entry.test_id}>
                        {entry.test_id}
                      </span>
                    </td>
                    <td className="max-w-[16rem] px-3 py-2 text-xs" style={{ color: "var(--fg-muted)" }}>
                      {entry.reason ?? (
                        <span style={{ color: "var(--status-error)" }}>no reason recorded</span>
                      )}
                    </td>
                    <td className="px-3 py-2 text-xs" style={{ color: "var(--fg-muted)" }}>
                      {entry.quarantined_by ?? "—"}
                    </td>
                    <td className="tnum px-3 py-2 text-xs whitespace-nowrap" style={{ color: "var(--fg-muted)" }}>
                      {formatWhen(entry.quarantined_at)}
                    </td>
                    <td className="tnum px-3 py-2 text-xs whitespace-nowrap" style={{ color: "var(--fg-muted)" }}>
                      {formatWhen(entry.expires_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>
        </>
      )}
    </div>
  );
}
