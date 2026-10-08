import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { EmailVerificationGatePage } from "@/pages/auth/EmailVerificationGatePage";
import type { CurrentUser } from "@/api/auth";

// The full-screen email-verification gate, driven through the REAL useCurrentUser query and
// the resend mutation with only `fetch` stubbed. The contract: a blocked-past-grace account
// sees the gate and can resend; a still-in-grace account is bounced to the app; a signed-out
// visitor is bounced to /login. "Blocked" is purely deadline-driven (isVerificationBlocked).

const blockedUser = {
  id: "11111111-1111-1111-1111-111111111111",
  email: "blocked@example.com",
  first_name: "Blocked",
  last_name: "User",
  display_name: "Blocked User",
  org_team_id: "22222222-2222-2222-2222-222222222222",
  org_name: "Blocked Org",
  org_role: "member",
  membership_count: 1,
  has_password: true,
  mfa_enabled: false,
  email_verified_at: null,
  email_verification_required: true,
  email_verification_deadline: "2020-01-01T00:00:00Z", // in the past → blocked
  verification_resend_available_at: "2019-12-25T00:05:00Z", // a link went out
  created_at: "2019-12-25T00:00:00Z",
} satisfies CurrentUser;

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function mockFetch(user: CurrentUser | null, supportEmail: string | null): { resends: number } {
  const calls = { resends: 0 };
  vi.stubGlobal(
    "fetch",
    vi.fn((input: unknown) => {
      const url = typeof input === "string" ? input : String((input as { url: unknown }).url);
      if (url.includes("/api/v1/config")) {
        return Promise.resolve(json(200, { product_name: "Databench", support_email: supportEmail }));
      }
      if (url.includes("/api/v1/auth/me")) {
        return Promise.resolve(user ? json(200, user) : new Response(null, { status: 401 }));
      }
      if (url.includes("/api/v1/auth/verify-email/resend")) {
        calls.resends += 1;
        return Promise.resolve(json(200, { message: "verification email sent" }));
      }
      if (url.includes("/api/v1/auth/logout")) return Promise.resolve(json(200, { message: "ok" }));
      return Promise.resolve(new Response(null, { status: 404 }));
    }),
  );
  return calls;
}

function renderGate(user: CurrentUser | null, supportEmail: string | null = null) {
  const calls = mockFetch(user, supportEmail);
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/verify-email-required"]}>
        <Routes>
          <Route path="/verify-email-required" element={<EmailVerificationGatePage />} />
          <Route path="/" element={<div>DASHBOARD</div>} />
          <Route path="/login" element={<div>LOGIN</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return calls;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("EmailVerificationGatePage", () => {
  it("renders the gate for a blocked account, naming the address", async () => {
    renderGate(blockedUser);
    expect(await screen.findByTestId("email-verification-gate")).toBeInTheDocument();
    expect(screen.getByText(/confirm your email address/i)).toBeInTheDocument();
    expect(screen.getByText("blocked@example.com")).toBeInTheDocument();
  });

  it("links the support address the deployment configured", async () => {
    renderGate(blockedUser, "help@northwind.test");
    const support = await screen.findByRole("link", { name: /contact support/i });
    expect(support).toHaveAttribute("href", "mailto:help@northwind.test");
  });

  it("sends the reader to their administrator when no support address is configured", async () => {
    renderGate(blockedUser, null);
    expect(await screen.findByText(/ask your administrator/i)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /contact support/i })).not.toBeInTheDocument();
  });

  it("resends the verification email on click", async () => {
    const calls = renderGate(blockedUser);
    fireEvent.click(await screen.findByRole("button", { name: /resend verification email/i }));
    await waitFor(() => expect(calls.resends).toBe(1));
    expect(await screen.findByText(/verification sent/i)).toBeInTheDocument();
  });

  it("does not claim a link was sent when none is pending", async () => {
    const calls = renderGate({ ...blockedUser, verification_resend_available_at: null });
    await screen.findByTestId("email-verification-gate");
    expect(screen.queryByText(/we sent a verification link/i)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /^send verification email$/i }));
    await waitFor(() => expect(calls.resends).toBe(1));
  });

  it("bounces a still-in-grace account to the app", async () => {
    renderGate({ ...blockedUser, email_verification_deadline: "2999-01-01T00:00:00Z" });
    expect(await screen.findByText("DASHBOARD")).toBeInTheDocument();
    expect(screen.queryByTestId("email-verification-gate")).not.toBeInTheDocument();
  });

  it("redirects a signed-out visitor to /login", async () => {
    renderGate(null);
    expect(await screen.findByText("LOGIN")).toBeInTheDocument();
  });
});
