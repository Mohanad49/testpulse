import { Fragment, useState } from "react";
import { api, useAsync } from "../api/client";
import { StatusLegend, StatusTimeline } from "../components/StatusTimeline";
import {
  Badge,
  Card,
  EmptyState,
  ErrorState,
  LoadingBlock,
  SectionHeading,
  formatPercent,
  formatWhen,
} from "../components/primitives";
import type { TestMetrics } from "../api/types";

/**
 * Ranked flakiest-first, each row expanding into its run history.
 *
 * The row does not expand into a modal or navigate away. Comparing patterns
 * between two flaky tests is the actual job here, and both a modal and a page
 * transition destroy the comparison by hiding one of the two things being
 * compared.
 */

function EvidenceBadges({ metrics }: { metrics: TestMetrics }) {
  return (
    <span className="flex flex-wrap gap-1">
      {metrics.flake_evidence.includes("same-commit") && (
        <Badge tone="danger" title="One commit produced two different outcomes. Close to proof.">
          same-commit
        </Badge>
      )}
      {metrics.flake_evidence.includes("rolling-flip") && (
        <Badge tone="warn" title="Pass rate inside the flake band with a high flip rate. Inferred from a pattern.">
          rolling-flip
        </Badge>
      )}
      {metrics.is_quarantined && <Badge tone="accent">quarantined</Badge>}
    </span>
  );
}

function ExpandedRow({ suite, testId }: { suite: string; testId: string }) {
  const { data, error, loading } = useAsync(
    (signal) => api.testDetail(suite, testId, signal),
    [suite, testId],
  );

  if (loading) return <LoadingBlock rows={2} label="Loading run history" />;
  if (error) return <p className="text-xs" style={{ color: "var(--status-failed)" }}>{error}</p>;
  if (!data) return null;

  return (
    <div className="space-y-3">
      <StatusTimeline points={data.timeline} size="full" />
      <p className="font-mono text-[11px] break-all" style={{ color: "var(--fg-subtle)" }}>
        {data.metrics.test_id}
      </p>
    </div>
  );
}

export function Flakiness({ suite }: { suite: string }) {
  const [expanded, setExpanded] = useState<string | null>(null);
  const { data, error, loading, reload } = useAsync(
    (signal) => api.tests(suite, { sortBy: "flakiness_score", order: "desc", limit: 100 }, signal),
    [suite],
  );

  if (error) return <ErrorState message={error} onRetry={reload} />;

  const flaky = (data?.items ?? []).filter((item) => item.is_flaky);

  return (
    <div className="animate-fade-rise space-y-4">
      <SectionHeading
        title="Flakiness leaderboard"
        hint="Ranked by flakiness score, but confirmed-flaky tests rank above it — a same-commit finding can score 0.00."
        actions={<StatusLegend />}
      />

      {loading ? (
        <LoadingBlock rows={6} label="Loading flaky tests" />
      ) : flaky.length === 0 ? (
        <EmptyState
          title="Nothing classified as flaky"
          detail="Either the suite is stable, or there is not enough history yet. Rolling-flip needs at least 5 scored runs before it will call anything flaky, and same-commit needs retries or a repeated commit."
        />
      ) : (
        <Card className="overflow-x-auto p-0">
          <table className="w-full min-w-[860px] text-sm">
            <caption className="sr-only">Flaky tests in {suite}, flakiest first</caption>
            <thead>
              <tr
                className="text-left text-xs"
                style={{ color: "var(--fg-muted)", borderBottom: "1px solid var(--border)" }}
              >
                <th scope="col" className="w-12 px-1 py-2"><span className="sr-only">Expand</span></th>
                <th scope="col" className="px-3 py-2 font-medium">Test</th>
                <th scope="col" className="px-3 py-2 font-medium">Evidence</th>
                <th scope="col" className="px-3 py-2 text-right font-medium">Score</th>
                <th scope="col" className="px-3 py-2 text-right font-medium">Pass</th>
                <th scope="col" className="px-3 py-2 text-right font-medium">Flip</th>
                <th scope="col" className="px-3 py-2 text-right font-medium">Runs</th>
                <th scope="col" className="px-3 py-2 font-medium">Last failure</th>
              </tr>
            </thead>
            <tbody>
              {flaky.map((item, index) => {
                const isOpen = expanded === item.test_id;
                // A test_id contains spaces, slashes, colons and parentheses, so
                // it cannot be used as an HTML id - aria-controls then points at
                // nothing and axe flags it as a critical violation. The row
                // index is stable within a render and is a valid id.
                const panelId = `flaky-history-${index}`;
                return (
                  // Fragment needs an explicit key. Without one React cannot
                  // track row identity across renders, and the expand state
                  // silently fails to stick.
                  <Fragment key={item.test_id}>
                    <tr style={{ borderBottom: isOpen ? "none" : "1px solid var(--border)" }}>
                      <td className="px-1 py-0">
                        {/*
                          The whole cell is the target, not just the glyph. A bare
                          chevron is about a 12px hit area, which is unusable on a
                          trackpad and well under the 44px guidance.
                        */}
                        <button
                          type="button"
                          onClick={() => setExpanded(isOpen ? null : item.test_id)}
                          aria-expanded={isOpen}
                          aria-controls={panelId}
                          className="flex h-11 w-11 cursor-pointer items-center justify-center rounded"
                          style={{ color: "var(--fg-muted)" }}
                        >
                          <span
                            aria-hidden="true"
                            className="inline-block transition-transform duration-200"
                            style={{ transform: isOpen ? "rotate(90deg)" : "none" }}
                          >
                            ›
                          </span>
                          <span className="sr-only">
                            {isOpen ? "Hide" : "Show"} run history for {item.display_name}
                          </span>
                        </button>
                      </td>
                      <td className="max-w-[26rem] px-3 py-2">
                        <span className="block truncate font-medium" title={item.display_name}>
                          {item.display_name}
                        </span>
                      </td>
                      <td className="px-3 py-2">
                        <EvidenceBadges metrics={item} />
                      </td>
                      <td className="tnum px-3 py-2 text-right font-medium">
                        {item.flakiness_score.toFixed(2)}
                      </td>
                      <td className="tnum px-3 py-2 text-right">
                        {formatPercent(item.pass_rate)}
                      </td>
                      <td className="tnum px-3 py-2 text-right" style={{ color: "var(--fg-muted)" }}>
                        {item.flip_rate.toFixed(2)}
                      </td>
                      <td className="tnum px-3 py-2 text-right" style={{ color: "var(--fg-muted)" }}>
                        {item.scored_runs}
                      </td>
                      <td className="tnum px-3 py-2 text-xs whitespace-nowrap" style={{ color: "var(--fg-muted)" }}>
                        {formatWhen(item.last_failed_at)}
                      </td>
                    </tr>
                    {isOpen && (
                      <tr style={{ borderBottom: "1px solid var(--border)" }}>
                        <td />
                        <td
                          id={panelId}
                          colSpan={7}
                          className="px-3 pb-4"
                          style={{ backgroundColor: "var(--bg-subtle)" }}
                        >
                          <ExpandedRow suite={suite} testId={item.test_id} />
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </Card>
      )}

      {!loading && flaky.length > 0 && (
        <p className="max-w-3xl text-xs leading-relaxed" style={{ color: "var(--fg-subtle)" }}>
          <strong style={{ color: "var(--fg-muted)" }}>Reading the score.</strong> It combines
          flip rate with how close the pass rate is to 50%, so a test that always fails scores
          0 — it is broken, not flaky. It describes the rolling-flip strategy only, which is why
          a same-commit finding can sit at the top with a score of 0.00.
        </p>
      )}
    </div>
  );
}
