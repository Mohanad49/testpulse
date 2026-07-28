import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

/**
 * The suite is seeded by scripts/seed-e2e.py, which ingests the committed
 * fixtures. That means these assertions can be exact rather than defensive:
 * the data is known, so "some tests are listed" becomes "these tests are
 * listed", and a vague assertion that passes on an empty page is worthless.
 */

const SUITE = "e2e-demo";

test.describe("suite overview", () => {
  test("shows headline metrics and the recent runs table", async ({ page }) => {
    await page.goto(`/suites/${SUITE}`);
    // exact, because "Pass rate" also matches the "Pass rate over time" heading
    // and a substring match resolves to two elements.
    await expect(page.getByText("Pass rate", { exact: true })).toBeVisible();
    await expect(page.getByText("Flaky tests", { exact: true })).toBeVisible();
    await expect(page.getByRole("table", { name: /recent runs/i })).toBeVisible();
  });

  test("renders a trend once there is more than one run", async ({ page }) => {
    await page.goto(`/suites/${SUITE}`);
    await expect(page.getByText("Needs at least two runs")).toHaveCount(0);
  });
});

test.describe("flakiness leaderboard", () => {
  test("lists flaky tests with the evidence that fired", async ({ page }) => {
    await page.goto(`/suites/${SUITE}/flaky`);
    await expect(page.getByRole("table", { name: /flaky tests/i })).toBeVisible();
    await expect(page.getByText(/rolling-flip|same-commit/).first()).toBeVisible();
  });

  test("expands a row into its run history", async ({ page }) => {
    await page.goto(`/suites/${SUITE}/flaky`);
    // Located by aria-controls, not by name. The accessible name deliberately
    // changes from "Show" to "Hide" on click, so a name-based locator stops
    // matching the element it just clicked - which is exactly how a test that
    // passes locally becomes a flake in CI.
    const toggle = page.locator('button[aria-controls="flaky-history-0"]');
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
    // The timeline is the thing being revealed, so assert on it and not just
    // on the attribute flipping.
    await expect(page.getByRole("group", { name: /run history/i }).first()).toBeVisible();
  });

  test("the timeline is keyboard navigable", async ({ page }) => {
    await page.goto(`/suites/${SUITE}/flaky`);
    await page.locator('button[aria-controls="flaky-history-0"]').click();
    const cells = page.getByRole("group", { name: /run history/i }).first().getByRole("button");
    await cells.first().focus();
    await page.keyboard.press("ArrowRight");
    await expect(cells.nth(1)).toBeFocused();
    await page.keyboard.press("ArrowLeft");
    await expect(cells.first()).toBeFocused();
  });

  test("every timeline cell names its status in text, not only in colour", async ({ page }) => {
    await page.goto(`/suites/${SUITE}/flaky`);
    await page.locator('button[aria-controls="flaky-history-0"]').click();
    const cells = page.getByRole("group", { name: /run history/i }).first().getByRole("button");
    const count = await cells.count();
    expect(count).toBeGreaterThan(0);
    for (let i = 0; i < count; i++) {
      await expect(cells.nth(i)).toHaveAttribute(
        "aria-label",
        /^(Passed|Failed|Error|Skipped)/,
      );
    }
  });
});

test.describe("other views", () => {
  test("slowest tests are ranked by p95", async ({ page }) => {
    await page.goto(`/suites/${SUITE}/slowest`);
    await expect(page.getByRole("table", { name: /slowest tests/i })).toBeVisible();
  });

  test("failure clusters group messages by root cause", async ({ page }) => {
    await page.goto(`/suites/${SUITE}/failures`);
    await expect(page.getByRole("heading", { name: /failure clusters/i })).toBeVisible();
  });

  test("quarantine explains itself when empty", async ({ page }) => {
    await page.goto(`/suites/${SUITE}/quarantine`);
    await expect(page.getByText(/nothing quarantined|quarantine/i).first()).toBeVisible();
  });

  test("an unknown test id gives a useful error rather than a blank page", async ({ page }) => {
    await page.goto(`/suites/${SUITE}/tests/does/not::Exist::at_all`);
    await expect(page.getByText(/could not load/i)).toBeVisible();
  });
});

test.describe("theme", () => {
  test("defaults to dark and the toggle persists across a reload", async ({ page }) => {
    await page.goto(`/suites/${SUITE}`);
    await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
    await page.getByRole("button", { name: /switch to light theme/i }).click();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
    await page.reload();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  });
});

/**
 * Accessibility, on the rendered page rather than on isolated components.
 *
 * This catches what the component-level scan cannot: landmark structure,
 * heading order, and contrast against the actual page background. Both of the
 * real a11y bugs found during Phase 4 were only visible at this level.
 */
const VIEWS = ["", "/flaky", "/slowest", "/failures", "/quarantine"];

for (const theme of ["dark", "light"] as const) {
  for (const view of VIEWS) {
    test(`axe: ${view || "overview"} (${theme})`, async ({ page }) => {
      await page.addInitScript(
        (value) => window.localStorage.setItem("testpulse-theme", value),
        theme,
      );
      await page.goto(`/suites/${SUITE}${view}`);
      await expect(page.locator("main")).toBeVisible();

      const results = await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
        .analyze();

      // Report the offending element and the measured ratio, not just the rule
      // name. "color-contrast (1)" tells you nothing about where to look.
      expect(
        results.violations.flatMap((v) =>
          v.nodes.map((n) => `${v.id}: ${n.target.join(" ")} :: ${n.failureSummary?.replace(/\s+/g, " ").slice(0, 200)}`),
        ),
      ).toEqual([]);
    });
  }
}
