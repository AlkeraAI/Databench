// A chat renamed somewhere else is renamed here too, without a reload.
//
// The chat header, the rail and the crumb trail all read the list under
// `chatKeys.chats()`, a family of its own, which only local actions (send,
// create, delete, prefs) ever invalidated. So a title changed in another tab, by
// a teammate, or by the agent itself stayed on the old name until the page was
// reopened. The frame has to reach the key the header actually reads.
//
// Two frames can carry a rename, and both are pinned here. The agent's own
// retitle announces `chat.updated`. A person renaming a chat writes it through
// the OBJECT route — a chat is a workspace object — which announces
// `workspace_object.changed` and no chat frame at all, so that one has to reach
// the same key or a teammate's rename is invisible.

import { render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { ds } = vi.hoisted(() => ({
  ds: {
    listChats: vi.fn(async () => [{ id: "c1", title: "Untitled chat", permissionMode: "default" }]),
  },
}));

vi.mock("@/pages/workspace/chat/data", () => ({
  chatData: () => ds,
  refetchWhileErrored: () => false as const,
}));

import type { CurrentUser } from "@/api/auth";
import { RealtimeBridge } from "@/api/events/RealtimeBridge";
import { resetRealtimeStatus } from "@/api/events/status";
import { keys } from "@/api/keys";
import { createQueryClient } from "@/api/queryClient";
import { useChatIdentity } from "@/pages/workspace/chat/controller/useChatIdentity";
import { openStream, scriptedFetch } from "@/tests/api/events/fakeSse";

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

/** The header/crumb read, on its own: the title this chat is showing. */
function Header() {
  const { title } = useChatIdentity({ handoff: null });
  return <h1 data-testid="chat-title">{title}</h1>;
}

const frame = (id: number, type: string, entityId: string, entity = "chat"): string =>
  `id: ${id}\nevent: ${type}\ndata: ${JSON.stringify({
    type,
    entity,
    entity_id: entityId,
    version: 2,
    org_id: USER.org_team_id,
  })}\n\n`;

let script: ReturnType<typeof scriptedFetch>;

beforeEach(() => {
  resetRealtimeStatus();
  script = scriptedFetch();
  ds.listChats.mockClear();
  ds.listChats.mockImplementation(async () => [
    { id: "c1", title: "Untitled chat", permissionMode: "default" },
  ]);
});

afterEach(() => {
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
          <Route path="/chat/:chatId" element={<Header />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("a chat renamed in another client", () => {
  it("renames the open chat's header on the chat.updated frame, with one refetch", async () => {
    const stream = openStream();
    script.answerStream(stream);
    mount();

    expect(await screen.findByText("Untitled chat")).toBeInTheDocument();
    expect(ds.listChats).toHaveBeenCalledTimes(1);

    // Renamed elsewhere. Nothing on this page has been told yet.
    ds.listChats.mockImplementation(async () => [
      { id: "c1", title: "Quarterly citations", permissionMode: "default" },
    ]);
    expect(screen.getByTestId("chat-title").textContent).toBe("Untitled chat");

    stream.push(frame(1, "chat.updated", "c1"));

    await waitFor(() => expect(screen.getByTestId("chat-title").textContent).toBe("Quarterly citations"), {
      timeout: 3_000,
    });
    // One frame, one re-read: the debounced scheduler must not fan the family
    // out into a second pass over the same list.
    expect(ds.listChats).toHaveBeenCalledTimes(2);
  });

  it("renames it on the object frame a person's rename actually announces", async () => {
    // The object route is the only way a reader renames a chat, and it rings no
    // chat doorbell — only this one.
    const stream = openStream();
    script.answerStream(stream);
    mount();

    expect(await screen.findByText("Untitled chat")).toBeInTheDocument();
    ds.listChats.mockImplementation(async () => [
      { id: "c1", title: "Quarterly citations", permissionMode: "default" },
    ]);

    stream.push(frame(1, "workspace_object.changed", "c1", "workspace_object"));

    await waitFor(
      () => expect(screen.getByTestId("chat-title").textContent).toBe("Quarterly citations"),
      { timeout: 3_000 },
    );
  });

  it("leaves the header alone on a frame that says nothing about chats", async () => {
    // The control: it is the chat event that refreshes the list, not any frame.
    const stream = openStream();
    script.answerStream(stream);
    mount();

    expect(await screen.findByText("Untitled chat")).toBeInTheDocument();
    ds.listChats.mockImplementation(async () => [
      { id: "c1", title: "Quarterly citations", permissionMode: "default" },
    ]);

    stream.push(frame(1, "kb_item.changed", "k1"));

    await new Promise((resolve) => setTimeout(resolve, 600));
    expect(screen.getByTestId("chat-title").textContent).toBe("Untitled chat");
    expect(ds.listChats).toHaveBeenCalledTimes(1);
  });
});
