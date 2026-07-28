import { useEffect } from "react";
import { Navigate, Route, Routes, useNavigate, useParams } from "react-router-dom";
import { api, useAsync } from "./api/client";
import { AppShell } from "./components/AppShell";
import { Card, ErrorState, LoadingBlock } from "./components/primitives";
import { Failures } from "./views/Failures";
import { Flakiness } from "./views/Flakiness";
import { Overview } from "./views/Overview";
import { Quarantine } from "./views/Quarantine";
import { Slowest } from "./views/Slowest";
import { TestDetail } from "./views/TestDetail";

/**
 * The suite lives in the URL rather than in component state, so a link to a
 * specific view of a specific suite is shareable and the back button behaves.
 * That matters more than usual here: the point of this tool is sending somebody
 * a link to the flaky test you want them to look at.
 */
function SuiteRoutes() {
  const { suite = "" } = useParams();
  const decoded = decodeURIComponent(suite);
  return (
    <Routes>
      <Route index element={<Overview suite={decoded} />} />
      <Route path="flaky" element={<Flakiness suite={decoded} />} />
      <Route path="slowest" element={<Slowest suite={decoded} />} />
      <Route path="failures" element={<Failures suite={decoded} />} />
      <Route path="quarantine" element={<Quarantine suite={decoded} />} />
      {/* Wildcard: test ids contain slashes. */}
      <Route path="tests/*" element={<TestDetail suite={decoded} />} />
    </Routes>
  );
}

function Shell() {
  const { suite = "" } = useParams();
  const navigate = useNavigate();
  const { data, error, loading, reload } = useAsync((signal) => api.suites(signal), []);

  const suites = data ?? [];
  const decoded = decodeURIComponent(suite);

  // If the URL names a suite that does not exist, fall back rather than render
  // five views that each 404 separately.
  useEffect(() => {
    if (!loading && suites.length > 0 && !suites.some((s) => s.name === decoded)) {
      navigate(`/suites/${encodeURIComponent(suites[0].name)}`, { replace: true });
    }
  }, [loading, suites, decoded, navigate]);

  if (error) {
    return (
      <div className="mx-auto max-w-2xl p-6">
        <ErrorState message={error} onRetry={reload} />
        <p className="mt-3 text-xs" style={{ color: "var(--fg-subtle)" }}>
          Is the API running? Start it with{" "}
          <code className="font-mono">uv run uvicorn testpulse_api.main:app</code>.
        </p>
      </div>
    );
  }

  if (loading) {
    return (
      <div className="mx-auto max-w-4xl p-6">
        <LoadingBlock rows={4} label="Loading suites" />
      </div>
    );
  }

  if (suites.length === 0) {
    return (
      <div className="mx-auto max-w-2xl p-6">
        <Card>
          <h1 className="text-sm font-semibold">Nothing ingested yet</h1>
          <p className="mt-2 text-xs leading-relaxed" style={{ color: "var(--fg-muted)" }}>
            TestPulse has no runs stored. Ingest a report to get started:
          </p>
          <pre
            className="mt-3 overflow-x-auto rounded p-3 font-mono text-[11px]"
            style={{ backgroundColor: "var(--bg-subtle)", color: "var(--fg-muted)" }}
          >
{`testpulse ingest --format junit --path ./reports/junit.xml \\
  --suite my-suite --commit $GITHUB_SHA --branch main --env ci`}
          </pre>
        </Card>
      </div>
    );
  }

  return (
    <AppShell
      suites={suites}
      suite={decoded}
      onSuiteChange={(next) => navigate(`/suites/${encodeURIComponent(next)}`)}
    >
      <SuiteRoutes />
    </AppShell>
  );
}

function Landing() {
  const { data, loading, error } = useAsync((signal) => api.suites(signal), []);
  if (loading) {
    return (
      <div className="mx-auto max-w-4xl p-6">
        <LoadingBlock rows={3} label="Loading suites" />
      </div>
    );
  }
  if (error || !data || data.length === 0) return <Shell />;
  return <Navigate to={`/suites/${encodeURIComponent(data[0].name)}`} replace />;
}

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Landing />} />
      <Route path="/suites/:suite/*" element={<Shell />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
