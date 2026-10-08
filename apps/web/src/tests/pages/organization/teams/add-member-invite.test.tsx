// The Teams page has ONE way to bring someone onto a team: the Add member dialog. It searches the
// org directory, and when the typed text is an email nobody in the org holds, the same dialog offers
// the email invitation instead. These tests drive the REAL route table (AppContent) with only
// `fetch` stubbed, so they fail if the dialog ever calls the invitation route for an address that
// belongs to an org member (which the backend would auto-accept, silently, with no email), if it
// offers a second invitation to an address already pending, or if the separate "Invite by email"
// surface comes back.

import { MemoryRouter } from "react-router-dom";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AppContent } from "@/App";
import { queryClient } from "@/api/queryClient";
import { resetTeamsStore } from "@/pages/organization/teams/data/provider";

const VIEWER = {
  id: "viewer",
  email: "vera@x.io",
  first_name: "Vera",
  last_name: "Ng",
  display_name: "Vera Ng",
  email_verified_at: "2026-01-01T00:00:00Z",
  email_verification_required: false,
  email_verification_deadline: null,
  has_password: true,
  // The org admin: admin of the root, and by descent of every team.
  admin_team_ids: ["root", "platform"],
};

const TEAMS = [
  { id: "root", name: "Tideline", parent_team_id: null, is_root: true, created_at: "2026-01-01T00:00:00Z", member_count: 3 },
  { id: "platform", name: "Platform", parent_team_id: "root", is_root: false, created_at: "2026-01-01T00:00:00Z", member_count: 1 },
];

const memberRow = (user: string, teamId: string, teamName: string, role = "member") => ({
  user_id: user,
  display_name: user[0].toUpperCase() + user.slice(1),
  email: `${user}@x.io`,
  first_name: user,
  last_name: "X",
  role,
  team_id: teamId,
  team_name: teamName,
  created_at: "2026-02-01T00:00:00Z",
  role_display: role === "admin" ? "Admin" : "Member",
  effective_role: role,
  direct_role: role,
  descent_role: null,
  descent_from_team_id: null,
  descent_from_team_name: null,
});

// Everyone in the org lands on the root roster (membership materializes up), which IS the directory
// the dialog searches. Marcus is already on Platform; Dana is in the org but not on Platform.
const ROSTERS: Record<string, unknown[]> = {
  root: [memberRow("viewer", "root", "Tideline", "admin"), memberRow("dana", "root", "Tideline"), memberRow("marcus", "platform", "Platform")],
  platform: [memberRow("marcus", "platform", "Platform")],
};

const pendingInvite = (email: string) => ({
  id: `inv-${email}`,
  team_id: "platform",
  email,
  role: "member",
  status: "pending",
  expires_at: "2026-10-01T00:00:00Z",
  created_at: "2026-09-01T00:00:00Z",
  resolved_at: null,
  invited_by_id: "viewer",
  role_display: "Member",
});

const DASH = { user: VIEWER, org: TEAMS[0], teams: TEAMS, pending_invitations: [], is_org_admin: false };
const CREDITS = { tier_key: "pro", tier_name: "Pro", pct_used: 10, reset_at: null, prepaid_credits: 0 };

const json = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

/** Pending invitations the team's invitations endpoint answers with. */
let pending: unknown[] = [];
/** When set, the invitation POST refuses with this 409 — the cross-org case the client cannot know. */
let inviteConflict: string | null = null;

function route(req: Request): Response {
  const p = new URL(req.url).pathname;
  if (p === "/api/v1/auth/me") return json(VIEWER);
  if (p === "/api/v1/teams") return json(TEAMS);
  if (p === "/api/v1/dashboard") return json(DASH);
  if (p === "/api/v1/me/credits") return json(CREDITS);
  if (p === "/api/v1/invitations/me") return json([]);
  // The connections plate's reads. An unserved query here would 404, retry with backoff, and hold
  // the invalidate-all that every mutation awaits — a hang, not a failure, in every assertion below.
  if (p === "/api/v1/plugins/connection-forms") return json([]);
  if (/^\/api\/v1\/teams\/[^/]+\/connections$/.test(p)) return json([]);
  const members = p.match(/^\/api\/v1\/teams\/([^/]+)\/members$/);
  if (members) return json(ROSTERS[members[1]] ?? []);
  const memberships = p.match(/^\/api\/v1\/teams\/([^/]+)\/memberships$/);
  if (memberships && req.method === "POST") return json(memberRow("dana", memberships[1], "Platform"), 201);
  const invitations = p.match(/^\/api\/v1\/teams\/([^/]+)\/invitations$/);
  if (invitations) {
    if (req.method === "POST") {
      if (inviteConflict) return json({ detail: inviteConflict }, 409);
      return json(pendingInvite("newbie@x.io"), 201);
    }
    return json(pending);
  }
  return json({ detail: `unmatched ${p}` }, 404);
}

interface Sent {
  method: string;
  path: string;
  body: unknown;
}

let fetchSpy: ReturnType<typeof vi.fn>;
let sent: Sent[];

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});
beforeEach(() => {
  queryClient.clear();
  resetTeamsStore();
  pending = [];
  inviteConflict = null;
  sent = [];
  fetchSpy = vi.fn(async (input: Request | string, init?: RequestInit) => {
    const req = input instanceof Request ? input : new Request(input, init);
    if (req.method !== "GET") {
      sent.push({ method: req.method, path: new URL(req.url).pathname, body: await req.clone().json().catch(() => null) });
    }
    return route(req);
  });
  vi.stubGlobal("fetch", fetchSpy);
});

const posts = (suffix: string): Sent[] => sent.filter((s) => s.method === "POST" && s.path.endsWith(suffix));

/** Reads of a team's invitation list — the pending list the page shows. */
const inviteReads = (): Request[] =>
  fetchSpy.mock.calls
    .map((c) => c[0] as Request)
    .filter((r) => r.method === "GET" && new URL(r.url).pathname === "/api/v1/teams/platform/invitations");

const renderTeams = () =>
  render(
    <MemoryRouter initialEntries={["/teams/platform"]}>
      <AppContent />
    </MemoryRouter>,
  );

/** Open the one consolidated dialog from the team masthead. */
async function openDialog(user: ReturnType<typeof userEvent.setup>): Promise<HTMLElement> {
  renderTeams();
  await screen.findByRole("heading", { name: "Platform" }, { timeout: 5000 });
  await user.click(await screen.findByRole("button", { name: "Add member" }));
  const dialog = await screen.findByRole("dialog", { name: /add member to platform/i });
  // Wait for the org directory to land — until it does the list is the "loading people" state, and
  // what the dialog makes of the typed text would be read from an empty directory. Then wait for the
  // dialog's own focus to settle on the search field: typing before the focus trap has run would put
  // the keystrokes wherever focus happened to be.
  await within(dialog).findByText("Dana", {}, { timeout: 5000 });
  await waitFor(() => expect(within(dialog).getByRole("searchbox")).toHaveFocus());
  return dialog;
}

const searchBox = (dialog: HTMLElement) => within(dialog).getByRole("searchbox");

describe("Add member: the org directory and the email invitation in one dialog", () => {
  it("picking a directory match adds the member — and sends no invitation", async () => {
    const user = userEvent.setup();
    const dialog = await openDialog(user);

    await user.type(searchBox(dialog), "dana");
    await user.click(await within(dialog).findByRole("button", { name: /dana/i }, { timeout: 5000 }));
    await user.click(within(dialog).getByRole("button", { name: "Add member" }));

    await waitFor(() => expect(posts("/memberships")).toHaveLength(1));
    expect(posts("/memberships")[0].path).toBe("/api/v1/teams/platform/memberships");
    expect(posts("/memberships")[0].body).toMatchObject({ user_id: "dana", role: "member" });
    // The whole point: a person who is already in the org is never invited by email.
    expect(posts("/invitations")).toHaveLength(0);
  });

  it("a member's own email finds their row, never the invite offer", async () => {
    const user = userEvent.setup();
    const dialog = await openDialog(user);

    await user.type(searchBox(dialog), "DANA@x.io");

    expect(await within(dialog).findByText("Dana", {}, { timeout: 5000 })).toBeInTheDocument();
    expect(within(dialog).queryByText(/isn’t in your organization yet/i)).not.toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "Send invite" })).not.toBeInTheDocument();
  });

  it("an unknown address becomes the invitation offer, and sends {email, role}", async () => {
    const user = userEvent.setup();
    const dialog = await openDialog(user);

    await user.type(searchBox(dialog), "newbie@x.io");

    // The offer names the address, the team, and the role that will be granted on acceptance.
    const offer = await within(dialog).findByText(/isn’t in your organization yet/i, {}, { timeout: 5000 });
    expect(offer).toHaveTextContent(/newbie@x\.io/);
    expect(within(dialog).getByText(/they’ll join Platform as a Member/i)).toBeInTheDocument();

    // The role chooser drives the invitation, so the offer must echo the CHOSEN role.
    await user.click(within(dialog).getByRole("radio", { name: /admin/i }));
    expect(within(dialog).getByText(/they’ll join Platform as an Admin/i)).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: "Send invite" }));

    await waitFor(() => expect(posts("/invitations")).toHaveLength(1));
    expect(posts("/invitations")[0].path).toBe("/api/v1/teams/platform/invitations");
    expect(posts("/invitations")[0].body).toEqual({ email: "newbie@x.io", role: "admin" });
    // No membership was written — the address has no account to add.
    expect(posts("/memberships")).toHaveLength(0);

    // The send is confirmed, the dialog closes, and the team's pending list re-reads.
    expect(await screen.findByText("Invitation sent to newbie@x.io.", {}, { timeout: 5000 })).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("dialog", { name: /add member to platform/i })).not.toBeInTheDocument());
    const readsBefore = inviteReads().length;
    await waitFor(() => expect(inviteReads().length).toBeGreaterThan(1));
    expect(readsBefore).toBeGreaterThan(0);
  });

  it("an address already invited is refused inline, with nothing sent", async () => {
    pending = [pendingInvite("waiting@x.io")];
    const user = userEvent.setup();
    const dialog = await openDialog(user);

    // Case-insensitive: the backend normalizes the address, so the dialog must too.
    await user.type(searchBox(dialog), "WAITING@x.io");

    expect(await within(dialog).findByText(/already invited, pending/i, {}, { timeout: 5000 })).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "Send invite" })).not.toBeInTheDocument();
    expect(posts("/invitations")).toHaveLength(0);
  });

  it.each([
    ["no @ at all", "testuser"],
    ["no dot in the domain", "testuser@test"],
    ["nothing before the @", "@test.com"],
  ])("a partial address (%s) offers no invite and asks for a whole one", async (_label, typed) => {
    const user = userEvent.setup();
    const dialog = await openDialog(user);

    await user.type(searchBox(dialog), typed);

    expect(
      await within(dialog).findByText(
        "Enter a full email address to invite someone.",
        {},
        { timeout: 5000 },
      ),
    ).toBeInTheDocument();
    expect(within(dialog).queryByText(/isn’t in your organization yet/i)).not.toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "Send invite" })).not.toBeInTheDocument();
  });

  it("a refusal replaces the offer instead of stacking on top of it", async () => {
    // The dead-end loop this fixes: the 409 appeared, the dialog went on promising "they'll join
    // Platform as a Member when they accept", and Send invite stayed enabled — so the only move
    // left was to press it again for the same refusal.
    inviteConflict = "A pending invitation for this email and team already exists";
    const user = userEvent.setup();
    const dialog = await openDialog(user);

    await user.type(searchBox(dialog), "elsewhere@other.io");
    await user.click(await within(dialog).findByRole("button", { name: "Send invite" }, { timeout: 5000 }));

    // The server's own sentence, verbatim — the client cannot know why it was refused.
    expect(
      await within(dialog).findByText(
        "A pending invitation for this email and team already exists",
        {},
        { timeout: 5000 },
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: /add member to platform/i })).toBeInTheDocument();
    expect(screen.queryByText(/invitation sent to/i)).not.toBeInTheDocument();
    // No affordance that repeats the refused action, and no claim it contradicts.
    expect(within(dialog).queryByRole("button", { name: "Send invite" })).not.toBeInTheDocument();
    expect(within(dialog).queryByText(/they’ll join Platform as/i)).not.toBeInTheDocument();
  });

  it("editing the address offers the invite again", async () => {
    inviteConflict = "A pending invitation for this email and team already exists";
    const user = userEvent.setup();
    const dialog = await openDialog(user);

    await user.type(searchBox(dialog), "elsewhere@other.io");
    await user.click(await within(dialog).findByRole("button", { name: "Send invite" }, { timeout: 5000 }));
    await within(dialog).findByText(
      "A pending invitation for this email and team already exists",
      {},
      { timeout: 5000 },
    );

    await user.type(searchBox(dialog), "x");
    expect(
      await within(dialog).findByRole("button", { name: "Send invite" }, { timeout: 5000 }),
    ).toBeInTheDocument();
  });

  it("the separate invite-by-email surface is gone", async () => {
    const user = userEvent.setup();
    renderTeams();
    await screen.findByRole("heading", { name: "Platform" }, { timeout: 5000 });
    // The pending-invitations plate renders (so this isn't passing on an unrendered page)...
    expect(await screen.findByText(/pending invitations/i)).toBeInTheDocument();
    // ...and carries no invite entry point of its own.
    expect(screen.queryByRole("button", { name: /invite by email/i })).not.toBeInTheDocument();

    await user.click(await screen.findByRole("button", { name: "Add member" }));
    expect(await screen.findByRole("dialog", { name: /add member to platform/i })).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: /invite to platform/i })).not.toBeInTheDocument();
  });
});
