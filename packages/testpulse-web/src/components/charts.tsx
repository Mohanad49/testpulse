import {
  Area,
  AreaChart,
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { formatMs, formatMsCompact } from "./primitives";

/**
 * Charts are deliberately spare: no legends on single-series charts, no gridlines
 * on the vertical axis, no axis titles. Every element that does not carry
 * information competes with the one that does.
 *
 * Every chart is also paired with the same numbers in text somewhere on the page.
 * That is the accessibility fallback the chart guidance asks for — a canvas of
 * SVG paths is not readable by assistive tech no matter how it is labelled, so
 * the chart is treated as an illustration of data that is stated elsewhere.
 */

const AXIS = { fontSize: 11, fill: "var(--fg-subtle)" };

/**
 * Below two points there is no line to draw, and Recharts renders an empty grid
 * that reads as "broken" rather than "not enough data yet". Say so instead.
 */
function NotEnoughData({ height }: { height: number }) {
  return (
    <div
      className="flex items-center justify-center rounded text-xs"
      style={{ height, color: "var(--fg-subtle)", backgroundColor: "var(--bg-subtle)" }}
    >
      Needs at least two runs to plot a trend.
    </div>
  );
}

function TooltipBox({ label, rows }: { label: string; rows: [string, string][] }) {
  return (
    <div
      className="rounded border px-2.5 py-1.5 text-xs shadow-lg"
      style={{ backgroundColor: "var(--card)", borderColor: "var(--border-strong)" }}
    >
      <p className="mb-1 font-medium">{label}</p>
      {rows.map(([key, value]) => (
        <p key={key} className="tnum" style={{ color: "var(--fg-muted)" }}>
          {key}: <span style={{ color: "var(--fg)" }}>{value}</span>
        </p>
      ))}
    </div>
  );
}

export interface RunPoint {
  label: string;
  passRate: number;
  durationMs: number;
  total: number;
  failed: number;
}

export function DurationTrendChart({ data }: { data: RunPoint[] }) {
  if (data.length < 2) return <NotEnoughData height={168} />;
  return (
    <ResponsiveContainer width="100%" height={168}>
      <AreaChart data={data} margin={{ top: 4, right: 4, bottom: 0, left: -8 }}>
        <defs>
          <linearGradient id="durationFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--accent)" stopOpacity={0.28} />
            <stop offset="100%" stopColor="var(--accent)" stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid stroke="var(--border)" vertical={false} />
        <XAxis dataKey="label" tick={AXIS} tickLine={false} axisLine={false} minTickGap={24} />
        <YAxis
          tick={AXIS}
          tickLine={false}
          axisLine={false}
          width={58}
          tickFormatter={(value: number) => formatMsCompact(value)}
        />
        <Tooltip
          cursor={{ stroke: "var(--border-strong)" }}
          content={({ active, payload, label }) =>
            active && payload?.length ? (
              <TooltipBox
                label={String(label)}
                rows={[["Duration", formatMs(Number(payload[0].value))]]}
              />
            ) : null
          }
        />
        <Area
          type="monotone"
          dataKey="durationMs"
          stroke="var(--accent)"
          strokeWidth={1.75}
          fill="url(#durationFill)"
          isAnimationActive={false}
          dot={false}
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}

export function PassRateChart({ data }: { data: RunPoint[] }) {
  if (data.length < 2) return <NotEnoughData height={168} />;
  return (
    <ResponsiveContainer width="100%" height={168}>
      <LineChart data={data} margin={{ top: 4, right: 4, bottom: 0, left: -12 }}>
        <CartesianGrid stroke="var(--border)" vertical={false} />
        <XAxis dataKey="label" tick={AXIS} tickLine={false} axisLine={false} minTickGap={24} />
        <YAxis
          tick={AXIS}
          tickLine={false}
          axisLine={false}
          width={50}
          domain={[0, 1]}
          tickFormatter={(value: number) => `${Math.round(value * 100)}%`}
        />
        <Tooltip
          cursor={{ stroke: "var(--border-strong)" }}
          content={({ active, payload, label }) =>
            active && payload?.length ? (
              <TooltipBox
                label={String(label)}
                rows={[
                  ["Pass rate", `${Math.round(Number(payload[0].value) * 100)}%`],
                  [
                    "Failed",
                    `${payload[0].payload.failed} of ${payload[0].payload.total}`,
                  ],
                ]}
              />
            ) : null
          }
        />
        <Line
          type="monotone"
          dataKey="passRate"
          stroke="var(--status-passed)"
          strokeWidth={1.75}
          isAnimationActive={false}
          dot={false}
        />
      </LineChart>
    </ResponsiveContainer>
  );
}

/** Duration history for one test, used on the detail view. */
export function TestDurationChart({ data }: { data: { label: string; durationMs: number }[] }) {
  if (data.length < 2) return <NotEnoughData height={150} />;
  return (
    <ResponsiveContainer width="100%" height={150}>
      <LineChart data={data} margin={{ top: 4, right: 4, bottom: 0, left: -8 }}>
        <CartesianGrid stroke="var(--border)" vertical={false} />
        <XAxis dataKey="label" tick={AXIS} tickLine={false} axisLine={false} minTickGap={30} />
        <YAxis
          tick={AXIS}
          tickLine={false}
          axisLine={false}
          width={58}
          tickFormatter={(value: number) => formatMsCompact(value)}
        />
        <Tooltip
          cursor={{ stroke: "var(--border-strong)" }}
          content={({ active, payload, label }) =>
            active && payload?.length ? (
              <TooltipBox
                label={String(label)}
                rows={[["Duration", formatMs(Number(payload[0].value))]]}
              />
            ) : null
          }
        />
        <Line
          type="monotone"
          dataKey="durationMs"
          stroke="var(--accent)"
          strokeWidth={1.75}
          isAnimationActive={false}
          dot={false}
        />
      </LineChart>
    </ResponsiveContainer>
  );
}
