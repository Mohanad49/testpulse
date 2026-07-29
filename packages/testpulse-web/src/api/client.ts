import { useCallback, useEffect, useRef, useState } from "react";
import type {
  FailureCluster,
  PagedTests,
  QuarantineList,
  RunSummary,
  Suite,
  SuiteHealth,
  TestDetail,
} from "./types";

export class ApiError extends Error {
  // Declared and assigned explicitly rather than as a constructor parameter
  // property. `erasableSyntaxOnly` is on in tsconfig.app.json, and a parameter
  // property is TypeScript syntax that cannot be erased to valid JS - so it
  // compiles under `tsc --noEmit` against the root config and fails under
  // `tsc -b`, which is what the build actually runs.
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, { signal, headers: { Accept: "application/json" } });
  if (!response.ok) {
    // The API returns one error shape everywhere, so there is exactly one place
    // that needs to know how to read it.
    let detail = `Request failed with ${response.status}`;
    try {
      const body = (await response.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      // Non-JSON error body (a proxy failure, say). Keep the status message.
    }
    throw new ApiError(response.status, detail);
  }
  return (await response.json()) as T;
}

const encodeSuite = encodeURIComponent;

export const api = {
  suites: (signal?: AbortSignal) => get<Suite[]>("/api/suites", signal),

  health: (suite: string, signal?: AbortSignal) =>
    get<SuiteHealth>(`/api/suites/${encodeSuite(suite)}/health`, signal),

  runs: (suite: string, limit = 30, signal?: AbortSignal) =>
    get<RunSummary[]>(`/api/suites/${encodeSuite(suite)}/runs?limit=${limit}`, signal),

  tests: (
    suite: string,
    options: { sortBy?: string; order?: "asc" | "desc"; limit?: number; offset?: number } = {},
    signal?: AbortSignal,
  ) => {
    const query = new URLSearchParams({
      sort_by: options.sortBy ?? "flakiness_score",
      order: options.order ?? "desc",
      limit: String(options.limit ?? 50),
      offset: String(options.offset ?? 0),
    });
    return get<PagedTests>(`/api/suites/${encodeSuite(suite)}/tests?${query}`, signal);
  },

  testDetail: (suite: string, testId: string, signal?: AbortSignal) =>
    // The suite segment is encoded; the test id is not. It legitimately contains
    // slashes and the route uses a :path converter to receive them, so encoding
    // them here would defeat the whole reason that route is shaped that way.
    // Spaces and other unsafe characters still need escaping, which is what the
    // targeted replacements do.
    get<TestDetail>(
      `/api/suites/${encodeSuite(suite)}/tests/${testId
        .split("/")
        .map((segment) => encodeURIComponent(segment))
        .join("/")}`,
      signal,
    ),

  failures: (suite: string, signal?: AbortSignal) =>
    get<FailureCluster[]>(`/api/suites/${encodeSuite(suite)}/failures`, signal),

  quarantine: (suite: string, signal?: AbortSignal) =>
    get<QuarantineList>(`/api/suites/${encodeSuite(suite)}/quarantine`, signal),
};

export interface AsyncState<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: () => void;
}

/**
 * Minimal fetch-on-mount hook.
 *
 * No react-query. This app makes ten read requests with no mutations, no
 * optimistic updates and no cache invalidation to reason about, and adding a
 * data layer for that is more surface area than it removes. The one thing that
 * genuinely matters here is aborting in-flight requests when the suite changes,
 * because otherwise a slow response for the previous suite lands after the fast
 * one for the new suite and silently renders the wrong data.
 */
export function useAsync<T>(loader: (signal: AbortSignal) => Promise<T>, deps: unknown[]): AsyncState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [nonce, setNonce] = useState(0);
  const loaderRef = useRef(loader);
  loaderRef.current = loader;

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);

    loaderRef
      .current(controller.signal)
      .then((result) => {
        if (!controller.signal.aborted) {
          setData(result);
          setLoading(false);
        }
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        setError(cause instanceof Error ? cause.message : "Something went wrong");
        setLoading(false);
      });

    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce]);

  const reload = useCallback(() => setNonce((value) => value + 1), []);
  return { data, error, loading, reload };
}
