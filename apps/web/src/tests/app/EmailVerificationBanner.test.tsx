import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { EmailVerificationBanner } from "@/app/EmailVerificationBanner";
import type { CurrentUser } from "@/api/auth";
import { formatDate } from "@/lib/format/date";

// The in-grace email-verification banner, driven through the REAL useCurrentUser query (fetch
// mocked). Contract: it shows only while the account owes a verified email
// (email_verification_required, which the backend leaves false for verified accounts AND platform
// staff), surfaces the grace deadline, and routes its resend action to the verification page —
// the page owns the actual resend (with the server-driven cooldown), never the banner.

const unverified = {
  id: "11111111-1111-1111-1111-111111111111",
  email: "ada@example.com",
  first_name: "Ada",
  last_name: "Lovelace",
  display_name: "Ada Lovelace",
  org_team_id: "22222222-2222-2222-2222-222222222222",
  org_name: "Northwind Labs",
  org_role: "member",
  membership_count: 1,
  has_password: true,
  mfa_enabled: false,
  platform_role: null,
  platform_role_display: null,
  email_verified_at: null,
  email_verification_required: true,
  email_verification_deadline: "2026-07-08T00:00:00Z",
  // A link went out (its resend window is what proves it).
  verification_resend_available_at: "2026-07-01T00:05:00Z",
  created_at: "2026-07-01T00:00:00Z",
} satisfies CurrentUser;

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

function mockFetch(user: CurrentUser | null): { resends: number } {
  const calls = { resends: 0 };
  vi.stubGlobal(
    "fetch",
    vi.fn((input: unknown) => {
      const url = typeof input === "string" ? input : String((input as { url: unknown }).url);
      if (url.includes("/api/v1/auth/verify-email/resend")) {
        calls.resends += 1;
        return Promise.resolve(json(200, { message: "verification email sent" }));
      }
      if (url.includes("/api/v1/auth/me")) {
        return Promise.resolve(user ? json(200, user) : new Response(null, { status: 401 }));
      }
      return Promise.resolve(new Response(null, { status: 404 }));
    }),
  );
  return calls;
}

function renderBanner(user: CurrentUser | null) {
  const calls = mockFetch(user);
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <EmailVerificationBanner />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return calls;
}


afterEach(() => {
  vi.unstubAllGlobals();
});

describe("EmailVerificationBanner", () => {
  it("shows the banner with the address and deadline when verification is required", async () => {
    renderBanner(unverified);
    const banner = await screen.findByTestId("email-verification-banner");
    expect(banner).toHaveTextContent("ada@example.com");
    expect(banner).toHaveTextContent(/open it to use the agent/i);
    expect(banner).toHaveTextContent(/keep account access past/i);
    // The deadline reads through the portal's shared date policy (medium month), so it
    // matches every other surface — not a hand-rolled long-month rendering of its own.
    expect(banner).toHaveTextContent(formatDate(unverified.email_verification_deadline));
    expect(screen.getByRole("link", { name: /resend verification email/i })).toBeInTheDocument();
  });

  it("hides for a verified account (email_verification_required false)", async () => {
    renderBanner({
      ...unverified,
      email_verified_at: "2026-07-02T00:00:00Z",
      email_verification_required: false,
      email_verification_deadline: null,
    });
    await waitFor(() => expect(global.fetch).toHaveBeenCalled());
    expect(screen.queryByTestId("email-verification-banner")).not.toBeInTheDocument();
  });

  it("hides for platform staff (exempt, email_verification_required false)", async () => {
    renderBanner({
      ...unverified,
      platform_role: "alkera_admin",
      email_verification_required: false,
      email_verification_deadline: null,
    });
    await waitFor(() => expect(global.fetch).toHaveBeenCalled());
    expect(screen.queryByTestId("email-verification-banner")).not.toBeInTheDocument();
  });

  it("hides when signed out", async () => {
    renderBanner(null);
    await waitFor(() => expect(global.fetch).toHaveBeenCalled());
    expect(screen.queryByTestId("email-verification-banner")).not.toBeInTheDocument();
  });

  it("routes the resend action to the verification page instead of resending inline", async () => {
    const calls = renderBanner(unverified);
    const link = await screen.findByRole("link", { name: /resend verification email/i });
    expect(link).toHaveAttribute("href", "/verify-email");
    // The banner never fires the resend itself — the page owns the mutation,
    // so the cooldown countdown is always in view when a resend happens.
    fireEvent.click(link);
    expect(calls.resends).toBe(0);
  });

  it("does not claim a link was sent when none is pending", async () => {
    renderBanner({ ...unverified, verification_resend_available_at: null });
    const banner = await screen.findByTestId("email-verification-banner");
    expect(banner).toHaveTextContent("Verify ada@example.com to use the agent");
    expect(banner).not.toHaveTextContent(/we sent/i);
    const link = screen.getByRole("link", { name: /^send verification email$/i });
    expect(link).toHaveAttribute("href", "/verify-email");
  });
});
