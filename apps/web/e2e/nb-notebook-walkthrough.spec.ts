// Two people and an agent editing one notebook, in a real browser.
//
// The harness page (dev-notebook.html) runs a stand-in for the server's half
// of the Loro lane on the app's own Loro, with a delay on every hop, and mounts
// the real notebook tab twice, for Ana and for Ben, each on a socket of its
// own; the agent is a third copy of the document on a third socket writing
// notebook operations. What this proves, that jsdom cannot: real keystrokes
// reach CodeMirror and the notebook's keymaps, text and structure converge
// across all three copies through the server, carets are drawn where the
// other person stands with their name, undo is per person, and the narrow
// layout fits a phone.

import { expect, test, type Locator, type Page } from "@playwright/test";

// A machine whose cached browser is not the build this Playwright pins can name
// one it has.
if (process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE) {
  test.use({ launchOptions: { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE } });
}

// Wide enough that each person's pane is past the narrow breakpoint (768 px)
// and has command mode; the phone test narrows it.
test.use({ viewport: { width: 1720, height: 1000 } });

type Who = "ana" | "ben";

interface Cells {
  order: string[];
  sources: Record<string, string>;
}

let problems: string[] = [];

test.beforeEach(({ page }) => {
  problems = [];
  page.on("console", (message) => {
    if (message.type() === "error") problems.push(`console: ${message.text()}`);
  });
  page.on("pageerror", (error) => problems.push(`page: ${error.message}`));
});

test.afterEach(() => {
  expect(problems).toEqual([]);
});

const pane = (page: Page, who: Who): Locator => page.locator(`[data-person="${who}"]`);
const cell = (page: Page, who: Who, id: string): Locator => pane(page, who).locator(`[data-cell-id="${id}"]`);
const editor = (page: Page, who: Who, id: string): Locator => cell(page, who, id).locator(".cm-content");

/** A cell editor's text: its lines, without anyone's caret flag or an empty cell's placeholder. */
async function textOf(page: Page, who: Who, id: string): Promise<string> {
  return editor(page, who, id).evaluate((content) =>
    [...content.querySelectorAll(".cm-line")]
      .map((line) => {
        const copy = line.cloneNode(true) as HTMLElement;
        copy.querySelectorAll(".alk-cm-caret, .cm-placeholder").forEach((drawn) => drawn.remove());
        return copy.textContent ?? "";
      })
      .join("\n"),
  );
}

/** The notebook as a person's editor shows it. */
async function shown(page: Page, who: Who): Promise<Cells> {
  const order = await pane(page, who)
    .locator("[data-cell-id]")
    .evaluateAll((cells) => cells.map((c) => c.getAttribute("data-cell-id") ?? ""));
  const sources: Record<string, string> = {};
  for (const id of order) sources[id] = await textOf(page, who, id);
  return { order, sources };
}

async function open(page: Page): Promise<string[]> {
  await page.goto("/dev-notebook.html");
  const seeded = await page.evaluate(() => window.seeded as string[]);
  for (const who of ["ana", "ben"] as const) {
    await expect(editor(page, who, seeded[2]!)).toHaveText("print(y)");
  }
  await page.waitForFunction(() => window.agentReady);
  return seeded;
}

/** Put the caret at the end of a cell's text. */
async function typeAtEnd(page: Page, who: Who, id: string, text: string, delay = 0): Promise<void> {
  await editor(page, who, id).click();
  await page.keyboard.press("ControlOrMeta+End");
  await page.keyboard.type(text, { delay });
}

/** Every copy (the server's, the agent's, Ana's and Ben's editors) holds the same notebook. */
async function expectConverged(page: Page): Promise<Cells> {
  let server: Cells = { order: [], sources: {} };
  await expect
    .poll(async () => {
      server = await page.evaluate(() => window.serverCells());
      const copies = [await page.evaluate(() => window.agentCells()), await shown(page, "ana"), await shown(page, "ben")];
      // A copy that differs is shown whole, so a failure says which and how.
      return copies.map((copy) => (JSON.stringify(copy) === JSON.stringify(server) ? "same as the server" : copy));
    })
    .toEqual(["same as the server", "same as the server", "same as the server"]);
  return server;
}

test("Ana's typing reaches Ben, and two people typing at once converge", async ({ page }) => {
  const [x, y] = await open(page);
  await typeAtEnd(page, "ana", x!, "0");
  await expect.poll(() => textOf(page, "ben", x!)).toBe("x = 10");

  // Ben starts typing while Ana's next words are still on the wire.
  await typeAtEnd(page, "ana", x!, "  # from ana", 5);
  await typeAtEnd(page, "ben", y!, "  # from ben", 5);
  const server = await expectConverged(page);
  expect(server.sources[x!]).toBe("x = 10  # from ana");
  expect(server.sources[y!]).toBe("y = x + 1  # from ben");
});

test("Ana's caret shows in Ben's view, in the cell she stands in, with her name", async ({ page }) => {
  const [x, y, z] = await open(page);
  await editor(page, "ana", y!).click();
  await page.keyboard.press("ControlOrMeta+End");
  const caret = cell(page, "ben", y!).locator(".alk-cm-caret");
  const name = cell(page, "ben", y!).locator(".alk-cm-caret__name");
  await expect(caret).toHaveCount(1);
  await expect(name).toHaveText("Ana");
  // Placed where her caret stands, so it shows.
  await expect(name).toBeVisible();
  await expect(cell(page, "ben", x!).locator(".alk-cm-caret")).toHaveCount(0);
  await expect(cell(page, "ben", z!).locator(".alk-cm-caret")).toHaveCount(0);
  // Ben's own view never draws Ana a caret in her own editor.
  await expect(pane(page, "ana").locator(".alk-cm-caret")).toHaveCount(0);
  await expect(cell(page, "ben", y!).getByLabel("Here: Ana")).toBeVisible();
  // The caret is a zero-width bar, so the line's text never moves for it, and
  // its name is drawn beside the editor's scroller, which would clip it.
  const geometry = await name.evaluate((flag) => {
    const editor = flag.closest(".cm-editor")!;
    const bar = editor.querySelector(".alk-cm-caret")!;
    return {
      width: bar.getBoundingClientRect().width,
      flag: getComputedStyle(flag).position,
      inScroller: editor.querySelector(".cm-scroller")!.contains(flag),
    };
  });
  expect(geometry).toEqual({ width: 0, flag: "absolute", inScroller: false });

  // She moves to another cell: the caret follows.
  await editor(page, "ana", z!).click();
  await expect(cell(page, "ben", z!).locator(".alk-cm-caret__name")).toHaveText("Ana");
  await expect(cell(page, "ben", y!).locator(".alk-cm-caret")).toHaveCount(0);
});

test("the agent inserts a cell and rewrites Ben's cell while Ben types elsewhere", async ({ page }) => {
  const [x, y, z] = await open(page);
  await typeAtEnd(page, "ben", y!, "  # ben's", 5);
  await expectConverged(page);

  const typing = typeAtEnd(page, "ben", z!, "  # still typing", 20);
  const [inserted] = await page.evaluate(
    (after) => window.agent([{ op: "insert", source: "z = 3", name: "extra", after }]),
    x!,
  );
  await page.evaluate((cellId) => window.agent([{ op: "replace", cell_id: cellId, source: "y = x * 2" }]), y!);
  await typing;

  const server = await expectConverged(page);
  expect(server.order).toEqual([x, inserted, y, z]);
  expect(server.sources[inserted!]).toBe("z = 3");
  expect(server.sources[y!]).toBe("y = x * 2");
  expect(server.sources[z!]).toBe("print(y)  # still typing");
});

test("command-mode keys insert, delete and restore a cell, and Shift+Enter asks for a run", async ({ page }) => {
  const [x, y, z] = await open(page);
  const notebook = pane(page, "ana").getByTestId("notebook-editor");
  await expect(notebook).not.toHaveClass(/nb-notebook--narrow/);
  await editor(page, "ana", x!).click();
  await page.keyboard.press("Escape");
  await expect(notebook).toHaveAttribute("data-mode", "command");

  await page.keyboard.press("b");
  await expect.poll(async () => (await shown(page, "ben")).order.length).toBe(4);
  const added = (await page.evaluate(() => window.serverCells())).order[1]!;
  expect([x, y, z]).not.toContain(added);
  expect((await page.evaluate(() => window.serverCells())).order).toEqual([x, added, y, z]);

  // The new cell is being edited; leave it, then delete it with D D.
  await page.keyboard.press("Escape");
  await page.keyboard.press("d");
  await page.keyboard.press("d");
  await expect.poll(async () => (await shown(page, "ben")).order).toEqual([x, y, z]);

  await page.keyboard.press("z");
  await expect.poll(async () => (await shown(page, "ben")).order.includes(added)).toBe(true);
  await expectConverged(page);

  // Shift+Enter in the editor runs the cell, with the frontier Ana holds.
  await editor(page, "ana", y!).click();
  await page.keyboard.press("Shift+Enter");
  await expect
    .poll(() => page.evaluate(() => window.requests.filter((r) => r.method === "POST" && r.url.endsWith("/runs")).map((r) => r.body)))
    .toEqual([expect.objectContaining({ target: { kind: "cells", ids: [y] }, confirm_expensive: false, frontier: expect.any(String) })]);
  // It ran rather than breaking the line.
  expect(await textOf(page, "ana", y!)).toBe("y = x + 1");
});

test("undo in Ana's editor takes back only Ana's last change", async ({ page }) => {
  const [x, y, z] = await open(page);
  await typeAtEnd(page, "ana", x!, "  # ana");
  await typeAtEnd(page, "ben", y!, "  # ben");
  await page.evaluate((cellId) => window.agent([{ op: "replace", cell_id: cellId, source: "print(x, y)" }]), z!);
  await expectConverged(page);

  // Ben's change and the agent's are newer than Ana's: undo still takes hers.
  await editor(page, "ana", x!).click();
  await page.keyboard.press("ControlOrMeta+z");
  const server = await expectConverged(page);
  expect(server.sources).toEqual({ [x!]: "x = 1", [y!]: "y = x + 1  # ben", [z!]: "print(x, y)" });

  // Nothing of Ana's is left to undo: another press changes nobody else's text.
  await page.keyboard.press("ControlOrMeta+z");
  await page.waitForTimeout(300);
  expect((await expectConverged(page)).sources).toEqual(server.sources);
});

test("a phone-width window shows one column with the floating Run button and no sideways scroll", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const [x] = await open(page);
  for (const who of ["ana", "ben"] as const) {
    await expect(pane(page, who).locator(".nb-notebook--narrow")).toBeVisible();
  }
  const [anaBox, benBox] = [await pane(page, "ana").boundingBox(), await pane(page, "ben").boundingBox()];
  expect(benBox!.y).toBeGreaterThanOrEqual(anaBox!.y + anaBox!.height);
  const overflow = await page.evaluate(() => ({ scroll: document.scrollingElement!.scrollWidth, inner: window.innerWidth }));
  expect(overflow.scroll).toBeLessThanOrEqual(overflow.inner);

  const fab = pane(page, "ana").locator(".nb-fab");
  await expect(fab).toBeVisible();
  await expect(fab).toHaveAccessibleName("Run cell");
  await fab.click();
  await expect
    .poll(() => page.evaluate(() => window.requests.filter((r) => r.url.endsWith("/runs")).map((r) => (r.body as { target: unknown }).target)))
    .toEqual([{ kind: "cells", ids: [x] }]);
});

test("a narrow pane's toolbar wraps its controls whole, with nothing overlapping or cut off", async ({ page }) => {
  // Two panes side by side, each about 530 px: below the narrow breakpoint.
  await page.setViewportSize({ width: 1090, height: 900 });
  await open(page);
  for (const who of ["ana", "ben"] as const) {
    const toolbar = pane(page, who).locator(".nb-toolbar");
    await expect(toolbar).toBeVisible();
    const layout = await toolbar.evaluate((bar) => {
      const box = bar.getBoundingClientRect();
      const items = [...bar.children]
        .map((el) => ({ name: (el.textContent ?? el.getAttribute("aria-label") ?? "").trim(), rect: el.getBoundingClientRect() }))
        .filter((item) => item.rect.width > 0 && item.rect.height > 0);
      const overlaps: string[] = [];
      items.forEach((a, i) =>
        items.slice(i + 1).forEach((b) => {
          const apart = a.rect.right <= b.rect.left + 0.5 || b.rect.right <= a.rect.left + 0.5 || a.rect.bottom <= b.rect.top + 0.5 || b.rect.bottom <= a.rect.top + 0.5;
          if (!apart) overlaps.push(`${a.name} / ${b.name}`);
        }),
      );
      // A control whose label broke onto a second line is taller than one row.
      const broken = [...bar.querySelectorAll("button")]
        .filter((b) => b.getBoundingClientRect().height > 0 && b.scrollHeight > 36)
        .map((b) => b.textContent?.trim() ?? "");
      return {
        width: box.width,
        scroll: bar.scrollWidth,
        client: bar.clientWidth,
        outside: items.filter((item) => item.rect.left < box.left - 0.5 || item.rect.right > box.right + 0.5).map((item) => item.name),
        overlaps,
        broken,
      };
    });
    expect(layout.width).toBeLessThan(600);
    expect(layout.scroll).toBeLessThanOrEqual(layout.client);
    expect(layout.outside).toEqual([]);
    expect(layout.overlaps).toEqual([]);
    expect(layout.broken).toEqual([]);
  }
  const overflow = await page.evaluate(() => ({ scroll: document.scrollingElement!.scrollWidth, inner: window.innerWidth }));
  expect(overflow.scroll).toBeLessThanOrEqual(overflow.inner);
});

test("the environment follows the kernel, a restart says so, and an install's outcome is shown", async ({ page }) => {
  await open(page);
  const ana = pane(page, "ana");
  const ben = pane(page, "ben");
  // The listing names the uv project; the kernel started on the default
  // environment before the project's spec was added.
  await page.evaluate(() =>
    window.engine({ type: "kernel.state", kernel_id: "k1", seq: 1, state: "idle", env_id: "default:.alkera/envs/default" }),
  );
  for (const who of [ana, ben]) {
    await expect(who.getByRole("toolbar", { name: "Notebook" })).toContainText("Default");
    await expect(who.getByTestId("env-pending")).toHaveText(/The kernel uses Default until a restart moves it to uv project\./);
  }

  // Ana restarts from the note; the engine restarts the kernel.
  await ana.getByTestId("env-pending").getByRole("button", { name: "Restart" }).click();
  await page.getByRole("dialog").getByRole("button", { name: /restart/i }).click();
  await expect.poll(() => page.evaluate(() => window.requests.filter((r) => r.url.endsWith("/kernel")).map((r) => r.body))).toEqual([{ action: "restart" }]);
  await page.evaluate(() => {
    window.engine({ type: "kernel.state", kernel_id: "k1", seq: 2, state: "restarting", env_id: "default:.alkera/envs/default" });
    window.engine({ type: "kernel.exited", kernel_id: "k1", seq: 3, reason: "restart" });
  });
  await expect(ana.getByText("Kernel restarted.")).toBeVisible();
  await expect(ana.getByText("The kernel stopped.")).toHaveCount(0);
  await page.evaluate(() => window.engine({ type: "kernel.state", kernel_id: "k2", seq: 1, state: "idle", env_id: "uv_project:." }));
  await expect(ana.getByTestId("env-pending")).toHaveCount(0);

  // An install the box refuses says why, after saying it is under way.
  await ana.locator("button.nb-tab", { hasText: "Environment" }).click();
  const panel = ana.getByRole("region", { name: "Environment" });
  await panel.getByRole("textbox", { name: "Packages to install" }).fill("six");
  await panel.getByRole("button", { name: "Install" }).click();
  await expect(panel.getByTestId("install-status")).toHaveText("Installing six…");
  await page.evaluate(() => window.engine({ type: "env.install", status: "error", packages: ["six"], message: "add failed (exit 1)" }));
  await expect(panel.getByTestId("install-status")).toHaveText("Could not install six: add failed (exit 1).");
  await expect(panel.getByRole("button", { name: "Install" })).toBeVisible();
});
