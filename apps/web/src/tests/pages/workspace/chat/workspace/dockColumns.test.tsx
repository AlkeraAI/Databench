// The chat's file dock is a column the reader drags, and the listing inside it
// is the Files page's five-column table — whose floors add up to nearly 600px.
// At the default split that pane is under 400, and the table was drawn clipped:
// headers cut to "K…", "Mo…", "Owne", row values wrapped and shaved off.
//
// The decision is asserted twice: once as arithmetic, on the pure function that
// makes it, and once through the real tab, whose wrapper names the columns it
// gave up so the sheet beside it can hide them. The layout those names drive is
// a CSS contract, and jsdom neither lays out nor cascades — so, as the tab's
// one-line-header contract already is, it is read off the sheet that states it.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { QueryClientProvider } from "@tanstack/react-query";
import { render, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import {
  dockColumnLayout,
  hiddenColumnsAttr,
} from "@/pages/workspace/chat/workspace/dockColumns";
import {
  tabKindFor,
  type WorkspaceCtx,
  type WorkspaceTab,
} from "@/pages/workspace/chat/workspace/tabKinds";

// Imported for its registration only; the component comes from the registry.
import "@/pages/workspace/chat/workspace/FilesTab";

describe("the columns a dock this wide can draw", () => {
  it("keeps the whole table when there is room for every floor", () => {
    expect(dockColumnLayout(960).hidden).toEqual([]);
    expect(dockColumnLayout(960).visible).toEqual(["name", "kind", "size", "modified", "owner"]);
  });

  it("gives up Owner and Kind first, as the page's own narrow row does", () => {
    // 200 (name) + 64 (size) + 150 (modified) = 414 fits; kind's 72 does not.
    expect(dockColumnLayout(480).hidden).toEqual(["kind", "owner"]);
    expect(dockColumnLayout(480).visible).toEqual(["name", "size", "modified"]);
  });

  it("gives up Modified next, at the width the dock actually opens to", () => {
    for (const width of [320, 395]) {
      expect(dockColumnLayout(width).visible).toEqual(["name", "size"]);
      expect(dockColumnLayout(width).hidden).toEqual(["kind", "modified", "owner"]);
    }
  });

  it("never gives up Name, and never asks it for a width it has not got", () => {
    const layout = dockColumnLayout(120);
    expect(layout.visible).toEqual(["name"]);
    // The floor is a budget the others are measured against, not a track: a
    // 200px track in a 120px pane is exactly the overflow being fixed.
    expect(layout.template).toBe("minmax(0, 2.6fr)");
  });

  it("hands over one track per visible cell, in the listing's own order", () => {
    const layout = dockColumnLayout(480);
    expect(layout.template.split(" minmax").length).toBe(layout.visible.length);
    expect(layout.template.startsWith("minmax(0, 2.6fr)")).toBe(true);
  });

  it("reads an unmeasured pane as the whole table, not as no room at all", () => {
    expect(dockColumnLayout(0).hidden).toEqual([]);
    expect(hiddenColumnsAttr(dockColumnLayout(0))).toBeUndefined();
  });

  it("names the given-up columns the way the sheet matches them", () => {
    expect(hiddenColumnsAttr(dockColumnLayout(320))).toBe("kind modified owner");
  });
});

/* --- the tab itself ----------------------------------------------------- */

const DRIVE = "drv_1";
const ROOT = "nd_root";
const CTX: WorkspaceCtx = { chatId: "cht_1", driveId: DRIVE, rootNodeId: ROOT };

function item(overrides: Partial<Item> & { id: string; name: string }): Item {
  return {
    ino: 1,
    driveId: DRIVE,
    kind: "file",
    subtype: null,
    nameDisplay: overrides.name,
    nameEncoding: "utf-8",
    nameFlags: { windows_safe: true, macos_safe: true, display_warning: false },
    pathBytes: "",
    parentId: ROOT,
    path: null,
    etag: "e1",
    ctag: "c1",
    attrs: { mtime: "2026-09-18T10:00:00Z" },
    file: { size: 8 },
    symlink: null,
    object: null,
    lease: null,
    stale: false,
    trust: null,
    locked: false,
    held: false,
    capabilities: { can_write: true },
    shared: false,
    trashed: false,
    ...overrides,
  } as unknown as Item;
}

const ROOT_FOLDER = item({ id: ROOT, name: "scratch", kind: "folder", parentId: "nd_chat" });
const ROWS: Item[] = [item({ id: "nd_note", name: "qa-note.md" })];

function json(body: unknown, status = 200): Promise<Response> {
  return Promise.resolve(
    new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } }),
  );
}

/** Every element measures this wide, which is how a jsdom pane gets a width at
 *  all — nothing here lays out. Restored between cases. */
function paneMeasures(px: number): void {
  Object.defineProperty(HTMLElement.prototype, "clientWidth", {
    configurable: true,
    get: () => px,
  });
}

function FilesTabComponent(props: { tab: WorkspaceTab; ctx: WorkspaceCtx }) {
  const kind = tabKindFor("files");
  if (kind === undefined) throw new Error("the `files` tab kind was never registered");
  return <kind.Component {...props} />;
}

async function mountDock(): Promise<HTMLElement> {
  const client = createQueryClient({ retry: false });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <FilesTabComponent tab={{ id: "files", kind: "files", name: "Files" }} ctx={CTX} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  // The listing arrives over the wire; the pane's own width does not wait for it.
  await waitFor(() => expect(document.querySelector(".alk-ws-files")).not.toBeNull());
  const pane = document.querySelector(".alk-ws-files");
  if (!(pane instanceof HTMLElement)) throw new Error("the dock never mounted");
  return pane;
}

describe("the dock at the width the reader left it", () => {
  const original = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "clientWidth");

  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = typeof input === "string" ? input : input.toString();
        if (/\/items\/[^/?]+\/children/.test(url)) return json({ value: ROWS, nextMarker: null });
        const one = /\/items\/([^/?]+)(?:\?|$)/.exec(url);
        if (one && decodeURIComponent(one[1] ?? "") === ROOT) return json(ROOT_FOLDER);
        if (one) return json({ code: "files.not_found", message: "no" }, 404);
        return json({ value: [], nextMarker: null });
      }),
    );
  });

  afterEach(() => {
    if (original) Object.defineProperty(HTMLElement.prototype, "clientWidth", original);
    else Reflect.deleteProperty(HTMLElement.prototype, "clientWidth");
    vi.unstubAllGlobals();
  });

  it("gives up three columns at 320 and hands over the two tracks that are left", async () => {
    paneMeasures(320);
    const pane = await mountDock();
    expect(pane.getAttribute("data-hide-columns")).toBe("kind modified owner");
    expect(pane.style.getPropertyValue("--chat-dock-grid")).toBe(
      "minmax(0, 2.6fr) minmax(64px, 0.7fr)",
    );
  });

  it("keeps Modified at 480", async () => {
    paneMeasures(480);
    const pane = await mountDock();
    expect(pane.getAttribute("data-hide-columns")).toBe("kind owner");
    expect(pane.style.getPropertyValue("--chat-dock-grid")).toContain("minmax(150px, 1.6fr)");
  });

  it("names nothing when the pane is wide enough for the whole table", async () => {
    paneMeasures(960);
    const pane = await mountDock();
    expect(pane.hasAttribute("data-hide-columns")).toBe(false);
  });
});

/* --- the layout those names drive --------------------------------------- */

const WORKSPACE_CSS = readFileSync(
  join(process.cwd(), "src/pages/workspace/chat/workspace/workspace.css"),
  "utf8",
).replace(/\/\*[\s\S]*?\*\//g, "");

/** The declarations of the first rule whose selector list ends in `selector`. */
function rule(selector: string): string {
  const at = WORKSPACE_CSS.indexOf(selector);
  expect(at, `no rule matching \`${selector}\``).toBeGreaterThan(-1);
  const open = WORKSPACE_CSS.indexOf("{", at);
  return WORKSPACE_CSS.slice(open + 1, WORKSPACE_CSS.indexOf("}", open));
}

describe("what the sheet does with them", () => {
  it("draws the row from the tracks the tab handed over", () => {
    expect(rule(".alk-ws-files .alk-files-grid[data-view=\"list\"] .alk-files-grid__lane")).toContain(
      "grid-template-columns: var(--chat-dock-grid",
    );
  });

  it("hides a named column's cells", () => {
    expect(
      rule('.alk-ws-files[data-hide-columns~="owner"]').trim(),
    ).toBe("display: none;");
  });

  it("ends a kept column's cell in an ellipsis rather than wrapping the row", () => {
    const cell = rule(
      '.alk-ws-files .alk-files-grid[data-view="list"] .alk-files-grid__cell[data-column="modified"]',
    );
    expect(cell).toContain("white-space: nowrap");
    expect(cell).toContain("text-overflow: ellipsis");
  });

  it("keeps the open file's header on one line, so a narrow dock folds rather than wraps", () => {
    // A second row of controls is a row the file itself pays for. The header
    // answers a narrow dock by moving actions into a menu (see
    // `visibleActionCount`), which needs the row not to wrap out from under the
    // measurement it is folding against.
    expect(rule(".alk-ws-file__bar")).toContain("flex-wrap: nowrap");
    expect(rule(".alk-ws-file__actions")).toContain("flex-wrap: nowrap");
  });
});
