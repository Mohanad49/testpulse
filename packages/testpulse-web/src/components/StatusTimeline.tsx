import { useId, useState } from "react";
import type { TimelinePoint, TestStatus } from "../api/types";

/**
 * The signature visual: one cell per run, oldest on the left, so a flake pattern
 * is legible without reading a single number.
 *
 * The design constraint that shaped everything here: **colour is never the only
 * channel.** Roughly one in twelve men has a colour vision deficiency, and
 * red/green is the exact pair they cannot separate — which is the pair every
 * test tool reaches for first. So each cell carries a glyph as well as a colour,
 * and the glyphs are distinguishable in monochrome:
 *
 *   passed  ▍ full-height bar
 *   failed  ✕ cross
 *   error   ! bang
 *   skipped – dash, drawn at half height and low contrast
 *
 * Printed in greyscale, screenshotted into a report, or viewed by someone with
 * deuteranopia, the pattern still reads. This also happens to be what makes it
 * pass axe rather than merely look like it should.
 */

const STATUS_META: Record<TestStatus, { colour: string; glyph: string; label: string }> = {
  passed: { colour: "var(--status-passed)", glyph: "▍", label: "Passed" },
  failed: { colour: "var(--status-failed)", glyph: "✕", label: "Failed" },
  error: { colour: "var(--status-error)", glyph: "!", label: "Error" },
  skipped: { colour: "var(--status-skipped)", glyph: "–", label: "Skipped" },
};

function formatWhen(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

interface Props {
  points: TimelinePoint[];
  /** Compact is the inline strip inside a table row; full is the detail view. */
  size?: "compact" | "full";
  onSelect?: (point: TimelinePoint) => void;
}

export function StatusTimeline({ points, size = "compact", onSelect }: Props) {
  const [active, setActive] = useState<number | null>(null);
  const captionId = useId();

  if (points.length === 0) {
    return <p className="text-xs" style={{ color: "var(--fg-subtle)" }}>No runs recorded.</p>;
  }

  const cellWidth = size === "compact" ? 10 : 18;
  const cellHeight = size === "compact" ? 22 : 34;
  const activePoint = active === null ? null : points[active];

  return (
    <div className="flex flex-col gap-2">
      {/*
        A list, not a row of buttons. The whole strip is one piece of
        information; making every cell a tab stop would mean 50 tab presses to
        cross one table row. Arrow keys move within it instead, which is the
        composite-widget pattern.
      */}
      <div
        className="flex items-end gap-[2px] overflow-x-auto pb-1"
        role="group"
        aria-labelledby={captionId}
      >
        {points.map((point, index) => {
          const meta = STATUS_META[point.status];
          const retried = point.retry_count !== null && point.retry_count > 0;
          return (
            <button
              key={`${point.run_id}-${index}`}
              type="button"
              tabIndex={index === (active ?? 0) ? 0 : -1}
              onFocus={() => setActive(index)}
              onMouseEnter={() => setActive(index)}
              onMouseLeave={() => setActive(null)}
              onClick={() => onSelect?.(point)}
              onKeyDown={(event) => {
                if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
                event.preventDefault();
                const next = event.key === "ArrowRight" ? index + 1 : index - 1;
                const clamped = Math.min(Math.max(next, 0), points.length - 1);
                setActive(clamped);
                const sibling = event.currentTarget.parentElement?.children[clamped];
                (sibling as HTMLElement | undefined)?.focus();
              }}
              className="relative shrink-0 rounded-[2px] transition-transform duration-150 hover:scale-y-110 focus-visible:scale-y-110"
              style={{
                width: cellWidth,
                height: point.status === "skipped" ? cellHeight * 0.45 : cellHeight,
                backgroundColor: meta.colour,
                // A retried run gets an outline as a third channel, because
                // "passed on retry" is the single most important cell in this
                // whole visual and must not look like an ordinary pass.
                boxShadow: retried ? "inset 0 0 0 2px var(--fg)" : undefined,
                opacity: point.status === "skipped" ? 0.55 : 1,
              }}
              aria-label={`${meta.label}${retried ? ", passed on retry" : ""} at ${formatWhen(
                point.started_at,
              )}${point.commit_sha ? `, commit ${point.commit_sha.slice(0, 7)}` : ""}`}
            >
              <span
                aria-hidden="true"
                className="pointer-events-none absolute inset-0 flex items-center justify-center font-mono leading-none"
                style={{
                  fontSize: size === "compact" ? 8 : 12,
                  color: "var(--bg)",
                  fontWeight: 600,
                }}
              >
                {size === "compact" && point.status === "passed" ? "" : meta.glyph}
              </span>
            </button>
          );
        })}
      </div>

      <p id={captionId} className="sr-only">
        Run history, oldest first. {points.length} runs. Use the arrow keys to move between
        runs.
      </p>

      {/*
        Detail appears below rather than in a floating tooltip. A tooltip on a
        10px target is unusable with a trackpad and invisible to a screen reader,
        and this needs no hit-testing to read.
      */}
      <div
        className="min-h-[1.75rem] text-xs"
        style={{ color: "var(--fg-muted)" }}
        aria-live="polite"
      >
        {activePoint ? (
          <span className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <span
              className="font-medium"
              style={{ color: STATUS_META[activePoint.status].colour }}
            >
              {STATUS_META[activePoint.status].glyph} {STATUS_META[activePoint.status].label}
            </span>
            <span className="tnum">{formatWhen(activePoint.started_at)}</span>
            {activePoint.commit_sha && (
              <span className="font-mono">{activePoint.commit_sha.slice(0, 7)}</span>
            )}
            <span className="tnum">{activePoint.duration_ms.toLocaleString()} ms</span>
            {activePoint.retry_count !== null && activePoint.retry_count > 0 && (
              <span style={{ color: "var(--status-error)" }}>
                {activePoint.retry_count} retr{activePoint.retry_count === 1 ? "y" : "ies"}
              </span>
            )}
          </span>
        ) : (
          <span style={{ color: "var(--fg-subtle)" }}>
            Hover or focus a run for detail.
          </span>
        )}
      </div>
    </div>
  );
}

/** Shared legend. Rendered once per view rather than per row. */
export function StatusLegend() {
  return (
    <ul className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs" style={{ color: "var(--fg-muted)" }}>
      {(Object.keys(STATUS_META) as TestStatus[]).map((status) => (
        <li key={status} className="flex items-center gap-1.5">
          <span
            aria-hidden="true"
            className="inline-flex h-3 w-3 items-center justify-center rounded-[2px] font-mono text-[8px] font-semibold"
            style={{ backgroundColor: STATUS_META[status].colour, color: "var(--bg)" }}
          >
            {status === "passed" ? "" : STATUS_META[status].glyph}
          </span>
          {STATUS_META[status].label}
        </li>
      ))}
      <li className="flex items-center gap-1.5">
        <span
          aria-hidden="true"
          className="inline-block h-3 w-3 rounded-[2px]"
          style={{ backgroundColor: "var(--status-passed)", boxShadow: "inset 0 0 0 2px var(--fg)" }}
        />
        Passed on retry
      </li>
    </ul>
  );
}

export { STATUS_META };
