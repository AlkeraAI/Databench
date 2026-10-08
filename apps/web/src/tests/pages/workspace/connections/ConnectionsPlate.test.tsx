// The team detail's Connections plate. It reads what the team has configured
// and hands the changing of it to the Connections page, where one dialog serves
// a person's own connections and their teams' alike — so this surface has no add
// dialog of its own to drift out of step with that one.

import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";
import {
  ConnectionsPlate,
  manageConnectionsHref,
} from "@/pages/workspace/connections/ConnectionsPlate";
import { MemoryRouter, Route, Routes } from "react-router-dom";

const TEAM_ID = "11111111-1111-1111-1111-111111111111";
const TEAM = "Analytics";

const ROW = {
  id: "22222222-2222-2222-2222-222222222222",
  team_id: TEAM_ID,
  team_name: TEAM,
  owner_user_id: null,
  created_by_id: "88888888-8888-8888-8888-888888888888",
  created_by_name: "Grace Hopper",
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
  last_verified_at: "2026-07-04T11:58:00.000Z",
  verification_state: null,
  credential_state: "present",
  reauth: null,
  members: null,
  created_at: "2026-07-04T00:00:00Z",
  updated_at: "2026-07-04T00:00:00Z",
};

const FORMS = { connectors: [{ name: "postgres", title: "PostgreSQL", form: {}, team_capable_methods: ["password"], ask_groups: {} }] };

function backend(rows: unknown[] = [ROW]) {
  return vi.fn(async (input: RequestInfo | URL) => {
    const url = input instanceof Request ? input.url : String(input);
    const body = url.includes("connection-forms") ? FORMS : rows;
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }) as unknown as typeof fetch;
}

/** The plate, mounted under a router that records where a click sends the admin. */
function renderPlate(fetchImpl: typeof fetch) {
  vi.stubGlobal("fetch", fetchImpl);
  return render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter initialEntries={["/org/teams"]}>
        <Routes>
          <Route path="/org/teams" element={<ConnectionsPlate teamId={TEAM_ID} teamName={TEAM} />} />
          <Route path="/connections" element={<div>connections page</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("ConnectionsPlate", () => {
  it("lists what the team has configured, and who put it there", async () => {
    renderPlate(backend());
    expect(await screen.findByText(ROW.handle)).toBeInTheDocument();
    expect(screen.getByText(`Added by ${ROW.created_by_name}`)).toBeInTheDocument();
    expect(screen.getByText("Auto-added")).toBeInTheDocument();
    expect(screen.getByText("Connected")).toBeInTheDocument();
  });

  it("offers no add dialog of its own — changing a connection is one flow", async () => {
    renderPlate(backend());
    await screen.findByText(ROW.handle);
    // Two add dialogs for one save is exactly what the lift removed.
    expect(screen.queryByRole("button", { name: /^Add/ })).toBeNull();
    expect(screen.queryByLabelText(/Connection name/)).toBeNull();
  });

  it("sends an admin to the Connections page with this team already picked", async () => {
    renderPlate(backend());
    await screen.findByText(ROW.handle);
    await userEvent.click(screen.getByRole("button", { name: /Manage connections/ }));
    expect(await screen.findByText("connections page")).toBeInTheDocument();
  });

  it("names the deep link the page reads the team from", () => {
    // The page reads `?team=` to pre-pick the owner; spelling it here once means
    // the two ends cannot drift.
    expect(manageConnectionsHref(TEAM_ID)).toBe(`/connections?team=${TEAM_ID}`);
  });

  it("says the one fact on an empty plate, and still offers the way through", async () => {
    // A heading over an empty frame reads as something that failed to load, so
    // the plate says which it is. One sentence, and still no explainer: a
    // paragraph about how team connections fan out told the reader nothing they
    // could act on.
    renderPlate(backend([]));
    expect(await screen.findByText(/Nothing this team's chats can query yet/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Manage connections/ })).toBeInTheDocument();
    expect(screen.queryByText(new RegExp(`every member of ${TEAM}`, "i"))).not.toBeInTheDocument();
  });

  it("says nothing while the read is still in flight", async () => {
    // "Nothing yet" and "not loaded yet" are different facts, and saying the
    // first while the second is true is the plate telling the reader something
    // untrue for as long as the request takes.
    let answer: (rows: unknown[]) => void = () => undefined;
    const held = new Promise<unknown[]>((resolve) => {
      answer = resolve;
    });
    renderPlate(
      vi.fn(async (input: RequestInfo | URL) => {
        const url = input instanceof Request ? input.url : String(input);
        const body = url.includes("connection-forms") ? FORMS : await held;
        return new Response(JSON.stringify(body), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }) as unknown as typeof fetch,
    );
    expect(screen.queryByText(/Nothing this team's chats can query yet/)).toBeNull();
    answer([]);
    expect(await screen.findByText(/Nothing this team's chats can query yet/)).toBeInTheDocument();
  });
});
