// The notebook toolbar's menus in a pane about 530 px wide, in a real browser.
//
// Beside a chat, the Panels menu opened leftwards out of the notebook's pane,
// where the pane cut it off and the chat was drawn over it, and the
// environment menu ran off the window's right edge. The harness page draws
// two notebooks side by side, each in a pane that clips what overflows it,
// like the chat's side pane. Every menu here must open wholly inside its own
// pane, on top of everything there, and the environment menu must name each
// environment without the box's paths.

import { expect, test, type Locator, type Page } from "@playwright/test";

if (process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE) {
  test.use({ launchOptions: { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE } });
}

// Two panes side by side, each about 530 px: below the narrow breakpoint.
test.use({ viewport: { width: 1090, height: 900 } });

type Who = "ana" | "ben";

const pane = (page: Page, who: Who): Locator => page.locator(`[data-person="${who}"]`);

async function open(page: Page): Promise<void> {
  await page.goto("/dev-notebook.html");
  for (const who of ["ana", "ben"] as const) {
    await expect(pane(page, who).locator(".nb-notebook--narrow")).toBeVisible();
    await expect(pane(page, who).locator("[data-cell-id]")).toHaveCount(3);
  }
}

interface Fit {
  inside: boolean;
  onTop: boolean;
  list: { left: number; right: number; top: number; bottom: number };
  bounds: { left: number; right: number; top: number; bottom: number };
}

/** Whether the open menu lies inside the visible part of its pane, and is
 *  what a click at each of its corners would reach. */
async function fitOf(page: Page, who: Who, label: string): Promise<Fit> {
  const list = pane(page, who).getByRole("menu", { name: label });
  await expect(list).toBeVisible();
  await expect(list).toHaveAttribute("data-placed", "true");
  return list.evaluate((el) => {
    const clip = el.closest(".nb-harness__tab")!.getBoundingClientRect();
    const bounds = {
      left: Math.max(clip.left, 0),
      top: Math.max(clip.top, 0),
      right: Math.min(clip.right, window.innerWidth),
      bottom: Math.min(clip.bottom, window.innerHeight),
    };
    const r = el.getBoundingClientRect();
    const inset = 3;
    const corners: [number, number][] = [
      [r.left + inset, r.top + inset],
      [r.right - inset, r.top + inset],
      [r.left + inset, r.bottom - inset],
      [r.right - inset, r.bottom - inset],
    ];
    return {
      inside: r.left >= bounds.left - 0.5 && r.right <= bounds.right + 0.5 && r.top >= bounds.top - 0.5 && r.bottom <= bounds.bottom + 0.5,
      onTop: corners.every(([x, y]) => el.contains(document.elementFromPoint(x, y))),
      list: { left: r.left, right: r.right, top: r.top, bottom: r.bottom },
      bounds,
    };
  });
}

for (const who of ["ana", "ben"] as const) {
  test(`${who}'s Panels menu opens inside the pane, on top`, async ({ page }) => {
    await open(page);
    await pane(page, who).getByRole("button", { name: "Panels" }).click();
    const fit = await fitOf(page, who, "Panels");
    expect(fit, JSON.stringify(fit)).toMatchObject({ inside: true, onTop: true });
    // It still works: a pick opens the panel, inside the pane too.
    await pane(page, who).getByRole("menuitemcheckbox", { name: "Outline" }).click();
    const sheet = pane(page, who).locator(".nb-panel--sheet");
    await expect(sheet).toBeVisible();
    const placed = await sheet.evaluate((el) => {
      const r = el.getBoundingClientRect();
      const p = el.closest(".nb-harness__pane")!.getBoundingClientRect();
      return r.left >= p.left - 0.5 && r.right <= p.right + 0.5;
    });
    expect(placed).toBe(true);
  });

  test(`${who}'s environment menu opens inside the pane and names environments without the box's paths`, async ({ page }) => {
    await open(page);
    const trigger = pane(page, who).getByRole("button", { name: "Environment", expanded: false });
    await expect(trigger).toHaveText("uv project");
    await trigger.click();
    const fit = await fitOf(page, who, "Environment");
    expect(fit, JSON.stringify(fit)).toMatchObject({ inside: true, onTop: true });
    const items = await pane(page, who).getByRole("menuitemcheckbox").allTextContents();
    expect(items).toEqual(["uv project", "Default · Python 3.12.13"]);
    for (const item of items) {
      expect(item).not.toContain("/opt/");
      expect(item).not.toMatch(/\(Python\s*\)/);
    }
  });
}

test("a cell's menu near the bottom of the pane opens upwards, inside it", async ({ page }) => {
  await page.setViewportSize({ width: 1090, height: 420 });
  await open(page);
  const cells = pane(page, "ben").locator("[data-cell-id]");
  const last = cells.last();
  await last.scrollIntoViewIfNeeded();
  await last.click({ position: { x: 40, y: 10 } });
  await last.getByRole("button", { name: "Cell actions" }).click();
  const list = last.getByRole("menu", { name: "Cell actions" });
  await expect(list).toHaveAttribute("data-placed", "true");
  const fit = await list.evaluate((el) => {
    const clip = el.closest(".nb-cells")!.getBoundingClientRect();
    const r = el.getBoundingClientRect();
    return { inside: r.top >= Math.max(clip.top, 0) - 0.5 && r.bottom <= Math.min(clip.bottom, window.innerHeight) + 0.5, height: r.height };
  });
  expect(fit.inside).toBe(true);
  expect(fit.height).toBeGreaterThan(40);
});
