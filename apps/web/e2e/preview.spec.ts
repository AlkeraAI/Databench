// Visual screenshot suite for the Alkera IDE surfaces.
//
// For each Alkera IDE surface we take a screenshot at the standard
// breakpoints and save them to e2e/__screenshots__/.
// Reviewers diff these PNGs against committed baselines; visual
// regressions block merge.

import { expect, test, type Page } from "@playwright/test";

interface Surface {
  name: string;
  entry: string;
}

const SURFACES: Surface[] = [
  { name: "sidecar", entry: "/sidecar" },
  { name: "chat", entry: "/chat" },
  { name: "editor-graph", entry: "/editor/graph/graph-jaffle-mart" },
  { name: "editor-chat", entry: "/editor/chat/chat-jaffle-mart" },
  { name: "preview-shell", entry: "/preview" },
];

const BREAKPOINTS = [
  { width: 280, height: 720, label: "narrow-280" },
  { width: 360, height: 720, label: "narrow-360" },
  { width: 420, height: 720, label: "compact-420" },
  { width: 600, height: 800, label: "comfortable-600" },
  { width: 1024, height: 768, label: "tablet-1024" },
  { width: 1440, height: 900, label: "desktop-1440" },
];

async function gotoSurface(page: Page, entry: string): Promise<void> {
  const url = `/vscode.html?entry=${encodeURIComponent(entry)}`;
  await page.goto(url, { waitUntil: "networkidle" });
  // Mark the host so CSS picks up vscode tokens.
  await page.evaluate(() => {
    document.body.setAttribute("data-alkera-host", "vscode");
    document.body.setAttribute("data-alkera-ide", "");
  });
  // Give the in-page deferred state (queries, RAF) a moment to settle.
  await page.waitForTimeout(250);
}

for (const surface of SURFACES) {
  for (const bp of BREAKPOINTS) {
    // Activity-bar / sidecar surface only renders meaningfully ≤ 600 px.
    if (surface.name === "sidecar" && bp.width > 600) continue;
    // The full preview shell needs ≥ 980 px or it collapses to a single column.
    if (surface.name === "preview-shell" && bp.width < 1024) continue;

    test(`${surface.name} @ ${bp.label}`, async ({ page }) => {
      await page.setViewportSize({ width: bp.width, height: bp.height });
      await gotoSurface(page, surface.entry);
      await page.screenshot({
        path: `e2e/__screenshots__/${surface.name}-${bp.label}.png`,
        fullPage: false,
      });
    });
  }
}

// Interaction-state screenshots — verify popovers portal out over the
// sidebar rather than getting clipped inside the composer card.
test("sidecar composer mode popover overlays the sidebar", async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 720 });
  await gotoSurface(page, "/sidecar");
  // The mode menu is the tallest of the composer's settings surfaces, so it is
  // the one with the most to clip if the card holds it in.
  const MODE = "Permission mode"; // pins-source: the accessible name the composer gives its mode control, the handle a screen reader user has on it.
  await page.getByRole("button", { name: MODE }).click();
  const menu = page.locator(`[role="listbox"][aria-label="${MODE}"]`);
  await menu.waitFor();
  const menuBox = await menu.boundingBox();
  const composerBox = await page.locator('form[aria-label="Message composer"]').boundingBox();
  if (!menuBox || !composerBox) throw new Error("menu or composer has no box");
  // It rises clear of the composer card and reads over the chat list, rather
  // than being trapped inside the card it opened from.
  expect(menuBox.y).toBeLessThan(composerBox.y);
  await page.screenshot({
    path: "e2e/__screenshots__/sidecar-mode-popover-360.png",
    fullPage: false,
  });
});

test("sidecar composer model popover overlays the sidebar", async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 720 });
  await gotoSurface(page, "/sidecar");
  await page.getByRole("button", { name: "Model" }).click();
  await page.waitForSelector('[role="listbox"][aria-label="Model"]');
  await page.screenshot({
    path: "e2e/__screenshots__/sidecar-model-popover-360.png",
    fullPage: false,
  });
});

// The two cases below drive the graph editor, which is away: while the graph
// engine is rewired, /editor/graph/:id renders GraphSurface's unavailable
// placeholder and no `alk-graph-editor` mounts. They come back with it.
test.fixme("enlarged node — file tree explorer with run history", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await gotoSurface(page, "/editor/graph/graph-jaffle-mart");
  await page.waitForSelector('[data-testid="alk-graph-editor"]');
  // Click the maximize button on the agent node (which has 2 seeded runs).
  await page
    .getByRole("button", { name: /^Expand Draft monthly_active_customers\.sql$/ })
    .click();
  await page.waitForSelector('[data-testid="alk-enlarged-node"]');
  await page.screenshot({
    path: "e2e/__screenshots__/editor-graph-enlarged-overview-1440.png",
    fullPage: false,
  });
  // Drill into Logs leaf for the most recent run.
  await page.getByRole("button", { name: /^Logs/ }).first().click();
  await page.screenshot({
    path: "e2e/__screenshots__/editor-graph-enlarged-logs-1440.png",
    fullPage: false,
  });
  // Trace leaf — verifies trace-row padding is symmetric.
  await page.getByRole("button", { name: /^Trace/ }).first().click();
  await page.screenshot({
    path: "e2e/__screenshots__/editor-graph-enlarged-trace-1440.png",
    fullPage: false,
  });
  // Tablet width to confirm the grid still works.
  await page.setViewportSize({ width: 1024, height: 768 });
  await page.screenshot({
    path: "e2e/__screenshots__/editor-graph-enlarged-logs-1024.png",
    fullPage: false,
  });
});

test.fixme("graph run state surfaces + delete confirm appears", async ({ page }) => {
  // Smaller viewport so fitView doesn't shrink nodes to illegibility.
  await page.setViewportSize({ width: 720, height: 720 });
  await gotoSurface(page, "/editor/graph/graph-jaffle-mart");
  await page.waitForSelector('[data-testid="alk-graph-editor"]');
  await page.getByRole("button", { name: "Run graph" }).click();
  await page.waitForTimeout(1500);
  await page.screenshot({
    path: "e2e/__screenshots__/editor-graph-running-720.png",
    fullPage: false,
  });
  await page.waitForTimeout(1200); // let run finish
  const firstDelete = page.getByRole("button", { name: /^Delete Read dbt_project\.yml$/ });
  await firstDelete.click();
  await page.waitForSelector('[role="dialog"][aria-label*="Delete Read dbt_project.yml"]');
  await page.screenshot({
    path: "e2e/__screenshots__/editor-graph-delete-confirm-720.png",
    fullPage: false,
  });
});
