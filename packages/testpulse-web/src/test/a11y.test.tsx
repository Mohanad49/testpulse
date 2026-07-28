import { render } from "@testing-library/react";
import { axe } from "jest-axe";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { StatusLegend, StatusTimeline } from "../components/StatusTimeline";
import { Badge, ErrorState, LoadingBlock, Trend } from "../components/primitives";
import type { TimelinePoint } from "../api/types";

/**
 * Accessibility scan.
 *
 * A QA tool that fails an accessibility scan is a bad advert for its author, so
 * this runs axe over the real components rather than over a simplified stand-in.
 *
 * axe catches machine-checkable failures: contrast, names, roles, structure. It
 * cannot check the thing this dashboard most needed to get right, which is that
 * status is never encoded by colour alone — so that has its own explicit tests
 * below.
 */

function timeline(statuses: TimelinePoint["status"][]): TimelinePoint[] {
  return statuses.map((status, index) => ({
    run_id: index + 1,
    started_at: new Date(2026, 6, 1, 9 + index).toISOString(),
    commit_sha: `abc123${index}`,
    branch: "main",
    status,
    raw_status: status,
    duration_ms: 120 + index * 10,
    retry_count: index === 2 ? 1 : 0,
    failure_message: status === "failed" ? "boom" : null,
  }));
}

describe("accessibility", () => {
  it("the status timeline has no axe violations", async () => {
    const { container } = render(
      <StatusTimeline points={timeline(["passed", "failed", "passed", "skipped", "error"])} />,
    );
    expect(await axe(container)).toHaveNoViolations();
  });

  it("the legend has no axe violations", async () => {
    const { container } = render(<StatusLegend />);
    expect(await axe(container)).toHaveNoViolations();
  });

  it("loading and error states have no axe violations", async () => {
    const { container } = render(
      <MemoryRouter>
        <LoadingBlock rows={3} label="Loading tests" />
        <ErrorState message="Something broke" onRetry={() => {}} />
      </MemoryRouter>,
    );
    expect(await axe(container)).toHaveNoViolations();
  });

  it("badges and trends have no axe violations", async () => {
    const { container } = render(
      <div>
        <Badge tone="danger">same-commit</Badge>
        <Badge tone="warn">rolling-flip</Badge>
        <Trend msPerRun={420} />
        <Trend msPerRun={-420} />
      </div>,
    );
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe("status is never carried by colour alone", () => {
  it("every timeline cell has a text label naming its status", () => {
    const { getAllByRole } = render(
      <StatusTimeline points={timeline(["passed", "failed", "error", "skipped"])} />,
    );
    const cells = getAllByRole("button");
    const labels = cells.map((cell) => cell.getAttribute("aria-label") ?? "");
    expect(labels[0]).toMatch(/^Passed/);
    expect(labels[1]).toMatch(/^Failed/);
    expect(labels[2]).toMatch(/^Error/);
    expect(labels[3]).toMatch(/^Skipped/);
  });

  it("a retried pass is distinguishable from an ordinary pass", () => {
    // The single most important cell in the visual: same colour as a pass, so it
    // needs the outline and it needs to say so out loud.
    const { getAllByRole } = render(
      <StatusTimeline points={timeline(["passed", "failed", "passed"])} />,
    );
    const labels = getAllByRole("button").map((cell) => cell.getAttribute("aria-label") ?? "");
    expect(labels[2]).toContain("passed on retry");
    expect(labels[0]).not.toContain("passed on retry");
  });

  it("each status renders a distinct glyph, so greyscale still reads", () => {
    const { container } = render(
      <StatusTimeline points={timeline(["failed", "error", "skipped"])} size="full" />,
    );
    const glyphs = Array.from(container.querySelectorAll("[aria-hidden='true']"))
      .map((node) => node.textContent?.trim())
      .filter(Boolean);
    expect(new Set(glyphs).size).toBeGreaterThanOrEqual(3);
  });
});

describe("the scan itself", () => {
  it("axe actually reports violations, so a clean run means something", async () => {
    // Same reasoning as the API contract tests: a green accessibility suite is
    // only evidence if the checker is known to be running. This renders
    // something obviously broken - an image with no alt text and a button with
    // no accessible name - and insists axe complains.
    const { container } = render(
      <div>
        <img src="x.png" />
        <button type="button" />
      </div>,
    );
    const results = await axe(container);
    expect(results.violations.length).toBeGreaterThan(0);
  });
});
