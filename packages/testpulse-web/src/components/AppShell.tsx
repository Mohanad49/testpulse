import { useEffect, useState, type ReactNode } from "react";
import { NavLink } from "react-router-dom";
import type { Suite } from "../api/types";
import { IS_STATIC } from "../api/client";

const THEME_KEY = "testpulse-theme";
type Theme = "dark" | "light";

function initialTheme(): Theme {
  const stored = localStorage.getItem(THEME_KEY);
  if (stored === "light" || stored === "dark") return stored;
  // Dark default even when the OS says light. This sits beside a terminal and a
  // CI log; defaulting to the system preference would hand a white page to
  // roughly half the people who open it.
  return "dark";
}

export function useTheme() {
  const [theme, setTheme] = useState<Theme>(initialTheme);
  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem(THEME_KEY, theme);
  }, [theme]);
  return { theme, toggle: () => setTheme((t) => (t === "dark" ? "light" : "dark")) };
}

function ThemeToggle() {
  const { theme, toggle } = useTheme();
  return (
    <button
      type="button"
      onClick={toggle}
      className="cursor-pointer rounded border px-2 py-1 text-xs font-medium transition-colors duration-150"
      style={{ borderColor: "var(--border)", color: "var(--fg-muted)" }}
      // The button says what it will do, not what the current state is; the
      // ambiguity of an icon-only toggle is a genuine usability complaint, not a
      // theoretical one.
      aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
    >
      {theme === "dark" ? "Light" : "Dark"}
    </button>
  );
}

const NAV = [
  { to: "", label: "Overview", end: true },
  { to: "flaky", label: "Flakiness" },
  { to: "slowest", label: "Slowest" },
  { to: "failures", label: "Failures" },
  { to: "quarantine", label: "Quarantine" },
];

interface Props {
  suites: Suite[];
  suite: string;
  onSuiteChange: (suite: string) => void;
  children: ReactNode;
}

export function AppShell({ suites, suite, onSuiteChange, children }: Props) {
  return (
    <div className="min-h-full" style={{ backgroundColor: "var(--bg)" }}>
      {/* Every keyboard user's first tab press should be able to skip the nav. */}
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-3 focus:top-3 focus:z-50 focus:rounded focus:px-3 focus:py-2"
        style={{ backgroundColor: "var(--accent)", color: "var(--accent-fg)" }}
      >
        Skip to content
      </a>

      <header
        className="sticky top-0 z-30 border-b backdrop-blur"
        style={{ backgroundColor: "color-mix(in srgb, var(--bg) 88%, transparent)", borderColor: "var(--border)" }}
      >
        <div className="mx-auto flex max-w-[1400px] flex-wrap items-center gap-x-6 gap-y-2 px-4 py-2.5">
          <div className="flex items-center gap-2">
            <span
              aria-hidden="true"
              className="inline-block h-3.5 w-1 rounded-sm"
              style={{ backgroundColor: "var(--status-passed)" }}
            />
            <span
              aria-hidden="true"
              className="inline-block h-3.5 w-1 rounded-sm"
              style={{ backgroundColor: "var(--status-failed)" }}
            />
            <span
              aria-hidden="true"
              className="mr-1 inline-block h-3.5 w-1 rounded-sm"
              style={{ backgroundColor: "var(--status-passed)" }}
            />
            <span className="text-sm font-semibold tracking-tight">TestPulse</span>
          </div>

          <label className="flex items-center gap-2 text-xs" style={{ color: "var(--fg-muted)" }}>
            <span>Suite</span>
            <select
              value={suite}
              onChange={(event) => onSuiteChange(event.target.value)}
              className="cursor-pointer rounded border px-2 py-1 text-xs"
              style={{
                backgroundColor: "var(--card)",
                borderColor: "var(--border)",
                color: "var(--fg)",
              }}
            >
              {suites.map((option) => (
                <option key={option.name} value={option.name}>
                  {option.name}
                </option>
              ))}
            </select>
          </label>

          <nav aria-label="Views" className="flex flex-wrap items-center gap-0.5">
            {NAV.map((item) => (
              <NavLink
                key={item.label}
                to={item.to ? `/suites/${encodeURIComponent(suite)}/${item.to}` : `/suites/${encodeURIComponent(suite)}`}
                end={item.end}
                className="rounded px-2.5 py-1 text-xs font-medium transition-colors duration-150"
                style={({ isActive }) => ({
                  color: isActive ? "var(--fg)" : "var(--fg-muted)",
                  backgroundColor: isActive ? "var(--card)" : "transparent",
                })}
              >
                {item.label}
              </NavLink>
            ))}
          </nav>

          <div className="ml-auto">
            <ThemeToggle />
          </div>
        </div>
      </header>

      {IS_STATIC && (
        /* Said plainly rather than hidden in a footer. Someone looking at this
           should know they are seeing a nightly snapshot of real runs, not a
           live service - claiming otherwise would be the kind of small
           dishonesty that undermines everything else on the page. */
        <p
          className="border-b px-4 py-1.5 text-center text-xs"
          style={{ backgroundColor: "var(--bg-subtle)", color: "var(--fg-muted)", borderColor: "var(--border)" }}
        >
          Static demo — real test runs from CI, exported nightly. The live API and
          ingest endpoint run in a self-hosted install.
        </p>
      )}

      <main id="main" className="mx-auto max-w-[1400px] px-4 py-5">
        {children}
      </main>
    </div>
  );
}
