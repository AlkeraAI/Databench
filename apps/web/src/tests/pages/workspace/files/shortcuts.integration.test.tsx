import { ContextMenu, Modal } from "@alkera/ui";
import { QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { createPortal } from "react-dom";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { buildContextMenuItems } from "@/pages/workspace/files/contextMenuItems";
import type { ClipboardItem } from "@/pages/workspace/files/state/clipboard";
import {
  FilesActions,
  type FilesOperationReport,
} from "@/pages/workspace/files/FilesActions";
import {
  SHORTCUT_BINDINGS,
  bindingsFor,
  shortcutLabel,
  type FilesAction,
  type Platform,
  type ShortcutBinding,
} from "@/pages/workspace/files/state/shortcuts";
import { useFilesShortcuts } from "@/pages/workspace/files/useFilesShortcuts";

// The Files keyboard table, driven end to end. The parametrised block is the
// AC: it is generated FROM the table, so a binding added to `shortcuts.ts`
// without a page action fails here rather than shipping as a dead key, and a
// binding deleted from the table takes its own case with it.

const DRIVE = "drv_1";

/** One clipboard entry, as a cut of `id` records it. */
function entry(id: string): ClipboardItem {
  return { id, etag: "e1", name: id };
}

function item(over: Partial<Item> = {}): Item {
  return {
    id: "nd_a",
    ino: 1,
    driveId: DRIVE,
    kind: "file",
    name: "report.csv",
    nameDisplay: "report.csv",
    nameEncoding: "utf-8",
    pathBytes: "",
    parentId: "nd_parent",
    path: "/home/dana/report.csv",
    etag: "e1",
    ctag: "c1",
    stale: false,
    locked: false,
    held: false,
    shared: false,
    trashed: false,
    capabilities: {
      can_read: true,
      can_write: true,
      can_share: true,
      can_delete: true,
      can_rename: true,
      can_download: true,
      can_lease: false,
      can_lease_request: false,
      can_lease_force: false,
    },
    ...over,
  } as Item;
}

const ALL_ACTIONS: readonly FilesAction[] = Array.from(
  new Set(SHORTCUT_BINDINGS.map((binding) => binding.action)),
);

/** The event one binding is pressed with, on its platform. */
function pressOf(binding: ShortcutBinding, platform: Platform): Record<string, unknown> {
  return {
    key: binding.key,
    shiftKey: binding.shift,
    metaKey: platform === "mac" && binding.accel,
    ctrlKey: platform === "other" && binding.accel,
  };
}

function Harness({
  platform,
  fired,
  handled = ALL_ACTIONS,
}: {
  platform: Platform;
  fired: FilesAction[];
  handled?: readonly FilesAction[];
}) {
  const handlers = Object.fromEntries(
    handled.map((action) => [action, () => fired.push(action)]),
  ) as Partial<Record<FilesAction, () => void>>;
  const { onKeyDown } = useFilesShortcuts({ platform, handlers });
  return (
    <div data-testid="grid" onKeyDown={onKeyDown}>
      <div tabIndex={0} data-testid="row">
        report.csv
      </div>
      <input aria-label="Search" />
    </div>
  );
}

describe.each<Platform>(["mac", "other"])("the Files keyboard table on %s", (platform) => {
  it.each(bindingsFor(platform).map((b) => [`${b.action} · ${b.shift ? "shift+" : ""}${b.accel ? "accel+" : ""}${b.key}`, b] as const))(
    "%s fires its action",
    (_id, binding) => {
      const fired: FilesAction[] = [];
      render(<Harness platform={platform} fired={fired} />);
      fireEvent.keyDown(screen.getByTestId("grid"), pressOf(binding, platform));
      expect(fired).toEqual([binding.action]);
    },
  );

  it.each(bindingsFor(platform).filter((b) => b.accel || b.shift).map((b) => [b.action, b] as const))(
    "%s does not fire without its modifiers",
    (_action, binding) => {
      const fired: FilesAction[] = [];
      render(<Harness platform={platform} fired={fired} />);
      // The negative twin of the case above: the same key, bare.
      fireEvent.keyDown(screen.getByTestId("grid"), { key: binding.key });
      expect(fired).not.toContain(binding.action);
    },
  );

  it("refuses the other platform's command modifier", () => {
    const fired: FilesAction[] = [];
    render(<Harness platform={platform} fired={fired} />);
    fireEvent.keyDown(screen.getByTestId("grid"), {
      key: "c",
      metaKey: platform !== "mac",
      ctrlKey: platform === "mac",
    });
    expect(fired).toEqual([]);
  });

  it("leaves the keystroke alone while a text field has focus", () => {
    const fired: FilesAction[] = [];
    render(<Harness platform={platform} fired={fired} />);
    fireEvent.keyDown(screen.getByLabelText("Search"), { key: "F2" });
    expect(fired).toEqual([]);
  });

  it("does not consume an action the page does not handle", () => {
    const fired: FilesAction[] = [];
    render(<Harness platform={platform} fired={fired} handled={[]} />);
    const event = new KeyboardEvent("keydown", { key: "F2", bubbles: true, cancelable: true });
    screen.getByTestId("grid").dispatchEvent(event);
    expect(fired).toEqual([]);
    expect(event.defaultPrevented).toBe(false);
  });
});

describe("Alt is never part of a Files shortcut", () => {
  it("ignores Alt+Cmd+C", () => {
    const fired: FilesAction[] = [];
    render(<Harness platform="mac" fired={fired} />);
    fireEvent.keyDown(screen.getByTestId("grid"), { key: "c", metaKey: true, altKey: true });
    expect(fired).toEqual([]);
  });
});

describe("the listing is one tab stop, and every other control keeps its own keys", () => {
  function Page({ fired }: { fired: FilesAction[] }) {
    const { onKeyDown } = useFilesShortcuts({
      platform: "other",
      handlers: {
        open: () => fired.push("open"),
        "quick-look": () => fired.push("quick-look"),
        trash: () => fired.push("trash"),
      },
    });
    return (
      <div onKeyDown={onKeyDown}>
        <button type="button">New folder</button>
        <label>
          <input type="checkbox" /> Show hidden
        </label>
        <div role="treegrid" aria-label="Listing">
          <div role="row">
            <div role="columnheader">
              <button type="button">Sort by Name</button>
            </div>
          </div>
          {["a.csv", "b.csv", "c.csv"].map((name, index) => (
            <div role="row" key={name} tabIndex={index === 1 ? 0 : -1} data-testid={name}>
              {name}
              <button type="button">Share {name}</button>
            </div>
          ))}
        </div>
        <button type="button">Details action</button>
      </div>
    );
  }

  it("Tab from a row leaves the listing for the control after it", () => {
    render(<Page fired={[]} />);
    const row = screen.getByTestId("b.csv");
    row.focus();
    const travelled = fireEvent.keyDown(row, { key: "Tab" });
    // Not the next row's Share button, and not the page body: past the rows.
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Details action" }));
    expect(travelled).toBe(false);
  });

  it("Shift+Tab from a row lands on the column header before it", () => {
    render(<Page fired={[]} />);
    const row = screen.getByTestId("b.csv");
    row.focus();
    fireEvent.keyDown(row, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Sort by Name" }));
  });

  it.each([
    ["a bar button", "New folder"],
    ["a column header", "Sort by Name"],
    ["a control after the listing", "Details action"],
  ])("Tab from %s is the browser's own", (_where, name) => {
    render(<Page fired={[]} />);
    const control = screen.getByRole("button", { name });
    control.focus();
    // Not prevented: the browser moves focus to the next control in document order.
    expect(fireEvent.keyDown(control, { key: "Tab" })).toBe(true);
    expect(fireEvent.keyDown(control, { key: "Tab", shiftKey: true })).toBe(true);
    expect(document.activeElement).toBe(control);
  });

  it.each([
    ["Enter", "Enter"],
    ["Space", " "],
  ])("%s on a button presses the button, not the listing's action", (_name, key) => {
    const fired: FilesAction[] = [];
    render(<Page fired={fired} />);
    const button = screen.getByRole("button", { name: "New folder" });
    button.focus();
    expect(fireEvent.keyDown(button, { key })).toBe(true);
    expect(fired).toEqual([]);
  });

  it("Space on a checkbox toggles it rather than opening a quick look", () => {
    const fired: FilesAction[] = [];
    render(<Page fired={fired} />);
    const box = screen.getByRole("checkbox", { name: "Show hidden" });
    box.focus();
    expect(fireEvent.keyDown(box, { key: " " })).toBe(true);
    expect(fired).toEqual([]);
  });

  it("Enter and Space on a row are still the listing's", () => {
    const fired: FilesAction[] = [];
    render(<Page fired={fired} />);
    const row = screen.getByTestId("b.csv");
    row.focus();
    expect(fireEvent.keyDown(row, { key: "Enter" })).toBe(false);
    expect(fireEvent.keyDown(row, { key: " " })).toBe(false);
    expect(fired).toEqual(["open", "quick-look"]);
  });

  it("a key other than Enter or Space on a button still reaches the table", () => {
    const fired: FilesAction[] = [];
    render(<Page fired={fired} />);
    const button = screen.getByRole("button", { name: "New folder" });
    button.focus();
    fireEvent.keyDown(button, { key: "Delete" });
    expect(fired).toEqual(["trash"]);
  });

  it("Ctrl+Tab is the browser's, not ours", () => {
    render(<Page fired={[]} />);
    const row = screen.getByTestId("b.csv");
    row.focus();
    expect(fireEvent.keyDown(row, { key: "Tab", ctrlKey: true })).toBe(true);
    expect(document.activeElement).toBe(row);
  });

  it("a key typed in a portalled overlay is not the page's", () => {
    const fired: string[] = [];
    function Overlaid() {
      const { onKeyDown } = useFilesShortcuts({
        platform: "other",
        handlers: { trash: () => fired.push("trash") },
      });
      return (
        <div onKeyDown={onKeyDown}>
          <div role="treegrid" aria-label="Listing">
            <div role="row" tabIndex={0} data-testid="row">
              report.csv
            </div>
          </div>
          <div tabIndex={0} data-testid="pane" />
          {createPortal(<button data-testid="overlay">Close</button>, document.body)}
        </div>
      );
    }

    render(<Overlaid />);
    const overlay = screen.getByTestId("overlay");
    overlay.focus();
    // React bubbles a portalled child's event through the COMPONENT tree, so a
    // key pressed in the file preview dialog reaches the page's binding even
    // though the dialog is mounted on `document.body`. The page must leave it
    // alone: the dialog traps its own Tab, and the row behind it is not what a
    // Delete typed inside it is aimed at.
    fireEvent.keyDown(overlay, { key: "Tab" });
    expect(document.activeElement).toBe(overlay);
    fireEvent.keyDown(overlay, { key: "Delete" });
    expect(fired).toEqual([]);

    // …and the page's own listing still hands Tab on, so the guard is not a blanket off.
    const row = screen.getByTestId("row");
    row.focus();
    fireEvent.keyDown(row, { key: "Tab" });
    expect(document.activeElement).toBe(screen.getByTestId("pane"));
  });
});

describe("the context menu shows the same table", () => {
  it.each<Platform>(["mac", "other"])("every captioned row on %s carries the table's label", (platform) => {
    const items = buildContextMenuItems({
      platform,
      targets: [item()],
      currentFolderId: "nd_parent",
      canWriteHere: true,
      clipboard: { mode: "copy", items: [entry("nd_b")], sourceFolderId: "nd_other" },
      onAction: () => undefined,
    });
    const captions = new Map(items.map((row) => [row.id, row.shortcut]));
    expect(captions.get("rename")).toBe(shortcutLabel("rename", platform));
    expect(captions.get("copy")).toBe(shortcutLabel("copy", platform));
    expect(captions.get("cut")).toBe(shortcutLabel("cut", platform));
    expect(captions.get("paste")).toBe(shortcutLabel("paste", platform));
    expect(captions.get("new-folder")).toBe(shortcutLabel("new-folder", platform));
    expect(captions.get("trash")).toBe(shortcutLabel("trash", platform));
    expect(captions.get("open")).toBe(shortcutLabel("open", platform));
    // Move to… has no binding at all, so it must show no caption rather than a stale one.
    expect(captions.get("move-to")).toBeUndefined();
  });

  it("names the whole context-menu item list, in order", () => {
    const items = buildContextMenuItems({
      platform: "other",
      targets: [item()],
      currentFolderId: "nd_parent",
      canWriteHere: true,
      clipboard: { mode: null, items: [], sourceFolderId: null },
      onAction: () => undefined,
    });
    expect(items.map((row) => row.id)).toEqual([
      "open",
      "open-new-tab",
      "rename",
      "share",
      // Under Share, because sharing and linking are halves of one errand —
      // handing this thing to somebody else. The link is an address and not the
      // access, so a row a person may only read still offers it.
      "copy-link",
      // Save as template… and New chat from template belong to a chat and a
      // template; a plain file has neither.
      "move-to",
      "copy-to",
      "copy",
      "cut",
      "paste",
      "duplicate",
      "download",
      "new-folder",
      "upload-files",
      "upload-folder",
      // "star" is retired from the menu. A file is not leasable, so none of the
      // lease four is drawn on one; a folder gets the one its lease state calls
      // for, which `menuRows` pins.
      // Versions sits beside Details: both look at one row rather than change it.
      "versions",
      "details",
      "trash",
    ]);
  });

  it("a folder nobody holds adds no lease row at all", () => {
    const items = buildContextMenuItems({
      platform: "other",
      targets: [item({ kind: "folder" })],
      currentFolderId: "nd_parent",
      canWriteHere: true,
      clipboard: { mode: null, items: [], sourceFolderId: null },
      onAction: () => undefined,
    });
    const ids = items.map((row) => row.id);
    // Details closes the block straight after the uploads: taking a folder out
    // is no longer offered anywhere, and the three that get one back need a
    // holder this folder does not have.
    expect(ids.indexOf("details")).toBe(ids.indexOf("upload-folder") + 1);
    expect(items.some((row) => row.label.startsWith("Lease for local use"))).toBe(false);
    // A folder is not a chat or a template, so neither of those rows is drawn.
    expect(ids).not.toContain("view-files");
    expect(ids).not.toContain("save-as-template");
  });

  it("in trash it is Restore and Delete forever and nothing else", () => {
    const items = buildContextMenuItems({
      platform: "other",
      targets: [item({ trashed: true })],
      currentFolderId: "nd_parent",
      canWriteHere: true,
      inTrash: true,
      clipboard: { mode: null, items: [], sourceFolderId: null },
      onAction: () => undefined,
    });
    expect(items.map((row) => row.id)).toEqual(["restore", "delete-forever"]);
  });
});

describe("a disabled row carries its reason", () => {
  it("prefers the server's own refusal over the client's wording", () => {
    const denied = item({
      capabilities: {
        can_read: true,
        can_write: false,
        can_share: false,
        can_delete: false,
        can_purge: false,
        can_rename: false,
        can_download: false,
        can_lease: false,
        can_lease_request: false,
        can_lease_force: false,
        refusals: { rename: "This folder is in use by Robin on MacBook Pro." },
      },
    } as Partial<Item>);
    const items = buildContextMenuItems({
      platform: "other",
      targets: [denied],
      currentFolderId: "nd_parent",
      canWriteHere: true,
      clipboard: { mode: null, items: [], sourceFolderId: null },
      onAction: () => undefined,
    });
    const byId = new Map(items.map((row) => [row.id, row]));
    expect(byId.get("rename")?.disabled).toBe("This folder is in use by Robin on MacBook Pro.");
    expect(byId.get("trash")?.disabled).toBe("You cannot delete this item.");
    expect(byId.get("download")?.disabled).toBe("You cannot download this item.");
    // A capability that IS granted leaves the row live.
    expect(byId.get("copy")?.disabled).toBeUndefined();
  });

  it("an empty selection drops the row actions and leaves the folder ones alone", () => {
    const items = buildContextMenuItems({
      platform: "other",
      targets: [],
      currentFolderId: "nd_parent",
      canWriteHere: true,
      clipboard: { mode: null, items: [], sourceFolderId: null },
      onAction: () => undefined,
    });
    const byId = new Map(items.map((row) => [row.id, row]));
    expect(byId.has("rename")).toBe(false);
    expect(byId.get("new-folder")?.disabled).toBeUndefined();
    expect(byId.get("upload-files")?.disabled).toBeUndefined();
  });

  it("a read-only folder refuses everything that would add to it", () => {
    const items = buildContextMenuItems({
      platform: "other",
      targets: [item()],
      currentFolderId: "nd_parent",
      canWriteHere: false,
      clipboard: { mode: "copy", items: [entry("nd_b")], sourceFolderId: "nd_other" },
      onAction: () => undefined,
    });
    const byId = new Map(items.map((row) => [row.id, row]));
    expect(byId.get("new-folder")?.disabled).toBe("You cannot add to this folder.");
    expect(byId.get("paste")?.disabled).toBe("You cannot add to this folder.");
    expect(byId.get("upload-folder")?.disabled).toBe("You cannot add to this folder.");
  });

  it("a cut pasted back where it came from says so", () => {
    const items = buildContextMenuItems({
      platform: "other",
      targets: [item()],
      currentFolderId: "nd_parent",
      canWriteHere: true,
      clipboard: { mode: "cut", items: [entry("nd_a")], sourceFolderId: "nd_parent" },
      onAction: () => undefined,
    });
    const byId = new Map(items.map((row) => [row.id, row]));
    expect(byId.get("paste")?.disabled).toBe("The cut items are already in this folder.");
  });

  it("offers the lease rows, each enabled only for whoever can run it", () => {
    const held = item({
      kind: "folder",
      lease: { holder: "Robin", machine: "MacBook Pro", purpose: "mount", mine: false },
    } as Partial<Item>);
    const mine = item({
      kind: "folder",
      lease: { holder: "Dana", machine: "Studio", purpose: "mount", mine: true },
    } as Partial<Item>);
    const reasons = (targets: readonly Item[], canForceRelease: boolean) => {
      const rows = new Map(
        buildContextMenuItems({
          platform: "other",
          targets,
          currentFolderId: "nd_parent",
          canWriteHere: true,
          canForceRelease,
          clipboard: { mode: null, items: [], sourceFolderId: null },
          onAction: () => undefined,
        }).map((row) => [row.id, row.disabled ?? null]),
      );
      return rows;
    };

    // Somebody else holds it: asking is open to any reader, taking it back is a
    // manager's — that is a permission, so it is named. Leasing and releasing do
    // not apply to a folder in somebody else's hands, so neither is drawn.
    const theirs = reasons([held], false);
    expect(theirs.get("request-release")).toBeNull();
    expect(theirs.get("force-release")).toBe("Only a manager can take a folder back.");
    expect(theirs.has("lease")).toBe(false);
    expect(theirs.has("release")).toBe(false);
    expect(reasons([held], true).get("force-release")).toBeNull();

    // My own lease: I can hand it back, and there is nobody to ask.
    const ownLease = reasons([mine], false);
    expect(ownLease.get("release")).toBeNull();
    expect(ownLease.has("request-release")).toBe(false);
    expect(ownLease.has("force-release")).toBe(false);
    expect(ownLease.has("lease")).toBe(false);

    // An ordinary file: there is no such thing as leasing one, so the four rows
    // are simply not in its menu.
    const plain = reasons([item()], false);
    for (const id of ["lease", "release", "request-release", "force-release"]) {
      expect(plain.has(id)).toBe(false);
    }
  });
});

describe("the menu on the page", () => {
  function stubFetch(): ReturnType<typeof vi.fn> {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({}), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }

  function Page({
    selection,
    downloads,
    onOpen,
    folderId = "nd_parent",
  }: {
    selection: readonly Item[];
    downloads?: string[];
    onOpen?: (row: Item) => void;
    folderId?: string;
  }) {
    return (
      <QueryClientProvider client={createQueryClient()}>
        <MemoryRouter>
          <FilesActions canWriteHere
            driveId={DRIVE}
            currentFolderId={folderId}
            selection={selection}
            platform="other"
            onOpen={onOpen}
            onDownload={(url) => downloads?.push(url)}
          >
            {({ triggerProps }) => (
              <div data-testid="grid" {...triggerProps}>
                <div tabIndex={0} data-testid="row">
                  report.csv
                </div>
              </div>
            )}
          </FilesActions>
        </MemoryRouter>
      </QueryClientProvider>
    );
  }

  beforeEach(() => stubFetch());
  afterEach(() => vi.unstubAllGlobals());

  it("Shift+F10 opens the menu at the focused row", async () => {
    render(<Page selection={[item()]} />);
    const row = screen.getByTestId("row");
    row.focus();
    fireEvent.keyDown(screen.getByTestId("grid"), { key: "F10", shiftKey: true });
    const menu = await screen.findByRole("menu", { name: "Actions for report.csv" });
    expect(menu).toBeInTheDocument();
    expect(screen.getByText("Move to trash")).toBeInTheDocument();
  });

  it("a right-click opens the same menu", async () => {
    render(<Page selection={[item()]} />);
    fireEvent.contextMenu(screen.getByTestId("grid"), { clientX: 40, clientY: 60 });
    expect(await screen.findByRole("menu", { name: "Actions for report.csv" })).toBeInTheDocument();
  });

  it("a fully-capable row enables every action it can run", async () => {
    // The projection's positive twin. A menu that greys every capability-gated row on a
    // row the server granted everything is the failure, so this states the opposite in full: given one target with every capability, none of the six
    // actions that read a capability is refused.
    const able = item({
      capabilities: {
        can_read: true,
        can_write: true,
        can_share: true,
        can_delete: true,
        can_rename: true,
        can_download: true,
        can_lease: false,
        can_lease_request: false,
        can_lease_force: false,
      },
    } as Partial<Item>);
    render(<Page selection={[able]} />);
    fireEvent.contextMenu(screen.getByTestId("grid"), { clientX: 10, clientY: 10 });
    await screen.findByRole("menu");
    for (const id of ["open", "rename", "move-to", "copy", "download", "trash"]) {
      const row = document.querySelector(`[data-item="${id}"]`);
      expect(row, id).not.toBeNull();
      expect(row?.getAttribute("aria-disabled"), id).not.toBe("true");
    }
  });

  it("with nothing selected the menu is what can be done to the folder", async () => {
    // The negative twin, and the shape the bug actually took: an empty selection is a menu
    // that can still create in the folder and nothing else.
    render(<Page selection={[]} />);
    fireEvent.contextMenu(screen.getByTestId("grid"), { clientX: 10, clientY: 10 });
    await screen.findByRole("menu");
    for (const id of ["open", "rename", "move-to", "copy", "download", "trash", "details"]) {
      expect(document.querySelector(`[data-item="${id}"]`), id).toBeNull();
    }
    expect(document.querySelector('[data-item="new-folder"]')?.getAttribute("aria-disabled")).not.toBe("true");
  });

  it("a disabled row is announced with its reason and cannot be selected", async () => {
    const opened: Item[] = [];
    const denied = item({
      capabilities: {
        can_read: false,
        can_write: false,
        can_share: false,
        can_delete: false,
        can_rename: false,
        can_download: false,
        can_lease: false,
        can_lease_request: false,
        can_lease_force: false,
      },
    } as Partial<Item>);
    render(<Page selection={[denied]} onOpen={(row) => opened.push(row)} />);
    fireEvent.contextMenu(screen.getByTestId("grid"), { clientX: 10, clientY: 10 });
    await screen.findByRole("menu");
    const open = document.querySelector('[data-item="open"]');
    expect(open?.getAttribute("aria-disabled")).toBe("true");
    expect(open?.getAttribute("title")).toBe("You do not have access to this item.");
    fireEvent.click(open as Element);
    expect(opened).toEqual([]);
  });

  it("Download goes to the item's content URL", async () => {
    const downloads: string[] = [];
    render(<Page selection={[item()]} downloads={downloads} />);
    fireEvent.contextMenu(screen.getByTestId("grid"), { clientX: 10, clientY: 10 });
    await screen.findByRole("menu");
    fireEvent.click(document.querySelector('[data-item="download"]') as Element);
    expect(downloads).toEqual([
      "/api/v1/files/drives/drv_1/items/nd_a/content?download=1",
    ]);
  });

  it("Move to trash sends the DELETE the row asked for", async () => {
    const fetchMock = stubFetch();
    render(<Page selection={[item()]} />);
    fireEvent.contextMenu(screen.getByTestId("grid"), { clientX: 10, clientY: 10 });
    await screen.findByRole("menu");
    fireEvent.click(document.querySelector('[data-item="trash"]') as Element);
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const request = fetchMock.mock.calls[0]?.[0] as Request;
    expect(request.method).toBe("DELETE");
    expect(request.url).toContain("/api/v1/files/drives/drv_1/items/nd_a");
    expect(request.headers.get("If-Match")).toBe("e1");
  });

  it("Ctrl+X then Ctrl+V in another folder issues the move", async () => {
    const fetchMock = stubFetch();
    const { rerender } = render(<Page selection={[item()]} folderId="nd_parent" />);
    fireEvent.keyDown(screen.getByTestId("grid"), { key: "x", ctrlKey: true });
    // Walking to another folder is what makes the paste a real move.
    rerender(<Page selection={[item()]} folderId="nd_dest" />);
    fireEvent.keyDown(screen.getByTestId("grid"), { key: "v", ctrlKey: true });
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const request = fetchMock.mock.calls[0]?.[0] as Request;
    expect(request.method).toBe("PATCH");
    expect(await request.clone().json()).toEqual({ parentId: "nd_dest" });
  });

  it("the paste carries the etag the row was cut at, not one looked up at paste time", async () => {
    const fetchMock = stubFetch();
    const { rerender } = render(<Page selection={[item()]} folderId="nd_parent" />);
    fireEvent.keyDown(screen.getByTestId("grid"), { key: "x", ctrlKey: true });
    // In the destination the cut row is not listed, so it cannot be selected there —
    // the shape a walk between folders leaves the page in.
    rerender(<Page selection={[]} folderId="nd_dest" />);
    fireEvent.keyDown(screen.getByTestId("grid"), { key: "v", ctrlKey: true });
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const request = fetchMock.mock.calls[0]?.[0] as Request;
    expect(request.method).toBe("PATCH");
    expect(request.url).toContain("/api/v1/files/drives/drv_1/items/nd_a");
    expect(request.headers.get("If-Match")).toBe("e1");
  });

  it("a cut pasted back into its own folder issues nothing", async () => {
    const fetchMock = stubFetch();
    render(<Page selection={[item()]} folderId="nd_parent" />);
    const grid = screen.getByTestId("grid");
    fireEvent.keyDown(grid, { key: "x", ctrlKey: true });
    fireEvent.keyDown(grid, { key: "v", ctrlKey: true });
    // The negative twin of the case above: a move onto itself is refused, not sent.
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

// The verbs the menu and the keyboard issue, and what each one reports back.
// The report is the seam the undo stack reads: an entry may only be recorded
// against an operation the SERVER named, so what matters here is not that a
// report happened but WHICH id it carries — and, for a trash, that it carries
// none.
describe("what a landed write reports", () => {
  interface Call {
    method: string;
    url: string;
    body: unknown;
  }

  /** A network that answers each Files route with its real status: a copy is
   *  always 202 with an operation, a release 200, a trash 204 with no body. */
  function stubRoutes(calls: Call[], leases: unknown[] = []): void {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const request = input as Request;
        const url = request.url;
        calls.push({
          method: request.method,
          url,
          body:
            request.method === "GET" || request.method === "DELETE"
              ? null
              : await request
                  .clone()
                  .json()
                  .catch(() => null),
        });
        const answer = (body: unknown, status = 200) =>
          new Response(status === 204 ? null : JSON.stringify(body), {
            status,
            headers: { "content-type": "application/json" },
          });
        if (url.includes("/leases")) return answer(leases);
        if (url.includes("/copy")) {
          return answer(
            {
              id: "op_copy_1",
              driveId: DRIVE,
              kind: "copy",
              state: "running",
              undoableUntil: "2026-09-10T00:00:00Z",
            },
            202,
          );
        }
        if (request.method === "DELETE") {
          // The route the page answers to: a trash runs AS an operation and comes
          // back 200 with it; only `?permanent=true` is the 204 with no inverse.
          if (url.includes("permanent=true")) return answer(null, 204);
          return answer(
            {
              id: "op_trash_1",
              driveId: DRIVE,
              kind: "trash",
              state: "done",
              undoableUntil: "2026-10-08T00:00:00Z",
            },
            200,
          );
        }
        return answer({});
      }),
    );
  }

  afterEach(() => vi.unstubAllGlobals());

  function Wired({
    selection,
    reports,
    folderId = "nd_parent",
    holderInstanceId,
    inTrash = false,
  }: {
    selection: readonly Item[];
    reports: FilesOperationReport[];
    folderId?: string;
    holderInstanceId?: string;
    inTrash?: boolean;
  }) {
    return (
      <QueryClientProvider client={createQueryClient()}>
        <MemoryRouter>
          <FilesActions canWriteHere
            driveId={DRIVE}
            currentFolderId={folderId}
            selection={selection}
            platform="other"
            inTrash={inTrash}
            holderInstanceId={holderInstanceId}
            onOperation={(report) => reports.push(report)}
          >
            {({ triggerProps }) => (
              <div data-testid="grid" {...triggerProps}>
                <div tabIndex={0} data-testid="row">
                  report.csv
                </div>
              </div>
            )}
          </FilesActions>
        </MemoryRouter>
      </QueryClientProvider>
    );
  }

  const leaseRow = {
    nodeId: "nd_a",
    epoch: 4,
    machine: "gpu-1",
    purpose: "mount",
    since: "2026-09-08T10:00:00Z",
    expiresAt: "2026-09-08T11:00:00Z",
  };

  it("a paste of a copy issues the copy route and reports the operation the server named", async () => {
    const calls: Call[] = [];
    stubRoutes(calls);
    const reports: FilesOperationReport[] = [];
    const { rerender } = render(
      <Wired selection={[item()]} reports={reports} folderId="nd_parent" />,
    );
    fireEvent.keyDown(screen.getByTestId("grid"), { key: "c", ctrlKey: true });
    rerender(<Wired selection={[item()]} reports={reports} folderId="nd_dest" />);
    fireEvent.keyDown(screen.getByTestId("grid"), { key: "v", ctrlKey: true });

    await waitFor(() => expect(reports).toHaveLength(1));
    const copy = calls.find((call) => call.url.includes("/copy"));
    expect(copy?.method).toBe("POST");
    expect(copy?.body).toMatchObject({ parentId: "nd_dest", conflictBehavior: "rename" });
    // The id is the server's, not one the browser minted: an undo posted against
    // anything else is a 404 on a real drive.
    expect(reports[0]).toMatchObject({
      kind: "copy",
      driveId: DRIVE,
      operationId: "op_copy_1",
      undoableUntil: "2026-09-10T00:00:00Z",
    });
  });

  it("a leased selection releases at the epoch my own lease row carries", async () => {
    // The facet deliberately carries no epoch, so Release is fenced on the row
    // the lease read answers — which is the only reason a leased selection
    // reads the leases at all.
    const calls: Call[] = [];
    const folder = item({
      id: "nd_a",
      kind: "folder",
      nameDisplay: "runs",
      name: "runs",
      lease: { holder: "me", machine: "gpu-1", purpose: "mount", mine: true },
    } as unknown as Partial<Item>);
    stubRoutes(calls, [leaseRow]);
    render(<Wired selection={[folder]} reports={[]} holderInstanceId="inst_7" />);

    fireEvent.contextMenu(screen.getByTestId("grid"), { clientX: 10, clientY: 10 });
    const menu = await screen.findByRole("menu");
    expect(within(menu).getByRole("menuitem", { name: /Rename/ })).toBeInTheDocument();
    await waitFor(() => expect(calls.some((call) => call.url.includes("/leases"))).toBe(true));
    fireEvent.click(document.querySelector('[data-item="release"]') as Element);

    await waitFor(() => expect(calls.some((call) => call.url.includes("/lease/release"))).toBe(true));
    const released = calls.find((call) => call.url.includes("/lease/release"));
    expect(released?.body).toMatchObject({ epoch: leaseRow.epoch, instanceId: "inst_7" });
  });

  it("a trash reports the operation the server ran it as", async () => {
    const calls: Call[] = [];
    stubRoutes(calls);
    const reports: FilesOperationReport[] = [];
    render(<Wired selection={[item()]} reports={reports} />);
    fireEvent.contextMenu(screen.getByTestId("grid"), { clientX: 10, clientY: 10 });
    await screen.findByRole("menu");
    fireEvent.click(document.querySelector('[data-item="trash"]') as Element);

    await waitFor(() => expect(reports).toHaveLength(1));
    expect(reports[0]?.kind).toBe("trash");
    // The handle the undo route accepts. Reporting none here — which is what the
    // page did while the route still answered 204 — left every trash unundoable.
    expect(reports[0]?.operationId).toBe("op_trash_1");
    expect(reports[0]?.undoableUntil).toBe("2026-10-08T00:00:00Z");
    expect(calls.some((call) => call.method === "DELETE")).toBe(true);
  });

  it("a delete forever reports no operation, because a purge has no inverse", async () => {
    const calls: Call[] = [];
    stubRoutes(calls);
    const reports: FilesOperationReport[] = [];
    render(<Wired selection={[item()]} reports={reports} inTrash />);
    fireEvent.contextMenu(screen.getByTestId("grid"), { clientX: 10, clientY: 10 });
    await screen.findByRole("menu");
    fireEvent.click(document.querySelector('[data-item="delete-forever"]') as Element);

    await waitFor(() =>
      expect(calls.some((call) => call.url.includes("permanent=true"))).toBe(true),
    );
    // The negative twin of the case above: the 204 branch. An id claimed here
    // would post an undo over bytes the server has already dropped.
    expect(reports).toEqual([]);
  });
});

// Every overlay the page draws over its listing, portalled or not.
//
// React bubbles a portalled child's event through the COMPONENT tree, and a
// non-portalled overlay is a DOM child of the very element the table hangs off —
// so both reach the page's binding, and both must be left alone. The one that is
// not portalled is the one that was missed: with the chooser open over a
// selected chat row, Delete trashed the chat behind it and Tab threw focus out
// of the chooser while its scrim stayed up.
describe("an overlay drawn over the listing owns the keyboard", () => {
  function Overlaid({ overlay, fired }: { overlay: ReactNode; fired: string[] }) {
    const { onKeyDown } = useFilesShortcuts({
      platform: "other",
      handlers: { trash: () => fired.push("trash"), rename: () => fired.push("rename") },
    });
    return (
      <QueryClientProvider client={createQueryClient({ retry: false })}>
        <div onKeyDown={onKeyDown}>
          <div tabIndex={0} data-testid="browser" />
          <div tabIndex={0} data-testid="pane" />
          {overlay}
        </div>
      </QueryClientProvider>
    );
  }

  /** One overlay per surface the page mounts, each the REAL component: a
   *  keystroke is aimed at whatever is on top, and every one of these can be. */
  const OVERLAYS: readonly {
    name: string;
    node: ReactNode;
    /** Reveal it, for a surface that opens on a click of its own. */
    reveal?: () => void;
    /** What the keystroke is typed into. */
    typedIn: () => HTMLElement;
  }[] = [
    {
      // The sheet every dialog on the page is built from — the file preview,
      // Move to…, Copy to…, Share…, Save as template…
      name: "a modal dialog",
      node: (
        <Modal open title="Copy 1 item to…" onClose={() => undefined}>
          <button type="button">Copy here</button>
        </Modal>
      ),
      typedIn: () => screen.getByRole("button", { name: "Copy here" }),
    },
    {
      name: "the row menu",
      node: (
        <ContextMenu
          open
          anchor={{ x: 10, y: 10 }}
          label="Actions"
          items={[{ id: "trash", label: "Move to trash" }]}
          onClose={() => undefined}
        />
      ),
      typedIn: () => screen.getByRole("menuitem", { name: /Move to trash/ }),
    },
  ];

  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(JSON.stringify([]), {
            status: 200,
            headers: { "content-type": "application/json" },
          }),
      ),
    );
  });

  afterEach(() => vi.unstubAllGlobals());

  it.each(OVERLAYS.map((overlay) => [overlay.name, overlay] as const))(
    "%s keeps Delete and F2 away from the row behind it",
    (_name, overlay) => {
      const fired: string[] = [];
      render(<Overlaid overlay={overlay.node} fired={fired} />);
      overlay.reveal?.();
      const typedIn = overlay.typedIn();
      typedIn.focus();

      fireEvent.keyDown(typedIn, { key: "Delete" });
      fireEvent.keyDown(typedIn, { key: "F2" });
      fireEvent.keyDown(typedIn, { key: "Tab" });

      expect(fired).toEqual([]);
      // Tab is the overlay's own to place: what it must never do is hand focus
      // to a page zone underneath, where the reader cannot see it.
      expect(document.activeElement).not.toBe(screen.getByTestId("browser"));
      expect(document.activeElement).not.toBe(screen.getByTestId("pane"));
    },
  );

  it("the narrow details sheet is a zone, not an overlay: its keys are still the page's", () => {
    // Under 900 px the pane is drawn as `role="dialog"` (FilesPage's shell) and
    // its keys are STILL the page's. A guard that read the role
    // alone would take the keyboard away from it.
    const fired: string[] = [];
    render(
      <Overlaid
        fired={fired}
        overlay={
          <div role="dialog" aria-label="Details">
            <button type="button">Details action</button>
          </div>
        }
      />,
    );
    const inSheet = screen.getByRole("button", { name: "Details action" });
    inSheet.focus();

    fireEvent.keyDown(inSheet, { key: "Delete" });

    expect(fired).toEqual(["trash"]);
  });
});
