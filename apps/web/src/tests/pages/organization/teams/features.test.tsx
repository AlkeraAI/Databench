import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/errors";
import type { components } from "@alkera/sdk";

// The recipient-side invitations panel, with its hooks (the network boundary) mocked and the
// rendered behavior asserted: the list resolves a team name and routes Accept/Decline to the right
// mutation with the right id. No class-name assertions. (The member drawer's coverage lives in
// MemberDrawer.test.tsx, driven through real hooks with `fetch` stubbed.)

type Invitation = components["schemas"]["RecipientInvitationRead"];
type Hook = { data?: unknown; isLoading?: boolean; isError?: boolean; error?: unknown };
type Mut = { mutate: ReturnType<typeof vi.fn>; isPending: boolean; variables?: unknown };

const state = {
  invites: { data: [] as Invitation[], isLoading: false, isError: false } as Hook,
  accept: { mutate: vi.fn(), isPending: false } as Mut,
  reject: { mutate: vi.fn(), isPending: false } as Mut,
};

vi.mock("@/api/teams", () => ({
  useMyInvitations: () => state.invites,
  useAcceptInvitationMutation: () => state.accept,
  useRejectInvitationMutation: () => state.reject,
}));

const { InvitesPanel } = await import("@/pages/organization/teams/overlays/InvitesPanel");

const invite = (id: string, teamName: string, overrides: Partial<Invitation> = {}): Invitation => ({
  id,
  team_id: `team-${id}`,
  team_name: teamName,
  org_name: "Tideline",
  inviter_display_name: "Ada Lovelace",
  email: "me@x.io",
  role: "member",
  status: "pending",
  expires_at: "2026-07-06T00:00:00Z",
  created_at: "2026-06-29T00:00:00Z",
  resolved_at: null,
  invited_by_id: null,
  role_display: "Member",
  refusal: null,
  ...overrides,
});

const REFUSAL = {
  code: "other_org" as const,
  message:
    "Your account belongs to Vendor Inc, and an account can belong to only one organization. To join Tideline, ask for an invitation to an email address you haven't used with Alkera.",
};

afterEach(cleanup);
beforeEach(() => {
  state.invites = { data: [], isLoading: false, isError: false };
  state.accept = { mutate: vi.fn(), isPending: false };
  state.reject = { mutate: vi.fn(), isPending: false };
});

describe("InvitesPanel", () => {
  it("shows the empty state when there are no pending invitations", () => {
    render(<InvitesPanel />);
    expect(screen.getByText(/no pending invitations/i)).toBeInTheDocument();
  });

  it("names each invitation from its own payload and routes Accept/Decline to the right id", async () => {
    const user = userEvent.setup();
    // TWO invites, so a bug that always routes to rows[0] would mis-route the SECOND card's actions.
    state.invites = { data: [invite("inv-9", "Platform"), invite("inv-7", "Runtime")], isLoading: false, isError: false };
    render(<InvitesPanel />);

    const runtimeCard = screen.getByText("Runtime in Tideline").closest("li")!;
    expect(within(runtimeCard).getByText(/From Ada Lovelace · Member/)).toBeInTheDocument();
    await user.click(within(runtimeCard).getByRole("button", { name: /accept/i }));
    expect(state.accept.mutate).toHaveBeenCalledWith("inv-7", expect.anything());
    await user.click(within(runtimeCard).getByRole("button", { name: /decline/i }));
    expect(state.reject.mutate).toHaveBeenCalledWith("inv-7", expect.anything());

    const platformCard = screen.getByText("Platform in Tideline").closest("li")!;
    await user.click(within(platformCard).getByRole("button", { name: /accept/i }));
    expect(state.accept.mutate).toHaveBeenLastCalledWith("inv-9", expect.anything());
  });

  it("an invitation into another organization shows why it can't be accepted, and can be declined", async () => {
    const user = userEvent.setup();
    state.invites = {
      data: [invite("inv-x", "Research", { org_name: "Other Co", refusal: REFUSAL })],
      isLoading: false,
      isError: false,
    };
    render(<InvitesPanel />);

    const card = screen.getByText("Research in Other Co").closest("li")!;
    expect(within(card).getByText(REFUSAL.message)).toBeInTheDocument();
    // No Accept the server would refuse; Decline clears it for the sender.
    expect(within(card).queryByRole("button", { name: /accept/i })).toBeNull();
    await user.click(within(card).getByRole("button", { name: /decline/i }));
    expect(state.reject.mutate).toHaveBeenCalledWith("inv-x", expect.anything());
  });

  it("a refused accept reports the server's reason, as an error", async () => {
    const user = userEvent.setup();
    const reason = "The account that issued this invitation can no longer grant access";
    state.accept.mutate = vi.fn((_id: string, opts: { onError: (e: unknown) => void }) => opts.onError(new ApiError(403, { error: { code: "invitation_refused", message: reason } })));
    state.invites = { data: [invite("inv-1", "Platform")], isLoading: false, isError: false };
    const resolved: Array<[string, boolean]> = [];
    render(<InvitesPanel onResolved={(text, ok) => resolved.push([text, ok])} />);

    await user.click(screen.getByRole("button", { name: /accept/i }));
    expect(resolved).toEqual([[reason, false]]);
  });

  it("an invitation to the org itself names one place", () => {
    state.invites = { data: [invite("inv-r", "Tideline")], isLoading: false, isError: false };
    render(<InvitesPanel />);
    expect(screen.getByText("Tideline")).toBeInTheDocument();
    expect(screen.queryByText(/Tideline in Tideline/)).toBeNull();
  });
});
