import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { enterOrg, forgetActiveOrg, setOrgNavigator } from "@/api/activeOrg";
import { createQueryClient } from "@/api/queryClient";
import { setSessionChannelFactory } from "@/api/sessionChannel";
import { InvitesPanel } from "@/pages/organization/teams/overlays/InvitesPanel";

// The recipient's invitations, through the real hooks with only `fetch` stubbed: accepting one
// into another org offers the switch into exactly that org, and keeps offering it after the list
// refreshes without the row; accepting one into the org already open offers nothing.

const ORG_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const ORG_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

const json = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

const INVITE = {
  id: "inv-1",
  team_id: "t-1",
  team_name: "Beta Labs",
  org_name: "Beta Labs",
  inviter_display_name: null,
  email: "v@x.io",
  role: "member",
  status: "pending",
  expires_at: "2026-12-01T00:00:00Z",
  created_at: "2026-10-01T00:00:00Z",
  resolved_at: null,
  invited_by_id: null,
  role_display: "Member",
  refusal: null,
};

let sent: Request[];
let navigated: string[];
let restore: (url: string) => void;
let accepted: boolean;
let joinedOrg: string;
let multiOrgEnabled: boolean;

function serve() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: Request) => {
      sent.push(input.clone());
      const path = new URL(input.url).pathname;
      if (path === "/api/v1/auth/me") return json({ id: "u", email: "v@x.io", first_name: "V", last_name: "N", org_team_id: ORG_A });
      if (path === "/api/v1/invitations/me") return json(accepted ? [] : [INVITE]);
      if (path === "/api/v1/config") return json({ self_hosted: false, multi_org_enabled: multiOrgEnabled });
      if (path === "/api/v1/invitations/inv-1/accept") {
        accepted = true;
        return json({ invitation: {}, joined_team_ids: ["t-1", joinedOrg], org_team_id: joinedOrg, org_name: "Beta Labs" });
      }
      if (path === "/api/v1/auth/refresh/org") {
        return json({ user: { org_team_id: ORG_B }, expires_at: new Date(Date.now() + 600_000).toISOString() });
      }
      return json({}, 404);
    }),
  );
}

function renderPanel() {
  render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter>
        <InvitesPanel />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  sent = [];
  navigated = [];
  accepted = false;
  joinedOrg = ORG_B;
  multiOrgEnabled = true;
  setSessionChannelFactory(null);
  restore = setOrgNavigator((url) => navigated.push(url));
  forgetActiveOrg();
  enterOrg(ORG_A);
  serve();
});

afterEach(() => {
  cleanup();
  setOrgNavigator(restore);
  setSessionChannelFactory(undefined);
  forgetActiveOrg();
  vi.unstubAllGlobals();
});

describe("InvitesPanel after an acceptance", () => {
  it("offers the switch into the org joined, after the row is gone", async () => {
    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: /accept/i }));
    // The list refreshes without the row, and the offer stays.
    expect(await screen.findByText(/no pending invitations/i)).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "Switch to Beta Labs" }));
    await waitFor(() => expect(navigated).toEqual(["/"]));
    const [request] = sent.filter((r) => new URL(r.url).pathname === "/api/v1/auth/refresh/org");
    expect(await request.json()).toEqual({ org_team_id: ORG_B });
  });

  it("offers nothing with several orgs per person off", async () => {
    multiOrgEnabled = false;
    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: /accept/i }));
    expect(await screen.findByText(/no pending invitations/i)).toBeInTheDocument();
    await waitFor(() => expect(sent.some((r) => new URL(r.url).pathname === "/api/v1/config")).toBe(true));
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.queryByRole("button", { name: "Switch to Beta Labs" })).toBeNull();
  });

  it("offers nothing when the invitation joined the org already open", async () => {
    joinedOrg = ORG_A;
    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: /accept/i }));
    expect(await screen.findByText(/no pending invitations/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Switch to Beta Labs" })).toBeNull();
  });
});
