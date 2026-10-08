import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useSearchParams } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { OAuthBusinessEmailPage } from "@/pages/auth/OAuthBusinessEmailPage";

// The personal-email nudge after an OAuth sign-up. Driven through the REAL register-context
// query against a REAL QueryClient, only `fetch` + `window.location` stubbed. The behavior:
// a valid ticket shows the "use your work account" prompt with the prefilled email and a
// primary button that starts a fresh OAuth handshake; "continue anyway" forwards to /signup
// carrying allow_personal=1; an invalid/expired ticket shows the expired card instead.

const CTX = { provider: "google", email: "ada@gmail.com", first_name: "Ada", last_name: "L" };

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

let fetchSpy: ReturnType<typeof vi.fn>;
const href = { value: "" };
beforeEach(() => {
  fetchSpy = vi.fn();
  vi.stubGlobal("fetch", fetchSpy);
  href.value = "";
  Object.defineProperty(window, "location", {
    configurable: true,
    value: {
      get href() {
        return href.value;
      },
      set href(v: string) {
        href.value = v;
      },
      origin: "https://app.example.com",
    },
  });
});
afterEach(() => vi.unstubAllGlobals());

// A probe at /signup that echoes the query string it was navigated with, so the test can
// assert WHAT was forwarded (oauth_ticket + return_to + allow_personal), not merely that
// some navigation happened.
function SignupProbe() {
  const [params] = useSearchParams();
  return <div>signup form ?{params.toString()}</div>;
}

function renderPage(search: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[`/oauth/business-email${search}`]}>
        <Routes>
          <Route path="/oauth/business-email" element={<OAuthBusinessEmailPage />} />
          <Route path="/signup" element={<SignupProbe />} />
          <Route path="/login" element={<div>sign in</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("OAuthBusinessEmailPage", () => {
  it("shows the work-account nudge with the prefilled personal email", async () => {
    fetchSpy.mockResolvedValue(json(200, CTX));
    renderPage("?oauth_ticket=tk-1&return_to=/teams");
    // The context loads async; wait for the personal email to appear in the nudge (a <strong>).
    expect(await screen.findByText("ada@gmail.com")).toBeInTheDocument();
    expect(screen.getByText(/that's a personal/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /sign in with your work account/i })).toBeInTheDocument();
  });

  it("starts a fresh signup OAuth handshake when the work-account button is clicked", async () => {
    const user = userEvent.setup();
    fetchSpy.mockResolvedValue(json(200, CTX));
    renderPage("?oauth_ticket=tk-1&return_to=/teams");

    await user.click(await screen.findByRole("button", { name: /sign in with your work account/i }));
    expect(href.value).toContain("/api/v1/auth/oauth/google/start");
    expect(href.value).toContain("intent=signup");
    expect(href.value).toContain("return_to=%2Fteams");
  });

  it("forwards to /signup carrying allow_personal=1 + the ticket + return_to when the user continues anyway", async () => {
    const user = userEvent.setup();
    fetchSpy.mockResolvedValue(json(200, CTX));
    renderPage("?oauth_ticket=tk-1&return_to=/teams");

    await user.click(await screen.findByRole("button", { name: /continue with ada@gmail.com anyway/i }));
    // allow_personal=1 is the whole point of this branch — it tells the backend to accept the
    // personal email; a navigation to /signup WITHOUT it loops the user back here, so it's pinned.
    const probe = await screen.findByText(/^signup form \?/);
    const forwarded = new URLSearchParams(probe.textContent!.replace(/^signup form \?/, ""));
    expect(forwarded.get("allow_personal")).toBe("1");
    expect(forwarded.get("oauth_ticket")).toBe("tk-1");
    expect(forwarded.get("return_to")).toBe("/teams");
  });

  it("shows the expired card when the ticket is missing", async () => {
    renderPage("");
    expect(await screen.findByText(/sign-up link expired/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /work account/i })).toBeNull();
  });

  it("shows the expired card when the context lookup fails (bad ticket)", async () => {
    fetchSpy.mockResolvedValue(json(410, { error: { code: "expired", message: "gone" } }));
    renderPage("?oauth_ticket=stale");
    expect(await screen.findByText(/sign-up link expired/i)).toBeInTheDocument();
  });
});
