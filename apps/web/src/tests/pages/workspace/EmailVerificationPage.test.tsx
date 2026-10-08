// The verification page's caller-observable contract: the resend button obeys
// the SERVER-driven cooldown (`verification_resend_available_at` on /auth/me),
// counting down live and re-enabling at zero; a resend re-reads the identity so
// the fresh window starts; a 429 resyncs instead of showing a bare error; the
// re-check action re-reads the identity and flips the page once verified.

import { QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { CurrentUser } from "@/api/auth";
import { resetRealtimeStatus, useRealtimeStatus } from "@/api/events/status";
import { createQueryClient } from "@/api/queryClient";
import { EmailVerificationPage } from "@/pages/workspace/EmailVerificationPage";

const BASE_USER = {
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
  email_verification_deadline: "2026-08-06T00:00:00Z",
  // A link went out and its resend window has elapsed.
  verification_resend_available_at: "2026-07-30T00:05:00Z",
  created_at: "2026-07-30T00:00:00Z",
} satisfies CurrentUser;

const json = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });

// Mutable "server": tests reshape `me` / `resendStatus` between interactions.
const server = {
  me: { ...BASE_USER } as CurrentUser,
  resends: 0,
  resendStatus: 200,
};

function stubFetch() {
  vi.stubGlobal(
    "fetch",
    vi.fn((input: unknown) => {
      const url = typeof input === "string" ? input : String((input as { url: unknown }).url);
      if (url.includes("/api/v1/auth/verify-email/resend")) {
        server.resends += 1;
        if (server.resendStatus === 503) {
          return Promise.resolve(
            json(
              {
                error: {
                  code: "email_send_failed",
                  message: "We couldn't send the verification email. Try again in a few minutes.",
                },
              },
              503,
            ),
          );
        }
        if (server.resendStatus === 429) {
          return Promise.resolve(
            json(
              { error: { code: "rate_limited", message: "An email was just sent." } },
              429,
            ),
          );
        }
        return Promise.resolve(json({ message: "Verification email sent" }));
      }
      if (url.includes("/api/v1/auth/me")) return Promise.resolve(json(server.me));
      return Promise.resolve(json({ detail: `unmatched ${url}` }, 404));
    }),
  );
}

function renderPage() {
  stubFetch();
  render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter initialEntries={["/verify-email"]}>
        <EmailVerificationPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const inFuture = (seconds: number) => new Date(Date.now() + seconds * 1000).toISOString();

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
  resetRealtimeStatus();
  server.me = { ...BASE_USER };
  server.resends = 0;
  server.resendStatus = 200;
});

describe("EmailVerificationPage", () => {
  it("disables resend under the server cooldown, counting down to re-enable", async () => {
    vi.useFakeTimers();
    server.me = { ...BASE_USER, verification_resend_available_at: inFuture(90) };
    renderPage();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0); // the /me fetch settles
    });

    const button = screen.getByRole("button", { name: /resend available in 1:30/i });
    expect(button).toBeDisabled();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(screen.getByRole("button", { name: /resend available in 30s/i })).toBeDisabled();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(screen.getByRole("button", { name: /^resend verification email$/i })).toBeEnabled();
  });

  it("resends when allowed, then re-reads /me so the fresh cooldown disables the button", async () => {
    renderPage();
    const button = await screen.findByRole("button", { name: /^resend verification email$/i });

    // The mutation's settle re-reads /me, which now reports the new window.
    server.me = { ...BASE_USER, verification_resend_available_at: inFuture(300) };
    fireEvent.click(button);

    await waitFor(() => expect(server.resends).toBe(1));
    expect(await screen.findByText(/verification email sent\. check your inbox/i)).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /resend available in/i })).toBeDisabled(),
    );
  });

  it("treats a 429 as a cooldown resync, not a bare error", async () => {
    // Another surface (the banner, the gate page) just resent: this page's
    // clock has no window yet, the server refuses, and /me supplies the truth.
    server.resendStatus = 429;
    renderPage();
    const button = await screen.findByRole("button", { name: /^resend verification email$/i });

    server.me = { ...BASE_USER, verification_resend_available_at: inFuture(240) };
    fireEvent.click(button);

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /resend available in/i })).toBeDisabled(),
    );
    // The countdown IS the explanation — no danger callout on top of it.
    expect(screen.queryByText(/an email was just sent/i)).toBeNull();
  });

  it("recheck flips to the verified state and toasts the good news", async () => {
    renderPage();
    await screen.findByText(/we sent a verification link to/i);

    server.me = {
      ...BASE_USER,
      email_verified_at: "2026-07-30T01:00:00Z",
      email_verification_required: false,
      email_verification_deadline: null,
    };
    fireEvent.click(screen.getByRole("button", { name: /^recheck$/i }));

    expect(await screen.findByText(/your email is verified/i)).toBeInTheDocument();
    expect(await screen.findByText("Email verified.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /back to the dashboard/i })).toHaveAttribute("href", "/");
    expect(screen.queryByRole("button", { name: /resend/i })).toBeNull();
  });

  it("recheck toasts 'still unverified' when nothing changed", async () => {
    renderPage();
    await screen.findByText(/we sent a verification link to/i);

    fireEvent.click(screen.getByRole("button", { name: /^recheck$/i }));

    expect(
      await screen.findByText(/still unverified\. open the link in your inbox/i),
    ).toBeInTheDocument();
    // The page itself stays on the unverified state.
    expect(screen.getByRole("button", { name: /resend verification email/i })).toBeInTheDocument();
  });

  it("re-reads the identity once a minute while unverified", async () => {
    vi.useFakeTimers();
    renderPage();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    const fetchSpy = global.fetch as ReturnType<typeof vi.fn>;
    const callUrl = (input: unknown) =>
      typeof input === "string" ? input : String((input as { url: unknown }).url);
    const meCalls = () =>
      fetchSpy.mock.calls.filter(([input]) => callUrl(input).includes("/api/v1/auth/me")).length;
    const before = meCalls();
    expect(before).toBeGreaterThan(0); // sanity: the mount fetch was counted

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(meCalls()).toBeGreaterThan(before);
  });

  it("the once-a-minute recheck is suspended while the event stream is live (user.email_verified arrives instead)", async () => {
    vi.useFakeTimers();
    useRealtimeStatus.getState().setSse("connected");
    renderPage();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    const fetchSpy = global.fetch as ReturnType<typeof vi.fn>;
    const callUrl = (input: unknown) =>
      typeof input === "string" ? input : String((input as { url: unknown }).url);
    const meCalls = () =>
      fetchSpy.mock.calls.filter(([input]) => callUrl(input).includes("/api/v1/auth/me")).length;
    const before = meCalls();
    expect(before).toBeGreaterThan(0);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3 * 60_000 + 1);
    });
    expect(meCalls()).toBe(before);

    // The stream drops: the fallback resumes on its own minute.
    await act(async () => {
      useRealtimeStatus.getState().setSse("down");
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_001);
    });
    expect(meCalls()).toBeGreaterThan(before);
  });

  it("does not claim a link was sent when none is pending, and offers the first send", async () => {
    // An email change whose mail the relay refused leaves no pending link.
    server.me = { ...BASE_USER, verification_resend_available_at: null };
    renderPage();
    const button = await screen.findByRole("button", { name: /^send verification email$/i });
    expect(screen.getByText(/send a verification link to/i)).toBeInTheDocument();
    expect(screen.queryByText(/we sent a verification link/i)).toBeNull();

    server.me = { ...BASE_USER, verification_resend_available_at: inFuture(300) };
    fireEvent.click(button);
    await waitFor(() => expect(server.resends).toBe(1));
    expect(await screen.findByText(/we sent a verification link to/i)).toBeInTheDocument();
  });

  it("shows a refused send as an error, never as sent", async () => {
    server.resendStatus = 503;
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /^resend verification email$/i }));

    expect(
      await screen.findByText("We couldn't send the verification email. Try again in a few minutes."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/verification email sent/i)).toBeNull();
    expect(screen.getByRole("button", { name: /^resend verification email$/i })).toBeEnabled();
  });
});
