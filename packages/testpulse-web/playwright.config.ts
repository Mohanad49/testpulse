import { defineConfig, devices } from "@playwright/test";

/**
 * E2E against the real dashboard talking to a real API over a seeded database.
 *
 * Nothing is mocked. The whole point of these tests is the seam between the
 * dashboard and the API — the unit tests already cover the components in
 * isolation, and a mocked API here would test that the mock matches the mock.
 */
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  // Two retries in CI, zero locally. The retries are not there to paper over
  // flakiness: they are there so TestPulse's own suite produces retry data,
  // which is the one input its highest-precision flake strategy needs. A tool
  // that cannot detect flakes in its own test suite is not much of a tool.
  retries: process.env.CI ? 2 : 0,
  reporter: process.env.CI
    ? [
        ["list"],
        ["json", { outputFile: "playwright-report.json" }],
        ["junit", { outputFile: "junit-results.xml" }],
      ]
    : [["list"]],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://127.0.0.1:4173",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: process.env.E2E_BASE_URL
    ? undefined
    : {
        command: "pnpm preview --port 4173 --strictPort",
        url: "http://127.0.0.1:4173",
        reuseExistingServer: !process.env.CI,
        timeout: 60_000,
      },
});
