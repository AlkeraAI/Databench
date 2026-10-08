// @vitest-environment jsdom
//
// `?place=` — the one query parameter the Files page reads off a link.
//
// It exists because a file shared without its folder has to be able to send the
// reader somewhere ("Shared with me"), and that somewhere has to survive a
// reload. Anything a link can carry, a stranger can write, so the value is
// matched against the two feeds by NAME and is never used to look anything up:
// an unknown value leaves the page on the listing it would have shown anyway.
//
// Scope, because this file has been read for more than it proves: the stubs here
// answer instantly, so a feed's rows appear in the same tick they are asked for.
// That pins the routing and nothing about WAITING — a feed whose read is slow
// looks identical to one that is broken from in here. The distinction between
// loading, empty and answered is driven in `feedSlowRead.test.tsx`, which holds
// the answer back until it has looked.

import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { FilesScreen, feedPlaceFrom } from "@/pages/workspace/files/FilesPage";

const DRIVE = "dr_1";
const ROOT = "nd_root";
const HOME = "nd_home";

function item(over: Partial<Item> = {}): Item {
  return {
    id: HOME,
    ino: 1,
    driveId: DRIVE,
    kind: "folder",
    name: "dana",
    nameDisplay: "dana",
    nameEncoding: "utf-8",
    parentId: ROOT,
    pathBytes: "/home/dana",
    path: "/home/dana",
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

const IN_MY_HOME = item({ id: "nd_budget", kind: "file", name: "budget.xlsx", nameDisplay: "budget.xlsx", parentId: HOME });
const FROM_SAM = item({ id: "nd_sam", kind: "file", name: "from-sam.txt", nameDisplay: "from-sam.txt", parentId: "nd_elsewhere" });
const OPENED_LATELY = item({ id: "nd_recent", kind: "file", name: "opened-lately.md", nameDisplay: "opened-lately.md", parentId: HOME });

function stubNetwork(): void {
  const json = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      if (url.includes("/sharedWithMe")) return json({ value: [FROM_SAM], nextMarker: null });
      if (url.includes("/recent")) return json({ value: [OPENED_LATELY], nextMarker: null });
      if (url.includes("/starred")) return json({ value: [], nextMarker: null });
      if (url.includes("/leases")) return json([]);
      if (url.includes("/permissions")) return json({ value: [], nextMarker: null });
      if (url.includes("/search")) return json({ value: [], nextMarker: null });
      if (url.includes("/trash")) return json({ entries: [], nextMarker: null });
      if (/\/files\/drives\/?(\?|$)/.test(url)) {
        return json({ id: DRIVE, orgId: "or_1", rootId: ROOT, homeId: HOME, quotaBytes: 0 });
      }
      if (url.includes(`/items/${ROOT}/children`)) return json({ value: [], nextMarker: null });
      if (url.includes(`/items/${HOME}/children`)) return json({ value: [IN_MY_HOME], nextMarker: null });
      if (url.includes("/children")) return json({ value: [], nextMarker: null });
      if (url.includes(`/items/${ROOT}`)) return json(item({ id: ROOT, name: "/", nameDisplay: "/", parentId: null }));
      if (url.includes("/items/")) return json(item());
      return json({});
    }),
  );
}

function stubViewport(): void {
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

function Where() {
  const location = useLocation();
  return <output data-testid="where">{`${location.pathname}${location.search}`}</output>;
}

function mount(at: string) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter initialEntries={[at]}>
        <Where />
        <Routes>
          <Route path="/files" element={<FilesScreen platform="other" />} />
          <Route path="/files/:nodeId" element={<FilesScreen platform="other" />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const where = () => screen.getByTestId("where").textContent;

beforeEach(() => {
  stubViewport();
  stubNetwork();
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("the ?place= allowlist", () => {
  it.each([
    ["recent", "recent"],
    ["sharedWithMe", "sharedWithMe"],
  ])("answers %s with the feed of that name", (raw, expected) => {
    expect(feedPlaceFrom(raw)).toBe(expected);
  });

  it.each([
    ["a rail place that is not a feed", "home"],
    ["the trash", "trash"],
    ["a retired place", "starred"],
    ["a place that is off the page", "leases"],
    ["a traversal", "../admin"],
    ["a path", "/api/v1/files/drives"],
    ["another spelling of a real feed", "RECENT"],
    ["a real feed with something after it", "recent;drop"],
    ["nothing at all", ""],
    ["an absent parameter", null],
  ])("ignores %s", (_label, raw) => {
    expect(feedPlaceFrom(raw)).toBeUndefined();
  });

  it("answers with a value from its own list, never with the caller's string", () => {
    // The returned value is what the page routes on, so it must be the matched
    // constant rather than the text off the URL.
    const raw = ["shared", "With", "Me"].join("");
    expect(feedPlaceFrom(raw)).toBe("sharedWithMe");
  });
});

describe("a link that names a feed", () => {
  it("shows that feed instead of the folder listing", async () => {
    mount(`/files/${HOME}?place=sharedWithMe`);

    expect(await screen.findByText("from-sam.txt")).toBeInTheDocument();
    expect(screen.queryByText("budget.xlsx")).toBeNull();
  });

  it("is a destination of its own: `/files?place=` is not redirected into the home folder", async () => {
    mount("/files?place=sharedWithMe");

    expect(await screen.findByText("from-sam.txt")).toBeInTheDocument();
    expect(where()).toBe("/files?place=sharedWithMe");
  });

  it("shows the folder listing when the parameter names something else", async () => {
    // Not a place at all: an unknown value leaves the page on the listing the
    // route already names rather than becoming part of a request.
    mount(`/files/${HOME}?place=../etc/passwd`);

    expect(await screen.findByText("budget.xlsx")).toBeInTheDocument();
    expect(screen.queryByText("from-sam.txt")).toBeNull();
  });
});

describe("the rail", () => {
  it("puts the feed it opened in the URL, so the view can be linked to", async () => {
    mount(`/files/${HOME}`);

    await screen.findByText("budget.xlsx");
    const recent = await screen.findByRole("button", { name: /Recent/ });
    await waitFor(() => expect(recent).toBeEnabled());
    await userEvent.click(recent);

    await waitFor(() => expect(where()).toBe(`/files/${HOME}?place=recent`));
    expect(await screen.findByText("opened-lately.md")).toBeInTheDocument();
  });

  it("drops the feed again when a folder is opened", async () => {
    mount(`/files/${HOME}?place=recent`);

    await screen.findByText("opened-lately.md");
    const home = await screen.findByRole("button", { name: /Home/ });
    await waitFor(() => expect(home).toBeEnabled());
    await userEvent.click(home);

    await waitFor(() => expect(where()).toBe(`/files/${HOME}`));
    expect(await screen.findByText("budget.xlsx")).toBeInTheDocument();
  });
});
