// An `/admin/orgs/:orgId` address the register cannot answer for. Two shapes, two
// different costs:
//
//   * an id that is not an org id at all — the guard resolves it to the shared
//     not-found page with no request made, the way the chat route does;
//   * a well-formed id the register 404s — the page resolves to its own
//     not-found, and the bands keyed by that id (members, settings, …) never
//     mount, so the failed read is not multiplied across the page.
//
// Driven through the REAL hooks against a real createQueryClient with only
// `fetch` stubbed, so what is counted is what the page would really send.

import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";
import { meKey } from "@/api/auth";
import {
  AdminOrgRoute,
  GONE_ORG,
  TAB_LABELS,
} from "@/pages/platform/admin/orgs/AdminOrgDetailPage";

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

/** Every admin read 404s; the viewer is seeded so the page's own role read never goes out. */
function stubGoneRegister() {
  const urls: string[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url =
      typeof Request !== "undefined" && input instanceof Request ? input.url : String(input);
    urls.push(url);
    return new Response(
      JSON.stringify({ error: { code: "not_found", message: "Organization not found" } }),
      {
        status: 404,
        headers: { "content-type": "application/json" },
      },
    );
  });
  vi.stubGlobal("fetch", fetchMock);
  return urls;
}

function renderAt(path: string, queries: { retry: boolean } = { retry: false }) {
  const qc = createQueryClient(queries.retry ? undefined : { retry: false });
  qc.setQueryData(meKey, STAFF);
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/admin/orgs/:orgId" element={<AdminOrgRoute />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("an org id the register cannot answer for", () => {
  it("resolves a 404 to the not-found state instead of shimmering under the detail shell", async () => {
    stubGoneRegister();
    renderAt(`/admin/orgs/${GONE_ID}`);

    await screen.findByText(GONE_ORG.title);
    expect(screen.getByRole("link", { name: GONE_ORG.action })).toHaveAttribute(
      "href",
      "/admin/orgs",
    );

    // None of the detail shell: no tabs to switch, and nothing still claiming to be loading.
    expect(screen.queryByRole("tab", { name: TAB_LABELS.members })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Loading")).not.toBeInTheDocument();
  });

  it("does not send the second read a mounted detail shell would make", async () => {
    const urls = stubGoneRegister();
    renderAt(`/admin/orgs/${GONE_ID}`);

    await screen.findByText(GONE_ORG.title);
    // The org read itself, and nothing keyed underneath it — the members band is
    // what the shell mounts first, and it never got the chance.
    expect(urls.filter((u) => u.includes(GONE_ID))).toEqual([
      expect.stringContaining(`/admin/v1/orgs/${GONE_ID}`),
    ]);
    expect(urls.some((u) => u.includes("/members"))).toBe(false);
  });

  it("asks once and answers at once, on the client the portal really builds", async () => {
    // The page already resolved a 404 to its not-found — but only once the read had
    // finished failing, and the portal's default retries three times behind a
    // backoff. So the address shimmered for seconds under the detail shell while the
    // register was asked the same refused question four times. A 404 is an answer.
    const urls = stubGoneRegister();
    renderAt(`/admin/orgs/${GONE_ID}`, { retry: true });

    await screen.findByText(GONE_ORG.title);
    expect(urls.filter((u) => u.includes(`/admin/v1/orgs/${GONE_ID}`))).toHaveLength(1);
  });

  it("answers an id that is not an org id without asking the register at all", async () => {
    const urls = stubGoneRegister();
    renderAt("/admin/orgs/not-an-org-id");

    await screen.findByText("This page doesn't exist");
    expect(screen.queryByText(GONE_ORG.title)).not.toBeInTheDocument();
    // Nothing was owed to an address the register could never have written.
    await waitFor(() => expect(urls.filter((u) => u.includes("not-an-org-id"))).toEqual([]));
  });
});
