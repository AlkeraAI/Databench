/**
 * A keystroke and a toolbar button refuse what the menu refuses, in its words.
 *
 * A viewer's right-click greyed Rename with "You can view this, not rename it",
 * yet Enter and F2 opened the rename editor anyway, and the server's 403 came
 * back only after they had typed a name. Upload files opened the picker into a
 * folder they could not add to, and the tray reported the failure after the
 * bytes were chosen. Every road to a capability-gated action now asks the menu
 * first: a refused row is refused before anything opens, a row that does not
 * apply (Rename on several rows) does nothing, and a permitted one runs.
 *
 * The refusal card of the inline rename also hangs below the field rather than
 * beside it: a row is one line high, and the sentence wrapped over the next
 * column and the next row with its first line cut off.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { FilesActions, type FilesActionsApi } from "@/pages/workspace/files/FilesActions";
import type { Platform } from "@/pages/workspace/files/state/shortcuts";

const OWNER_CAPS = {
  can_read: true,
  can_write: true,
  can_share: true,
  can_delete: true,
  can_rename: true,
  can_download: true,
};

/** What the server sends a person holding Can view. */
const VIEWER_CAPS = {
  can_read: true,
  can_write: false,
  can_share: false,
  can_delete: false,
  can_rename: false,
  can_download: true,
  refusals: {
    can_write: "insufficient_role",
    can_share: "insufficient_role",
    can_delete: "insufficient_role",
    can_rename: "insufficient_role",
  },
};

function item(id: string, capabilities: Record<string, unknown>): Item {
  return {
    id,
    name: `${id}.txt`,
    nameDisplay: `${id}.txt`,
    kind: "file",
    etag: "7",
    parentId: "nd_folder",
    capabilities,
  } as unknown as Item;
}

interface Opened {
  renamed: string[];
  uploads: number;
  folders: number;
}

function mount(options: {
  selection: Item[];
  canWriteHere: boolean;
  platform?: Platform;
}): { opened: Opened; api: () => FilesActionsApi; keyTarget: HTMLElement } {
  const opened: Opened = { renamed: [], uploads: 0, folders: 0 };
  let api: FilesActionsApi | null = null;
  render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter>
        <FilesActions
          driveId="drv_1"
          currentFolderId="nd_folder"
          selection={options.selection}
          canWriteHere={options.canWriteHere}
          platform={options.platform ?? "other"}
          onRename={(one) => opened.renamed.push(one.id)}
          onUploadFiles={() => {
            opened.uploads += 1;
          }}
          onNewFolder={() => {
            opened.folders += 1;
          }}
        >
          {(actions) => {
            api = actions;
            return (
              <div data-testid="listing" tabIndex={-1} onKeyDown={actions.onKeyDown}>
                listing
              </div>
            );
          }}
        </FilesActions>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { opened, api: () => api!, keyTarget: screen.getByTestId("listing") };
}

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response("{}", { status: 200, headers: { "content-type": "application/json" } })),
  );
});
afterEach(() => vi.unstubAllGlobals());

describe("renaming from the keyboard", () => {
  it.each([
    ["F2", "other" as Platform],
    ["F2", "mac" as Platform],
    ["Enter", "mac" as Platform],
  ])("%s on %s says why a viewer may not rename, and opens nothing", (key, platform) => {
    const { opened, keyTarget } = mount({
      selection: [item("a", VIEWER_CAPS)],
      canWriteHere: false,
      platform,
    });
    fireEvent.keyDown(keyTarget, { key });
    expect(opened.renamed).toEqual([]);
    expect(screen.getByRole("alert")).toHaveTextContent("You can view this, not rename it.");
  });

  it("opens the editor for someone who may rename", () => {
    const { opened, keyTarget } = mount({ selection: [item("a", OWNER_CAPS)], canWriteHere: true });
    fireEvent.keyDown(keyTarget, { key: "F2" });
    expect(opened.renamed).toEqual(["a"]);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("renames nothing when several rows are selected, as the menu offers no Rename", () => {
    const { opened, keyTarget } = mount({
      selection: [item("a", OWNER_CAPS), item("b", OWNER_CAPS)],
      canWriteHere: true,
      platform: "mac",
    });
    fireEvent.keyDown(keyTarget, { key: "Enter" });
    fireEvent.keyDown(keyTarget, { key: "F2" });
    expect(opened.renamed).toEqual([]);
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

describe("the create buttons, which run the menu's actions", () => {
  it.each([["upload-files"], ["new-folder"], ["upload-folder"]] as const)(
    "%s in a folder a viewer cannot add to says so before anything opens",
    (action) => {
      const { opened, api } = mount({ selection: [], canWriteHere: false });
      act(() => api().run(action));
      expect(opened).toEqual({ renamed: [], uploads: 0, folders: 0 });
      expect(screen.getByRole("alert")).toHaveTextContent("You cannot add to this folder.");
    },
  );

  it("opens the picker in a folder the caller may add to", () => {
    const { opened, api } = mount({ selection: [], canWriteHere: true });
    act(() => api().run("upload-files"));
    act(() => api().run("new-folder"));
    expect(opened).toEqual({ renamed: [], uploads: 1, folders: 1 });
  });
});

describe("the inline rename's refusal", () => {
  const css = readFileSync(
    join(process.cwd(), "src/pages/workspace/files/files-page.css"),
    "utf8",
  ).replace(/\/\*[\s\S]*?\*\//g, "");
  const rule = (selector: string): string => {
    const found = css
      .split("}")
      .find((chunk) => chunk.split("{")[0]!.trim() === selector);
    expect(found, `no rule for ${selector}`).toBeDefined();
    return found!;
  };

  it("hangs below the field as a card instead of wrapping inside a one-line row", () => {
    expect(rule(".alk-files-rename")).toMatch(/position:\s*relative/);
    const error = rule(".alk-files-rename__error");
    expect(error).toMatch(/position:\s*absolute/);
    expect(error).toMatch(/top:\s*calc\(100%/);
    expect(error).toMatch(/max-inline-size:/);
    expect(error).toMatch(/background:/);
  });
});
