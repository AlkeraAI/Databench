// The banner over the chat follows the machine's liveness LIVE — and clears
// the same way — without a reload.
//
// The reader once learned that their workspace had gone only by refreshing the
// page, and found the banner still up after the box was back. The machine's
// reachability is announced as a `compute_machine.changed` frame (by the box's
// heartbeat when it recovers, by the reachability sweep when it goes quiet);
// the frame invalidates the machine read and the chat's own row, the refetch
// carries the new state, and the banner and the composer follow it both ways.
// The poll that floors it is fifteen seconds away, well past this test: the
// frame is the whole mechanism here.

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CurrentUser } from "@/api/auth";
import { RealtimeBridge } from "@/api/events/RealtimeBridge";
import { resetRealtimeStatus } from "@/api/events/status";
import { keys } from "@/api/keys";
import { createQueryClient } from "@/api/queryClient";
import { chatFact, chatFactForMachine } from "../../../fixtures/statusFacts";
import { openStream, scriptedFetch } from "@/tests/api/events/fakeSse";

vi.mock("@/pages/workspace/chat/ChatSurface", () => ({
  ChatSurface: (props: { chatId?: string; unavailable?: boolean; notice?: unknown }) => (
    <div
      data-testid="chat-surface"
      data-chat-id={props.chatId ?? ""}
      data-unavailable={props.unavailable ? "yes" : "no"}
    >
      {props.notice as never}
    </div>
  ),
}));

import { ChatPage } from "@/pages/workspace/chat/ChatPage";

const USER = {
  id: "11111111-1111-1111-1111-111111111111",
  email: "dana@example.com",
  first_name: "Dana",
  last_name: "Reader",
  display_name: "Dana Reader",
  org_team_id: "22222222-2222-2222-2222-222222222222",
  org_name: "Northwind Labs",
  org_role: "member",
  membership_count: 1,
  has_password: true,
  mfa_enabled: false,
  platform_role: null,
  platform_role_display: null,
  email_verified_at: "2026-07-30T00:00:00Z",
  email_verification_required: false,
  email_verification_deadline: null,
  verification_resend_available_at: null,
  created_at: "2026-07-30T00:00:00Z",
} satisfies CurrentUser;

const MACHINE_ID = "807bf89a-464c-4867-8b08-6e020a9bd8a3";
const UNREACHABLE = "The machine is unreachable";
const RELEASED = /The machine this chat ran on is no longer active\./;

const CHAT = {
  id: "c1",
  title: "add a png related to the poem",
  created_at: "2026-09-19T20:30:00Z",
  updated_at: "2026-09-19T20:50:00Z",
  last_seq: 116,
  machine_id: MACHINE_ID,
  machine_status: "ready",
  machine_refusal_reason: null,
};

/** The machine read as the backend answers it, mutable so the test can move it. */
let machineRow: Record<string, unknown>;
/** The chat row's own word on its machine. The server derives it from the
 *  bound allocation, so a reaped box reads `none` here too — the binding stays. */
let chatMachineStatus = "ready";

function machine(status: string, id: string | null = MACHINE_ID): Record<string, unknown> {
  return {
    machine_id: id,
    status,
    name: "demo-box",
    reason: null,
    last_heartbeat_at: "2026-09-19T20:53:59Z",
    status_fact: id === null ? chatFact("no_machine") : null,
  };
}

function scriptApi() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      const at = new URL(url, "http://x").pathname;
      const json = (body: unknown) =>
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      // The server's read of the chat says what its machine's word means for it.
      const chat = {
        ...CHAT,
        machine_status: chatMachineStatus,
        // A chat still bound to a reaped box reads as released.
        status: chatFactForMachine(chatMachineStatus === "none" ? "stranded" : chatMachineStatus),
      };
      if (/\/api\/v1\/chats\/[^/?]+$/.test(at)) return json(chat);
      if (at.startsWith("/api/v1/chats")) return json({ items: [chat], next_cursor: null });
      if (at.includes("/api/v1/machines/current")) return json(machineRow);
      if (at.includes("/api/v1/auth/me")) return json(USER);
      return json({});
    }),
  );
}

const frame = (id: number, type: string, entityId: string): string =>
  `id: ${id}\nevent: ${type}\ndata: ${JSON.stringify({ type, entity: "compute_machine", entity_id: entityId, version: 1, org_id: USER.org_team_id })}\n\n`;

let script: ReturnType<typeof scriptedFetch>;

beforeEach(() => {
  resetRealtimeStatus();
  machineRow = machine("ready");
  chatMachineStatus = "ready";
  script = scriptedFetch();
  scriptApi();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function mount() {
  const qc = createQueryClient({ retry: false });
  qc.setQueryData(keys.auth.me, USER);
  render(
    <QueryClientProvider client={qc}>
      <RealtimeBridge fetch={script.fetch} />
      <MemoryRouter initialEntries={["/chat/c1"]}>
        <Routes>
          <Route path="/chat/:chatId" element={<ChatPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return qc;
}

const composerUnavailable = () =>
  screen.getByTestId("chat-surface").getAttribute("data-unavailable");

describe("a chat bound to the org's box", () => {
  it("shows the banner when the box goes quiet and clears it when the box is back, on frames alone", async () => {
    const stream = openStream();
    script.answerStream(stream);
    mount();

    // Ready: no chrome at all, and the reader may compose.
    await waitFor(() => expect(composerUnavailable()).toBe("no"));
    expect(screen.queryByRole("status")).toBeNull();

    // The sweep found the box's heartbeat lapsed and announced it. Nothing on
    // this page has been told yet.
    machineRow = machine("unreachable");
    expect(screen.queryByText(UNREACHABLE)).toBeNull();
    stream.push(frame(1, "compute_machine.changed", MACHINE_ID));

    expect(await screen.findByText(UNREACHABLE, undefined, { timeout: 3_000 })).toBeInTheDocument();
    expect(composerUnavailable()).toBe("yes");

    // The box's next heartbeat landed and announced the recovery: the banner
    // goes and the composer comes back — no reload, no second message.
    machineRow = machine("ready");
    stream.push(frame(2, "compute_machine.changed", MACHINE_ID));

    await waitFor(() => expect(screen.queryByText(UNREACHABLE)).toBeNull(), { timeout: 3_000 });
    expect(composerUnavailable()).toBe("no");
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("reopens the composer the moment the box says it is restarting, in the same surface", async () => {
    // The daemon was killed (unreachable), then its supervisor brought it back
    // and the first beat said `restarting`: the same box, keeping its chats,
    // takes a message now. The surface is not remounted to get there.
    const stream = openStream();
    script.answerStream(stream);
    mount();
    await waitFor(() => expect(composerUnavailable()).toBe("no"));
    const surface = screen.getByTestId("chat-surface");

    machineRow = machine("unreachable");
    stream.push(frame(1, "compute_machine.changed", MACHINE_ID));
    expect(await screen.findByText(UNREACHABLE, undefined, { timeout: 3_000 })).toBeInTheDocument();
    expect(composerUnavailable()).toBe("yes");

    machineRow = machine("restarting");
    chatMachineStatus = "restarting";
    stream.push(frame(2, "compute_machine.changed", MACHINE_ID));
    expect(
      await screen.findByText(/is restarting and picks this chat up again/, undefined, { timeout: 3_000 }),
    ).toBeInTheDocument();
    expect(composerUnavailable()).toBe("no");
    expect(screen.queryByText(/being replaced/)).toBeNull();
    expect(screen.getByTestId("chat-surface")).toBe(surface);
  });

  it("follows a reaped box to 'no workspace' and back to ready under the same id", async () => {
    // The meter reaped the row (`none`), then the box re-registered and kept
    // its id: the chat's binding still holds, so the banner clears without the
    // chat having to be placed again.
    const stream = openStream();
    script.answerStream(stream);
    mount();
    await waitFor(() => expect(composerUnavailable()).toBe("no"));

    // The org has no live machine, and the chat's row — still bound to the
    // reaped id — reads `none` off that same allocation. One frame refreshes
    // both reads (the event map names the chat family beside the machine key).
    machineRow = machine("none", null);
    chatMachineStatus = "none";
    stream.push(frame(1, "compute_machine.changed", MACHINE_ID));
    expect(await screen.findByText(RELEASED, undefined, { timeout: 3_000 })).toBeInTheDocument();
    expect(composerUnavailable()).toBe("yes");

    machineRow = machine("ready");
    chatMachineStatus = "ready";
    stream.push(frame(2, "compute_machine.changed", MACHINE_ID));
    await waitFor(() => expect(screen.queryByText(RELEASED)).toBeNull(), { timeout: 3_000 });
    expect(composerUnavailable()).toBe("no");
  });
});
