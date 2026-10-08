// Taking a box's lease back asks why first.
//
// Forcing a box off a chat's or a workspace's folder cuts off a turn that may
// be in flight; the server keeps a reason on record and refuses one without.
// So the page asks for it, holds the request until there is one, and sends it.
// A lease a person holds (a mount) is taken back as before, with no question
// and no body, which is also all an older server ever sees.

import { QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { FilesActions, type FilesActionsApi } from "@/pages/workspace/files/FilesActions";
import { forceReleaseNeedsReason } from "@/pages/workspace/files/ForceReleaseDialog";

const DRIVE = "dr_1";

function held(purpose: string): Item {
  return {
    id: `nd_${purpose}`,
    name: "Revenue model",
    nameDisplay: "Revenue model",
    kind: "folder",
    etag: "7",
    parentId: "nd_root",
    capabilities: { can_read: true, can_write: true, can_lease: true },
    lease: { holder: "localdev-1", machine: "localdev-1", purpose, mine: false },
  } as unknown as Item;
}

interface Sent {
  path: string;
  body: string;
}

function stubForce(): Sent[] {
  const sent: Sent[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request ? input : new Request(String(input), init);
      if (request.method === "GET") {
        return new Response(JSON.stringify({ entries: [] }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      sent.push({ path: new URL(request.url, "http://x").pathname, body: await request.clone().text() });
      return new Response(JSON.stringify({ node_id: "n", epoch: 2 }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }),
  );
  return sent;
}

let api: FilesActionsApi | null = null;

function renderActions(selection: readonly Item[]) {
  render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter>
        <FilesActions canWriteHere canForceRelease driveId={DRIVE} currentFolderId="nd_root" selection={selection}>
          {(actions) => {
            api = actions;
            return null;
          }}
        </FilesActions>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  api = null;
  vi.unstubAllGlobals();
});

describe("taking a lease back", () => {
  it.each([
    ["chat", true],
    ["workspace", true],
    ["mount", false],
    ["share", false],
    [null, false],
  ] as const)("asks for a reason for a %s lease: %s", (purpose, asks) => {
    expect(forceReleaseNeedsReason(purpose)).toBe(asks);
  });

  it("asks why for a box's lease, holds the request until a reason is given, and sends it", async () => {
    const sent = stubForce();
    renderActions([held("chat")]);
    api?.run("force-release");

    const dialog = await screen.findByRole("dialog");
    expect(sent).toEqual([]);
    const key = within(dialog).getByRole("button", { name: "Take back" });
    expect(key).toBeDisabled();
    // Spaces alone are not a reason.
    fireEvent.change(within(dialog).getByRole("textbox", { name: "Reason" }), { target: { value: "   " } });
    expect(key).toBeDisabled();
    fireEvent.change(within(dialog).getByRole("textbox", { name: "Reason" }), {
      target: { value: "  The agent is writing to the wrong folder  " },
    });
    fireEvent.click(key);

    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]?.path).toBe(`/api/v1/files/drives/${DRIVE}/items/nd_chat/lease/force-release`);
    expect(JSON.parse(sent[0]?.body ?? "{}")).toEqual({ reason: "The agent is writing to the wrong folder" });
  });

  it("sends nothing when the reason is abandoned", async () => {
    const sent = stubForce();
    renderActions([held("workspace")]);
    api?.run("force-release");
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(sent).toEqual([]);
  });

  it("takes a person's lease back at once, with no question and no body", async () => {
    const sent = stubForce();
    renderActions([held("mount")]);
    api?.run("force-release");
    await waitFor(() => expect(sent).toHaveLength(1));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(sent[0]?.body).toBe("");
  });
});
