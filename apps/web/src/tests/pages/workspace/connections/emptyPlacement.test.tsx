// What a page with nothing on it says, and how many ways it offers to fix that.
//
// An empty page is where a reader decides whether the product is broken or
// simply new. It gets one fact and one action: a second copy of the same key in
// the masthead is one control too many, and a paragraph explaining who a
// connection belongs to is the first question the dialog itself asks.

import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useState, type ReactNode } from "react";

import { createQueryClient } from "@/api/queryClient";
import { TopbarSlotsContext } from "@/app/Topbar";
import { ConnectionsPage } from "@/pages/workspace/connections/ConnectionsPage";

const FORMS = {
  connectors: [
    { name: "postgres", title: "PostgreSQL", form: {}, team_capable_methods: ["password"], ask_groups: {} },
  ],
};

const ROW = {
  id: "22222222-2222-2222-2222-222222222222",
  team_id: null,
  team_name: null,
  owner_user_id: "88888888-8888-8888-8888-888888888888",
  created_by_id: "88888888-8888-8888-8888-888888888888",
  created_by_name: "Vera Ng",
  can_manage: true,
  plugin: "postgres",
  handle: "wh_main",
  shared_values: { host: "db.internal" },
  auth_mode: "shared",
  auth_method: "password",
  member_fields: [],
  values_doc: [],
  ask_groups: [],
  shared_consent: null,
  auto_add: true,
  enabled: true,
  has_shared_secret: true,
  has_primary_secret: true,
  credential_version: 1,
  oauth_client_id: null,
  has_oauth_client_secret: false,
  oauth_config: null,
  badge: "connected",
  badge_reason: "",
  outcome: "ok",
  last_detail: "",
  last_verified_at: null,
  verification_state: null,
  credential_state: "present",
  reauth: null,
  members: null,
  created_at: "2026-07-04T00:00:00Z",
  updated_at: "2026-07-04T00:00:00Z",
};

function backend(rows: unknown[]) {
  return vi.fn(async (input: RequestInfo | URL) => {
    const url = input instanceof Request ? input.url : String(input);
    const body = url.includes("connection-forms")
      ? FORMS
      : url.includes("/auth/me")
        ? { id: "88888888-8888-8888-8888-888888888888", email: "vera@x.io" }
        : url.includes("/teams")
          ? []
          : rows;
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }) as unknown as typeof fetch;
}

/** The shell's masthead slot, so the page's add key has somewhere to portal. */
function Shell({ children }: { children: ReactNode }) {
  const [actions, setActions] = useState<HTMLElement | null>(null);
  return (
    <TopbarSlotsContext.Provider
      value={{ subtitle: null, actions, framed: true, setTitleHidden: () => {}, setTopbarHidden: () => {} }}
    >
      <div data-testid="topbar-actions" ref={setActions} />
      {children}
    </TopbarSlotsContext.Provider>
  );
}

function renderPage(rows: unknown[]) {
  vi.stubGlobal("fetch", backend(rows));
  return render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter initialEntries={["/connections"]}>
        <Shell>
          <Routes>
            <Route path="/connections" element={<ConnectionsPage />} />
          </Routes>
        </Shell>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** Where the add key is: in the masthead slot, or in the page's own body. */
function addKeys(): { masthead: number; body: number } {
  const all = screen.queryAllByRole("button", { name: "Add connection" });
  const slot = screen.getByTestId("topbar-actions");
  const masthead = all.filter((key) => slot.contains(key)).length;
  return { masthead, body: all.length - masthead };
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("the Connections page with nothing on it", () => {
  it("says the one fact and offers exactly one way to fix it", async () => {
    renderPage([]);
    expect(await screen.findByRole("heading", { name: "No connections yet" })).toBeInTheDocument();
    // One control, in the page's own body where the reader is looking.
    await waitFor(() => expect(addKeys()).toEqual({ masthead: 0, body: 1 }));
  });

  it("keeps the masthead key once there is a list to add to", async () => {
    renderPage([ROW]);
    await screen.findByText("wh_main");
    expect(addKeys()).toEqual({ masthead: 1, body: 0 });
    expect(screen.queryByRole("heading", { name: "No connections yet" })).toBeNull();
  });

  it("states the fact and offers the button, with no sentence explaining the button", async () => {
    renderPage([]);
    await screen.findByRole("heading", { name: "No connections yet" });
    expect(screen.queryByText(/Add a warehouse, a database or a lake/)).toBeNull();
    // Who a connection belongs to is the dialog's first question, not a
    // paragraph the reader has to get past to reach the button.
    expect(screen.queryByText(/yours alone/i)).toBeNull();
    expect(screen.queryByText(/pick a team instead/i)).toBeNull();
  });

  // That the key on the empty page opens the SAME dialog the masthead key does
  // is pinned by the page's own suite: every add case there starts by clicking
  // the one control named "Add connection" on a page whose list came back empty
  // (`openForm` in ConnectionsPage.test.tsx), so the whole add flow already
  // runs through this key.
});
