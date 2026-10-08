// Real-browser proof that a filter Dropdown does NOT change width when the
// selection changes. This is a layout fact (computed box widths) that jsdom
// cannot observe, so it lives here rather than in the component unit test.
//
// The surface is the knowledge catalogue at /editor/context, whose filters are
// FilterMenu triggers over the shared Dropdown. The Assay filter is the sharp case: "All grades" is far
// wider than "Verified", so a trigger that sized to the current label would
// visibly jump between them. The trigger's ghost value stack must hold one
// width across every pick.

import { test, expect, type Page } from "@playwright/test";

import { stopViteReloadLoop } from "./_chatHarness";

async function gotoKnowledge(page: Page): Promise<void> {
  await stopViteReloadLoop(page);
  await page.goto("/vscode.html?entry=/editor/context", { waitUntil: "networkidle" });
  await page.waitForSelector(".alk-dropdown-trigger__valuestack");
}

test("a filter dropdown holds a constant width across selections", async ({ page }) => {
  await page.setViewportSize({ width: 1200, height: 800 });
  await gotoKnowledge(page);

  const trigger = page.getByRole("button", { name: /^Assay/ });
  await expect(trigger).toBeVisible();

  async function widthAfter(optionLabel: RegExp, shows: RegExp): Promise<number> {
    await trigger.click();
    await page.getByRole("menuitemradio", { name: optionLabel }).click();
    await expect(trigger).toHaveAccessibleName(shows);
    const box = await trigger.boundingBox();
    if (!box) throw new Error("filter trigger has no bounding box");
    return box.width;
  }

  const start = await trigger.boundingBox();
  if (!start) throw new Error("filter trigger has no bounding box");
  const narrow = await widthAfter(/^Verified/, /Verified/);
  const mid = await widthAfter(/^Agent-set/, /Agent-set/);
  const reset = await widthAfter(/^All grades/, /All grades/);

  // Sub-pixel equality. A trigger that resized to the current label would make
  // "Verified" tens of px narrower than "All grades" and fail here.
  expect(Math.abs(narrow - start.width)).toBeLessThan(0.5);
  expect(Math.abs(mid - start.width)).toBeLessThan(0.5);
  expect(Math.abs(reset - start.width)).toBeLessThan(0.5);
});
