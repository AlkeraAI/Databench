import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { enterOrg, forgetActiveOrg, setOrgNavigator } from "@/api/activeOrg";
import { GENERIC_FAILURE } from "@/api/errors";
import { createQueryClient } from "@/api/queryClient";
import { setSessionChannelFactory } from "@/api/sessionChannel";
import { LoginPage } from "@/pages/auth/LoginPage";
import { SignupPage } from "@/pages/auth/SignupPage";
import { AuthActionsProvider, type AuthActions } from "@/pages/auth/auth-actions";
import { RAW_FAILURES, expectNoRawFailureText } from "@/tests/fixtures/rawFailures";

// An invitation link opened by someone already signed in, through the real hooks with only
// `fetch` stubbed: Accept calls the by-token route, an acceptance into another org offers the
// switch into exactly that org, and every refusal the route answers is said, with the step it
// asks for. The same panel answers on the sign-in page when it carries the invitation.

const ORG_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const ORG_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

const PREVIEW = {
  email: "vera@x.io",
  team_id: "t-1",
  team_name: "Data",
  org_team_id: ORG_B,
  org_name: "Beta Labs",
  role: "member",
  inviter_display_name: null,
  expires_at: "2026-12-01T00:00:00Z",
};

const ME = {
  id: "u-1",
  email: "Vera@x.io",
  first_name: "Vera",
  last_name: "Ng",
  org_team_id: ORG_A,
  org_name: "Acme",
  admin_team_ids: [],
};

const json = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const refusal = (status: number, code: string, message: string) =>
  json({ error: { code, message, trace_id: "t" } }, status);

let navigated: string[];
let restore: (url: string) => void;
let sent: Request[];
let acceptAnswer: () => Response;
let me: () => Response;
let multiOrgEnabled: boolean;

function serve() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: Request) => {
      sent.push(input.clone());
      const path = new URL(input.url).pathname;
      if (path === "/api/v1/auth/me") return me();
      if (path === "/api/v1/config") return json({ self_hosted: false, multi_org_enabled: multiOrgEnabled });
      if (path === "/api/v1/invitations/by-token/tok-1/accept") return acceptAnswer();
      if (path === "/api/v1/invitations/by-token/tok-1") return json(PREVIEW);
      if (path === "/api/v1/auth/logout") return json({ message: "ok" });
      if (path === "/api/v1/auth/refresh/org") {
        return json({ user: { ...ME, org_team_id: ORG_B }, expires_at: new Date(Date.now() + 600_000).toISOString() });
      }
      return json({ providers: [] });
    }),
  );
}

const noActions: AuthActions = {
  login: async () => {},
  signup: async () => {},
  oauthRegister: async () => {},
  completeProfile: async () => {},
  requestPasswordReset: async () => {},
  resetPassword: async () => {},
};

function Where() {
  const location = useLocation();
  return <p data-testid="where">{location.pathname + location.search}</p>;
}

function renderAt(path: string) {
  return render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter initialEntries={[path]}>
        <AuthActionsProvider actions={noActions}>
          <Routes>
            <Route path="/signup" element={<SignupPage />} />
            <Route path="/login" element={<LoginPage />} />
            <Route path="*" element={<Where />} />
          </Routes>
        </AuthActionsProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const posted = (path: string) =>
  sent.filter((r) => r.method === "POST" && new URL(r.url).pathname === path);

beforeEach(() => {
  navigated = [];
  sent = [];
  me = () => json(ME);
  multiOrgEnabled = true;
  acceptAnswer = () =>
    json({ invitation: {}, joined_team_ids: ["t-1", ORG_B], org_team_id: ORG_B, org_name: "Beta Labs" });
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

describe.each([
  ["the invitation landing", "/signup?invite=tok-1"],
  ["the sign-in page carrying it", "/login?invite=tok-1"],
])("a signed-in invitee on %s", (_label, path) => {
  it("accepts, then switches into the org the invitation joined", async () => {
    renderAt(path);
    fireEvent.click(await screen.findByRole("button", { name: "Accept invitation" }));
    expect(await screen.findByText("Joined Beta Labs.")).toBeInTheDocument();
    expect(posted("/api/v1/invitations/by-token/tok-1/accept")).toHaveLength(1);

    fireEvent.click(screen.getByRole("button", { name: "Switch to Beta Labs" }));
    await waitFor(() => expect(navigated).toEqual(["/"]));
    const [switchRequest] = posted("/api/v1/auth/refresh/org");
    expect(await switchRequest.json()).toEqual({ org_team_id: ORG_B });
  });
});

describe("a signed-in invitee", () => {
  it("offers no switch when the invitation joined the org already open", async () => {
    acceptAnswer = () =>
      json({ invitation: {}, joined_team_ids: ["t-1", ORG_A], org_team_id: ORG_A, org_name: "Acme" });
    renderAt("/signup?invite=tok-1");
    fireEvent.click(await screen.findByRole("button", { name: "Accept invitation" }));
    expect(await screen.findByText("Joined Acme.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Switch to (Acme|Beta Labs)$/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByTestId("where")).toHaveTextContent(/^\/$/);
    expect(posted("/api/v1/auth/refresh/org")).toHaveLength(0);
  });

  it.each([
    ["invitation_accepted", "This invitation has already been accepted."],
    ["invitation_declined", "This invitation was declined."],
    ["invitation_revoked", "This invitation was withdrawn."],
    ["invitation_expired", "This invitation has expired."],
  ])("says a closed link was %s, and joins nothing", async (code, message) => {
    acceptAnswer = () => refusal(410, code, message);
    renderAt("/signup?invite=tok-1");
    fireEvent.click(await screen.findByRole("button", { name: "Accept invitation" }));
    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Switch to (Acme|Beta Labs)$/ })).toBeNull();
  });

  it.each(RAW_FAILURES)("says %s in the general sentence, never in its own words", async (_label, answer) => {
    acceptAnswer = answer;
    renderAt("/signup?invite=tok-1");
    fireEvent.click(await screen.findByRole("button", { name: "Accept invitation" }));
    expect(await screen.findByText(GENERIC_FAILURE)).toBeInTheDocument();
    expectNoRawFailureText();
    // The invitee can try again from the same place.
    expect(screen.getByRole("button", { name: "Accept invitation" })).toBeInTheDocument();
  });

  it("says a deactivated membership refuses it", async () => {
    acceptAnswer = () =>
      refusal(409, "membership_deactivated", "Your membership in this organization is deactivated.");
    renderAt("/signup?invite=tok-1");
    fireEvent.click(await screen.findByRole("button", { name: "Accept invitation" }));
    expect(await screen.findByText("Your membership in this organization is deactivated.")).toBeInTheDocument();
  });

  it("shows the server's sentence for another account, and signs out back to sign-in with the link", async () => {
    const message = "This invitation is for v***@x.io. Sign in with that email to accept.";
    acceptAnswer = () => refusal(409, "invitation_other_account", message);
    renderAt("/signup?invite=tok-1");
    fireEvent.click(await screen.findByRole("button", { name: "Accept invitation" }));
    expect(await screen.findByText(message)).toBeInTheDocument();
    me = () => refusal(401, "unauthorized", "not authenticated");
    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
    await waitFor(() => expect(posted("/api/v1/auth/logout")).toHaveLength(1));
    // The sign-in page carries the invitation, so the right account comes back to it.
    const signup = await screen.findByRole("link", { name: "Create an account" });
    expect(signup.getAttribute("href")).toBe("/signup?invite=tok-1");
    expect(screen.getByRole("button", { name: "Sign in" })).toBeInTheDocument();
  });

  it("sends an unverified account to verify its email", async () => {
    acceptAnswer = () =>
      refusal(403, "email_verification_required", "Verify your email address to perform this action.");
    renderAt("/signup?invite=tok-1");
    fireEvent.click(await screen.findByRole("button", { name: "Accept invitation" }));
    fireEvent.click(await screen.findByRole("button", { name: "Verify email" }));
    expect(await screen.findByTestId("where")).toHaveTextContent(/^\/verify-email$/);
  });

  it("says an invitation the server does not know", async () => {
    acceptAnswer = () => refusal(404, "not_found", "Invitation not found");
    renderAt("/signup?invite=tok-1");
    fireEvent.click(await screen.findByRole("button", { name: "Accept invitation" }));
    expect(await screen.findByText("Invitation not found")).toBeInTheDocument();
  });
});

describe("a signed-in invitee with several orgs per person off", () => {
  it.each([
    ["the invitation landing", "/signup?invite=tok-1"],
    ["the sign-in page carrying it", "/login?invite=tok-1"],
  ])("on %s opens their invitations, as before, and accepts nothing here", async (_label, path) => {
    multiOrgEnabled = false;
    renderAt(path);
    fireEvent.click(await screen.findByRole("button", { name: "Open your invitations" }));
    expect(await screen.findByTestId("where")).toHaveTextContent("/teams?tab=invites");
    expect(screen.queryByRole("button", { name: "Accept invitation" })).toBeNull();
    expect(posted("/api/v1/invitations/by-token/tok-1/accept")).toHaveLength(0);
  });
});

describe("the sign-in page carrying an invitation, signed out", () => {
  it("shows the sign-in form, never the accept panel", async () => {
    me = () => refusal(401, "unauthorized", "not authenticated");
    renderAt("/login?invite=tok-1");
    expect(await screen.findByRole("button", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Accept invitation" })).toBeNull();
    expect(posted("/api/v1/invitations/by-token/tok-1/accept")).toHaveLength(0);
  });
});
