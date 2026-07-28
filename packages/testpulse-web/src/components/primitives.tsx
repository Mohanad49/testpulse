import type { ReactNode } from "react";

export function formatMs(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  const minutes = Math.floor(ms / 60_000);
  const seconds = Math.round((ms % 60_000) / 1000);
  return `${minutes}m ${seconds}s`;
}

/**
 * Shorter form for chart axes, where a full "44m 16s" wraps and clips.
 * One unit only: axis labels are for orientation, the tooltip carries precision.
 */
export function formatMsCompact(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)}s`;
  if (ms < 3_600_000) return `${Math.round(ms / 60_000)}m`;
  return `${(ms / 3_600_000).toFixed(1)}h`;
}

export function formatPercent(value: number | null): string {
  // Null is not zero. An all-skipped test has not passed 0% of the time, it has
  // no pass rate at all, and rendering it as 0% puts it next to genuinely broken
  // tests.
  if (value === null) return "—";
  return `${Math.round(value * 100)}%`;
}

export function formatWhen(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/**
 * Axis labels for a series of runs.
 *
 * Date-only labels are useless when a suite runs many times a day - every tick
 * reads "May 30" and the axis carries no information at all. This checks the
 * actual span of the data and switches to clock time when it is inside a day,
 * which is the common case for CI.
 */
export function runAxisLabels(timestamps: string[]): string[] {
  if (timestamps.length === 0) return [];
  const times = timestamps.map((iso) => new Date(iso).getTime());
  const spansMoreThanADay = Math.max(...times) - Math.min(...times) > 24 * 60 * 60 * 1000;
  return times.map((time) =>
    new Date(time).toLocaleString(
      undefined,
      spansMoreThanADay
        ? { month: "short", day: "numeric" }
        : { hour: "2-digit", minute: "2-digit" },
    ),
  );
}

export function Card({
  children,
  className = "",
  as: Tag = "div",
}: {
  children: ReactNode;
  className?: string;
  as?: "div" | "section" | "article";
}) {
  return (
    <Tag
      className={`rounded-lg border p-4 ${className}`}
      style={{ backgroundColor: "var(--card)", borderColor: "var(--border)" }}
    >
      {children}
    </Tag>
  );
}

export function SectionHeading({
  title,
  hint,
  actions,
}: {
  title: string;
  hint?: string;
  actions?: ReactNode;
}) {
  return (
    <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
      <div>
        <h2 className="text-sm font-semibold tracking-tight">{title}</h2>
        {hint && (
          <p className="mt-0.5 text-xs" style={{ color: "var(--fg-subtle)" }}>
            {hint}
          </p>
        )}
      </div>
      {actions}
    </div>
  );
}

/**
 * Skeletons rather than a spinner, sized to the content they stand in for.
 * A spinner tells you to wait; a skeleton tells you what is coming and stops
 * the layout jumping when it arrives, which is the CLS problem in one sentence.
 */
export function Skeleton({ className = "" }: { className?: string }) {
  return <div className={`skeleton ${className}`} aria-hidden="true" />;
}

export function LoadingBlock({ rows = 4, label }: { rows?: number; label: string }) {
  return (
    <div role="status" aria-live="polite" className="space-y-2">
      <span className="sr-only">{label}</span>
      {Array.from({ length: rows }).map((_, index) => (
        <Skeleton key={index} className="h-8 w-full" />
      ))}
    </div>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <Card className="text-sm">
      <p className="font-medium" style={{ color: "var(--status-failed)" }}>
        Could not load this view
      </p>
      <p className="mt-1" style={{ color: "var(--fg-muted)" }}>
        {message}
      </p>
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="mt-3 cursor-pointer rounded border px-2.5 py-1 text-xs font-medium transition-colors duration-150"
          style={{ borderColor: "var(--border-strong)", color: "var(--fg)" }}
        >
          Try again
        </button>
      )}
    </Card>
  );
}

export function EmptyState({ title, detail }: { title: string; detail: string }) {
  return (
    <Card className="text-center">
      <p className="text-sm font-medium">{title}</p>
      <p className="mx-auto mt-1 max-w-md text-xs" style={{ color: "var(--fg-muted)" }}>
        {detail}
      </p>
    </Card>
  );
}

export function Badge({
  children,
  tone = "neutral",
  title,
}: {
  children: ReactNode;
  tone?: "neutral" | "danger" | "warn" | "ok" | "accent";
  title?: string;
}) {
  const tones: Record<string, { fg: string; bg: string }> = {
    // Foreground is a dedicated token, not the status colour. A badge tints its
    // own background with that colour, so reusing it for the text puts a hue
    // against a washed version of itself and fails contrast.
    neutral: { fg: "var(--fg-muted)", bg: "var(--bg-subtle)" },
    danger: { fg: "var(--badge-danger-fg)", bg: "color-mix(in srgb, var(--status-failed) 14%, transparent)" },
    warn: { fg: "var(--badge-warn-fg)", bg: "color-mix(in srgb, var(--status-error) 16%, transparent)" },
    ok: { fg: "var(--badge-ok-fg)", bg: "color-mix(in srgb, var(--status-passed) 16%, transparent)" },
    accent: { fg: "var(--badge-accent-fg)", bg: "color-mix(in srgb, var(--accent) 16%, transparent)" },
  };
  const { fg, bg } = tones[tone];
  return (
    <span
      title={title}
      className="inline-flex items-center rounded px-1.5 py-0.5 text-[11px] font-medium whitespace-nowrap"
      style={{ color: fg, backgroundColor: bg }}
    >
      {children}
    </span>
  );
}

/**
 * A trend arrow that also says which way it points in text.
 *
 * An arrow glyph alone is colour-and-shape only; screen readers announce nothing
 * useful for "▲". The visible number carries the meaning and the arrow is
 * decoration on top of it.
 */
export function Trend({ msPerRun }: { msPerRun: number }) {
  const rounded = Math.round(msPerRun);
  if (Math.abs(rounded) < 1) {
    return (
      <span className="tnum text-xs" style={{ color: "var(--fg-subtle)" }}>
        flat
      </span>
    );
  }
  const slower = rounded > 0;
  return (
    <span
      className="tnum text-xs whitespace-nowrap"
      style={{ color: slower ? "var(--status-error)" : "var(--status-passed)" }}
      title={`${slower ? "Slower" : "Faster"} by ${Math.abs(rounded)}ms per run`}
    >
      <span aria-hidden="true">{slower ? "▲" : "▼"}</span>{" "}
      {slower ? "+" : "−"}
      {formatMsCompact(Math.abs(rounded))}/run
    </span>
  );
}
