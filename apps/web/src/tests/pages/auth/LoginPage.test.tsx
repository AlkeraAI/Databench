import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LoginPage } from "@/pages/auth/LoginPage";
import { RealAuthActionsProvider } from "@/pages/auth/auth-actions";

// The login page driven through the REAL auth seam (RealAuthActionsProvider → useLogin →
// the request layer) against a REAL QueryClient, with only `fetch` stubbed. The behavior
// under test is the MFA challenge: an MFA-protected account 401s the first attempt with
// `mfa_required`, and ONLY THEN does the page reveal a code field and resubmit email +
// password + code. The asymmetric case (a plain bad-credentials 401 must NOT reveal the
// field) is what a naive "any 401 → show MFA" implementation gets wrong, so it's pinned too.

const USER = {
  id: "u-1",
  first_name: "Ada",
  last_name: "Lovelace",
  email: "ada@acme.com",
  email_verification_required: false,
  has_password: true,
  created_at: "2026-01-01T00:00:00Z",
};

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}
const envelope = (code: string, message: string) => ({ error: { code, message } });

// openapi-fetch calls global fetch with a Request object, so the matcher reads `req.url`.
const reqUrl = (req: Request): string => req.url;

let fetchSpy: ReturnType<typeof vi.fn>;
beforeEach(() => {
  fetchSpy = vi.fn();
  vi.stubGlobal("fetch", fetchSpy);
});
afterEach(() => vi.unstubAllGlobals());

function renderLogin(entry = "/login") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[entry]}>
        <RealAuthActionsProvider>
          <Routes>
            <Route path="/login" element={<LoginPage />} />
            <Route path="/" element={<div>workspace home</div>} />
          </Routes>
        </RealAuthActionsProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** Each call to /auth/login shifts the next scripted Response; the OAuth-providers
 *  query is answered inertly so the page's federated buttons stay empty. */
function scriptLogin(...loginResponses: Response[]) {
  let i = 0;
  fetchSpy.mockImplementation(async (req: Request) => {
    const url = reqUrl(req);
    if (url.includes("/oauth/providers")) return json(200, { providers: [] });
    if (url.includes("/auth/login")) return loginResponses[Math.min(i++, loginResponses.length - 1)].clone();
    return json(200, { providers: [] });
  });
}

describe("LoginPage — MFA challenge", () => {
  it("hides the code field until the first attempt returns mfa_required", async () => {
    const user = userEvent.setup();
    scriptLogin(json(401, envelope("mfa_required", "Enter your code")));
    renderLogin();

    // No code field before the first attempt.
    expect(screen.queryByLabelText(/authentication code/i)).toBeNull();

    await user.type(screen.getByLabelText(/email/i), "ada@acme.com");
    await user.type(screen.getByLabelText(/^password/i), "correct-horse");
    await user.click(screen.getByRole("button", { name: /sign in/i }));

    // After mfa_required, the code field appears.
    expect(await screen.findByLabelText(/authentication code/i)).toBeInTheDocument();
  });

  it("resubmits with the entered code and signs in on success", async () => {
    const user = userEvent.setup();
    scriptLogin(
      json(401, envelope("mfa_required", "Enter your code")),
      json(200, { user: USER, expires_at: "2026-12-01T00:00:00Z" }),
    );
    renderLogin();

    await user.type(screen.getByLabelText(/email/i), "ada@acme.com");
    await user.type(screen.getByLabelText(/^password/i), "correct-horse");
    await user.click(screen.getByRole("button", { name: /sign in/i }));

    const code = await screen.findByLabelText(/authentication code/i);
    await user.type(code, "123456");
    await user.click(screen.getByRole("button", { name: /sign in/i }));

    // The second login call must carry the code (and the original credentials).
    await waitFor(async () => {
      const loginReqs = fetchSpy.mock.calls.map((c) => c[0] as Request).filter((r) => reqUrl(r).includes("/auth/login"));
      const bodies = await Promise.all(loginReqs.map((r) => r.clone().json()));
      const withCode = bodies.find((b) => b.mfa_code === "123456");
      expect(withCode).toMatchObject({ email: "ada@acme.com", password: "correct-horse", mfa_code: "123456" });
    });
  });

  it("does NOT reveal the code field for a plain bad-credentials 401", async () => {
    // The asymmetric guard: only mfa_required / mfa_invalid reveal the field; a wrong-password
    // 401 must show the error WITHOUT the MFA prompt.
    const user = userEvent.setup();
    scriptLogin(json(401, envelope("invalid_credentials", "That email and password don't match.")));
    renderLogin();

    await user.type(screen.getByLabelText(/email/i), "ada@acme.com");
    await user.type(screen.getByLabelText(/^password/i), "wrong");
    await user.click(screen.getByRole("button", { name: /sign in/i }));

    expect(await screen.findByText(/don't match/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/authentication code/i)).toBeNull();
  });

  it("flags an invalid code with a field error and keeps the field shown", async () => {
    const user = userEvent.setup();
    scriptLogin(
      json(401, envelope("mfa_required", "Enter your code")),
      json(401, envelope("mfa_invalid", "That code didn't match.")),
    );
    renderLogin();

    await user.type(screen.getByLabelText(/email/i), "ada@acme.com");
    await user.type(screen.getByLabelText(/^password/i), "correct-horse");
    await user.click(screen.getByRole("button", { name: /sign in/i }));

    const code = await screen.findByLabelText(/authentication code/i);
    await user.type(code, "000000");
    await user.click(screen.getByRole("button", { name: /sign in/i }));

    // Still shown, now with the inline mismatch error.
    expect(await screen.findByText(/that code didn't match/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/authentication code/i)).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------- //
// A refused federated sign-in
// --------------------------------------------------------------------------- //
//
// The OAuth/SSO callbacks can only answer a browser with a redirect, so every
// refusal arrives as `/login?oauth_error=<reason>`. Nothing on this page read that
// parameter, so a user an org's SSO enforcement (or a provider allow-list) had
// refused was bounced back to a pristine login form with no explanation — a
// server-side gate that reads to the user as a broken button. These pin that each
// reason the two callbacks emit lands as copy telling the user what to do next.

/** Every reason `_login_error_redirect` (oauth.py) and `_login_error` (sso.py) can
 *  emit, with a fragment of the action its notice must offer. The list is the union
 *  of both routes' literals plus every `OAuthLoginBlockedError.reason` they funnel
 *  through — keep it in step with those three files. */
const OAUTH_REASONS: Array<[reason: string, action: RegExp]> = [
  ["sso_required", /identity provider/i],
  ["provider_disabled", /another provider your admin allows/i],
  ["email_unverified", /verify it with the provider/i],
  ["already_linked", /belongs to a different user/i],
  ["account_deactivated", /admin to restore it/i],
  ["no_email", /allow email access/i],
  ["expired", /start it again/i],
  ["state", /only follow sign-in links you opened yourself/i],
  ["exchange", /try again in a moment/i],
  ["provider_unavailable", /try again shortly/i],
  ["unknown_provider", /pick one of the providers shown/i],
  ["sso_domain_mismatch", /use your work address/i],
  ["sso_org_mismatch", /organization that owns the account/i],
  ["sso_not_configured", /no identity provider configured/i],
  ["sso_misconfigured", /check the connection/i],
  ["replay", /already used/i],
];

describe("LoginPage — a refused federated sign-in", () => {
  beforeEach(() => scriptLogin(json(401, envelope("invalid_credentials", "Nope."))));

  it("tells an SSO-governed account where to sign in instead", async () => {
    // The refusal a real org hits: `_assert_federated_login_allowed` blocks the
    // social button once the org enforces SSO. There IS another way in — the page
    // has to say which, or a deliberate policy reads as an outage.
    renderLogin("/login?oauth_error=sso_required");

    expect(await screen.findByText(/requires single sign-on/i)).toBeInTheDocument();
    expect(screen.getByText(/your admin can send you the link/i)).toBeInTheDocument();
  });

  it.each(OAUTH_REASONS)("explains %s with something to do about it", async (reason, action) => {
    renderLogin(`/login?oauth_error=${reason}`);
    expect(await screen.findByText(action)).toBeInTheDocument();
  });

  it("gives every reason its own headline", () => {
    // One shared headline across all of them would technically "render something"
    // while telling the user nothing — pin that the notices are actually distinct.
    const titles = new Set<string>();
    for (const [reason] of OAUTH_REASONS) {
      const view = renderLogin(`/login?oauth_error=${reason}`);
      titles.add(view.container.querySelector(".alk-callout__title")?.textContent ?? "");
      view.unmount();
    }
    expect(titles.size).toBe(OAUTH_REASONS.length);
  });

  it("falls back to generic copy for an unknown reason and never echoes it", async () => {
    // The parameter is attacker-supplied: anyone can send a victim to
    // /login?oauth_error=<anything>. It keys a fixed table and is never rendered,
    // so an unrecognized (or hostile) value degrades to the generic notice.
    const hostile = "<img src=x onerror=alert(1)>your session expired, call 555-0100";
    renderLogin(`/login?oauth_error=${encodeURIComponent(hostile)}`);

    expect(await screen.findByText(/couldn't finish that sign-in/i)).toBeInTheDocument();
    expect(screen.queryByText(/555-0100/)).toBeNull();
    expect(document.body.innerHTML).not.toContain("onerror");
  });

  it("shows no notice on a clean /login", () => {
    const view = renderLogin();
    expect(view.container.querySelector(".alk-callout")).toBeNull();
  });

  it("never lets the URL drive the form's MFA state", async () => {
    // The asymmetric guard, and the reason the notice is a separate reading from
    // `submit.errorCode`: the parameter is attacker-supplied, so it must never be
    // treated as an authentication signal. Wiring it into `mfaRequired` would let a
    // crafted link demand an authenticator code from an account that has none —
    // a form the victim cannot submit, and a convincing prompt to phish a code.
    renderLogin("/login?oauth_error=mfa_required");
    expect(await screen.findByText(/couldn't finish that sign-in/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/authentication code/i)).toBeNull();
  });

  it("lets a fresh sign-in failure supersede the stale notice", async () => {
    // The notice describes a trip the user has already left. Once they attempt
    // email + password here, THAT error is the one that matters — two banners
    // disagreeing about why sign-in failed is worse than none.
    const user = userEvent.setup();
    scriptLogin(json(401, envelope("invalid_credentials", "That email and password don't match.")));
    renderLogin("/login?oauth_error=provider_unavailable");

    expect(await screen.findByText(/try again shortly/i)).toBeInTheDocument();

    await user.type(screen.getByLabelText(/email/i), "ada@acme.com");
    await user.type(screen.getByLabelText(/^password/i), "wrong");
    await user.click(screen.getByRole("button", { name: /sign in/i }));

    expect(await screen.findByText(/don't match/i)).toBeInTheDocument();
    expect(screen.queryByText(/try again shortly/i)).toBeNull();
  });
});

describe("LoginPage — required marks", () => {
  it("asks for email and password without an asterisk, and still requires both", async () => {
    scriptLogin(json(401, envelope("invalid_credentials", "Wrong email or password")));
    renderLogin();
    const email = await screen.findByLabelText(/^email/i);
    const password = screen.getByLabelText(/^password/i);
    expect(email).toBeRequired();
    expect(password).toBeRequired();
    expect(document.querySelector(".alk-field__req")).toBeNull();
  });
});
