import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SignupPage } from "@/pages/auth/SignupPage";
import {
  AuthActionsProvider,
  AuthError,
  type AuthActions,
  type OAuthRegisterInput,
  type SignupInput,
} from "@/pages/auth/auth-actions";
import { CARRIED_AUTH_PARAMS } from "@/app/extensions/portal";

// A parameter an extension carries between sign-in and sign-up, registered the way one
// does. The open pages carry none of their own.
CARRIED_AUTH_PARAMS.register({ key: "via", landing: (via) => `/welcome?via=${encodeURIComponent(via)}` });

// The signup page against the auth-actions SEAM (a recording stub), with `fetch`
// stubbed for its two queries: the OAuth-providers list (empty → the federated
// buttons hide) and, per test, the invitation preview / OAuth register context.

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

/** URL-dispatching fetch stub; unmatched paths answer an empty providers list so the
 *  OAuth buttons stay hidden, and `/auth/me` answers 401 (signed out) unless a test routes it.
 *  openapi-fetch passes a Request, so match on `req.url`. */
function stubFetch(routes: Record<string, Response> = {}) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (req: Request) => {
      for (const [path, response] of Object.entries(routes)) {
        if (req.url.includes(path)) return response.clone();
      }
      if (req.url.includes("/auth/me")) return json(401, { detail: "Not authenticated" });
      return json(200, { providers: [] });
    }),
  );
}

beforeEach(() => stubFetch());
afterEach(() => vi.unstubAllGlobals());

function newQueryClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

// The minimal signup: only email + password. Name + organization moved to the
// complete-profile step, so this form must NOT render those fields, and it submits just the
// credentials (plus the invite token when present) through the auth-actions seam.

function stubActions(overrides: Partial<AuthActions> = {}): AuthActions {
  return {
    login: async () => {},
    signup: async () => {},
    oauthRegister: async () => {},
    completeProfile: async () => {},
    requestPasswordReset: async () => {},
    resetPassword: async () => {},
    ...overrides,
  };
}

function renderSignup(path: string, actions: AuthActions = stubActions()) {
  render(
    <QueryClientProvider client={newQueryClient()}>
      <MemoryRouter initialEntries={[path]}>
        <AuthActionsProvider actions={actions}>
          <Routes>
            <Route path="/signup" element={<SignupPage />} />
          </Routes>
        </AuthActionsProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** Fill and submit the email/password form. */
function submitCredentials(email = "ada@company.com") {
  fireEvent.change(screen.getByLabelText(/email/i), { target: { value: email } });
  fireEvent.change(screen.getByLabelText(/^password/i), { target: { value: "a-good-password" } });
  fireEvent.click(screen.getByRole("button", { name: /create account/i }));
}

describe("SignupPage", () => {
  it("renders only email + password — no name or organization fields", () => {
    renderSignup("/signup");
    expect(screen.getByLabelText(/email/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/^password/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/first name/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/last name/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/organization/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/confirm/i)).not.toBeInTheDocument();
  });

  it("submits just the credentials for a new-org signup", async () => {
    const calls: SignupInput[] = [];
    renderSignup("/signup", stubActions({ signup: async (input) => void calls.push(input) }));

    submitCredentials();

    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0]).toEqual({
      email: "ada@company.com",
      password: "a-good-password",
      inviteToken: undefined,
      allowPersonalEmail: false,
    });
  });

  it("blocks the signup when the credentials are empty/invalid", async () => {
    // Clicking with an empty email + password must NOT call the seam — the
    // client-side validation gate has to hold before any network attempt.
    const calls: SignupInput[] = [];
    renderSignup("/signup", stubActions({ signup: async (input) => void calls.push(input) }));

    fireEvent.click(screen.getByRole("button", { name: /create account/i }));

    // Give any (incorrectly) fired async submit a chance to land, then assert none did.
    await Promise.resolve();
    expect(calls).toHaveLength(0);
  });

  it("carries the invite token from the URL into the signup", async () => {
    const calls: SignupInput[] = [];
    renderSignup("/signup?invite=tok-123", stubActions({ signup: async (input) => void calls.push(input) }));

    fireEvent.change(screen.getByLabelText(/email/i), { target: { value: "mem@company.com" } });
    fireEvent.change(screen.getByLabelText(/^password/i), { target: { value: "a-good-password" } });
    fireEvent.click(screen.getByRole("button", { name: /accept and create account/i }));

    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].inviteToken).toBe("tok-123");
  });

  it("keeps a carried parameter on the sign-in cross-link", () => {
    renderSignup("/signup?via=team");
    expect(screen.getByRole("link", { name: /sign in/i })).toHaveAttribute("href", "/login?via=team");
  });
});

describe("SignupPage — personal-email gate", () => {
  it("submits the gated default (allowPersonalEmail: false) without the opt-in param", async () => {
    const calls: SignupInput[] = [];
    renderSignup("/signup", stubActions({ signup: async (input) => void calls.push(input) }));

    submitCredentials("ada@gmail.com");

    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].allowPersonalEmail).toBe(false);
  });

  it("honors ?allow_personal=1 (the business-email page's continue-anyway hand-off)", async () => {
    const calls: SignupInput[] = [];
    renderSignup("/signup?allow_personal=1", stubActions({ signup: async (input) => void calls.push(input) }));

    submitCredentials("ada@gmail.com");

    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].allowPersonalEmail).toBe(true);
  });

  it("offers — and resubmits — the explicit opt-in after the backend blocks a personal email", async () => {
    // First attempt: the backend's business-email gate refuses. The page must surface
    // an explicit "continue anyway" that resubmits the SAME credentials, opted in.
    const calls: SignupInput[] = [];
    const actions = stubActions({
      signup: async (input) => {
        calls.push(input);
        if (!input.allowPersonalEmail) {
          throw new AuthError("Please sign up with your work email.", "personal_email_blocked");
        }
      },
    });
    renderSignup("/signup", actions);

    submitCredentials("ada@gmail.com");

    const anyway = await screen.findByRole("button", { name: /continue with this email anyway/i });
    expect(screen.getByText(/please sign up with your work email/i)).toBeInTheDocument();
    fireEvent.click(anyway);

    await waitFor(() => expect(calls).toHaveLength(2));
    expect(calls[1]).toMatchObject({ email: "ada@gmail.com", allowPersonalEmail: true });
  });
});

describe("SignupPage — invitation preview", () => {
  const PREVIEW = {
    email: "invited@acme.test",
    team_id: "t-1",
    team_name: "Data Platform",
    org_team_id: "o-1",
    org_name: "Acme Data",
    role: "member",
    inviter_display_name: null,
    expires_at: "2026-12-01T00:00:00Z",
  };

  it("names the inviting team and org from ?invite=<token>", async () => {
    stubFetch({ "/invitations/by-token/tok-123": json(200, PREVIEW) });
    renderSignup("/signup?invite=tok-123");

    expect(await screen.findByText("Data Platform")).toBeInTheDocument();
    expect(screen.getByText("Acme Data")).toBeInTheDocument();
  });

  it("calls the destination an organization, as the email does, and says it once", async () => {
    stubFetch({ "/invitations/by-token/tok-123": json(200, PREVIEW) });
    renderSignup("/signup?invite=tok-123");

    expect(await screen.findByText("Data Platform")).toBeInTheDocument();
    expect(screen.getByText("Set a password to join your organization.")).toBeInTheDocument();
    const page = document.body.textContent ?? "";
    expect(page).not.toMatch(/\bteam\b/i);
    // The lede asks for the password; nothing else on the page asks again.
    expect(page.match(/Set a password/g)).toHaveLength(1);
  });

  it("flags an unknown invitation and offers no form to submit", async () => {
    stubFetch({ "/invitations/by-token/tok-dead": json(404, { detail: "Invitation not found" }) });
    const calls: SignupInput[] = [];
    renderSignup("/signup?invite=tok-dead", stubActions({ signup: async (input) => void calls.push(input) }));

    expect(await screen.findByText(/invalid or expired/i)).toBeInTheDocument();
    // A dead link leaves nothing live under the refusal: no fields, no submit.
    expect(screen.queryByRole("button", { name: /accept and create account/i })).toBeNull();
    expect(screen.queryByLabelText(/^password/i)).toBeNull();
    expect(calls).toHaveLength(0);
  });

  it("says an accepted invitation was accepted, and signing in lands home", async () => {
    stubFetch({
      "/invitations/by-token/tok-used": json(410, {
        detail: { code: "invitation_accepted", message: "This invitation has already been accepted. Sign in to continue." },
      }),
    });
    renderSignup("/signup?invite=tok-used");

    expect(await screen.findByText(/already been accepted/i)).toBeInTheDocument();
    expect(screen.queryByText(/invalid or expired/i)).toBeNull();
    expect(screen.getByRole("link", { name: "Sign in" }).getAttribute("href")).toBe("/login");
    expect(screen.queryByLabelText(/^password/i)).toBeNull();
  });

  it("sends an invited reader with an account to sign in and land on their invitations", async () => {
    stubFetch({ "/invitations/by-token/tok-123": json(200, PREVIEW) });
    renderSignup("/signup?invite=tok-123");

    await screen.findByText("Data Platform");
    const signIn = screen.getByRole("link", { name: "Sign in" });
    expect(signIn.getAttribute("href")).toBe(`/login?return_to=${encodeURIComponent("/teams?tab=invites")}`);
  });

  it("sends an invited reader to sign in carrying the invitation where several orgs per person is on", async () => {
    stubFetch({
      "/invitations/by-token/tok-123": json(200, PREVIEW),
      "/api/v1/config": json(200, { self_hosted: false, multi_org_enabled: true }),
    });
    renderSignup("/signup?invite=tok-123");

    await screen.findByText("Data Platform");
    await waitFor(() =>
      expect(screen.getByRole("link", { name: "Sign in" }).getAttribute("href")).toBe("/login?invite=tok-123"),
    );
  });

  it("answers a signed-in invitee with their invitations, never a create-account form", async () => {
    stubFetch({
      "/invitations/by-token/tok-123": json(200, PREVIEW),
      "/auth/me": json(200, { id: "u1", email: "Invited@acme.test", admin_team_ids: [] }),
    });
    render(
      <QueryClientProvider client={newQueryClient()}>
        <MemoryRouter initialEntries={["/signup?invite=tok-123"]}>
          <AuthActionsProvider actions={stubActions()}>
            <Routes>
              <Route path="/signup" element={<SignupPage />} />
              <Route path="/teams" element={<p>invitations list</p>} />
            </Routes>
          </AuthActionsProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    fireEvent.click(await screen.findByRole("button", { name: "Open your invitations" }));
    expect(await screen.findByText("invitations list")).toBeInTheDocument();
  });

  it("tells a reader signed in as someone else whose invitation it is", async () => {
    stubFetch({
      "/invitations/by-token/tok-123": json(200, PREVIEW),
      "/auth/me": json(200, { id: "u2", email: "other@acme.test", admin_team_ids: [] }),
    });
    renderSignup("/signup?invite=tok-123");

    expect(await screen.findByText(/This invitation is for invited@acme.test/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign out" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /accept and create account/i })).toBeNull();
    expect(screen.queryByRole("button", { name: "Open your invitations" })).toBeNull();
  });

  // The signup route compares the submitted address against the invited one and
  // refuses a mismatch, so the invitee must not be asked to reproduce it.
  it("fills the email in from the invitation and won't let it be changed", async () => {
    stubFetch({ "/invitations/by-token/tok-123": json(200, PREVIEW) });
    const calls: SignupInput[] = [];
    renderSignup("/signup?invite=tok-123", stubActions({ signup: async (input) => void calls.push(input) }));

    const field = screen.getByLabelText(/email/i) as HTMLInputElement;
    await waitFor(() => expect(field.value).toBe("invited@acme.test"));
    expect(field).toBeDisabled();

    // Even if something drives the field, the invited address is what's sent.
    fireEvent.change(field, { target: { value: "someone.else@acme.test" } });
    fireEvent.change(screen.getByLabelText(/^password/i), { target: { value: "a-good-password" } });
    fireEvent.click(screen.getByRole("button", { name: /accept and create account/i }));

    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].email).toBe("invited@acme.test");
  });

  it("names one destination when the invitation is to the org root", async () => {
    stubFetch({
      "/invitations/by-token/tok-root": json(200, {
        ...PREVIEW,
        team_id: "o-1",
        team_name: "Acme Data",
      }),
    });
    renderSignup("/signup?invite=tok-root");

    const callout = await screen.findByText(/Joining/);
    expect(callout.textContent).toContain("Joining Acme Data.");
    expect(callout.textContent).not.toContain("Acme Data in Acme Data");
  });

  it("names one destination when the team and the org share a name", async () => {
    stubFetch({
      "/invitations/by-token/tok-same": json(200, { ...PREVIEW, team_name: "Acme Data" }),
    });
    renderSignup("/signup?invite=tok-same");

    const callout = await screen.findByText(/Joining/);
    expect(callout.textContent).toContain("Joining Acme Data.");
    expect(callout.textContent).not.toContain("in Acme Data");
  });
});

describe("SignupPage — OAuth register mode (?oauth_ticket=)", () => {
  const CTX = {
    provider: "google",
    email: "ada@gmail.com",
    first_name: "Ada",
    last_name: "Lovelace",
    has_invite: false,
  };

  it("prefills the provider identity, hides the password, and registers through the seam", async () => {
    stubFetch({ "/auth/oauth/register/context": json(200, CTX) });
    const calls: OAuthRegisterInput[] = [];
    renderSignup(
      "/signup?oauth_ticket=t-1",
      stubActions({ oauthRegister: async (input) => void calls.push(input) }),
    );

    // Provider-verified email shown but not editable; no password or captcha path.
    const email = await screen.findByLabelText(/email/i);
    await waitFor(() => expect(email).toHaveValue("ada@gmail.com"));
    expect(email).toBeDisabled();
    expect(screen.queryByLabelText(/password/i)).not.toBeInTheDocument();

    // Name prefilled from the ticket; org defaulted from the first name, editable.
    expect(screen.getByLabelText(/first name/i)).toHaveValue("Ada");
    expect(screen.getByLabelText(/last name/i)).toHaveValue("Lovelace");
    const org = screen.getByLabelText(/organization name/i);
    expect(org).toHaveValue("Ada's Organization");
    fireEvent.change(org, { target: { value: "Northwind" } });

    fireEvent.click(screen.getByRole("button", { name: /create account/i }));

    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0]).toEqual({
      ticket: "t-1",
      firstName: "Ada",
      lastName: "Lovelace",
      orgName: "Northwind",
      allowPersonalEmail: false,
    });
  });

  it("hides the org field when the ticket carries an invite and sends no orgName", async () => {
    stubFetch({ "/auth/oauth/register/context": json(200, { ...CTX, has_invite: true }) });
    const calls: OAuthRegisterInput[] = [];
    renderSignup(
      "/signup?oauth_ticket=t-2",
      stubActions({ oauthRegister: async (input) => void calls.push(input) }),
    );

    await waitFor(() => expect(screen.getByLabelText(/first name/i)).toHaveValue("Ada"));
    expect(screen.queryByLabelText(/organization name/i)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /accept and create account/i }));

    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].orgName).toBeUndefined();
  });

  it("threads ?allow_personal=1 into the OAuth register (the business-email hand-off)", async () => {
    stubFetch({ "/auth/oauth/register/context": json(200, CTX) });
    const calls: OAuthRegisterInput[] = [];
    renderSignup(
      "/signup?oauth_ticket=t-1&allow_personal=1",
      stubActions({ oauthRegister: async (input) => void calls.push(input) }),
    );

    await waitFor(() => expect(screen.getByLabelText(/first name/i)).toHaveValue("Ada"));
    fireEvent.click(screen.getByRole("button", { name: /create account/i }));

    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].allowPersonalEmail).toBe(true);
  });

  it("shows the expired state for a dead ticket instead of a broken form", async () => {
    stubFetch({ "/auth/oauth/register/context": json(400, { detail: "Invalid or expired ticket" }) });
    renderSignup("/signup?oauth_ticket=t-dead");

    expect(await screen.findByText(/sign-up link expired/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/first name/i)).not.toBeInTheDocument();
  });
});

// With several orgs per person, a registration for an address that already has an account answers
// 409 `account_exists` and names the sign-in to go to instead, keeping the invitation. With that
// off, the server answers a plain 409 sentence, which renders as sent.
describe("SignupPage for an address that already has an account", () => {
  const EXISTS = "You already have an account. Sign in to continue.";

  function LoginProbe() {
    const location = useLocation();
    return <p data-testid="login">{location.pathname + location.search}</p>;
  }

  function renderWithLogin(path: string, actions: AuthActions) {
    render(
      <QueryClientProvider client={newQueryClient()}>
        <MemoryRouter initialEntries={[path]}>
          <AuthActionsProvider actions={actions}>
            <Routes>
              <Route path="/signup" element={<SignupPage />} />
              <Route path="/login" element={<LoginProbe />} />
            </Routes>
          </AuthActionsProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    );
  }

  const refuse = (details: Record<string, unknown> | null, code = "account_exists", message = EXISTS) =>
    stubActions({
      signup: async () => {
        throw new AuthError(message, code, details);
      },
      oauthRegister: async () => {
        throw new AuthError(message, code, details);
      },
    });

  it("says so and signs in carrying the invitation the server names", async () => {
    stubFetch({ "/invitations/by-token/tok-123": json(200, { email: "invited@acme.test", team_id: "t", team_name: "T", org_team_id: "o", org_name: "O", role: "member", inviter_display_name: null, expires_at: "2026-12-01T00:00:00Z" }) });
    renderWithLogin("/signup?invite=tok-123", refuse({ next: "/login?invite=tok-123" }));
    await waitFor(() => expect((screen.getByLabelText(/email/i) as HTMLInputElement).value).toBe("invited@acme.test"));
    fireEvent.change(screen.getByLabelText(/^password/i), { target: { value: "a-good-password" } });
    fireEvent.click(screen.getByRole("button", { name: /accept and create account/i }));

    expect(await screen.findByText(EXISTS)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByTestId("login")).toHaveTextContent(/^\/login\?invite=tok-123$/);
  });

  it.each([
    ["an absolute URL", "https://evil.example/login"],
    ["a protocol-relative URL", "//evil.example/login"],
    ["a page that is not sign-in", "/teams"],
  ])("never follows %s as the next step", async (_label, next) => {
    renderWithLogin("/signup", refuse({ next }));
    submitCredentials();
    fireEvent.click(await screen.findByRole("button", { name: "Sign in" }));
    expect(await screen.findByTestId("login")).toHaveTextContent(/^\/login$/);
  });

  it("shows the plain refusal as sent when several orgs per person is off", async () => {
    const plain = "An account with that email already exists. Sign in instead.";
    renderWithLogin("/signup", refuse(null, "error", plain));
    submitCredentials();
    expect(await screen.findByText(plain)).toBeInTheDocument();
    expect(screen.getByText("Couldn't create your account")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Sign in" })).toBeNull();
  });

  it("does the same for a provider registration", async () => {
    stubFetch({
      "/auth/oauth/register/context": json(200, {
        provider: "google",
        email: "ada@acme.com",
        first_name: "Ada",
        last_name: "Lovelace",
        has_invite: true,
      }),
    });
    renderWithLogin("/signup?oauth_ticket=t-9", refuse({ next: "/login?invite=tok-7" }));
    await waitFor(() => expect(screen.getByLabelText(/first name/i)).toHaveValue("Ada"));
    fireEvent.click(screen.getByRole("button", { name: /accept and create account/i }));
    fireEvent.click(await screen.findByRole("button", { name: "Sign in" }));
    expect(await screen.findByTestId("login")).toHaveTextContent(/^\/login\?invite=tok-7$/);
  });
});
