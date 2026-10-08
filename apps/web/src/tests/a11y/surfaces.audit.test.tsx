// The standing audit: no surface ships a control a screen reader announces as
// nothing.
//
// It renders the real surfaces and asks the rendered tree, so it fails the day a
// name is taken off a control — not the day a file is renamed. A new icon-only
// button anywhere inside one of these surfaces is caught by the surface's own
// case without this file being edited.

import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";

import { createQueryClient } from "@/api/queryClient";
import type { Item } from "@/api/files";
import { FilesBrowser } from "@/pages/workspace/files/FilesBrowser";
import { FILES_NARROW_QUERY, FilesHomeProvider, FilesPage } from "@/pages/workspace/files/FilesPage";
import { FileTabHeader } from "@/pages/workspace/chat/workspace/FileTabHeader";

import { MachineDetailPage } from "@/pages/organization/machines/MachineDetailPage";
import { MachinesPage } from "@/pages/organization/machines/MachinesPage";
import { WorkspaceMachineChip } from "@/pages/workspace/workspaces/WorkspaceMachine";

import { machinesServer, removeTopbar, renderAt as renderMachinesAt } from "../pages/organization/machines/machinesServer";
import { accessibleName, reportUnnamed, unnamedControls } from "./accessibleName";

const DRIVE = "drv_1";
const PARENT = "nd_parent";

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
    parentId: PARENT,
    path: null,
    etag: "e1",
    ctag: "c1",
    attrs: null,
    file: null,
    symlink: null,
    object: null,
    lease: null,
    stale: false,
    trust: null,
    locked: false,
    held: false,
    capabilities: {},
    shared: false,
    trashed: false,
    ...overrides,
  } as unknown as Item;
}

const ROWS: Item[] = [
  item({ id: "nd_a", name: "alpha.txt", file: { size: 1200 } as Item["file"] }),
  item({ id: "nd_c", name: "charlie", kind: "folder" }),
];

function stubChildren(rows: Item[] = ROWS): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(() =>
      Promise.resolve(
        new Response(JSON.stringify({ value: rows, nextMarker: null }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      ),
    ),
  );
}

function stubViewport(narrow: boolean): void {
  vi.stubGlobal(
    "matchMedia",
    (query: string): MediaQueryList =>
      ({
        media: query,
        matches: query === FILES_NARROW_QUERY ? narrow : false,
        onchange: null,
        addEventListener: () => undefined,
        removeEventListener: () => undefined,
        addListener: () => undefined,
        removeListener: () => undefined,
        dispatchEvent: () => false,
      }) as unknown as MediaQueryList,
  );
}

function mount(children: ReactNode) {
  return render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter initialEntries={["/files/nd_parent"]}>
        <Routes>
          <Route path="/files/:nodeId" element={<>{children}</>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** The audit itself: every operable control on screen has a name. */
function expectEveryControlNamed(surface: string): void {
  const found = unnamedControls(document.body);
  expect(found, reportUnnamed(surface, found)).toEqual([]);
}

beforeEach(() => {
  stubViewport(false);
  stubChildren();
  window.localStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

describe("every control on a portal surface has a name", () => {
  it("the Files shell — rail, toolbar, details", async () => {
    mount(
      <FilesHomeProvider nodeId="nd_parent">
        <FilesPage
          browser={<div>listing</div>}
          pane={<p>details</p>}
          placeNodeIds={{ home: "nd_home" }}
        />
      </FilesHomeProvider>,
    );
    await screen.findByText("listing");
    expectEveryControlNamed("Files shell");
  });

  it("the Files listing — sort headers, rows, view switch", async () => {
    mount(<FilesBrowser driveId={DRIVE} parentId={PARENT} platform="other" />);
    await waitFor(() => expect(document.querySelectorAll("[data-row-id]").length).toBe(ROWS.length));
    expectEveryControlNamed("Files listing (list)");

    await userEvent.click(screen.getByRole("button", { name: "Grid" }));
    await waitFor(() => expect(screen.queryAllByRole("columnheader")).toHaveLength(0));
    expectEveryControlNamed("Files listing (grid)");
  });

  it("the compact file-tab header, folded and unfolded", () => {
    const actions = [
      { id: "wrap", label: "Soft wrap", icon: <svg aria-hidden />, run: () => undefined, pressed: true },
      { id: "download", label: "Download", icon: <svg aria-hidden />, run: () => undefined },
      { id: "close", label: "Close", icon: <svg aria-hidden />, run: () => undefined },
    ];
    mount(<FileTabHeader name="notes.md" path="/notes.md" facts={["1 KB"]} actions={actions} />);
    expectEveryControlNamed("File tab header");
    for (const action of actions) {
      expect(screen.getByRole("button", { name: action.label })).toBeInTheDocument();
    }
  });
});

describe("every control on the machine surfaces has a name", () => {
  let server: ReturnType<typeof machinesServer>;
  beforeEach(() => {
    server = machinesServer();
  });
  afterEach(() => removeTopbar());

  it("the Machines page and its row menu", async () => {
    renderMachinesAt(<MachinesPage />, "/settings/organization/machines");
    await screen.findByText("A100 Lab", { selector: ".alk-machine__name" });
    expectEveryControlNamed("Machines page");
    await userEvent.click(screen.getByRole("button", { name: "Actions for A100 Lab" }));
    expectEveryControlNamed("Machines row menu");
  });

  it("a machine's page with its editors open", async () => {
    renderMachinesAt(<MachineDetailPage />, `/settings/organization/machines/${server.machines[0].id}`, "/settings/organization/machines/:machineId");
    await screen.findByRole("heading", { name: "Who can use it" });
    for (const edit of screen.getAllByRole("button", { name: "Edit" })) await userEvent.click(edit);
    expectEveryControlNamed("Machine page");
  });

  it("the workspace machine chip and the change-machine dialog", async () => {
    const lab = server.machines[0].card;
    server.workspaceMachines["ws-1"] = { card: lab, pin: lab.org_machine_id ?? null, active_move: null, can_move: true, targets: [lab] };
    renderMachinesAt(<WorkspaceMachineChip workspaceId="ws-1" />, "/workspaces/ws-1");
    await userEvent.click(await screen.findByRole("button", { name: "A100 Lab, Running" }));
    await screen.findByRole("dialog", { name: "Change workspace machine" });
    expectEveryControlNamed("Change workspace machine");
  });
});

describe("the audit itself", () => {
  // The audit is only worth its green if it goes red when a name is removed —
  // otherwise it is a test of nothing. These drive the scanner over a tree the
  // test builds, so the two cases differ by exactly one attribute.
  it("names a control by its label, its content, its title, then nothing", () => {
    const host = document.createElement("div");
    host.innerHTML = `
      <button aria-label="Close">x</button>
      <button>Save</button>
      <button title="Copy link"><svg></svg></button>
      <button><svg></svg></button>
    `;
    document.body.append(host);
    const named = Array.from(host.querySelectorAll("button")).map((b) => accessibleName(b));
    expect(named).toEqual(["Close", "Save", "Copy link", ""]);
    expect(unnamedControls(host)).toHaveLength(1);
  });

  it("reads a name through aria-labelledby and an image's alt, and ignores aria-hidden text", () => {
    const host = document.createElement("div");
    host.innerHTML = `
      <span id="audit-heading">Delete folder</span>
      <button aria-labelledby="audit-heading"><svg></svg></button>
      <button><img alt="Alkera" src="/a.png"></button>
      <button><span aria-hidden="true">decorative</span></button>
    `;
    document.body.append(host);
    const [byId, byAlt, hiddenOnly] = Array.from(host.querySelectorAll("button"));
    expect(accessibleName(byId!)).toBe("Delete folder");
    expect(accessibleName(byAlt!)).toBe("Alkera");
    expect(accessibleName(hiddenOnly!)).toBe("");
  });

  it("does not report a control the accessibility tree cannot see", () => {
    const host = document.createElement("div");
    host.innerHTML = `
      <div aria-hidden="true"><button><svg></svg></button></div>
      <button hidden><svg></svg></button>
      <button role="presentation"><svg></svg></button>
    `;
    document.body.append(host);
    expect(unnamedControls(host)).toEqual([]);
  });
});
