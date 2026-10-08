import { expect, test, type Locator } from "@playwright/test";

import { gotoSurface, installFixture } from "./_chatHarness";

const SCREENSHOT_DIR = "e2e/__screenshots__/document-explorer";

/** Read the geometry the row contract owns, not incidental text widths. */
async function rowGeometry(rows: Locator) {
  return rows.evaluateAll((elements) =>
    elements.map((row) => {
      const box = row.querySelector<HTMLElement>(".plg-doc__box");
      const content = row.querySelector<HTMLElement>(".plg-doc__content");
      if (!box || !content) throw new Error("document row is missing its box or content lane");
      const rowRect = row.getBoundingClientRect();
      const boxRect = box.getBoundingClientRect();
      return {
        boxX: boxRect.x,
        boxY: boxRect.y,
        contentSize: getComputedStyle(content).fontSize,
        flexWrap: getComputedStyle(row).flexWrap,
        rowCenter: rowRect.y + rowRect.height / 2,
        boxCenter: boxRect.y + boxRect.height / 2,
      };
    }),
  );
}

test("document and passage pages share one header and row geometry", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 820 });
  await installFixture(page, {
    engine: {
      "plugin.list": {
        plugins: [
          {
            name: "notion",
            version: "1",
            description: "Knowledge documents",
            surfaces: ["context"],
            active: true,
            enabled: true,
            has_form: true,
            detected_connections: [],
          },
        ],
      },
      "connection.list": {
        connections: [
          {
            handle: "workspace",
            plugin: "notion",
            dialect: null,
            environment: "",
            added: true,
            status: "added",
            discovered_from: "",
            detail: "",
            enabled: true,
          },
        ],
      },
      "connection.detected": { connections: [] },
      "knowledge.listDocuments": {
        has_selection: true,
        documents: [
          {
            document_id: "runbook",
            title: "Warehouse runbook",
            url: "https://notion.example/runbook",
            archived: false,
            synced_sections: 3,
            selected: false,
            sections: [
              { section_id: "load", title: "Nightly load order", selected: false },
              { section_id: "page", title: "Who to page", selected: false },
              { section_id: "restatement", title: "Restating a partition", selected: false },
            ],
          },
          {
            document_id: "long",
            title: "A very long document title that has to truncate before the source control",
            url: "https://notion.example/long",
            archived: false,
            synced_sections: 0,
            selected: false,
            sections: [],
          },
        ],
      },
    },
  });
  await gotoSurface(page, "/editor/plugins", "input[placeholder='Search plugins & connections']");
  await page.getByRole("button", { name: "Choose documents" }).first().click();
  await expect(page.getByRole("checkbox", { name: "Select all" })).toBeVisible();

  const rootHeader = page.locator(".plg-frame__head");
  const rootHeaderPadding = await rootHeader.evaluate((element) => {
    const style = getComputedStyle(element);
    return { block: style.paddingBlock, inline: style.paddingInline };
  });
  const rootSelectX = (await rootHeader.locator(".plg-doc__box").boundingBox())?.x;
  const rootHeaderBox = await rootHeader.boundingBox();
  if (rootSelectX == null || !rootHeaderBox) throw new Error("root select-all box has no geometry");
  const rootSelectInset = rootSelectX - rootHeaderBox.x;

  const rootRows = await rowGeometry(page.locator(".plg-doc__line"));
  expect(new Set(rootRows.map((row) => row.boxX)).size).toBe(1);
  expect(new Set(rootRows.map((row) => row.contentSize)).size).toBe(1);
  for (const row of rootRows) {
    expect(row.flexWrap).toBe("nowrap");
    expect(Math.abs(row.rowCenter - row.boxCenter)).toBeLessThanOrEqual(1);
  }
  expect(Math.abs((rootRows[0]?.boxX ?? 0) - rootSelectX)).toBeLessThanOrEqual(0.5);

  const runbookBox = page.getByRole("checkbox", { name: "Sync Warehouse runbook" });
  const runbookBadge = page.getByRole("button", {
    name: "Open 3 passages in Warehouse runbook",
  });
  await expect(runbookBox).not.toBeChecked();
  await expect(runbookBadge.locator("svg")).toBeVisible();
  await page.getByText("Warehouse runbook", { exact: true }).click();
  await expect(runbookBox).toBeChecked();
  await expect(page.locator(".plg-frame__crumb")).toHaveCount(0);
  await page.screenshot({ path: `${SCREENSHOT_DIR}/root.png`, fullPage: false });

  await runbookBadge.click();
  const crumb = page.locator(".plg-frame__crumb");
  const passageHeader = page.locator(".plg-frame__head");
  await expect(crumb).toContainText("Warehouse runbook");
  const crumbBox = await crumb.boundingBox();
  const passageHeaderBox = await passageHeader.boundingBox();
  if (!crumbBox || !passageHeaderBox) throw new Error("passage header has no geometry");
  expect(passageHeaderBox.y).toBeGreaterThanOrEqual(crumbBox.y + crumbBox.height - 0.5);

  const passageHeaderPadding = await passageHeader.evaluate((element) => {
    const style = getComputedStyle(element);
    return { block: style.paddingBlock, inline: style.paddingInline };
  });
  expect(passageHeaderPadding).toEqual(rootHeaderPadding);
  const passageSelectX = (await passageHeader.locator(".plg-doc__box").boundingBox())?.x;
  if (passageSelectX == null) throw new Error("passage select-all box has no geometry");
  expect(Math.abs(passageSelectX - passageHeaderBox.x - rootSelectInset)).toBeLessThanOrEqual(0.5);

  const passageRows = await rowGeometry(page.locator(".plg-doc__line"));
  expect(new Set(passageRows.map((row) => row.boxX)).size).toBe(1);
  expect(new Set(passageRows.map((row) => row.contentSize)).size).toBe(1);
  for (const row of passageRows) {
    expect(row.flexWrap).toBe("nowrap");
    expect(Math.abs(row.rowCenter - row.boxCenter)).toBeLessThanOrEqual(1);
  }
  expect(Math.abs((passageRows[0]?.boxX ?? 0) - passageSelectX)).toBeLessThanOrEqual(0.5);
  await page.screenshot({ path: `${SCREENSHOT_DIR}/passages.png`, fullPage: false });
});
