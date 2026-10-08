import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CompleteProfilePage } from "@/pages/auth/CompleteProfilePage";
import type { CurrentUser } from "@/api/auth";
import { AuthActionsProvider, type AuthActions, type CompleteProfileInput } from "@/pages/auth/auth-actions";

// The complete-profile step, driven through the REAL useCurrentUser query (fetch mocked) and
// the auth-actions SEAM (a recording stub). Contract: a new-org account (org_name === "")
// gets the organization field and submits an org name; an invited member (named org) gets
// only name fields and submits no org name; an already-named account is bounced to the app.

const newOrgUser = {
  id: "11111111-1111-1111-1111-111111111111",
  email: "ada@example.com",
  first_name: "",
  last_name: "",
  display_name: "",
  org_team_id: "22222222-2222-2222-2222-222222222222",
  org_name: "", // unnamed → new-org admin → org field shown
  org_role: "member",
  membership_count: 1,
  has_password: true,
  mfa_enabled: false,
  email_verification_required: false,
  created_at: "2026-06-01T00:00:00Z",
} satisfies CurrentUser;

// A different org name than the one the new-org case types ("Northwind Labs"),
// so a buggy `needsOrg` that keyed off that specific string instead of the
// empty-string sentinel ("") would be caught.
const invitedUser = { ...newOrgUser, org_name: "Acme Data" }; // named org → no org field
const completedUser = { ...newOrgUser, first_name: "Ada", last_name: "Lovelace" };

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

function mockMe(user: CurrentUser | null): void {
  vi.stubGlobal(
    "fetch",
    vi.fn((input: unknown) => {
      const url = typeof input === "string" ? input : String((input as { url: unknown }).url);
      if (url.includes("/api/v1/auth/me")) {
        return Promise.resolve(user ? json(200, user) : new Response(null, { status: 401 }));
      }
      return Promise.resolve(new Response(null, { status: 404 }));
    }),
  );
}

/** A recording seam stub — only completeProfile is exercised; the rest are inert. */
function stubActions(onComplete: (input: CompleteProfileInput) => void): AuthActions {
  return {
    login: async () => {},
    signup: async () => {},
    oauthRegister: async () => {},
    completeProfile: async (input) => onComplete(input),
    requestPasswordReset: async () => {},
    resetPassword: async () => {},
  };
}

function renderPage(
  user: CurrentUser | null,
  onComplete: (input: CompleteProfileInput) => void = () => {},
  entry = "/complete-profile",
) {
  mockMe(user);
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[entry]}>
        <AuthActionsProvider actions={stubActions(onComplete)}>
          <Routes>
            <Route path="/complete-profile" element={<CompleteProfilePage />} />
            <Route path="/" element={<div>DASHBOARD</div>} />
            <Route path="/login" element={<div>LOGIN</div>} />
            <Route path="/teams" element={<div>TEAMS</div>} />
          </Routes>
        </AuthActionsProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("CompleteProfilePage", () => {
  it("shows the organization field for a new-org account and submits an org name", async () => {
    const calls: CompleteProfileInput[] = [];
    renderPage(newOrgUser, (input) => calls.push(input));

    const org = await screen.findByLabelText(/organization name/i);
    fireEvent.change(screen.getByLabelText(/first name/i), { target: { value: "Ada" } });
    fireEvent.change(screen.getByLabelText(/last name/i), { target: { value: "Lovelace" } });
    fireEvent.change(org, { target: { value: "Northwind Labs" } });
    fireEvent.click(screen.getByRole("button", { name: /continue/i }));

    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0]).toEqual({ firstName: "Ada", lastName: "Lovelace", orgName: "Northwind Labs" });
  });

  it("hides the organization field for an invited member and submits no org name", async () => {
    const calls: CompleteProfileInput[] = [];
    renderPage(invitedUser, (input) => calls.push(input));

    await screen.findByLabelText(/first name/i);
    expect(screen.queryByLabelText(/organization name/i)).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText(/first name/i), { target: { value: "Mem" } });
    fireEvent.change(screen.getByLabelText(/last name/i), { target: { value: "Ber" } });
    fireEvent.click(screen.getByRole("button", { name: /continue/i }));

    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0]).toEqual({ firstName: "Mem", lastName: "Ber", orgName: undefined });
  });

  it("blocks submission until the required fields are filled", async () => {
    const calls: CompleteProfileInput[] = [];
    renderPage(newOrgUser, (input) => calls.push(input));

    await screen.findByLabelText(/first name/i);
    fireEvent.click(screen.getByRole("button", { name: /continue/i }));
    // Empty required fields → the action never runs.
    await waitFor(() => expect(screen.getByText(/enter your first name/i)).toBeInTheDocument());
    expect(calls).toHaveLength(0);
  });

  it("blocks a new-org submission when the org field is left empty", async () => {
    // Names filled but the org (required only in the new-org branch) blank — the
    // org-required validation must still gate the submit, so the action never runs.
    const calls: CompleteProfileInput[] = [];
    renderPage(newOrgUser, (input) => calls.push(input));

    fireEvent.change(await screen.findByLabelText(/first name/i), { target: { value: "Ada" } });
    fireEvent.change(screen.getByLabelText(/last name/i), { target: { value: "Lovelace" } });
    fireEvent.click(screen.getByRole("button", { name: /continue/i }));

    await waitFor(() => expect(screen.getByText(/name your organization/i)).toBeInTheDocument());
    expect(calls).toHaveLength(0);
  });

  it("bounces an already-named account to the app", async () => {
    renderPage(completedUser);
    expect(await screen.findByText("DASHBOARD")).toBeInTheDocument();
  });

  it("bounces an already-named account to the ?return_to= destination", async () => {
    // Nothing to finish → straight on to where the guard said they were going.
    renderPage(completedUser, () => {}, `/complete-profile?return_to=${encodeURIComponent("/teams")}`);
    expect(await screen.findByText("TEAMS")).toBeInTheDocument();
  });

  it("ignores a hostile ?return_to= on the bounce (open-redirect guard)", async () => {
    renderPage(completedUser, () => {}, "/complete-profile?return_to=//evil.com");
    expect(await screen.findByText("DASHBOARD")).toBeInTheDocument();
  });

  it("redirects a signed-out visitor to /login", async () => {
    renderPage(null);
    expect(await screen.findByText("LOGIN")).toBeInTheDocument();
  });
});
