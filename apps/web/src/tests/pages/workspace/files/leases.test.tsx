import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";
import type { Item, LeaseRow } from "@/api/files";
import { LeaseBadge, LeaseFacetPane, LeaseReadOnlyState } from "@/pages/workspace/files/LeaseBadge";
import { MyLeases } from "@/pages/workspace/files/MyLeases";
import { RightPane } from "@/pages/workspace/files/RightPane";
import { canLease, leasedRefusal, relativeTime } from "@/pages/workspace/files/useLeaseFacet";
import { ApiError } from "@/api/errors";

// The lease surface driven through the real hooks: `fetch` is the only thing
// stubbed, so a button that "calls its hook" is asserted by the request the
// server would actually have received, not by a spy on the hook.

const NOW = new Date("2026-09-09T10:15:00.000Z");
const SINCE = "2026-09-09T10:12:00.000Z";
const SYNCED = "2026-09-09T10:14:48.000Z"; // 12 s before NOW
const EXPIRES = "2026-09-09T10:42:00.000Z"; // 30 minutes after `since`

type Caps = Record<string, boolean>;

/** What the committed OpenAPI document says a node's capability map carries.
 *  Read from the file rather than from the generated TS type, because the
 *  generated type ends in an index signature and would accept any key. */
function wireCapabilityProperties(): Record<string, unknown> {
  const path = resolve(process.cwd(), "../../packages/shared-openapi/openapi.json");
  const document = JSON.parse(readFileSync(path, "utf-8")) as {
    components: { schemas: Record<string, { properties?: Record<string, unknown> }> };
  };
  return document.components.schemas.Capabilities?.properties ?? {};
}

function leasedItem(overrides: Partial<Item> = {}, caps: Caps = {}): Item {
  return {
    id: "node-1",
    ino: 1,
    driveId: "drive-1",
    kind: "folder",
    name: "models",
    nameDisplay: "models",
    nameEncoding: "utf-8",
    pathBytes: "/home/robin/models",
    path: "/home/robin/models",
    etag: "etag-1",
    ctag: "ctag-1",
    stale: false,
    locked: false,
    held: false,
    lease: {
      holder: "Robin",
      machine: "MacBook Pro",
      purpose: "mount",
      since: SINCE,
      expires_at: EXPIRES,
      last_sync_at: SYNCED,
      mine: false,
    },
    capabilities: { can_read: true, ...caps } as Item["capabilities"],
    ...overrides,
  } as Item;
}

function unleasedItem(caps: Caps = {}): Item {
  const item = leasedItem({}, caps);
  return { ...item, lease: null };
}

/** Every request the component made, in order. The generated client hands
 *  `fetch` a `Request`, so the method and the body are read off that. */
interface Seen {
  url: string;
  method: string;
  body: string | null;
}
let calls: Seen[] = [];

function seen(input: RequestInfo | URL, init?: RequestInit): Seen {
  if (input instanceof Request) {
    return {
      url: input.url,
      method: input.method,
      body:
        typeof init?.body === "string"
          ? init.body
          : ((input as Request & { _body?: string })._body ?? null),
    };
  }
  return {
    url: typeof input === "string" ? input : input.toString(),
    method: init?.method ?? "GET",
    body: typeof init?.body === "string" ? init.body : null,
  };
}

function stubFetch(leases: LeaseRow[] = [], write?: { status: number; body: unknown }) {
  calls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const call = seen(input, init);
      if (input instanceof Request && call.body === null && call.method !== "GET") {
        call.body = await input.clone().text();
      }
      calls.push(call);
      if (call.url.includes("/permissions")) {
        return new Response(JSON.stringify({ value: [], nextMarker: null }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      if (call.method === "GET") {
        return new Response(JSON.stringify(leases), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      const answer = write ?? { status: 200, body: {} };
      return new Response(JSON.stringify(answer.body), {
        status: answer.status,
        headers: { "content-type": "application/json" },
      });
    }),
  );
}

function renderWithClient(ui: React.ReactElement) {
  const client = createQueryClient();
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.setSystemTime(NOW);
  stubFetch();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("the lease badge", () => {
  it("names the holder and nothing else: no machine, no start, no sync age", () => {
    renderWithClient(<LeaseBadge item={leasedItem()} />);
    const text = screen.getByText(/In use by/).textContent ?? "";
    expect(text).toBe("In use by Robin");
    expect(text).not.toMatch(/synced|since|MacBook/);
  });

  it("says the holder is you when the facet says the lease is mine", () => {
    const mine = leasedItem({
      lease: { ...leasedItem().lease, mine: true } as Item["lease"],
    });
    renderWithClient(<LeaseBadge item={mine} />);
    expect(screen.getByText(/In use by you/)).toBeInTheDocument();
    expect(screen.queryByText(/In use by Robin/)).not.toBeInTheDocument();
  });

  it("marks a holder whose sync has fallen behind, and only then", () => {
    const { unmount } = renderWithClient(<LeaseBadge item={leasedItem({ stale: true })} />);
    expect(screen.getByText("behind")).toBeInTheDocument();
    unmount();
    renderWithClient(<LeaseBadge item={leasedItem({ stale: false })} />);
    expect(screen.queryByText("behind")).not.toBeInTheDocument();
  });

  it("renders nothing for an item that is not leased", () => {
    const { container } = renderWithClient(<LeaseBadge item={unleasedItem()} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows items inside a leased folder as read-only with their sync age", () => {
    renderWithClient(<LeaseReadOnlyState item={leasedItem()} />);
    expect(screen.getByText("Read-only, synced 12 s ago")).toBeInTheDocument();
  });

  it("keeps counting between two frames without a request", async () => {
    // The sync age lives in the pane now; it still ticks on the clock alone.
    renderWithClient(<LeaseFacetPane item={leasedItem()} />);
    expect(screen.getByText(/12 s ago/)).toBeInTheDocument();
    const before = calls.length;
    await vi.advanceTimersByTimeAsync(5000);
    await waitFor(() => expect(screen.getByText(/17 s ago/)).toBeInTheDocument());
    expect(calls.length).toBe(before);
  });

  it("shows the whole facet in the right pane", () => {
    renderWithClient(<LeaseFacetPane item={leasedItem()} />);
    const pane = screen.getByLabelText("Lease");
    expect(pane).toHaveTextContent("Robin");
    expect(pane).toHaveTextContent("MacBook Pro");
    expect(pane).toHaveTextContent("mount");
    expect(pane).toHaveTextContent("12 s ago");
  });
});

describe("relative time", () => {
  it.each([
    ["12 s ago", "2026-09-09T10:14:48.000Z"],
    ["3 min ago", "2026-09-09T10:12:00.000Z"],
    ["2 h ago", "2026-09-09T08:15:00.000Z"],
    ["2 d ago", "2026-09-07T10:15:00.000Z"],
    ["just now", "2026-09-09T10:15:30.000Z"],
  ])("reads %s", (expected, iso) => {
    expect(relativeTime(iso, NOW.getTime())).toBe(expected);
  });

  it("is absent when the facet never synced", () => {
    expect(relativeTime(null, NOW.getTime())).toBeNull();
    expect(relativeTime("not-a-date", NOW.getTime())).toBeNull();
  });
});

describe("capability gating", () => {
  it.each([
    ["lease", "can_lease"],
    ["lease_request", "can_lease_request"],
    ["lease_force", "can_lease_force"],
  ] as const)("allows %s only when the item grants it", (action, key) => {
    expect(canLease(leasedItem({}, { [key]: true }), action)).toBe(true);
    expect(canLease(leasedItem({}, { [key]: false }), action)).toBe(false);
    expect(canLease(leasedItem(), action)).toBe(false);
  });

  it("gates on the three keys the wire declares and on nothing else", () => {
    // The gate was once spelled in camelCase, which the server has never sent:
    // every lease control was therefore permanently refused in production while
    // the suite stayed green on fixtures that minted the key. The contract is
    // read off the committed schema so a rename on either side is a red test,
    // not a dead button.
    const declared = Object.keys(wireCapabilityProperties());
    expect(declared).toEqual(
      expect.arrayContaining(["can_lease", "can_lease_request", "can_lease_force"]),
    );

    // …and a spelling the wire does NOT declare grants nothing.
    const invented = { canLease: true, canLeaseRequest: true, canLeaseForce: true } as Caps;
    expect(canLease(leasedItem({}, invented), "lease")).toBe(false);
    expect(canLease(leasedItem({}, invented), "lease_request")).toBe(false);
    expect(canLease(leasedItem({}, invented), "lease_force")).toBe(false);
  });

});

describe("a refused write", () => {
  it("leaves any other failure to its own handling", () => {
    expect(leasedRefusal(new ApiError(500, { error: { code: "internal", message: "boom" } }))).toBe(
      null,
    );
    expect(leasedRefusal(new Error("boom"))).toBe(null);
    expect(
      leasedRefusal(
        new ApiError(409, { error: { code: "files.leased", message: "held by Robin" } }),
      ),
    ).toBe("held by Robin");
  });
});

describe("my leases", () => {
  it("lists the leases the server returned for me", async () => {
    stubFetch([
      {
        nodeId: "node-1",
        epoch: 7,
        machine: "MacBook Pro",
        purpose: "mount",
        since: SINCE,
        expiresAt: EXPIRES,
        lastSyncAt: SYNCED,
      },
      {
        nodeId: "node-2",
        epoch: 2,
        machine: "gpu-box-04",
        purpose: "box",
        since: SINCE,
        expiresAt: EXPIRES,
        lastSyncAt: null,
      },
    ]);
    renderWithClient(<MyLeases driveId="drive-1" />);
    const list = await screen.findByRole("list", { name: "My leases" });
    expect(list).toHaveTextContent("MacBook Pro");
    expect(list).toHaveTextContent("synced 12 s ago");
    expect(list).toHaveTextContent("gpu-box-04");
    expect(list).toHaveTextContent("not synced yet");
    expect(calls[0].url).toContain("mine=true");
  });

  it("says what to do when I hold nothing", async () => {
    stubFetch([]);
    renderWithClient(<MyLeases driveId="drive-1" />);
    expect(await screen.findByText(/You have no folders out for local use/)).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "My leases" })).not.toBeInTheDocument();
  });

  it("opens the lease the person clicked", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const onOpen = vi.fn();
    stubFetch([
      {
        nodeId: "node-9",
        epoch: 1,
        machine: "MacBook Pro",
        purpose: "mount",
        since: SINCE,
        expiresAt: EXPIRES,
        lastSyncAt: SYNCED,
      },
    ]);
    renderWithClient(<MyLeases driveId="drive-1" onOpen={onOpen} />);
    await user.click(await screen.findByRole("button", { name: /MacBook Pro/ }));
    expect(onOpen).toHaveBeenCalledWith("node-9");
  });
});

describe("the details pane", () => {
  it("states a lease and offers nothing to do about it, whatever the caller may do", async () => {
    // Every lease rung at once: the pane still draws the facet and not one
    // control over it. Ask for it back, take back and the take-back confirmation
    // belong to the row menu.
    const every = leasedItem(
      {},
      { can_lease: true, can_lease_request: true, can_lease_force: true },
    );
    renderWithClient(<RightPane driveId="drive-1" item={every} />);

    const heading = await screen.findByRole("heading", { name: "Lease" });
    expect(heading.closest("section")).toHaveTextContent("Robin");
    for (const name of ["Ask for it back", "Take back", "Keep the lease", "Release"]) {
      expect(screen.queryByRole("button", { name })).toBeNull();
    }
    expect(screen.queryByRole("button", { name: "Lease for local use…" })).toBeNull();
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(screen.queryByText(/grace period/)).toBeNull();
    // …and the pane is still the pane: the metadata rows are untouched.
    expect(screen.getByText("Kind")).toBeInTheDocument();
    expect(screen.getByText("Size")).toBeInTheDocument();
    expect(screen.getByText("Modified")).toBeInTheDocument();
  });

  it("says nothing about leases on an item that has none", async () => {
    renderWithClient(<RightPane driveId="drive-1" item={unleasedItem({ can_lease: true })} />);
    await screen.findByText("Who can access");
    expect(screen.queryByRole("heading", { name: "Lease" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Lease for local use…" })).toBeNull();
  });

  it("names a chat's hold on a folder in the purpose the server sent", async () => {
    // A chat's box holds its working folder under `purpose: "chat"`, which is
    // not a purpose a person can pick in the form — rendering only the three
    // that are would have left the pane blank exactly where it matters most.
    const held = leasedItem({
      lease: { ...leasedItem().lease, purpose: "chat" } as Item["lease"],
    });
    renderWithClient(<RightPane driveId="drive-1" item={held} />);

    const heading = await screen.findByRole("heading", { name: "Lease" });
    const facet = heading.closest("section") as HTMLElement;
    expect(facet).toHaveTextContent("chat");
    expect(facet).toHaveTextContent("Robin");
    expect(facet).toHaveTextContent("MacBook Pro");
  });
});

// The three lease rungs the server sends on every node. A gate spelled against
// a key the wire does not carry is a control that is permanently refused, which
// is the shape these pin — one rung apart, on one folder.
describe("the lease rungs the wire carries", () => {
  const HELD_SINCE = "2026-09-09T10:12:00.000Z";

  function folder(caps: Caps, lease: Item["lease"] = null): Item {
    return {
      id: "nd_models",
      ino: 2,
      driveId: "dr_1",
      kind: "folder",
      name: "models",
      nameDisplay: "models",
      nameEncoding: "utf-8",
      parentId: "nd_home",
      pathBytes: "/home/dana/models",
      path: "/home/dana/models",
      etag: "et_1",
      ctag: "ct_1",
      stale: false,
      locked: false,
      held: false,
      shared: false,
      trashed: false,
      lease,
      capabilities: { can_read: true, can_write: true, ...caps },
    } as Item;
  }

  const HELD_BY_SOMEONE_ELSE = {
    holder: "Robin",
    machine: "MacBook Pro",
    purpose: "mount",
    since: HELD_SINCE,
    expires_at: EXPIRES,
    last_sync_at: SYNCED,
    mine: false,
  } as Item["lease"];

  beforeEach(() => {
    vi.useRealTimers();
    calls = [];
    stubFetch([]);
  });

  it("reads a held folder the same way at every rung", async () => {
    // A manager and a writer are shown the same thing, because the pane no
    // longer draws anything a rung could gate: the facet, and nothing else.
    const rungs: Caps[] = [
      { can_lease_force: true, can_lease_request: true },
      { can_lease: true, can_lease_request: true },
    ];
    for (const caps of rungs) {
      const { unmount } = renderWithClient(
        <RightPane driveId="dr_1" item={folder(caps, HELD_BY_SOMEONE_ELSE)} />,
      );
      const heading = await screen.findByRole("heading", { name: "Lease" });
      expect(heading.closest("section")).toHaveTextContent("MacBook Pro");
      expect(screen.queryByRole("button", { name: "Take back" })).toBeNull();
      expect(screen.queryByRole("button", { name: "Ask for it back" })).toBeNull();
      unmount();
    }
  });

  it("draws no lease section on a folder nobody is holding", async () => {
    const cases: Caps[] = [{ can_lease: true }, { can_lease: false, can_lease_request: true }];
    for (const caps of cases) {
      const { unmount } = renderWithClient(<RightPane driveId="dr_1" item={folder(caps)} />);
      await screen.findByText("Who can access");
      expect(screen.queryByRole("button", { name: "Lease for local use…" })).toBeNull();
      expect(screen.queryByRole("heading", { name: "Lease" })).toBeNull();
      unmount();
    }
  });
});
