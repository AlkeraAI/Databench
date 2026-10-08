// A frame that names no folder names none of the reader's.
//
// The transcript drops its memo of "where is this path?" when the chat's folder
// changes, and it learns of a change from two frames: the node frame that names
// the folder a node sits in, and the lease frame that names the leased folder
// the machine is writing into. Both are matched against the two folders a
// reference can be answered from — the chat's own node and its working
// directory — and BOTH of those are `undefined` until their reads settle.
//
// So a frame carrying no id at all must not be read as naming them. The node
// branch has always said so; the lease branch, added later, compared whatever
// it found against the same list, and a list holding `undefined` matches a
// frame holding `undefined`. The cost is a transcript that re-walks the drive
// for every unrelated lease in the org during the second before its own ids
// arrive — the exact stampede the memo exists to prevent.

import { QueryClientProvider } from "@tanstack/react-query";
import type { QueryClient } from "@tanstack/react-query";
import { act, cleanup, render, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RealtimeEventFrame } from "@/api/events/eventMap";
import { publishFrame, resetFrameBus } from "@/api/events/frameBus";
import { keys } from "@/api/keys";
import { createQueryClient } from "@/api/queryClient";
import { MACHINE_REFRESH_MS } from "@/lib/limits";
import {
  CHAT_FOLDER_COALESCE_MS,
  useChatFolderChanges,
} from "@/pages/workspace/chat/useChatFolderChanges";

const CHAT_ID = "cht_1";
/** The chat's own folder, once the chat read has answered. */
const CHAT_NODE = "nd_chat";
const SOMEONE_ELSE = "nd_elsewhere";

function Probe(): ReactElement {
  const changes = useChatFolderChanges(CHAT_ID);
  return <span data-testid="changes">{changes}</span>;
}

/** A frame as it reaches the bus, carrying exactly the ids named — a writer
 *  that names none is a frame with none, which is the case under test. */
function frame(
  type: "file_lease.changed" | "file_node.changed",
  ids: Partial<Record<"entity_id" | "lease_node_id" | "parent_id" | "reason", string>> = {},
): RealtimeEventFrame {
  return {
    type,
    entity: type === "file_lease.changed" ? "file_lease" : "file_node",
    version: 1,
    org_id: "org_1",
    drive_id: "drv_1",
    ...ids,
  } as RealtimeEventFrame;
}

let client: QueryClient;

/** Mount the hook; seed the chat read first when the case wants the chat's own
 *  folder already known. Everything else is left in flight, which is the state
 *  a transcript opens in. */
function mount(seed?: { chatNodeId: string }): void {
  client = createQueryClient({ retry: false });
  if (seed !== undefined) {
    client.setQueryData(keys.chats.one(CHAT_ID), {
      id: CHAT_ID,
      files_node_id: seed.chatNodeId,
    } as unknown);
  }
  render(
    <QueryClientProvider client={client}>
      <Probe />
    </QueryClientProvider>,
  );
}

/** Wait past the hook's own coalesce window, so a bump it queued has landed. */
async function settle(): Promise<void> {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, CHAT_FOLDER_COALESCE_MS * 2));
  });
}

function changes(): string {
  return screen.getByTestId("changes").textContent ?? "";
}

beforeEach(() => {
  // Every read the hook takes stays in flight, so the folder ids a case has not
  // seeded stay unresolved for the whole case.
  vi.stubGlobal(
    "fetch",
    vi.fn(() => new Promise(() => {})),
  );
});

afterEach(() => {
  cleanup();
  resetFrameBus();
  vi.unstubAllGlobals();
});

describe("a frame that names no folder, while the chat's own are unresolved", () => {
  it.each([
    ["a lease frame naming neither the leased node nor an entity", frame("file_lease.changed")],
    ["a node frame naming no parent", frame("file_node.changed")],
  ])("%s leaves the transcript's memo alone", async (_label, sent) => {
    mount();
    expect(changes()).toBe("0");

    publishFrame(sent);
    await settle();

    expect(changes()).toBe("0");
  });
});

describe("a lease frame naming a folder", () => {
  it.each([
    ["named on the payload", { lease_node_id: CHAT_NODE }],
    ["named only as the entity, by an older server", { entity_id: CHAT_NODE }],
  ])("%s re-asks the chat's own folder", async (_label, ids) => {
    mount({ chatNodeId: CHAT_NODE });

    publishFrame(frame("file_lease.changed", ids));
    await settle();

    expect(changes()).toBe("1");
  });

  it("somewhere else in the drive costs the transcript nothing", async () => {
    mount({ chatNodeId: CHAT_NODE });

    publishFrame(frame("file_lease.changed", { lease_node_id: SOMEONE_ELSE }));
    await settle();

    expect(changes()).toBe("0");
  });
});

describe("a machine saving into the chat's folder", () => {
  const save = () =>
    frame("file_node.changed", { parent_id: CHAT_NODE, entity_id: "nd_ticks", reason: "live_saved" });
  const wait = (ms: number) => act(() => vi.advanceTimersByTimeAsync(ms));

  afterEach(() => {
    vi.useRealTimers();
  });

  it("drops the memo once per window however many saves, the first at once", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mount({ chatNodeId: CHAT_NODE });
    act(() => publishFrame(save()));
    await wait(CHAT_FOLDER_COALESCE_MS * 2);
    expect(changes()).toBe("1");
    for (let second = 0; second < 60; second += 1) {
      act(() => publishFrame(save()));
      await wait(1_000);
    }
    const passes = Number(changes());
    expect(passes).toBeGreaterThan(1);
    expect(passes).toBeLessThanOrEqual(1 + Math.ceil(60_000 / MACHINE_REFRESH_MS) + 1);
  });

  it("still answers a person's change within the coalescing window, mid-burst", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mount({ chatNodeId: CHAT_NODE });
    act(() => publishFrame(save()));
    await wait(CHAT_FOLDER_COALESCE_MS * 2);
    act(() => publishFrame(save()));
    await wait(CHAT_FOLDER_COALESCE_MS * 2);
    expect(changes()).toBe("1");
    // A rename raises a node frame with no reason.
    act(() => publishFrame(frame("file_node.changed", { parent_id: CHAT_NODE, entity_id: "nd_x" })));
    await wait(CHAT_FOLDER_COALESCE_MS * 2);
    expect(changes()).toBe("2");
  });
});
