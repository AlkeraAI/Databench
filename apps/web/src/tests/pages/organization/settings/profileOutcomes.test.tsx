// The profile page, driven through the network with the real hooks, the real toast viewport and
// the real settings shell. Three defects a walk of the page found, each pinned here:
//
//  * Two-factor "Set up" posted no password, got the server's 403 asking for one, and showed that
//    refusal under a green check with no field to type the password into.
//  * Every profile failure rendered as a success toast, and a blank first name surfaced Pydantic's
//    own "String should have at least 1 character".
//  * The Google and GitHub "Connect" buttons only toasted "Connecting Google…" and did nothing.

import { MemoryRouter } from "react-router-dom";
import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";
import { ProfileSettingsPage } from "@/pages/organization/settings/SettingsPage";

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
};

const ENROLL = { secret: "JBSWY3DPEHPK3PXP", otpauth_uri: "otpauth://totp/Alkera:vera?secret=JBSWY3DPEHPK3PXP" };

const json = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

const refusal = (status: number, code: string, message: string, details?: unknown): Response =>
  json({ error: { code, message, trace_id: "t", ...(details ? { details } : {}) } }, status);

/** What the enroll route answers, given the password the request carried (undefined: none). */
let enrollAnswer: (password: string | undefined) => Response;
/** What PATCH /users/{id} answers. */
let patchAnswer: () => Response;
let identities: unknown[];
let sessions: { jti: string; current: boolean; client: string }[];
const sent: { method: string; path: string; body: unknown }[] = [];

async function route(req: Request): Promise<Response> {
  const p = new URL(req.url).pathname;
  if (req.method !== "GET") {
    const body: unknown = await req.clone().json().catch(() => null);
    sent.push({ method: req.method, path: p, body });
    if (p === "/api/v1/auth/mfa/enroll") {
      const password = (body as { current_password?: string } | null)?.current_password;
      return enrollAnswer(password);
    }
    if (p === `/api/v1/users/${VIEWER.id}`) return patchAnswer();
    if (p === "/api/v1/auth/logout-all") {
      // The server keeps the asking browser and ends every other session.
      sessions = sessions.filter((s) => s.current);
      return json({ message: "Signed out everywhere else" });
    }
  }
  if (p === "/api/v1/auth/me") return json(VIEWER);
  if (p === "/api/v1/auth/identities") return json({ identities });
  if (p === "/api/v1/auth/mfa/status") return json({ enabled: false, backup_codes_remaining: 0 });
  if (p === "/api/v1/auth/sessions")
    return json({
      sessions: sessions.map((s) => ({
        jti: s.jti,
        token_type: "session",
        issued_at: "2026-09-01T00:00:00Z",
        expires_at: "2026-10-01T00:00:00Z",
        last_used_at: "2026-09-27T00:00:00Z",
        label: null,
        client: s.client,
        current: s.current,
      })),
    });
  return json({ detail: `unmatched ${req.method} ${p}` }, 404);
}

beforeEach(() => {
  sent.length = 0;
  identities = [];
  sessions = [];
  enrollAnswer = (password) =>
    password === undefined
      ? refusal(
          403,
          "current_password_required",
          "Your current password is required to turn on two-factor authentication.",
        )
      : password === "correct horse"
        ? json(ENROLL)
        : refusal(
            403,
            "current_password_invalid",
            "That password isn't correct. 3 more tries before the account is locked for 15 minutes.",
            { attempts_left: 3 },
          );
  patchAnswer = () => json(VIEWER);
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: Request | string, init?: RequestInit) =>
      route(input instanceof Request ? input : new Request(input, init)),
    ),
  );
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function renderPage() {
  render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter>
        <ProfileSettingsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** Every toast on screen, with the tone it was drawn in. */
const toasts = () =>
  [...document.querySelectorAll<HTMLElement>(".alk-toast__main")].map((el) => ({
    tone: el.getAttribute("data-tone"),
    text: el.textContent?.trim() ?? "",
  }));

async function setUpTwoFactor() {
  const user = userEvent.setup();
  renderPage();
  await user.click(await screen.findByRole("button", { name: "Set up" }));
  return user;
}

describe("two-factor setup asks for the password the server needs", () => {
  it("prompts for the current password instead of toasting the refusal", async () => {
    await setUpTwoFactor();
    const field = await screen.findByLabelText("Current password");
    await waitFor(() => expect(document.activeElement).toBe(field));
    // A missing password is a prompt, not a failure — and never a success.
    expect(toasts()).toEqual([]);
  });

  it("retries with the password and shows the setup key", async () => {
    const user = await setUpTwoFactor();
    await user.type(await screen.findByLabelText("Current password"), "correct horse");
    await user.click(screen.getByRole("button", { name: "Continue" }));

    expect(await screen.findByRole("img", { name: "Two-factor setup QR code" })).toBeInTheDocument();
    const enrolls = sent.filter((call) => call.path === "/api/v1/auth/mfa/enroll");
    expect(enrolls.map((call) => call.body)).toEqual([{}, { current_password: "correct horse" }]);
    expect(screen.queryByLabelText("Current password")).toBeNull();
  });

  it("keeps the prompt open and shows the server's sentence with the tries left on a wrong password", async () => {
    const user = await setUpTwoFactor();
    await user.type(await screen.findByLabelText("Current password"), "wrong");
    await user.click(screen.getByRole("button", { name: "Continue" }));

    const field = await screen.findByLabelText("Current password");
    await waitFor(() => expect(field).toHaveAttribute("aria-invalid", "true"));
    expect(
      screen.getByText("That password isn't correct. 3 more tries before the account is locked for 15 minutes."),
    ).toBeInTheDocument();
    expect(field).toHaveValue("");
    expect(toasts().filter((t) => t.tone === "success")).toEqual([]);
  });

  it("toasts any other refusal as an error", async () => {
    enrollAnswer = () =>
      refusal(403, "reauth_required", "Sign in again to turn on two-factor authentication.");
    await setUpTwoFactor();

    await waitFor(() =>
      expect(toasts()).toEqual([
        { tone: "danger", text: "Sign in again to turn on two-factor authentication." },
      ]),
    );
    expect(screen.queryByLabelText("Current password")).toBeNull();
  });
});

describe("profile failures are drawn as failures", () => {
  it("refuses a blank first name on the field, before any request", async () => {
    const user = userEvent.setup();
    renderPage();
    const first = await screen.findByRole("textbox", { name: "First name" });
    await user.clear(first);

    expect(first).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByText("Enter your first name.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(sent.filter((call) => call.method === "PATCH")).toEqual([]);
  });

  it("toasts a refused save in the danger tone, without the validator's raw text", async () => {
    patchAnswer = () =>
      refusal(422, "validation_error", "The request failed validation.", {
        errors: [{ type: "string_too_long", loc: ["body", "last_name"], msg: "String should have at most 255 characters" }],
      });
    const user = userEvent.setup();
    renderPage();
    const last = await screen.findByRole("textbox", { name: "Last name" });
    await user.type(last, "son");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(toasts()).toHaveLength(1));
    const [toast] = toasts();
    expect(toast.tone).toBe("danger");
    expect(toast.text).not.toMatch(/String should have/);
  });

  it("says the verification link went to the new address when the server reports one pending", async () => {
    patchAnswer = () =>
      json({
        ...VIEWER,
        email: "vera@new.io",
        email_verified_at: null,
        verification_resend_available_at: "2026-09-29T00:05:00Z",
      });
    const user = userEvent.setup();
    renderPage();
    const email = await screen.findByRole("textbox", { name: "Email" });
    await user.clear(email);
    await user.type(email, "vera@new.io");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() =>
      expect(toasts()).toEqual([
        { tone: "success", text: "Profile saved. We sent a verification link to vera@new.io." },
      ]),
    );
  });

  it("says the link was NOT sent when the email change left none pending", async () => {
    patchAnswer = () =>
      json({ ...VIEWER, email: "vera@new.io", email_verified_at: null, verification_resend_available_at: null });
    const user = userEvent.setup();
    renderPage();
    const email = await screen.findByRole("textbox", { name: "Email" });
    await user.clear(email);
    await user.type(email, "vera@new.io");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(toasts()).toHaveLength(1));
    const [toast] = toasts();
    expect(toast.tone).toBe("danger");
    expect(toast.text).toMatch(/couldn't send a verification link to vera@new\.io/);
  });

  it("never takes an organization's view of a colleague for the caller's own account", async () => {
    // What the route answers an org admin editing someone else: no verification
    // state, nothing to seed the signed-in user's cache with.
    patchAnswer = () =>
      json({
        id: VIEWER.id,
        email: VIEWER.email,
        first_name: VIEWER.first_name,
        last_name: "Ngson",
        display_name: "Vera Ngson",
        created_at: "2026-01-01T00:00:00Z",
      });
    const user = userEvent.setup();
    renderPage();
    await user.type(await screen.findByRole("textbox", { name: "Last name" }), "son");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(toasts()).toHaveLength(1));
    expect(toasts()[0].tone).toBe("danger");
  });

  it("toasts a landed save in the success tone", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.type(await screen.findByRole("textbox", { name: "Last name" }), "son");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(toasts()).toEqual([{ tone: "success", text: "Profile saved." }]));
  });
});

describe("sign-in providers", () => {
  it("lists a linked provider as connected and offers no button that cannot connect the other", async () => {
    identities = [
      { provider: "github", email_at_link: "vera@x.io", last_login_at: null, linked_at: "2026-01-01T00:00:00Z" },
    ];
    renderPage();
    // Loaded: the linked provider's row is on screen.
    expect(await screen.findByText("GitHub")).toBeInTheDocument();
    const section = screen.getByText("GitHub").closest("#profile-signin") as HTMLElement;
    expect(within(section).getByText("Connected")).toBeInTheDocument();
    // There is no signed-in link flow, so the unlinked provider is not offered at all.
    expect(within(section).queryByRole("button", { name: /connect/i })).toBeNull();
    expect(within(section).queryByText("Google")).toBeNull();
  });
});

describe("sign out everywhere", () => {
  it("keeps this page signed in and drops only the other sessions", async () => {
    sessions = [
      { jti: "here", current: true, client: "Firefox · 10.0.0.0/24" },
      { jti: "there", current: false, client: "Safari · 10.9.9.0/24" },
    ];
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Sign out everywhere" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Sign out everywhere" }));

    await waitFor(() =>
      expect(toasts()).toEqual([{ tone: "success", text: "Signed out of all other sessions." }]),
    );
    expect(sent.map((call) => call.path)).toEqual(["/api/v1/auth/logout-all"]);
    // Still on the profile, with the viewer's own details and this device's session.
    expect(screen.getByRole("textbox", { name: "Email" })).toHaveValue(VIEWER.email);
    await waitFor(() => expect(screen.queryByText(/Safari/)).toBeNull());
    expect(screen.getByRole("button", { name: "Sign out everywhere" })).toBeDisabled();
  });
});
