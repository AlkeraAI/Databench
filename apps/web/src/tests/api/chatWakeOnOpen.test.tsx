// Opening a chat wakes it: the hook asks the server once per open and once per
// return to the window, only for a reader the server said may send, and hands
// back the server's sentence when the machine may not be started.

import { QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useWakeOnOpen, useWakeWorkspaceOnOpen } from "@/api/chats";
import { createQueryClient } from "@/api/queryClient";

interface ProbeProps {
  chatId: string | undefined;
  canSend: boolean | undefined;
  state?: string;
}

function Probe({ chatId, canSend, state = "asleep" }: ProbeProps) {
  const opened = useWakeOnOpen(chatId, canSend, state);
  return (
    <button type="button" onClick={opened.wake} data-refusal={opened.refusal ?? ""}>
      run
    </button>
  );
}

/** Stub the wake route; returns the paths asked for, in order. */
function scriptWake(respond: () => Response): string[] {
  const asked: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      asked.push(`${init?.method ?? "GET"} ${new URL(String(input), "http://x").pathname}`);
      return respond();
    }),
  );
  return asked;
}

const waking = () =>
  new Response(JSON.stringify({ outcome: "waking" }), { status: 200, headers: { "content-type": "application/json" } });

function mount(chatId: string | undefined, canSend: boolean | undefined, state?: string) {
  const client = createQueryClient({ retry: false });
  const tree = (id: string | undefined, may: boolean | undefined, said?: string) => (
    <QueryClientProvider client={client}>
      <Probe chatId={id} canSend={may} {...(said ? { state: said } : {})} />
    </QueryClientProvider>
  );
  const view = render(tree(chatId, canSend, state));
  return {
    redraw: (id: string | undefined, may: boolean | undefined, said?: string) => view.rerender(tree(id, may, said)),
  };
}

function WorkspaceProbe({ workspaceId, awake }: { workspaceId: string | undefined; awake: boolean }) {
  useWakeWorkspaceOnOpen(workspaceId, awake);
  return null;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("useWakeOnOpen", () => {
  it("asks once on open and not again on a redraw", async () => {
    const asked = scriptWake(waking);
    const view = mount("c1", true);
    await waitFor(() => expect(asked).toEqual(["POST /api/v1/chats/c1/wake"]));
    view.redraw("c1", true);
    view.redraw("c1", true);
    await act(async () => {});
    expect(asked).toEqual(["POST /api/v1/chats/c1/wake"]);
  });

  it("asks only once the server has said the reader may send", async () => {
    const asked = scriptWake(waking);
    const view = mount("c1", undefined);
    await act(async () => {});
    expect(asked, "the chat row has not answered").toEqual([]);
    view.redraw("c1", true);
    await waitFor(() => expect(asked).toEqual(["POST /api/v1/chats/c1/wake"]));
  });

  it.each([
    ["a reader who may not send", "c1", false],
    ["a chat that does not exist yet", undefined, true],
  ])("asks for nothing for %s, on open, on focus and on a run", async (_who, chatId, canSend) => {
    const asked = scriptWake(waking);
    mount(chatId, canSend);
    await act(async () => {
      window.dispatchEvent(new Event("focus"));
      screen.getByRole("button", { name: "run" }).click();
    });
    expect(asked).toEqual([]);
  });

  it("asks again when the window comes back into focus and when a tab asks", async () => {
    const asked = scriptWake(waking);
    mount("c1", true);
    await waitFor(() => expect(asked).toHaveLength(1));
    await act(async () => {
      window.dispatchEvent(new Event("focus"));
    });
    await waitFor(() => expect(asked).toHaveLength(2));
    await act(async () => {
      screen.getByRole("button", { name: "run" }).click();
    });
    await waitFor(() => expect(asked).toHaveLength(3));
    expect(new Set(asked)).toEqual(new Set(["POST /api/v1/chats/c1/wake"]));
  });

  it.each(["awake", "working"])("asks for nothing while the chat reads %s, on open, on focus and on a run", async (state) => {
    const asked = scriptWake(waking);
    mount("c1", true, state);
    await act(async () => {
      window.dispatchEvent(new Event("focus"));
      screen.getByRole("button", { name: "run" }).click();
    });
    expect(asked).toEqual([]);
  });

  it("leaves a chat that went back to sleep under the open page asleep until the reader returns", async () => {
    const asked = scriptWake(waking);
    const view = mount("c1", true, "asleep");
    await waitFor(() => expect(asked).toHaveLength(1));
    // The chat wakes, sits idle and is slept again: the row changing asks for nothing.
    view.redraw("c1", true, "waking");
    view.redraw("c1", true, "awake");
    view.redraw("c1", true, "asleep");
    await act(async () => {});
    expect(asked).toHaveLength(1);
    await act(async () => {
      window.dispatchEvent(new Event("focus"));
    });
    await waitFor(() => expect(asked).toHaveLength(2));
  });

  it("moves to the chat the reader opens next", async () => {
    const asked = scriptWake(waking);
    const view = mount("c1", true);
    await waitFor(() => expect(asked).toHaveLength(1));
    view.redraw("c2", true);
    await waitFor(() => expect(asked).toEqual(["POST /api/v1/chats/c1/wake", "POST /api/v1/chats/c2/wake"]));
  });

  it("hands back the server's sentence when the org cannot pay for the start", async () => {
    scriptWake(
      () =>
        new Response(
          JSON.stringify({
            error: { code: "insufficient_credit", message: "Not enough credit to run this machine for a minute; add credit and try again." },
          }),
          { status: 402, headers: { "content-type": "application/json" } },
        ),
    );
    mount("c1", true);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "run" }).getAttribute("data-refusal")).toBe(
        "Not enough credit to run this machine for a minute; add credit and try again.",
      ),
    );
  });

  it("says nothing for a failure that is not a refusal to start", async () => {
    scriptWake(() => new Response("{}", { status: 500, headers: { "content-type": "application/json" } }));
    mount("c1", true);
    await act(async () => {});
    await act(async () => {});
    expect(screen.getByRole("button", { name: "run" }).getAttribute("data-refusal")).toBe("");
  });
});

describe("useWakeWorkspaceOnOpen", () => {
  function mountWorkspace(workspaceId: string | undefined, awake: boolean) {
    render(
      <QueryClientProvider client={createQueryClient({ retry: false })}>
        <WorkspaceProbe workspaceId={workspaceId} awake={awake} />
      </QueryClientProvider>,
    );
  }

  it("asks the workspace's own route once, and takes an empty answer", async () => {
    const asked = scriptWake(() => new Response(null, { status: 204 }));
    mountWorkspace("w1", false);
    await waitFor(() => expect(asked).toEqual(["POST /api/v1/workspaces/w1/wake"]));
  });

  it.each([
    ["a workspace that already reads as up", "w1", true],
    ["a workspace the page does not have yet", undefined, false],
  ])("asks for nothing for %s", async (_what, workspaceId, awake) => {
    const asked = scriptWake(waking);
    mountWorkspace(workspaceId, awake);
    await act(async () => {
      window.dispatchEvent(new Event("focus"));
    });
    expect(asked).toEqual([]);
  });
});
