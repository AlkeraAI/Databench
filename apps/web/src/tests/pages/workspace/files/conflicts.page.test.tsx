// A file both sides changed, as the Files route really renders it: `FilesScreen`
// over a stubbed network, nothing hand-mounted. The drive already kept both
// versions — the original under its name, the other as a conflicted copy beside
// it — so the page lists two files and asks the reader nothing, even while the
// drive still holds an open conflict record for the pair.

import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { resetRealtimeStatus, useRealtimeStatus } from "@/api/events/status";
import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { FilesScreen } from "@/pages/workspace/files/FilesPage";

const COPY_NAME = "report (conflicted copy from demo-box, 2026-09-24 03.25 UTC).md";

function item(over: Record<string, unknown>): Item {
  return {
    ino: 7,
    driveId: "dr_1",
    kind: "file",
    nameEncoding: "utf-8",
    etag: "et_1",
    ctag: "ct_1",
    stale: false,
    locked: false,
    held: false,
    shared: false,
    trashed: false,
    capabilities: { can_read: true, can_write: true, can_share: true, can_delete: true },
    ...over,
  } as unknown as Item;
}

const FOLDER = item({ id: "nd_home", kind: "folder", name: "home", nameDisplay: "home" });
const REPORT = item({
  id: "nd_report",
  name: "report.md",
  nameDisplay: "report.md",
  parentId: "nd_home",
  lease: { machine: "box-7f", machine_name: "demo-box", node_id: "nd_home" },
});
const COPY = item({
  id: "nd_copy",
  name: COPY_NAME,
  nameDisplay: COPY_NAME,
  parentId: "nd_home",
  conflictOf: "nd_report",
});

const CONFLICT = {
  id: "cf_1",
  nodeId: "nd_report",
  baseVersionId: null,
  theirsVersionId: "v_theirs",
  mineVersionId: "v_mine",
  state: "auto",
  copyNodeId: "nd_copy",
  arrivedFrom: "web",
  who: "Dana",
};

function stubApi(conflicts: unknown[]) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      const answer = (body: unknown) =>
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      if (url.includes("/conflicts")) return answer({ value: conflicts });
      if (url.includes("/leases")) return answer([]);
      if (url.includes("/permissions")) return answer({ value: [], nextMarker: null });
      if (url.includes("/trash")) return answer({ entries: [], nextMarker: null });
      if (/\/files\/drives\/?(\?|$)/.test(url)) {
        return answer({ id: "dr_1", orgId: "or_1", rootId: "nd_root", quotaBytes: 0 });
      }
      if (url.includes("/children")) return answer({ value: [REPORT, COPY], nextMarker: null });
      if (url.includes("/items/nd_report")) return answer(REPORT);
      if (url.includes("/items/nd_copy")) return answer(COPY);
      if (url.includes("/items/")) return answer(FOLDER);
      return answer({});
    }),
  );
}

function mount() {
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
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter initialEntries={["/files/nd_home"]}>
        <Routes>
          <Route path="/files/:nodeId" element={<FilesScreen />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  resetRealtimeStatus();
  useRealtimeStatus.getState().setSse("connected");
});
afterEach(() => {
  vi.unstubAllGlobals();
  resetRealtimeStatus();
});

describe("a settled conflict on the Files page", () => {
  it("lists the original and the conflicted copy, and offers no choice between them", async () => {
    stubApi([CONFLICT]);
    mount();

    const grid = await screen.findByRole("treegrid");
    const copyRow = (await within(grid).findByText(COPY_NAME)).closest('[role="row"]');
    const reportRow = within(grid).getByText("report.md").closest('[role="row"]');
    expect(copyRow).not.toBeNull();
    expect(reportRow).not.toBeNull();
    // Long enough for any read the page starts beside the listing to land.
    await new Promise((resolve) => setTimeout(resolve, 50));

    expect(screen.queryByRole("region", { name: "Conflicts" })).toBeNull();
    for (const label of ["Keep this", "Keep the other", "Keep both"]) {
      expect(screen.queryByRole("button", { name: label })).toBeNull();
    }
    expect(document.body).not.toHaveTextContent(/Kept .*version of report\.md/);
  });

  it("lists the conflicted copy as an ordinary row: its name says what it is, no chip does", async () => {
    stubApi([]);
    mount();

    const grid = await screen.findByRole("treegrid");
    const copyRow = (await within(grid).findByText(COPY_NAME)).closest('[role="row"]');
    expect(copyRow).not.toBeNull();
    // The name carries the whole fact; a chip beside it only repeated it and,
    // in a narrow name cell, cut both down to "confl…".
    expect((copyRow as HTMLElement).querySelector(".alk-files-live-chip")).toBeNull();
    expect(within(copyRow as HTMLElement).queryByText("conflicted copy", { exact: true })).toBeNull();
  });
});
