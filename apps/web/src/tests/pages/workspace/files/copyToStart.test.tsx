/**
 * Copy to… opens where the rows are, as Move to… does.
 *
 * It opens at the folder being listed now, and at the reader's home only where there is
 * no folder being listed (Recent, Shared with me).
 */

import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { CopyToDialog } from "@/pages/workspace/files/CopyToDialog";

function folder(id: string, name: string): Item {
  return {
    id,
    ino: 1,
    driveId: "dr_1",
    kind: "folder",
    name,
    nameDisplay: name,
    nameEncoding: "utf-8",
    pathBytes: `/home/dana/${name}`,
    path: `/home/dana/${name}`,
    etag: "et",
    ctag: "ct",
    stale: false,
    locked: false,
    held: false,
    shared: false,
    trashed: false,
    capabilities: { can_read: true, can_write: true, can_share: true, can_delete: true },
  } as Item;
}

let listed: string[] = [];

beforeEach(() => {
  listed = [];
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
        return answer({ id: "dr_1", orgId: "or_1", rootId: "nd_root", homeId: "nd_home", quotaBytes: 0 });
      }
      const children = /\/items\/(nd_[a-z]+)\/children/.exec(url)?.[1];
      if (children) {
        listed.push(children);
        return answer({ value: [folder(`nd_in_${children}`, `in-${children}`)], nextMarker: null });
      }
      const node = /\/items\/(nd_[a-z]+)$/.exec(url)?.[1];
      if (node) return answer(folder(node, node));
      return answer({ value: [], nextMarker: null });
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function mount(props: Partial<React.ComponentProps<typeof CopyToDialog>> = {}): void {
  render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter>
        <CopyToDialog
          open
          driveId="dr_1"
          count={1}
          initialRect={{ width: 800, height: 400 }}
          onCancel={vi.fn()}
          onConfirm={vi.fn()}
          {...props}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Copy to…", () => {
  it("opens in the folder being listed", async () => {
    mount({ startFolderId: "nd_work", startLabel: "work" });
    expect(await screen.findByText("in-nd_work")).toBeInTheDocument();
    expect(listed).toContain("nd_work");
    expect(listed).not.toContain("nd_home");
    expect(listed).not.toContain("nd_root");
  });

  it("opens at the reader's home where no folder is being listed", async () => {
    mount();
    expect(await screen.findByText("in-nd_home")).toBeInTheDocument();
    await waitFor(() => expect(listed).toContain("nd_home"));
  });
});
