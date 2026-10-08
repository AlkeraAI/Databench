import { StrictMode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { VerifyEmailPage } from "@/pages/auth/VerifyEmailPage";

// The email-verification token page, driven through the REAL useVerifyEmail mutation with
// only `fetch` stubbed. The contract: a 200 verifies and lands on success; a 409 (token
// already used / replayed by StrictMode's double-fire) is ALSO success — never the error;
// a 400 (expired/unknown) surfaces the failure.

const verifiedUser = {
  id: "11111111-1111-1111-1111-111111111111",
  email: "new@example.com",
  first_name: "New",
  last_name: "User",
  display_name: "New User",
  org_team_id: "22222222-2222-2222-2222-222222222222",
  email_verified_at: "2026-06-01T00:00:00Z",
  created_at: "2026-05-01T00:00:00Z",
};

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** The Nth verify call returns the Nth scripted response (last one repeats) — models
 *  StrictMode's duplicate fire, e.g. 200 then 409. */
function mockVerify(...responses: Response[]): void {
  let n = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn((input: unknown) => {
      const url = typeof input === "string" ? input : String((input as { url: unknown }).url);
      if (url.includes("/api/v1/auth/verify-email/")) {
        const r = responses[Math.min(n, responses.length - 1)];
        n += 1;
        return Promise.resolve(r.clone());
      }
      return Promise.resolve(new Response(null, { status: 404 }));
    }),
  );
}

function renderPage(token: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <StrictMode>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[`/verify-email/${token}`]}>
          <Routes>
            <Route path="/verify-email/:token" element={<VerifyEmailPage />} />
            <Route path="/" element={<div>DASHBOARD</div>} />
            <Route path="/login" element={<div>LOGIN</div>} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </StrictMode>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("VerifyEmailPage", () => {
  it("lands on success and never flashes an error on a duplicate verify (200 then 409)", async () => {
    mockVerify(json(200, verifiedUser), json(409, { detail: "Email is already verified" }));
    renderPage("good-token");

    expect(await screen.findByRole("heading", { name: /email verified/i })).toBeInTheDocument();
    expect(screen.queryByText(/could not verify/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /couldn't verify/i })).not.toBeInTheDocument();
  });

  it("treats an already-verified token (409) as success", async () => {
    mockVerify(json(409, { detail: "Email is already verified" }));
    renderPage("used-token");

    expect(await screen.findByRole("heading", { name: /email verified/i })).toBeInTheDocument();
  });

  it("shows the failure for an invalid or expired token (400)", async () => {
    mockVerify(json(400, { error: { code: "invalid_token", message: "Verification token has expired" } }));
    renderPage("bad-token");

    expect(await screen.findByText(/verification token has expired/i)).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /^email verified$/i })).not.toBeInTheDocument();
  });
});
