import type {
  FailureCluster,
  PagedTests,
  QuarantineList,
  RunSummary,
  Suite,
  SuiteHealth,
  TestDetail,
  TestMetrics,
} from "./types";

/**
 * Serves the dashboard from static JSON instead of a live API.
 *
 * This is what makes a permanently free public demo possible. Every free
 * application host either sleeps between requests - a ~50 second cold start on
 * the one visit that matters - or eventually withdraws its free tier. A file on
 * a CDN does neither, and the data behind it is still real: CI writes genuine
 * runs to Postgres nightly and exports these files from it.
 *
 * The exporter emits the API's exact field names, so this module only decides
 * where bytes come from. There is one set of types, not two that drift.
 */

interface SuiteBundle {
  suite: string;
  generated_at: string;
  health: SuiteHealth | null;
  tests: TestMetrics[];
  failures: FailureCluster[];
  quarantine: QuarantineList;
  details: Record<string, TestDetail>;
}

const BASE = "/data";

// One bundle per suite, fetched once and reused. Every view of a suite is then
// instant, which is the other half of why this beats a sleeping server.
const cache = new Map<string, Promise<SuiteBundle>>();
let indexPromise: Promise<{ suites: { name: string; file: string }[] }> | null = null;

async function loadIndex(signal?: AbortSignal) {
  indexPromise ??= fetch(`${BASE}/index.json`, { signal }).then((r) => {
    if (!r.ok) throw new Error(`Could not load the demo data (${r.status}).`);
    return r.json();
  });
  return indexPromise;
}

async function loadSuite(suite: string, signal?: AbortSignal): Promise<SuiteBundle> {
  const existing = cache.get(suite);
  if (existing) return existing;

  const promise = loadIndex(signal).then(async (index) => {
    const entry = index.suites.find((s) => s.name === suite);
    if (!entry) throw new Error(`No data for suite ${suite}.`);
    const response = await fetch(`${BASE}/${entry.file}`, { signal });
    if (!response.ok) throw new Error(`Could not load ${suite} (${response.status}).`);
    return (await response.json()) as SuiteBundle;
  });

  cache.set(suite, promise);
  // A failed load must not be cached, or one flaky network request poisons the
  // suite for the rest of the session with no way to retry.
  promise.catch(() => cache.delete(suite));
  return promise;
}

export const staticApi = {
  suites: async (signal?: AbortSignal): Promise<Suite[]> =>
    (await loadIndex(signal)).suites.map((s) => ({ name: s.name })),

  health: async (suite: string, signal?: AbortSignal): Promise<SuiteHealth> => {
    const bundle = await loadSuite(suite, signal);
    if (!bundle.health) throw new Error(`No runs stored for suite ${suite}.`);
    return bundle.health;
  },

  runs: async (suite: string, limit = 30, signal?: AbortSignal): Promise<RunSummary[]> =>
    ((await loadSuite(suite, signal)).health?.recent_runs ?? []).slice(0, limit),

  tests: async (
    suite: string,
    options: { sortBy?: string; order?: "asc" | "desc"; limit?: number; offset?: number } = {},
    signal?: AbortSignal,
  ): Promise<PagedTests> => {
    const bundle = await loadSuite(suite, signal);
    const sortBy = options.sortBy ?? "flakiness_score";
    const order = options.order ?? "desc";
    const limit = options.limit ?? 50;
    const offset = options.offset ?? 0;

    // The sort is reimplemented rather than trusting the exported order,
    // because the dashboard offers several sorts and the file can only be
    // written in one. Null sorts last in both directions, matching the API.
    const sorted = [...bundle.tests].sort((a, b) => {
      const av = (a as unknown as Record<string, unknown>)[sortBy];
      const bv = (b as unknown as Record<string, unknown>)[sortBy];
      if (av == null && bv == null) return 0;
      if (av == null) return 1;
      if (bv == null) return -1;
      const cmp =
        typeof av === "string" && typeof bv === "string"
          ? av.localeCompare(bv)
          : Number(av) - Number(bv);
      return order === "desc" ? -cmp : cmp;
    });

    return {
      suite_name: suite,
      total: sorted.length,
      limit,
      offset,
      sort_by: sortBy,
      order,
      items: sorted.slice(offset, offset + limit),
    };
  },

  testDetail: async (suite: string, testId: string, signal?: AbortSignal): Promise<TestDetail> => {
    const bundle = await loadSuite(suite, signal);
    const detail = bundle.details[testId];
    if (!detail) throw new Error(`No test ${testId} recorded in suite ${suite}.`);
    return detail;
  },

  failures: async (suite: string, signal?: AbortSignal): Promise<FailureCluster[]> =>
    (await loadSuite(suite, signal)).failures,

  quarantine: async (suite: string, signal?: AbortSignal): Promise<QuarantineList> =>
    (await loadSuite(suite, signal)).quarantine,
};

/** When the exported data was generated. Null until a bundle has been loaded. */
export async function generatedAt(suite: string): Promise<string | null> {
  try {
    return (await loadSuite(suite)).generated_at;
  } catch {
    return null;
  }
}
