import { expect, test } from "@playwright/test";

import { gotoSurface, installFixture } from "./_chatHarness";

const SCREENSHOT_DIR = "e2e/__screenshots__/empty-surfaces";

test("knowledge and lineage use the same centered medium empty-state contract", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 760 });
  await installFixture(page, { contextTotal: 0, lineageTotal: 0 });

  await gotoSurface(page, "/editor/context", ".alk-emptystate");
  const knowledge = page.locator(".alk-emptystate");
  await expect(knowledge).toHaveAttribute("data-size", "md");
  await expect(knowledge.locator(".alk-emptystate__mark > svg")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Nothing in the catalog yet" })).toBeVisible();
  const knowledgeCenters = await knowledge.evaluate((plate) => {
    const region = plate.parentElement;
    if (!region) throw new Error("knowledge empty state has no layout region");
    const plateBox = plate.getBoundingClientRect();
    const regionBox = region.getBoundingClientRect();
    return {
      plateX: plateBox.x + plateBox.width / 2,
      plateY: plateBox.y + plateBox.height / 2,
      regionX: regionBox.x + regionBox.width / 2,
      regionY: regionBox.y + regionBox.height / 2,
    };
  });
  expect(Math.abs(knowledgeCenters.plateX - knowledgeCenters.regionX)).toBeLessThanOrEqual(1);
  expect(Math.abs(knowledgeCenters.plateY - knowledgeCenters.regionY)).toBeLessThanOrEqual(1);
  await page.screenshot({ path: `${SCREENSHOT_DIR}/knowledge-empty.png`, fullPage: false });

  await gotoSurface(page, "/editor/lineage", ".alk-emptystate");
  const lineage = page.locator(".alk-emptystate");
  await expect(lineage).toHaveAttribute("data-size", "md");
  await expect(lineage.locator(".alk-emptystate__mark > svg")).toBeVisible();
  await expect(page.getByRole("heading", { name: "No lineage yet" })).toBeVisible();
  await page.screenshot({ path: `${SCREENSHOT_DIR}/lineage-empty.png`, fullPage: false });
});
