// An `/admin/users/:userId` address the console cannot answer for.
// Two shapes, the same two answers the org register gives:
//
//   * an id that is not an id at all — the route resolves it to the shared not-found page and
//     asks the server nothing (the server could only answer 422);
//   * a well-formed id the server 404s — the page resolves to its own gone state at once, never
//     to "We couldn't load… / Try again" behind a retry ladder of the same refused question.
//
// Driven through the REAL hooks on the client the portal builds (its real retry policy), with only
// `fetch` stubbed, so what is counted is what the page would really send.

import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { meKey } from "@/api/auth";
import { createQueryClient } from "@/api/queryClient";
import { AdminUserRoute, GONE_USER } from "@/pages/platform/admin/users/AdminUserDetailPage";

const GONE_ID = "00000000-0000-4000-8000-000000000000";

const STAFF = {
  email: "staff@example.com",
  first_name: "Sam",
  last_name: "Staff",
  id: "u-staff",
  org_team_id: "t-root",
  org_name: "Tideline",
  display_name: "Sam Staff",
  platform_role: "alkera_admin",
  email_verification_required: false,
  has_password: true,
  mfa_enabled: false,
  created_at: "2026-01-01T00:00:00Z",
};

/** The user register lists nobody; every read keyed by an id answers 404. */
function stubGone(): string[] {
  const urls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      urls.push(url);
      const json = (body: unknown, status: number) =>
        new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
      if (new URL(url).pathname.endsWith("/admin/v1/users")) return json([], 200);
      return json({ error: { code: "not_found", message: "Not found" } }, 404);
    }),
  );
  return urls;
}

function renderAt(path: string) {
  const qc = createQueryClient();
  qc.setQueryData(meKey, STAFF);
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/admin/users/:userId" element={<AdminUserRoute />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe.each([
  // The user page reads the register, which answers without the id.
  { page: "user", base: "/admin/users", gone: GONE_USER, asked: "/admin/v1/users" },
])("an $page id the console cannot answer for", ({ base, gone, asked }) => {
  it("resolves an unknown id to the gone state, asking once", async () => {
    const urls = stubGone();
    renderAt(`${base}/${GONE_ID}`);

    expect(await screen.findByText(gone.title)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: gone.action })).toHaveAttribute("href", base);
    expect(screen.queryByText(/couldn.t load/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /try again/i })).not.toBeInTheDocument();
    expect(urls.filter((u) => new URL(u).pathname === asked)).toHaveLength(1);
  });

  it("answers a malformed id with the not-found page, asking nothing", async () => {
    const urls = stubGone();
    renderAt(`${base}/not-an-id`);

    expect(await screen.findByText("This page doesn't exist")).toBeInTheDocument();
    expect(screen.queryByText(gone.title)).not.toBeInTheDocument();
    await waitFor(() => expect(urls.filter((u) => u.includes("not-an-id"))).toEqual([]));
  });
});

describe("a user the register lists", () => {
  it("is shown, never gone, while no other read about them answers", async () => {
    const LIVE = { ...STAFF, id: GONE_ID, email: "ada@northwind.test", platform_role: null };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = input instanceof Request ? input.url : String(input);
        const json = (body: unknown, status: number) =>
          new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
        if (new URL(url).pathname.endsWith("/admin/v1/users")) return json([LIVE], 200);
        return json({ error: { code: "not_found", message: "Not found" } }, 404);
      }),
    );
    renderAt(`/admin/users/${GONE_ID}`);

    expect(await screen.findByRole("heading", { name: LIVE.email })).toBeInTheDocument();
    expect(screen.queryByText(GONE_USER.title)).not.toBeInTheDocument();
  });
});
