// The Connections page with no connection checks installed, as the open platform
// ships it: the open backend serves no check route, so no row offers one and
// nothing asks for one. The product's checked flow is ConnectionsPage.test.tsx.

import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("a connection row with no checks installed", () => {
  it("offers edit and remove, and no check", async () => {
    renderPage([ROW]);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Actions for wh_main" }));
    expect(await screen.findByRole("menuitem", { name: /Edit/ })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /Verify now/ })).toBeNull();
  });
});
