/**
 * A member's home reads as the home mark and its owner's name, everywhere.
 *
 * A home is stored under its owner's id (`/home/<id>`), which is an address and
 * never a label: the server sends the owner's current name on the `home` facet
 * (and, for a row inside a home, on `pathHome`), and every surface — the rows,
 * the trail, a row's location, a path, the tab title — renders through the one
 * shared label from `@alkera/ui`. Another member's home reads as THEIR name.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { keys } from "@/api/keys";
import { createQueryClient } from "@/api/queryClient";
import { specificFromCache } from "@/app/documentTitle";
import { Breadcrumbs } from "@/pages/workspace/files/Breadcrumbs";
import { FilesScreen } from "@/pages/workspace/files/FilesPage";
import { displayNameOf, displayPath, isHome, locationOf } from "@/lib/files/columns";

const ME = "6b1f0b1e-0000-4000-8000-00000000000a";
const OTHER = "6b1f0b1e-0000-4000-8000-00000000000b";
const MY_HOME = "nd_home_me";
const OTHER_HOME = "nd_home_other";

function item(over: Partial<Item> & { id: string; name: string }): Item {
  return {
    ino: 7,
    driveId: "dr_1",
    kind: "folder",
    nameDisplay: over.name,
    nameEncoding: "utf-8",
    pathBytes: "",
    etag: "et_1",
    ctag: "ct_1",
    stale: false,
    locked: false,
    held: false,
    shared: false,
    trashed: false,
    capabilities: { can_read: true, can_write: true, can_share: true, can_delete: true },
    ...over,
  } as Item;
}

function homeOf(nodeId: string, ownerId: string, ownerName: string): Item["home"] {
  return { node_id: nodeId, owner_id: ownerId, owner_name: ownerName };
}

/** Another member's home, shared with the viewer, as the server sends it. */
const THEIR_HOME = item({
  id: OTHER_HOME,
  name: OTHER,
  parentId: "nd_container",
  pathBytes: `/home/${OTHER}`,
  home: homeOf(OTHER_HOME, OTHER, "Olive Other"),
  pathHome: homeOf(OTHER_HOME, OTHER, "Olive Other"),
});

/** A file directly in that home. */
const THEIR_FILE = item({
  id: "nd_plan",
  name: "plan.md",
  kind: "file",
  parentId: OTHER_HOME,
  parentName: "Olive Other",
  pathBytes: `/home/${OTHER}/plan.md`,
  pathHome: homeOf(OTHER_HOME, OTHER, "Olive Other"),
});

const MY_HOME_ITEM = item({
  id: MY_HOME,
  name: ME,
  parentId: "nd_container",
  pathBytes: `/home/${ME}`,
  home: homeOf(MY_HOME, ME, "Dana Ruiz"),
  pathHome: homeOf(MY_HOME, ME, "Dana Ruiz"),
});

describe("the label every surface reads", () => {
  it("reads a home as its owner's name, never the id it is stored under", () => {
    expect(displayNameOf(MY_HOME_ITEM)).toBe("Dana Ruiz");
    expect(displayNameOf(THEIR_HOME)).toBe("Olive Other");
    expect(isHome(THEIR_HOME)).toBe(true);
    expect(isHome(THEIR_FILE)).toBe(false);
  });

  it("reads a home the server could not name as the fallback, not the id", () => {
    const unnamed = item({ ...MY_HOME_ITEM, home: homeOf(MY_HOME, ME, "") });
    expect(displayNameOf(unnamed)).toBe("Member");
    expect(displayNameOf(unnamed)).not.toContain(ME);
  });

  it("names the owner in a path, and locates a row in a home by the home", () => {
    expect(displayPath(THEIR_FILE)).toBe("/home/Olive Other/plan.md");
    expect(displayPath(THEIR_HOME)).toBe("/home/Olive Other");
    expect(locationOf(THEIR_FILE)).toEqual({ id: OTHER_HOME, name: "Olive Other", home: true });
  });

  it("names the tab after the owner of the home it shows", () => {
    const client = new QueryClient();
    client.setQueryData(keys.files.item(OTHER_HOME), THEIR_HOME);
    expect(specificFromCache(client, `/files/${OTHER_HOME}`)).toBe("Olive Other");
  });
});

describe("the trail", () => {
  it("draws the home mark inside a home's segment, and no mark on a plain folder", () => {
    render(
      <MemoryRouter>
        <Breadcrumbs
          segments={[
            { id: OTHER_HOME, name: "Olive Other", home: true },
            { id: "nd_reports", name: "Reports" },
          ]}
        />
      </MemoryRouter>,
    );
    const trail = screen.getByRole("navigation", { name: "Breadcrumb" });
    const home = within(trail).getByRole("button", { name: /Olive Other/ });
    expect(home.querySelector("svg[data-home='true']")).not.toBeNull();
    const current = trail.querySelector(".alk-files-crumbs__current");
    expect(current).toHaveTextContent("Reports");
    expect(current?.querySelector("svg")).toBeNull();
  });
});

function stubApi(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      const answer = (body: unknown) =>
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      if (/\/files\/drives\/?(\?|$)/.test(url)) {
        return answer({ id: "dr_1", orgId: "or_1", rootId: "nd_root", homeId: MY_HOME, quotaBytes: 0 });
      }
      if (url.includes("/leases")) return answer([]);
      if (url.includes(`/items/${OTHER_HOME}/children`)) {
        return answer({ value: [THEIR_FILE], nextMarker: null });
      }
      if (url.includes("/items/nd_container/children")) {
        return answer({ value: [MY_HOME_ITEM, THEIR_HOME], nextMarker: null });
      }
      if (url.includes("/children")) return answer({ value: [], nextMarker: null });
      if (url.includes(`/items/${OTHER_HOME}`)) return answer(THEIR_HOME);
      if (url.includes(`/items/${MY_HOME}`)) return answer(MY_HOME_ITEM);
      if (url.includes("/items/nd_container")) {
        return answer(item({ id: "nd_container", name: "home", parentId: "nd_root" }));
      }
      if (url.includes("/items/")) return answer(item({ id: "nd_root", name: "" }));
      return answer({ value: [], nextMarker: null });
    }),
  );
  vi.stubGlobal(
    "matchMedia",
    (query: string): MediaQueryList =>
      ({
        media: query,
        matches: false,
        onchange: null,
        addEventListener: () => undefined,
        removeEventListener: () => undefined,
        addListener: () => undefined,
        removeListener: () => undefined,
        dispatchEvent: () => false,
      }) as unknown as MediaQueryList,
  );
}

function mount(at: string) {
  return render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter initialEntries={[at]}>
        <Routes>
          <Route path="/files/:nodeId" element={<FilesScreen platform="other" />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => stubApi());
afterEach(() => {
  vi.unstubAllGlobals();
  document.body.innerHTML = "";
});

describe("the Files page", () => {
  it("opens another member's home under their name, with the home mark, and never its id", async () => {
    mount(`/files/${OTHER_HOME}`);
    const trail = await screen.findByRole("navigation", { name: "Breadcrumb" });
    await within(trail).findByText("Olive Other");
    const current = trail.querySelector(".alk-files-crumbs__current");
    expect(current?.querySelector("svg[data-home='true']")).not.toBeNull();
    await screen.findByText("plan.md");
    expect(document.body.textContent).not.toContain(OTHER);
  });

  it("lists every member's home in the container by its owner's name, each with the mark", async () => {
    mount("/files/nd_container");
    await screen.findByText("Dana Ruiz");
    await screen.findByText("Olive Other");
    for (const id of [MY_HOME, OTHER_HOME]) {
      const row = document.querySelector(`[data-row-id='${id}']`);
      expect(row?.querySelector("[data-home='true'] svg")).not.toBeNull();
    }
    await waitFor(() => expect(document.body.textContent).not.toContain(OTHER));
    expect(document.body.textContent).not.toContain(ME);
  });
});
