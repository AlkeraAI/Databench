// The chat's Files pane keeps a row's name beside its live chip.
//
// The rule that stops the chip taking the whole name cell lived in the Files
// page's own stylesheet, which only the `/files` route loads. On a cold load of
// a chat, the pane drew the chip at its full nowrap width and squeezed the name
// beside it to nothing: a row reading only "left on the workspace machine, not
// saved" with no word of which file.
//
// jsdom lays nothing out, so the width itself cannot be measured. What decides
// it can be: the stylesheets a cold load of the pane brings are exactly the ones
// its modules import, statically, from the pane's own file down. This test walks
// that import graph, loads every sheet it finds into the document, renders the
// real pane over a row the machine is holding, and asks the cascade what the
// chip it painted is bounded by — so the rule counts only if it reaches THIS
// chip through a sheet THIS pane loads.

import { readFileSync, existsSync } from "node:fs";
import { dirname, join, resolve } from "node:path";

import { QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { tabKindFor, type WorkspaceCtx } from "@/pages/workspace/chat/workspace/tabKinds";
import { useWorkspaceStore } from "@/pages/workspace/chat/workspace/workspaceStore";

import "@/pages/workspace/chat/workspace/FilesTab";

const SRC = join(process.cwd(), "src");
const PANE = join(SRC, "pages/workspace/chat/workspace/FilesTab.tsx");
const FILES_PAGE_CSS = join(SRC, "pages/workspace/files/files-page.css");

/** Where a module specifier lands on disk, or null for a package import. */
function resolveModule(from: string, spec: string): string | null {
  let base: string;
  if (spec.startsWith("@/")) base = join(SRC, spec.slice(2));
  else if (spec.startsWith(".")) base = resolve(dirname(from), spec);
  else return null;
  if (spec.endsWith(".css")) return existsSync(base) ? base : null;
  for (const candidate of [base, `${base}.tsx`, `${base}.ts`, join(base, "index.tsx"), join(base, "index.ts")]) {
    if (/\.(tsx?)$/.test(candidate) && existsSync(candidate)) return candidate;
  }
  return null;
}

/** Every plain stylesheet the module graph under `entry` loads with it. Only
 *  static imports count: a lazily imported module's sheet arrives only when
 *  something opens it, which a cold load of the pane never does. */
function stylesheetsLoadedBy(entry: string): string[] {
  const seen = new Set<string>();
  const sheets = new Set<string>();
  const queue = [entry];
  const statement = /^\s*(?:import|export)\s+(type\s+)?(?:[^"';]*?\s+from\s+)?["']([^"']+)["']/gm;
  while (queue.length > 0) {
    const file = queue.pop()!;
    if (seen.has(file)) continue;
    seen.add(file);
    const source = readFileSync(file, "utf8");
    for (const match of source.matchAll(statement)) {
      if (match[1]) continue; // a type import carries no code and no sheet
      const target = resolveModule(file, match[2]!);
      if (target === null) continue;
      if (target.endsWith(".css")) {
        if (!target.endsWith(".module.css")) sheets.add(target);
      } else queue.push(target);
    }
  }
  return [...sheets];
}

const DRIVE = "drv_1";
const CHAT_NODE = "nd_chat";
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
    attrs: { mtime: "2026-09-01T10:00:00Z" },
    file: { size: 3 },
    symlink: null,
    object: null,
    lease: null,
    live: null,
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

const CHAT_FOLDER = item({
  id: CHAT_NODE,
  name: "3952c9e2.alkerachat",
  kind: "folder",
  parentId: "nd_home",
  file: null,
  object: {
    id: "obj_chat_1",
    type: "chat",
    title: "qa-proof pool-gvisor-b8",
    web_url: "/chat/cht_1",
    metadata: { files_node_id: ROOT },
  } as Item["object"],
});
const ROOT_FOLDER = item({ id: ROOT, name: "scratch", kind: "folder", parentId: CHAT_NODE, file: null });
/** A nameless row: bytes the machine wrote and never sent. */
const LEFT_BEHIND = item({
  id: "nd_hello",
  name: "hello.txt",
  live: { state: "writing", content: "unsynced" } as Item["live"],
});

function json(body: unknown, status = 200): Promise<Response> {
  return Promise.resolve(
    new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } }),
  );
}

beforeEach(() => {
  const items: Record<string, Item> = {
    [CHAT_NODE]: CHAT_FOLDER,
    [ROOT]: ROOT_FOLDER,
    [LEFT_BEHIND.id]: LEFT_BEHIND,
  };
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input instanceof Request ? input.url : input.toString();
      if (/\/items\/nd_root\/children/.test(url)) return json({ value: [LEFT_BEHIND], nextMarker: null });
      const one = /\/items\/([^/?]+)(?:\?|$)/.exec(url);
      if (one) {
        const found = items[decodeURIComponent(one[1] ?? "")];
        return found ? json(found) : json({ code: "files.not_found", message: "no" }, 404);
      }
      return json({ value: [], nextMarker: null });
    }),
  );
  useWorkspaceStore.setState({ chats: {} });
});

afterEach(() => {
  vi.unstubAllGlobals();
  document.head.querySelectorAll("style[data-sheet]").forEach((node) => node.remove());
  useWorkspaceStore.setState({ chats: {} });
});

function loadSheets(paths: string[]): void {
  for (const path of paths) {
    const style = document.createElement("style");
    style.setAttribute("data-sheet", path);
    style.textContent = readFileSync(path, "utf8");
    document.head.appendChild(style);
  }
}

function mountPane() {
  const kind = tabKindFor("files");
  if (kind === undefined) throw new Error("the `files` tab kind was never registered");
  const Component = kind.Component;
  return render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter>
        <Component tab={{ id: "files", kind: "files", name: "Files" }} ctx={CTX} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("a chat's Files pane on a cold load", () => {
  it("does not load the Files page's stylesheet — the page it belongs to is not open", () => {
    // The premise of the defect, pinned so a later change that happens to pull
    // the page sheet in does not quietly stand in for the pane's own rule.
    expect(stylesheetsLoadedBy(PANE)).not.toContain(FILES_PAGE_CSS);
  });

  it("bounds a row's live chip in the name cell, so the file's name keeps its room", async () => {
    loadSheets(stylesheetsLoadedBy(PANE));
    mountPane();

    const row = await waitFor(() => {
      const found = document.querySelector<HTMLElement>('[data-row-id="nd_hello"]');
      expect(found?.querySelector(".alk-files-live-chip") ?? null).not.toBeNull();
      return found!;
    });
    const chip = row.querySelector<HTMLElement>(".alk-files-live-chip")!;
    expect(chip.textContent).toMatch(/not saved/);
    // The chip and the name share the name cell: that cell is where the chip
    // must give way.
    const cell = chip.closest('[data-column="name"]');
    expect(cell).not.toBeNull();
    expect(cell).toHaveTextContent("hello.txt");

    const style = getComputedStyle(chip);
    // Never more than a share of the cell, able to shrink below its text, and
    // cut with an ellipsis rather than pushing the name out.
    expect(style.maxWidth).toBe("40%");
    expect(["0", "0px"]).toContain(style.minWidth);
    expect(style.overflow).toBe("hidden");
    expect(style.textOverflow).toBe("ellipsis");
  });

  it("draws the create form in the pane's own style, not the browser's", async () => {
    loadSheets(stylesheetsLoadedBy(PANE));
    mountPane();
    const create = await waitFor(() => {
      const found = [...document.querySelectorAll<HTMLButtonElement>(".alk-files-browser__create")].find((b) => b.textContent === "New notebook");
      expect(found?.disabled).toBe(false);
      return found!;
    });
    fireEvent.click(create);
    const form = document.querySelector<HTMLElement>("form.alk-files__new-folder")!;
    expect(form).not.toBeNull();
    // The rules that size the field and its buttons to the bar reach this
    // form through a sheet this pane loads.
    expect(getComputedStyle(form).display).toBe("flex");
    const submit = [...form.querySelectorAll("button")].find((b) => b.textContent === "Create")!;
    expect(getComputedStyle(submit).cursor).toBe("pointer");
    expect(getComputedStyle(submit).borderRadius).not.toBe("");
  });
});
