/**
 * The four things a customer saw wrong on their first visit to Files.
 *
 * Each pin fails on the code as it was: the rail greyed three places out and
 * said nothing; Download opened a tab a popup blocker eats; the collision
 * prompt parked its only explanation in a `title`; and the Owner column
 * printed a raw UUID for every row including the person's own.
 */

import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { FilesActions, type FilesActionsApi } from "@/pages/workspace/files/FilesActions";
import { OwnerCell, ownerLabel } from "@/pages/workspace/files/OwnerCell";
import { FILES_PLACES, Sidebar, placeReason } from "@/pages/workspace/files/Sidebar";
import { UploadTray } from "@/pages/workspace/files/UploadTray";
import { buildContextMenuItems } from "@/pages/workspace/files/contextMenuItems";
import { emptyClipboard } from "@/pages/workspace/files/state/clipboard";
import { shortcutLabel } from "@/pages/workspace/files/state/shortcuts";

const DRIVE = "dr_1";
const ME = "0c0b5192-6465-4fa7-aa55-2cf43ca1fcb0";
const THEM = "9f11aa20-1111-4444-8888-2cf43ca1fcb0";

function item(overrides: Partial<Item> = {}): Item {
  return {
    id: "nd_1",
    name: "notes.md",
    nameDisplay: "notes.md",
    kind: "file",
    etag: "7",
    parentId: "nd_root",
    // Every capability, as the server sends them to an owner: the page asks the
    // menu before it runs a write, and a row with none would be refused.
    capabilities: {
      can_read: true,
      can_write: true,
      can_share: true,
      can_delete: true,
      can_rename: true,
      can_download: true,
    },
    ...overrides,
  } as Item;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  document.body.innerHTML = "";
});

/* ------------------------------------------------------------------ rail */

describe("the Files rail", () => {
  const FEEDS = ["recent", "sharedWithMe"] as const;

  it.each(FEEDS)("%s is reachable once the drive has loaded, with no node id", (id) => {
    const place = FILES_PLACES.flat().find((entry) => entry.id === id)!;
    expect(place.feed).toBe(true);
    expect(
      placeReason(place, { onSelect: true, reachable: [], driveReady: true }),
    ).toBeUndefined();
  });

  it("no longer lists Starred at all", () => {
    expect(FILES_PLACES.flat().find((entry) => entry.id === "starred")).toBeUndefined();
  });

  it("navigates when a feed place is clicked", async () => {
    const chosen: string[] = [];
    render(
      <MemoryRouter>
        <Sidebar driveReady onSelect={(place) => chosen.push(place)} />
      </MemoryRouter>,
    );
    const recent = screen.getByRole("button", { name: /Recent/ });
    expect(recent).not.toBeDisabled();
    await userEvent.click(recent);
    expect(chosen).toEqual(["recent"]);
  });

  it("never disables a place without a reason a person can read", () => {
    render(
      <MemoryRouter>
        <Sidebar driveReady onSelect={() => {}} />
      </MemoryRouter>,
    );
    for (const button of screen.getAllByRole("button")) {
      if (!(button as HTMLButtonElement).disabled) continue;
      const described = button.getAttribute("aria-describedby");
      expect(described).not.toBeNull();
      const why = document.getElementById(described!);
      // Visible text, not a `title` a touch user can never surface.
      expect(why?.textContent?.trim()).toBeTruthy();
    }
  });
});

describe("the rail while the drive is still loading", () => {
  it("is marked busy and adds no loading line of its own under every place", () => {
    // The listing beside it already says "Opening your files…"; a copy of the same wait under each
    // place was a second loading line on one screen.
    render(
      <MemoryRouter>
        <Sidebar onSelect={() => {}} />
      </MemoryRouter>,
    );
    expect(screen.getByRole("navigation", { name: "Places" })).toHaveAttribute("aria-busy", "true");
    expect(screen.queryByText("Waiting for your drive to load.")).toBeNull();
  });
});

/* -------------------------------------------------------------- download */

describe("Download", () => {
  it("saves through an anchor rather than opening a tab", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(JSON.stringify({ entries: [] }), {
            status: 200,
            headers: { "content-type": "application/json" },
          }),
      ),
    );
    const open = vi.fn();
    vi.stubGlobal("open", open);

    const anchors: HTMLAnchorElement[] = [];
    const created = document.createElement.bind(document);
    vi.spyOn(document, "createElement").mockImplementation(((tag: string) => {
      const element = created(tag);
      if (tag === "a") {
        const anchor = element as HTMLAnchorElement;
        anchor.click = vi.fn();
        anchors.push(anchor);
      }
      return element;
    }) as typeof document.createElement);

    let api: FilesActionsApi | null = null;
    render(
      <QueryClientProvider client={createQueryClient()}>
        <MemoryRouter>
          <FilesActions canWriteHere driveId={DRIVE} currentFolderId="nd_root" selection={[item()]}>
            {(actions) => {
              api = actions;
              return <div />;
            }}
          </FilesActions>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    api!.run("download");

    await waitFor(() => expect(anchors).toHaveLength(1));
    const anchor = anchors[0]!;
    expect(anchor.getAttribute("download")).toBe("notes.md");
    expect(anchor.getAttribute("href")).toContain(`/drives/${DRIVE}/items/nd_1/content`);
    expect(anchor.getAttribute("href")).toContain("disposition=attachment");
    expect(anchor.click).toHaveBeenCalledTimes(1);
    expect(open).not.toHaveBeenCalled();
  });
});

/* ------------------------------------------------------- collision prompt */

describe("the name-collision prompt", () => {
  const props = {
    rows: [],
    skippedSidecars: 0,
    identicalCopies: 0,
    alreadyInFiles: 0,
    finished: 0,
    refusal: null,
    resumable: [],
    onPause: () => {},
    onResume: () => {},
    onCancel: () => {},
    onAnswerConflict: () => {},
  };

  it("offers all three answers, each of them live, and says what Replace does", async () => {
    // All three are real answers, and what Replace will do is said as text a touch screen and a screen reader reach.
    const answered: [string, unknown][] = [];
    render(
      <UploadTray
        {...props}
        conflicts={[{ uploadId: "up_1", name: "notes.md", parentId: "nd_root" }]}
        onAnswerConflict={(uploadId, answer) => answered.push([uploadId, answer])}
      />,
    );

    for (const label of ["Replace", "Keep both", "Skip"]) {
      expect(screen.getByRole("button", { name: label })).not.toBeDisabled();
    }
    expect(screen.getByText(/Replace keeps the file and adds your upload as a new version/))
      .toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Replace" }));
    // Answered by the upload that asked, not by the name it spells: a drop can
    // carry two files called `notes.md`, each with its own question.
    expect(answered).toEqual([["up_1", "replace"]]);
  });
});

/* ------------------------------------------------------------ owner cell */

describe("the Owner cell", () => {
  it.each([
    ["the server's name for a colleague", { attrs: { owner: THEM }, ownerName: "Dana Rowe" }, "Dana Rowe"],
    ["the server's label for a machine", { attrs: { owner: THEM }, ownerName: "Agent" }, "Agent"],
    ["your own row reads You", { attrs: { owner: ME }, ownerName: "Ana Ruiz" }, "You"],
    ["an owner the server did not name is a dash, never the id", { attrs: { owner: THEM } }, "—"],
    ["a name inside attrs is not the wire's label", { attrs: { owner: THEM, ownerName: "Dana Rowe" } }, "—"],
    ["an unowned row is a dash", { attrs: {} }, "—"],
  ])("%s", (_name, fields, text) => {
    const label = ownerLabel(item(fields as Partial<Item>), ME);
    expect(label.text).toBe(text);
  });

  it("renders You for the signed-in person's own rows", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(JSON.stringify({ id: ME, first_name: "A", last_name: "B" }), {
            status: 200,
            headers: { "content-type": "application/json" },
          }),
      ),
    );
    render(
      <QueryClientProvider client={createQueryClient()}>
        <OwnerCell item={item({ attrs: { owner: ME } } as Partial<Item>)} />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(screen.getByText("You")).toBeInTheDocument());
    expect(screen.queryByText(ME)).toBeNull();
  });
});

/* ------------------------------------------------------- shortcut captions */

describe("the context menu's shortcut captions", () => {
  // The menu is the only place a person is told which key does what, so a
  // caption naming a key the platform does not bind is the product lying: a
  // macOS laptop has no forward Delete, and the table binds the trash to Cmd+Backspace.
  it.each([
    ["mac", "trash", "\u2318\u232b"],
    ["other", "trash", "Delete"],
    ["mac", "new-folder", "\u21e7\u2318N"],
    ["other", "new-folder", "Ctrl+Shift+N"],
    ["mac", "copy", "\u2318C"],
    ["other", "copy", "Ctrl+C"],
  ] as const)("%s captions %s as %s", (platform, action, caption) => {
    expect(shortcutLabel(action, platform)).toBe(caption);
  });

  it("every caption the menu shows is the resolved platform's, never the other's", () => {
    for (const platform of ["mac", "other"] as const) {
      const items = buildContextMenuItems({
        platform,
        targets: [item()],
        currentFolderId: "nd_root",
        canWriteHere: true,
        clipboard: emptyClipboard,
        onAction: () => {},
      });
      let captions = 0;
      for (const row of items) {
        if (row.shortcut === undefined) continue;
        captions += 1;
        // A caption may only spell the modifiers its own platform has: a mac
        // caption never says Ctrl or names the forward-Delete key it lacks, and
        // a PC caption never wears a glyph no PC keyboard prints.
        if (platform === "mac") {
          expect(row.shortcut).not.toMatch(/Ctrl|Shift\+|^Delete$/);
        } else {
          expect(row.shortcut).not.toMatch(/[\u2318\u21e7\u232b\u2326\u21a9]/);
        }
      }
      expect(captions).toBeGreaterThan(0);
      const trash = items.find((row) => row.id === "trash");
      expect(trash?.shortcut).toBe(platform === "mac" ? "\u2318\u232b" : "Delete");
    }
  });
});
