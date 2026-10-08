// The browser's chat page: its route, its rail, and what it says when the
// machine behind the chat is not ready.
//
// The rule the banner cases exist for is that a chat must never hang silently.
// Whatever the machine is doing, the reader is told which of the four states it
// is in, and the composer stays where it is — disabled — rather than vanishing.

import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { MACHINE_POLL_MS } from "@/api/chats";
import { createQueryClient } from "@/api/queryClient";
import type { StatusFact } from "@/api/status";
import { chatFact, chatFactForMachine } from "../../../fixtures/statusFacts";
import { keys } from "@/api/keys";
import { hasChildren, NAV, type NavLeaf } from "@/app/nav";

// The chat composition itself is proven by its own suite; what this file is
// about is the page around it, so the surface is stood in for by a marker that
// reports the props the page hands it.
vi.mock("@/pages/workspace/chat/ChatSurface", () => ({
  ChatSurface: (props: {
    chatId?: string;
    unavailable?: boolean;
    unavailableReason?: string;
    notice?: unknown;
    canSend?: boolean;
  }) => (
    <div
      data-testid="chat-surface"
      data-chat-id={props.chatId ?? ""}
      data-can-send={props.canSend === undefined ? "" : String(props.canSend)}
      data-unavailable={props.unavailable ? "yes" : "no"}
      data-reason={props.unavailableReason ?? ""}
    >
      {props.notice as never}
    </div>
  ),
}));

import { CHAT_GONE_TITLE, ChatPage, READ_ONLY_CHAT } from "@/pages/workspace/chat/ChatPage";
import { chatData, chatHost } from "@/pages/workspace/chat/data";

const CHAT = {
  id: "c1",
  title: "Yesterday's orders",
  machine_id: "m1",
  machine_status: "ready" as const,
  created_at: "2026-09-06T12:00:00Z",
  updated_at: "2026-09-06T12:00:00Z",
  last_seq: 3,
};

type MachineStatus = "starting" | "ready" | "unreachable" | "none" | "refused" | "asleep";

function scriptFetch(
  status: MachineStatus,
  chats = [CHAT],
  reason: string | null = null,
  fact: StatusFact | null = chatFactForMachine(status),
) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (/\/api\/v1\/chats\/[^/?]+$/.test(new URL(url, "http://x").pathname)) {
        return new Response(
          JSON.stringify({
            ...CHAT,
            machine_status: status,
            machine_refusal_reason: reason,
            status: fact,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (url.includes("/api/v1/chats")) {
        return new Response(
          JSON.stringify({
            items: chats.map((c) => ({ ...c, machine_status: status, status: chatFactForMachine(status) })),
            next_cursor: null,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (url.includes("/api/v1/machines/current")) {
        return new Response(
          JSON.stringify({ machine_id: "m1", status, name: "box-1", reason: null, status_fact: null }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (url.includes("/api/v1/auth/me")) {
        return new Response(JSON.stringify({ id: "u1", email: "dana@example.com" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      return new Response("{}", { status: 200, headers: { "content-type": "application/json" } });
    }),
  );
}

/** The chat row and the live machine disagreeing — what a cached chat row and a
 *  box that has since stopped answering actually look like to this page. */
function scriptDisagreeing(
  chatStatus: MachineStatus,
  live: { status: MachineStatus; machine_id?: string | null; reason?: string | null },
  /** What the server's next read of the chat says, once it has caught up. */
  chatStatus2: StatusFact | null = null,
) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      const json = (body: unknown) =>
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      if (url.includes("/api/v1/machines/current")) {
        return json({
          machine_id: live.machine_id === undefined ? "m1" : live.machine_id,
          status: live.status,
          name: "box-1",
          reason: live.reason ?? null,
        });
      }
      if (/\/api\/v1\/chats\/[^/?]+$/.test(new URL(url, "http://x").pathname)) {
        return json({ ...CHAT, machine_status: chatStatus, machine_refusal_reason: null, status: chatStatus2 });
      }
      if (url.includes("/api/v1/chats")) {
        return json({ items: [{ ...CHAT, machine_status: chatStatus, status: chatStatus2 }], next_cursor: null });
      }
      if (url.includes("/api/v1/auth/me")) return json({ id: "u1", email: "dana@example.com" });
      return json({});
    }),
  );
}

/** The chat read refusing, which is what an unshared chat looks like: private
 *  by default, so the row a reader was never granted simply is not there. */
function scriptRefusedChat(status: number) {
  const json = (payload: unknown, code = 200): Response =>
    new Response(JSON.stringify(payload), {
      status: code,
      headers: { "content-type": "application/json" },
    });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      const at = new URL(url, "http://x").pathname;
      if (/\/api\/v1\/chats\/[^/?]+$/.test(at)) {
        return json({ detail: status === 404 ? "Not found" : "Forbidden" }, status);
      }
      if (at.startsWith("/api/v1/chats")) return json({ items: [], next_cursor: null });
      if (url.includes("/api/v1/machines/current")) {
        return json({ machine_id: "m1", status: "ready", name: "box-1", reason: null });
      }
      if (url.includes("/api/v1/auth/me")) return json({ id: "u1", email: "dana@example.com" });
      return json({});
    }),
  );
}

/** A chat whose Files node hands this reader `rung`. That node IS what a send
 *  is decided against, so it is what the composer must read. */
function scriptRung(canWrite: boolean) {
  const json = (payload: unknown, code = 200): Response =>
    new Response(JSON.stringify(payload), {
      status: code,
      headers: { "content-type": "application/json" },
    });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      // openapi-fetch hands a Request, not a string: stringifying one yields
      // "[object Request]" and every branch below would miss.
      const url = input instanceof Request ? input.url : String(input);
      const at = new URL(url, "http://x").pathname;
      if (/\/api\/v1\/chats\/[^/?]+$/.test(at)) {
        return json({ ...CHAT, machine_status: "ready", files_node_id: "nd_chat" });
      }
      if (at.startsWith("/api/v1/files/drives") && at.includes("/items/")) {
        return json({
          id: "nd_chat",
          driveId: "drv_1",
          kind: "folder",
          name: "c1.alkerachat",
          nameDisplay: "Yesterday's orders",
          parentId: "nd_root",
          etag: "1",
          ctag: "c1",
          capabilities: { can_read: true, can_write: canWrite, can_share: false, refusals: {} },
        });
      }
      if (at === "/api/v1/files/drives") {
        return json({ id: "drv_1", orgId: "org_1", rootId: "nd_root", quotaBytes: 1 });
      }
      if (at.startsWith("/api/v1/chats")) {
        return json({ items: [{ ...CHAT, machine_status: "ready" }], next_cursor: null });
      }
      if (url.includes("/api/v1/machines/current")) {
        return json({ machine_id: "m1", status: "ready", name: "box-1", reason: null });
      }
      if (url.includes("/api/v1/auth/me")) return json({ id: "u1", email: "dana@example.com" });
      return json({});
    }),
  );
}

/** Reports the router's location, so where a refused chat lands is read off
 *  the URL rather than inferred from what is on screen. */
function Where() {
  return <span data-testid="at">{useLocation().pathname}</span>;
}

/** The same wire, with `/auth/me` held open until the test lets it answer — the
 *  real shape of a page load, where the signed-in account lands a moment after
 *  the chat surface is already up. */
function scriptLateAccount(): { answerAccount: () => void } {
  let answerAccount = (): void => {};
  const account = new Promise<void>((resolve) => {
    answerAccount = () => resolve();
  });
  const json = (payload: unknown) =>
    new Response(JSON.stringify(payload), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      const at = new URL(url, "http://x").pathname;
      if (url.includes("/api/v1/auth/me")) {
        await account;
        return json({ id: "u1", email: "dana@example.com" });
      }
      if (/\/api\/v1\/chats\/[^/?]+$/.test(at)) {
        return json({ ...CHAT, machine_status: "ready" });
      }
      if (at.startsWith("/api/v1/chats")) {
        return json({ items: [{ ...CHAT, machine_status: "ready" }], next_cursor: null });
      }
      if (url.includes("/api/v1/machines/current")) {
        return json({ machine_id: "m1", status: "ready", name: "box-1", reason: null });
      }
      return json({});
    }),
  );
  return { answerAccount };
}

function renderChat(path = "/chat") {
  const client = createQueryClient({ retry: false });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Where />
        <Routes>
          <Route path="/chat" element={<ChatPage />} />
          <Route path="/chat/new" element={<ChatPage />} />
          <Route path="/chat/:chatId" element={<ChatPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return client;
}

beforeEach(() => {
  scriptFetch("ready");
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("the chat route", () => {
  it("mounts the shared chat surface with no chat open at /chat", async () => {
    renderChat("/chat");
    const surface = await screen.findByTestId("chat-surface");
    expect(surface.getAttribute("data-chat-id")).toBe("");
  });

  it("opens the chat named in the path", async () => {
    renderChat("/chat/c1");
    const surface = await screen.findByTestId("chat-surface");
    expect(surface.getAttribute("data-chat-id")).toBe("c1");
  });

  it("lists the org's chats in the rail and marks the open one", async () => {
    renderChat("/chat/c1");
    const rail = await screen.findByRole("navigation", { name: /chats/i });
    const row = await within(rail).findByRole("button", { name: /^Yesterday's orders/ });
    expect(row.getAttribute("aria-current")).toBe("page");
  });

  it("says so plainly rather than showing an empty rail", async () => {
    scriptFetch("ready", []);
    renderChat("/chat");
    expect(await screen.findByText("No chats yet.")).toBeInTheDocument();
  });

  it("claims the whole routed content box rather than sitting inside its padding", async () => {
    // The shell drops the content box's padding and its scrollbar for a page
    // that marks itself this way. Without the mark the split view is laid out
    // inside a scrolling, padded box: the gutters stop at the padding's edge
    // and dragging one scrolls the page instead of moving a column.
    renderChat("/chat/c1");
    const surface = await screen.findByTestId("chat-surface");
    const page = surface.closest(".chat-page");
    expect(page).not.toBeNull();
    expect(page).toHaveAttribute("data-content-fill");
  });

  it("keeps one chat source when the signed-in account arrives", async () => {
    // The source holds the chat's folded transcript, and the open chat's
    // subscription is bound to the instance it opened on. Rebuilding the
    // runtime when the account resolved handed the surface — which is not
    // remounted for it — a second, empty source: every read then came off a
    // fold holding only what this tab had sent since, so the reader's next
    // message appeared twice with the conversation above it gone.
    const { answerAccount } = scriptLateAccount();
    renderChat("/chat/c1");
    await screen.findByTestId("chat-surface");
    const opened = chatData();
    expect(chatHost().account().email, "the account has not landed yet").toBeNull();

    answerAccount();

    await waitFor(() => expect(chatHost().account().email).toBe("dana@example.com"));
    expect(chatData(), "the chat is still read through the source it opened on").toBe(opened);
  });
});

describe("who may send", () => {
  function scriptCanSend(canSend: boolean) {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        const json = (body: unknown) =>
          new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
        if (/\/api\/v1\/chats\/[^/?]+$/.test(new URL(url, "http://x").pathname)) {
          return json({ ...CHAT, can_send: canSend });
        }
        if (url.includes("/api/v1/chats")) return json({ items: [CHAT], next_cursor: null });
        if (url.includes("/api/v1/machines/current")) {
          return json({ machine_id: "m1", status: "ready", name: "box-1", reason: null });
        }
        if (url.includes("/api/v1/auth/me")) return json({ id: "u1", email: "dana@example.com" });
        return json({});
      }),
    );
  }

  it.each([
    ["a reader the server refuses sends to", false],
    ["a reader the server lets send", true],
  ])("hands the surface what the chat read says for %s", async (_who, canSend) => {
    scriptCanSend(canSend);
    renderChat("/chat/c1");
    const surface = await screen.findByTestId("chat-surface");
    await waitFor(() => expect(surface.getAttribute("data-can-send")).toBe(String(canSend)));
  });
});

describe("opening a chat wakes it", () => {
  /** The wake requests the page made, in order. */
  function scriptOpen(canSend: boolean, sessionState = "asleep"): string[] {
    const wakes: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const path = new URL(url, "http://x").pathname;
        const json = (body: unknown) =>
          new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
        if (path.endsWith("/wake")) {
          wakes.push(`${init?.method ?? "GET"} ${path}`);
          return json({ outcome: "waking" });
        }
        if (/\/api\/v1\/chats\/[^/?]+$/.test(path)) {
          return json({ ...CHAT, can_send: canSend, session_state: sessionState });
        }
        if (url.includes("/api/v1/chats")) return json({ items: [CHAT], next_cursor: null });
        if (url.includes("/api/v1/machines/current")) {
          return json({ machine_id: "m1", status: "ready", name: "box-1", reason: null });
        }
        if (url.includes("/api/v1/auth/me")) return json({ id: "u1", email: "dana@example.com" });
        return json({});
      }),
    );
    return wakes;
  }

  it("asks for nothing when the server says the chat is already awake", async () => {
    const wakes = scriptOpen(true, "awake");
    renderChat("/chat/c1");
    const surface = await screen.findByTestId("chat-surface");
    await waitFor(() => expect(surface.getAttribute("data-can-send")).toBe("true"));
    await act(async () => {});
    expect(wakes).toEqual([]);
  });

  it("asks once for a reader the server lets send, however often the page draws", async () => {
    const wakes = scriptOpen(true);
    const client = renderChat("/chat/c1");
    const surface = await screen.findByTestId("chat-surface");
    await waitFor(() => expect(surface.getAttribute("data-can-send")).toBe("true"));
    await waitFor(() => expect(wakes).toEqual(["POST /api/v1/chats/c1/wake"]));
    // The chat row is read again (a poll, an event): the page draws, and asks
    // for no second wake.
    await client.refetchQueries({ queryKey: ["chats", "c1"] });
    await client.refetchQueries({ queryKey: ["chats", "c1"] });
    expect(wakes).toEqual(["POST /api/v1/chats/c1/wake"]);
  });

  it("asks for nothing when the server says this reader may not send", async () => {
    const wakes = scriptOpen(false);
    renderChat("/chat/c1");
    const surface = await screen.findByTestId("chat-surface");
    await waitFor(() => expect(surface.getAttribute("data-can-send")).toBe("false"));
    expect(wakes).toEqual([]);
  });

  it("asks for nothing on a chat that has not been sent yet", async () => {
    const wakes = scriptOpen(true);
    renderChat("/chat/new");
    await screen.findByTestId("chat-surface");
    expect(wakes).toEqual([]);
  });
});

describe("while the page is still finding out", () => {
  // Both reads are in flight for a moment on every navigation. Reading an
  // unknown status as "no workspace" disabled the composer and told the reader
  // their org has no machine — on a chat whose box is running fine.
  function scriptSlow() {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        const json = (body: unknown) =>
          new Response(JSON.stringify(body), {
            status: 200,
            headers: { "content-type": "application/json" },
          });
        if (url.includes("/api/v1/auth/me")) return json({ id: "u1", email: "dana@example.com" });
        if (url.includes("/api/v1/machines/current") || /\/api\/v1\/chats\/[^/?]+$/.test(new URL(url, "http://x").pathname)) {
          return new Promise<Response>(() => undefined);
        }
        if (url.includes("/api/v1/chats")) return json({ items: [CHAT], next_cursor: null });
        return json({});
      }),
    );
  }

  it("does not tell the reader their org has no workspace before it knows", async () => {
    scriptSlow();
    renderChat("/chat/c1");
    const surface = await screen.findByTestId("chat-surface");
    expect(surface.getAttribute("data-reason")).toBe("");
    expect(screen.queryByText("No machine can serve your organization right now")).toBeNull();
  });

  it("leaves the composer alone until a status is known", async () => {
    scriptSlow();
    renderChat("/chat/c1");
    const surface = await screen.findByTestId("chat-surface");
    expect(surface.getAttribute("data-unavailable")).toBe("no");
  });
});

describe("the workspace nav", () => {
  it("carries a Chat leaf pointing at the route", () => {
    const workspace = NAV.find((group) => group.group === "Personal");
    const leaves = (workspace?.items ?? []).filter((item): item is NavLeaf => !hasChildren(item));
    expect(leaves.map((leaf) => [leaf.label, leaf.to])).toContainEqual(["Chat", "/chat"]);
  });
});

describe("what the page says about the machine", () => {
  it.each(["starting", "unreachable", "none", "refused"] as MachineStatus[])(
    "%s reads in the server's own words",
    async (status) => {
      scriptFetch(status);
      renderChat("/chat/c1");
      const fact = chatFactForMachine(status);
      expect(await screen.findByText(fact?.sentence ?? "")).toBeInTheDocument();
      expect(screen.getByRole("status")).toHaveTextContent(fact?.label ?? "");
    },
  );

  it.each(["starting", "unreachable", "none", "refused", "asleep"] as MachineStatus[])(
    "the rail spends no words on %s",
    async (status) => {
      // Every row in the list carries the same state at once during the kill
      // drill, so a word here becomes a column of them beside the titles. The
      // row says it with a light; the banner above the transcript says it in
      // sentences, where there is room for what happens next.
      scriptFetch(status);
      renderChat("/chat/c1");
      const rail = await screen.findByRole("navigation", { name: /chats/i });
      const row = await within(rail).findByRole("button", { name: /^Yesterday's orders/ });
      await waitFor(() => expect(row.textContent).toBe("Yesterday's orders"));
      expect(within(rail).queryByText(status)).toBeNull();
    },
  );

  it("reads a refusal for its kind, and never shows the box's own words", async () => {
    // What reached a tenant: the box's sentence, naming a platform address,
    // under a support escalation.
    scriptFetch(
      "refused",
      [CHAT],
      "this chat's folder could not be taken: 203.0.113.7 is not reachable from the machine serving it",
      chatFact("waiting_files"),
    );
    renderChat("/chat/c1");
    const banner = (
      await screen.findByText("The machine serving it can't reach the chat's files right now.")
    ).closest("[role=status]");
    expect(banner?.textContent ?? "").not.toMatch(/107\.21\.75\.118|could not be taken|support|contact/i);
    expect(screen.queryByTestId("chat-machine-support")).toBeNull();
  });

  it("reads a refusal it has no kind for as a wait, not as a chat that can't run", async () => {
    // A workspace being handed between machines on a wake or a switch reached
    // the reader as "This chat can't run right now". The server reads a
    // refusal with no kind as a wait; the page draws what it wrote.
    scriptFetch("refused", [CHAT], "a reason this build has never seen", chatFact("waiting"));
    renderChat("/chat/c1");
    const banner = (await screen.findByText(/The chat continues as soon as the machine can run it\./)).closest(
      "[role=status]",
    );
    expect(banner?.textContent ?? "").not.toMatch(/never seen/);
    expect(screen.queryByText(/This chat can't run right now/)).toBeNull();
  });

  it("carries no reason line for any other machine state", async () => {
    scriptFetch("starting", [CHAT], "stale");
    renderChat("/chat/c1");
    await screen.findByText(chatFactForMachine("starting")?.sentence ?? "");
    expect(screen.queryByTestId("chat-machine-reason")).toBeNull();
  });

  it("tells the reader what happens next, not just that something is wrong", async () => {
    scriptFetch("none");
    renderChat("/chat/c1");
    // The server's sentence states the fact; the page polls and clears it.
    expect(await screen.findByText("No machine can serve your organization right now.")).toBeInTheDocument();
    expect(screen.queryByTestId("chat-machine-support")).toBeNull();
  });

  it("shows no banner at all once the machine is ready", async () => {
    scriptFetch("ready");
    renderChat("/chat/c1");
    await screen.findByTestId("chat-surface");
    await waitFor(() => {
      expect(screen.queryByRole("status")).toBeNull();
    });
  });

  it.each(["starting", "unreachable", "none", "refused"] as MachineStatus[])(
    "disables the composer while the machine is %s, and says why",
    async (status) => {
      scriptFetch(status);
      renderChat("/chat/c1");
      const surface = await screen.findByTestId("chat-surface");
      await waitFor(() => {
        expect(surface.getAttribute("data-unavailable")).toBe("yes");
      });
      expect(surface.getAttribute("data-reason")).not.toBe("");
    },
  );

  it("leaves the composer usable once the machine is ready", async () => {
    scriptFetch("ready");
    renderChat("/chat/c1");
    const surface = await screen.findByTestId("chat-surface");
    await waitFor(() => {
      expect(surface.getAttribute("data-unavailable")).toBe("no");
    });
  });
});

describe("the live machine outranks the chat row", () => {
  it("disables the composer over a box that stopped answering, even while the chat row still says ready", async () => {
    // The kill drill. The chat row is a cached read of a binding; the machine's
    // own heartbeat is the live fact. Preferring the row here is the silent
    // hang the banner exists to rule out: a composer that accepts a question
    // nothing is listening for.
    scriptDisagreeing("ready", { status: "unreachable" });
    renderChat("/chat/c1");
    expect(
      await screen.findByText("The machine is unreachable"),
    ).toBeInTheDocument();
    const surface = screen.getByTestId("chat-surface");
    await waitFor(() => {
      expect(surface.getAttribute("data-unavailable")).toBe("yes");
    });
  });

  it("opens the composer once the box answers, even while the chat row still says starting", async () => {
    // The inverse, and just as bad: a chat opened while the box was booting
    // stayed permanently uncomposable, and a reload never helped.
    scriptDisagreeing("starting", { status: "ready" });
    renderChat("/chat/c1");
    const surface = await screen.findByTestId("chat-surface");
    await waitFor(() => {
      expect(surface.getAttribute("data-unavailable")).toBe("no");
    });
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("keeps the chat's own word when the chat is bound to some other machine", async () => {
    // One machine per org today, but the resolver is shaped for more: a chat
    // bound to a box that is not the org's current one must not borrow that
    // box's health.
    scriptDisagreeing("unreachable", { status: "ready", machine_id: "m-other" });
    renderChat("/chat/c1");
    expect(
      await screen.findByText("The machine is unreachable"),
    ).toBeInTheDocument();
  });

  it("keeps the machine's own words off the page", async () => {
    // The compute plane's reason is written for operators and can name the
    // platform's hosts; the state's own copy already says what happens next.
    // The chat bound to a box that left the plane: the server's read of the
    // chat says so in its own words.
    scriptDisagreeing(
      "ready",
      { status: "none", reason: "the pod at 10.0.3.7 was terminated by the provider" },
      chatFactForMachine("stranded"),
    );
    renderChat("/chat/c1");
    await screen.findByText(/The machine this chat ran on is no longer active\./);
    expect(screen.queryByText(/terminated by the provider|10\.0\.3\.7/)).toBeNull();
  });
});

describe("the drill: a box that dies while the reader is looking at it", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("names the dead box without waiting for an event to arrive", async () => {
    // `compute_machine.changed` is the fast path, but it travels over the same
    // connection a dead box may have taken down, and the transition it
    // announces is emitted by a periodic sweep rather than by the machine
    // itself. The poll is the floor under both: the reader is told, and the
    // composer closes, whether or not a frame ever lands.
    let status: MachineStatus = "ready";
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        const json = (body: unknown) =>
          new Response(JSON.stringify(body), {
            status: 200,
            headers: { "content-type": "application/json" },
          });
        if (url.includes("/api/v1/machines/current"))
          return json({ machine_id: "m1", status, name: "box-1", reason: null });
        if (/\/api\/v1\/chats\/[^/?]+$/.test(new URL(url, "http://x").pathname))
          return json({ ...CHAT, machine_refusal_reason: null });
        if (url.includes("/api/v1/chats")) return json({ items: [CHAT], next_cursor: null });
        if (url.includes("/api/v1/auth/me")) return json({ id: "u1", email: "dana@example.com" });
        return json({});
      }),
    );
    // Faked before the page mounts: the poll's timer has to be one this test
    // owns, or advancing the clock moves nothing.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    renderChat("/chat/c1");
    const surface = await screen.findByTestId("chat-surface");
    await waitFor(() => {
      expect(surface.getAttribute("data-unavailable")).toBe("no");
    });

    status = "unreachable";
    await vi.advanceTimersByTimeAsync(MACHINE_POLL_MS + 1_000);
    await waitFor(() => {
      expect(screen.queryByText("The machine is unreachable")).not.toBeNull();
    });
    expect(surface.getAttribute("data-unavailable")).toBe("yes");
  });
});

describe("what a machine event refreshes", () => {
  it("the machine state and the chat list hang under keys an event names", () => {
    // The banner is only ever as fresh as the key the event map invalidates, so
    // the page must read through exactly those keys.
    expect(keys.machines.current).toEqual(["machines", "current"]);
    expect(keys.chats.all).toEqual(["chats"]);
  });
});

describe("a chat the reader cannot read", () => {
  it("draws nothing of a chat the server says it has none of", async () => {
    scriptRefusedChat(404);
    renderChat("/chat/c1");

    // A 404 is the one answer that means the chat is not there for this reader
    // - deleted, or in another org, and deliberately indistinguishable - so the
    // page is a dead end that says so and offers a way on. None of the chat
    // shell is drawn around it: no composer, and none of the chat-scoped reads
    // the surface would start.
    expect(await screen.findByText(CHAT_GONE_TITLE)).toBeTruthy();
    expect(screen.queryByTestId("chat-surface")).toBeNull();
  });

  it("says the chat exists but is closed when the server refuses rather than hides", async () => {
    scriptRefusedChat(403);
    renderChat("/chat/c1");

    expect(await screen.findByText("Ask the owner to share it with you.")).toBeTruthy();
    expect(screen.queryByTestId("chat-surface")).toBeNull();
  });

  it("keeps the surface for a failure that is not a refusal", async () => {
    scriptRefusedChat(500);
    renderChat("/chat/c1");

    // A server fault is not "this chat is not yours": the reader keeps the chat
    // they were already looking at rather than being told it does not exist.
    expect(await screen.findByTestId("chat-surface")).toBeTruthy();
  });
});

describe("a reader whose rung on the chat cannot send", () => {
  it("takes the composer away with the reason, before anything is typed", async () => {
    scriptRung(false);
    renderChat("/chat/c1");

    const surface = await screen.findByTestId("chat-surface");
    await waitFor(() => expect(surface).toHaveAttribute("data-unavailable", "yes"));
    expect(surface).toHaveAttribute("data-reason", READ_ONLY_CHAT);
  });

  it("leaves the composer alone for a rung that can", async () => {
    scriptRung(true);
    renderChat("/chat/c1");

    const surface = await screen.findByTestId("chat-surface");
    // The machine is ready and the rung carries a message: nothing is taken
    // away, so the disable above is the rung's doing and not the default.
    await waitFor(() => expect(surface).toHaveAttribute("data-unavailable", "no"));
    expect(surface).toHaveAttribute("data-reason", "");
  });
});

describe("a chat the box put to sleep", () => {
  it("raises no banner, keeps the composer, and marks the row calmly", async () => {
    // The box is answering (the live machine is ready) and said it holds no
    // session for this chat. That is not an error plate, not a notice, and
    // not a disabled composer: the send is what wakes it.
    scriptDisagreeing("asleep", { status: "ready" });
    renderChat("/chat/c1");
    const surface = await screen.findByTestId("chat-surface");
    await waitFor(() => {
      expect(surface.getAttribute("data-unavailable")).toBe("no");
    });
    expect(screen.queryByText(/This chat is asleep/)).toBeNull();
    expect(document.querySelector('[data-status="asleep"]')).toBeNull();
    expect(screen.queryByTestId("chat-machine-support")).toBeNull();
    // The rail marks it with the quiet crescent, named but never spelled out:
    // a resting chat is not a warning.
    const rail = screen.getByRole("navigation", { name: /chats/i });
    await waitFor(() => expect(within(rail).queryAllByRole("img")).toHaveLength(0));
    expect(within(rail).queryByText(/asleep/i)).toBeNull();
  });

  it("lets a box that stopped answering outrank a sleeping row", async () => {
    // A chat asleep on a dead box is a dead box: the machine's own word wins.
    scriptDisagreeing("asleep", { status: "unreachable" });
    renderChat("/chat/c1");
    expect(
      await screen.findByText("The machine is unreachable"),
    ).toBeInTheDocument();
    const surface = screen.getByTestId("chat-surface");
    await waitFor(() => {
      expect(surface.getAttribute("data-unavailable")).toBe("yes");
    });
  });
});
