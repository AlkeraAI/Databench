// The composer rail is ONE row at every panel width the IDE can give it.
//
// The sweep drives 170..640px in 10px steps with the longest labels the
// vocabulary can produce and asserts the rail never becomes two rows and no
// control leaves the unit. Two narrower cases follow it: the smaller control
// notch below the sweep, and the right-aligned closing group once the model and
// effort chips fold away.
//
// Every label the sweep leans on is read back off the rendered menu, never
// retyped here: the mode vocabulary lives in the app and the sweep has to fail
// when a longer word lands in it.

import { expect, test, type Page } from "@playwright/test";

import { CHAT_ID, LONG_MODEL, gotoSurface, installFixture } from "./_chatHarness";

async function openChat(page: Page): Promise<void> {
  await gotoSurface(page, `/chat/${CHAT_ID}`, ".chat-composer-rail");
}

const MODE_TRIGGER = /^Permission mode:/;

/** Select the mode whose label is the longest the app actually offers, and hand
 *  that label back. Read from the menu so a new, longer mode name lands in the
 *  sweep on its own. */
async function pickLongestMode(page: Page): Promise<string> {
  await page.getByRole("button", { name: MODE_TRIGGER }).click();
  const options = page.getByRole("option");
  await expect(options.first()).toBeVisible();
  const labels = await options.evaluateAll((nodes) =>
    nodes.map((node) => node.querySelector(".chat-composer-opt__label")?.textContent?.trim() ?? ""),
  );
  let at = 0;
  for (let i = 1; i < labels.length; i += 1) if (labels[i].length > labels[at].length) at = i;
  const longest = labels[at];
  // By index, not by accessible name: an option row carries its description in
  // the same button, so the name is never the label alone.
  await options.nth(at).click();
  await expect(page.getByRole("button", { name: `Permission mode: ${longest}` })).toBeVisible();
  return longest;
}

/** One reading of the rail at the current viewport. */
interface RailReading {
  height: number;
  control: number;
  modelCap: { width: number; max: number } | null;
  overflow: number;
  outside: string[];
  shown: string[];
}

async function readRail(page: Page): Promise<RailReading> {
  return page.evaluate(() => {
    const rail = document.querySelector<HTMLElement>(".chat-composer-rail");
    if (!rail) throw new Error("no rail");
    const railBox = rail.getBoundingClientRect();
    const control = Number.parseFloat(getComputedStyle(rail).getPropertyValue("--chat-composer-ctl")) || 0;
    const items = Array.from(rail.children) as HTMLElement[];
    const name = (el: HTMLElement): string =>
      el.dataset.rail ?? (el.classList.contains("chat-composer-slashkey") ? "slash" : el.classList.contains("chat-composer-send") ? "send" : el.className);
    const outside: string[] = [];
    for (const item of items) {
      if (getComputedStyle(item).display === "none") continue;
      const box = item.getBoundingClientRect();
      // Half a pixel of tolerance: layout lands on fractional device pixels.
      if (box.left < railBox.left - 0.5 || box.right > railBox.right + 0.5) {
        outside.push(`${name(item)} [${Math.round(box.left)}..${Math.round(box.right)}] vs rail [${Math.round(railBox.left)}..${Math.round(railBox.right)}]`);
      }
    }
    // The model label's cap is what closes the rung arithmetic, so read what it
    // actually rendered at rather than trusting the declaration.
    const modelItem = items.find((item) => item.dataset.rail === "model");
    const modelLabel = modelItem?.querySelector<HTMLElement>(".chat-composer-trigger__label");
    const modelCap =
      modelLabel && getComputedStyle(modelLabel).display !== "none"
        ? {
            width: modelLabel.getBoundingClientRect().width,
            max: Number.parseFloat(getComputedStyle(modelLabel).maxWidth) || 0,
          }
        : null;
    return {
      height: railBox.height,
      control,
      modelCap,
      overflow: rail.scrollWidth - rail.clientWidth,
      outside,
      shown: items
        .filter((item) => getComputedStyle(item).display !== "none")
        .map((item) => {
          const label = item.querySelector<HTMLElement>(".chat-composer-trigger__label");
          const text = label && getComputedStyle(label).display !== "none" ? label.innerText.trim() : "";
          return text ? `${name(item)}:${text}` : name(item);
        }),
    };
  });
}

test("rail holds one row from 170 to 640", async ({ page }) => {
  await page.setViewportSize({ width: 640, height: 800 });
  await installFixture(page);
  await openChat(page);
  const longestMode = await pickLongestMode(page);

  const log: string[] = [];
  const failures: string[] = [];
  let last: RailReading | undefined;
  for (let width = 170; width <= 640; width += 10) {
    await page.setViewportSize({ width, height: 800 });
    // Let the container query and the ResizeObserver-driven layout settle.
    await page.waitForTimeout(60);
    const reading = await readRail(page);
    log.push(
      `${String(width).padStart(3)}  h=${reading.height.toFixed(1)}  ctl=${reading.control}  ovf=${reading.overflow}  ${reading.shown.join(" | ")}`,
    );
    // One control row: the rail's own height IS the control height when nothing
    // has wrapped, and exactly double it the moment something does.
    if (reading.height > reading.control + 0.5) {
      failures.push(`${width}px: rail is ${reading.height}px tall, past the ${reading.control}px control row`);
    }
    if (reading.overflow > 0) failures.push(`${width}px: rail overflows its box by ${reading.overflow}px`);
    for (const item of reading.outside) failures.push(`${width}px: ${item}`);
    if (reading.modelCap && reading.modelCap.width > reading.modelCap.max + 0.5) {
      failures.push(`${width}px: model label ran to ${reading.modelCap.width}px past its ${reading.modelCap.max}px cap`);
    }
    if (width === 640) last = reading;
  }
  console.log(`RAIL SWEEP (mode "${longestMode}", model "${LONG_MODEL}")\n${log.join("\n")}`);
  expect(failures, failures.join("\n")).toEqual([]);
  // The sweep only proves anything if the worst case was on screen: every
  // control the rail can carry, each with its widest label.
  expect(last?.shown.map((entry) => entry.split(":")[0])).toEqual([
    "slash",
    "model",
    "effort",
    "mode",
    "send",
  ]);
});

test("below the sweep the controls take their smaller notch and still hold", async ({ page }) => {
  await page.setViewportSize({ width: 640, height: 800 });
  await installFixture(page);
  await openChat(page);
  await pickLongestMode(page);

  // Narrower than any VS Code sidebar, which is the point: the notch exists so
  // the floor still fits a container the product should never see. Unproven
  // otherwise -- the 170..640 sweep never leaves 30px controls.
  await page.setViewportSize({ width: 150, height: 800 });
  await page.waitForTimeout(60);
  const reading = await readRail(page);
  expect(reading.height).toBeLessThanOrEqual(28.5);
  expect(reading.overflow).toBe(0);
  expect(reading.outside).toEqual([]);
});

test("the narrow rungs right-align the combined menu and send", async ({ page }) => {
  await page.setViewportSize({ width: 640, height: 800 });
  await installFixture(page);
  await openChat(page);
  await pickLongestMode(page);

  // Below the chips rung the model and effort chips fold into the mode menu,
  // and the closing group (mode + send) must sit flush against the rail's
  // right edge -- the free width opens between the slash key and the group,
  // never after send. The auto margin's anchor must be a VISIBLE item: were it
  // left on the folded (display:none) effort chip, the group would strand at
  // the rail's left and this test fails.
  await page.setViewportSize({ width: 180, height: 800 });
  await page.waitForTimeout(60);
  const layout = await page.evaluate(() => {
    const rail = document.querySelector<HTMLElement>(".chat-composer-rail");
    const root = document.querySelector<HTMLElement>(".chat-composer-root");
    if (!rail || !root) throw new Error("no rail");
    const box = (selector: string) => {
      const el = rail.querySelector<HTMLElement>(selector);
      if (!el) return null;
      if (getComputedStyle(el).display === "none") return null;
      const b = el.getBoundingClientRect();
      return { left: b.left, right: b.right };
    };
    return {
      containerWidth: root.getBoundingClientRect().width,
      rail: { left: rail.getBoundingClientRect().left, right: rail.getBoundingClientRect().right },
      slash: box(".chat-composer-slashkey"),
      model: box('[data-rail="model"]'),
      effort: box('[data-rail="effort"]'),
      mode: box('[data-rail="mode"]'),
      send: box(".chat-composer-send"),
    };
  });

  // Precondition: this IS the narrow rung -- the container sits below the
  // chips rung, so both foldable chips are off the rail.
  expect(layout.containerWidth).toBeLessThan(196);
  expect(layout.model).toBeNull();
  expect(layout.effort).toBeNull();

  if (!layout.slash || !layout.mode || !layout.send) throw new Error("rail cells missing");
  // The slash key opens the rail at its left edge.
  expect(Math.abs(layout.slash.left - layout.rail.left)).toBeLessThan(0.5);
  // Send closes the rail at its right edge, mode tight beside it.
  expect(Math.abs(layout.send.right - layout.rail.right)).toBeLessThan(0.5);
  expect(layout.send.left - layout.mode.right).toBeLessThan(8);
  // The rail's free width all sits between the slash key and the group.
  expect(layout.mode.left - layout.slash.right).toBeGreaterThan(8);
});
