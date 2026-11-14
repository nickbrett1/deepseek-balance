import { test, expect } from "@playwright/test";

// The preview server's origin, for the one test that embeds `/` in an iframe.
const ORIGIN = `http://127.0.0.1:${process.env.E2E_PORT || 8117}`;
import {
  analysis,
  analysisUnconfigured,
  history,
  widget,
  widgetEmpty,
} from "./fixtures.js";

/**
 * The render half of the port.
 *
 * pytest owns the contract (`tests/test_views.py`: what `/ui/*` carries, and
 * that a pacing verdict is a domain rule). This suite owns what a reader sees
 * given that JSON, with every response mocked -- so it needs no Python, no
 * database and no snapshots, and it cannot fail because of a server detail.
 */

async function mock(
  page,
  { widget: card = widget, days = history(), table = analysis } = {},
) {
  await page.route("**/ui/**", (route) => {
    const url = route.request().url();
    const body = url.includes("/ui/widget.json")
      ? card
      : url.includes("/ui/analysis.json")
        ? table
        : days;
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(body),
    });
  });
}

/** The pathname is the router: `/` is the card, `/history` the drill-in. */
async function open(page, path = "/") {
  await page.goto(path, { waitUntil: "networkidle" });
}

test.describe("the homepage widget", () => {
  test("is the balance, the pacing verdict and two tables — no charts", async ({
    page,
  }) => {
    await mock(page);
    await open(page);

    await expect(page.locator(".topbar")).toContainText("DeepSeek Balance");
    // The verdict is the server's: 0.7/1.3 bands are a domain rule, not a colour.
    await expect(page.locator(".topbar .pill.warn")).toHaveText("above normal");
    await expect(page.locator(".figure")).toHaveText("87.6");
    await expect(page.locator(".tbl-row")).toHaveCount(10);
    await expect(page.locator("svg")).toHaveCount(0);
    await expect(page.locator("text=Spend per day")).toHaveCount(0);
  });

  test("shows today's spend with its verdict, then the bands with their thresholds", async ({
    page,
  }) => {
    await mock(page);
    await open(page);

    const spent = page.locator(".tbl-row").first();
    await expect(spent).toContainText("Spent today");
    await expect(spent).toContainText("12.4 CNY");
    const high = page.locator(".tbl-row").filter({ hasText: "Unusually high" });
    await expect(high).toContainText("6 (15%)");
    await expect(high).toContainText("over 0.62 CNY");
    await expect(high.locator(".tone-warn")).toHaveCount(1);
  });

  test("says it is waiting rather than drawing an empty card", async ({
    page,
  }) => {
    await mock(page, { widget: widgetEmpty });
    await open(page);
    await expect(
      page.locator("text=Waiting for the first snapshot"),
    ).toBeVisible();
    await expect(page.locator(".figure")).toHaveCount(0);
  });

  test("reports its content height, not the height of the iframe it was given", async ({
    page,
  }) => {
    await mock(page);
    // The same-origin iframe is the contract: Homepage embeds `/` in one and
    // resizes the card from this message. It is deliberately much taller than
    // the card's content -- the whole bug was that the card measured the frame
    // it had already been given and asked for more on every poll.
    const VIEWPORT = 900;
    await page.setContent(
      `<iframe src="${ORIGIN}/" style="width:380px;height:${VIEWPORT}px;border:0"></iframe>`,
    );

    // Let the card paint before taking the two polls: the reporter's interval
    // starts at mount, and a report taken mid-load is legitimately short.
    const card = page.frameLocator("iframe");
    await expect(card.locator(".figure")).toBeVisible();

    const heights = await page.evaluate(
      () =>
        new Promise((resolve) => {
          const seen = [];
          const onMessage = (event) => {
            if (event.data?.type !== "homepage-iframe-resize") return;
            seen.push(event.data.height);
            if (seen.length === 2) {
              window.removeEventListener("message", onMessage);
              resolve(seen);
            }
          };
          window.addEventListener("message", onMessage);
          setTimeout(() => resolve(seen), 7000);
        }),
    );

    expect(heights).toHaveLength(2);
    // Content height, so strictly shorter than the 900px frame it was given.
    expect(heights[0]).toBeLessThan(VIEWPORT);
    expect(heights[1]).toBeLessThan(VIEWPORT);
    // And the second poll must not ask for more: that is the feedback loop.
    expect(heights[1]).toBeLessThanOrEqual(heights[0]);
  });

  test("drops the house footer when embedded, because the parent owns the chrome", async ({
    page,
  }) => {
    await mock(page);
    await open(page, "/");
    await expect(page.locator(".footer")).toHaveCount(0);
  });
});

test.describe("the drill-in", () => {
  test("puts today's six numbers in the stat strip", async ({ page }) => {
    await mock(page);
    await open(page, "/history");

    const cards = page.locator(".stat-card");
    await expect(cards).toHaveCount(6);
    await expect(cards.first()).toContainText("Spent today");
    await expect(cards.first()).toContainText("above normal");
    await expect(cards.last()).toContainText("Started the day at");
  });

  test("draws three charts, with today as a dashed projection", async ({
    page,
  }) => {
    await mock(page);
    await open(page, "/history");

    const series = page.locator(".bar-series");
    await expect(series).toHaveCount(3);
    await expect(series.nth(0)).toContainText("Spend per day");
    await expect(series.nth(1)).toContainText("Usage time per day");
    await expect(series.nth(2)).toContainText("Cost per hour");

    // Fourteen complete days plus today's projection: the projection is
    // outline-only, because a part-spent day is a claim and not a measurement.
    // 13 measured days with a bar plus the one blank day that keeps its empty
    // slot, plus today's projection.
    await expect(series.nth(0).locator("rect.bar")).toHaveCount(15);
    await expect(series.nth(0).locator("rect.bar.projected")).toHaveCount(1);
    await expect(series.nth(0).locator("text.today")).toHaveCount(1);
  });

  test("gives the cost axis the reader's minor unit", async ({ page }) => {
    await mock(page);
    await open(page, "/history");
    await expect(page.locator(".bar-series").nth(2)).toContainText("分/hour");
  });

  test("averages complete days only, never today's projection", async ({
    page,
  }) => {
    await mock(page);
    await open(page, "/history");
    // 14 days, one of them with no spend: the label counts complete days.
    // 14 days, one of them with no spend at all: the average is over the days
    // that had any, and the label says so.
    await expect(page.locator(".avgline")).toContainText(
      "last 13 complete days",
    );
    await expect(page.locator(".avgline")).toContainText("avg spend/day");
  });

  test("fetches the range again rather than filtering in the browser", async ({
    page,
  }) => {
    const requested = [];
    await page.route("**/ui/history.json*", (route) => {
      const url = new URL(route.request().url());
      requested.push(url.searchParams.get("days"));
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(
          history({ days: Number(url.searchParams.get("days")) }),
        ),
      });
    });
    await page.route("**/ui/analysis.json*", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(analysis),
      }),
    );
    await open(page, "/history");

    // Two segmented controls on this page (range and status), so the range one
    // is named rather than counted.
    const range = page.getByRole("group", { name: "History range" });
    await expect(range.locator("button.on")).toHaveText(/14 days/);
    await range.locator("button", { hasText: "Last month" }).click();
    await expect.poll(() => requested.at(-1)).toBe("30");
    await expect(range.locator("button.on")).toHaveText(/Last month/);
  });

  test("labels each diagnosis and keeps pending distinct from failed", async ({
    page,
  }) => {
    await mock(page);
    await open(page, "/history");

    // The page has two tables; the diagnosis one is the last.
    const rows = page.locator(".data-table-wrap").last().locator(".tbl-row");
    await expect(rows).toHaveCount(5);
    const failed = rows.filter({ hasText: "analysis failed" });
    await expect(failed.locator(".pill.bad")).toHaveText("failed");
    await expect(failed).toContainText("giving up");
    const pending = rows.filter({ hasText: "analysis pending" });
    await expect(pending.locator(".pill")).toHaveText("analysing");
    // "Explains" is its own column: traced cost ÷ balance drop.
    await expect(rows.first()).toContainText("94%");
    await expect(rows.first()).toContainText("92%");
  });

  test("pages back with a cursor the server handed it", async ({ page }) => {
    const before = [];
    await page.route("**/ui/analysis.json**", (route) => {
      const url = new URL(route.request().url());
      before.push(url.searchParams.get("before"));
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(
          before.length > 1 ? { ...analysis, has_more: false } : analysis,
        ),
      });
    });
    await page.route("**/ui/history.json**", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(history()),
      }),
    );
    await open(page, "/history");

    const older = page.locator(".pager button");
    await expect(older).toBeEnabled();
    await older.click();
    await expect.poll(() => before.at(-1)).toBe("2026-11-13T09:00:00+00:00");
    await expect(older).toBeDisabled();
  });

  test("says why the table is empty when analysis is switched off", async ({
    page,
  }) => {
    await mock(page, { table: analysisUnconfigured });
    await open(page, "/history");
    await expect(
      page.locator(".table-card, .empty-card").first(),
    ).toBeVisible();
    await expect(page.locator("text=Analysis is disabled")).toBeVisible();
  });
});

test.describe("both screens", () => {
  test("carry the house frame and no tab row", async ({ page }) => {
    await mock(page);
    await open(page, "/history");
    await expect(page.locator(".topbar")).toContainText("DeepSeek Balance");

    // A page is still a page: it keeps the default house footer. Only the
    // embedded card opts out (app-shell 1.11.0's `embedded` prop).
    await expect(page.locator(".footer")).toContainText(
      "house design system · @nb",
    );

    // One page, so nothing to tab between. The nav used to offer "Today",
    // which pointed at the Homepage card — a view whose content is the top of
    // this very page. A destination that shows strictly less is not a sibling.
    await expect(page.locator(".main-nav")).toHaveCount(0);
    await expect(
      page.getByRole("navigation", { name: "Sections" }),
    ).toHaveCount(0);
    // And no back pill either: the page is entered from a dashboard link, so
    // there is no in-app parent to go back to.
    await expect(page.locator(".topbar a.pill")).toHaveCount(0);

    await open(page, "/");
    // The card is iframed: no tabs pushing the balance down, and no back pill.
    await expect(page.locator(".main-nav")).toHaveCount(0);
    await expect(page.locator(".topbar a.pill")).toHaveCount(0);
  });

  test("render without a console error", async ({ page }) => {
    const errors = [];
    page.on("console", (message) => {
      if (message.type() === "error") errors.push(message.text());
    });
    page.on("pageerror", (error) => errors.push(String(error)));

    await mock(page);
    await open(page, "/");
    await open(page, "/history");
    expect(errors).toEqual([]);
  });

  test("stay on one column at phone width", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await mock(page);
    await open(page, "/history");
    const columns = await page
      .locator(".stat-strip")
      .evaluate(
        (el) => getComputedStyle(el).gridTemplateColumns.split(" ").length,
      );
    expect(columns).toBe(1);
    // A record wraps instead of pushing the table past the viewport.
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - window.innerWidth,
    );
    expect(overflow).toBeLessThanOrEqual(1);
  });
});
