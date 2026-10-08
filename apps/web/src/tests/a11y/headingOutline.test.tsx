// The shell's masthead owns the page's only h1, so a page's own top-level sections are the second
// level. The admin registers left theirs at the Card default of 3, which put a hole in the outline
// of every register: a screen-reader user navigating by heading jumps h1 → h3 with nothing between.

import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { components } from "@alkera/sdk";

vi.mock("@/api/admin/admin", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useAdminUsers: () => ({
    data: [
      {
        id: "8b1f0c22-0000-4000-8000-000000000001",
        email: "ada@example.com",
        display_name: "Ada Lovelace",
        created_at: "2026-01-01T00:00:00Z",
        email_verified_at: "2026-01-01T00:00:00Z",
        mtd_billed_nanos: 0,
        platform_role: null,
        banned: false,
        ban_reason: null,
        disposable_email: false,
        signup_ip: null,
        org_name: null,
      },
    ],
    isError: false,
    refetch: vi.fn(),
  }),
}));

const { AdminOrgsPage } = await import("@/pages/platform/admin/orgs/AdminOrgsPage");
const { AdminUsersPage } = await import("@/pages/platform/admin/users/AdminUsersPage");

type Org = components["schemas"]["OrgRead"];

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const ORGS: Org[] = [
  { name: "Tideline Analytics", id: "a1b2c3d4-aaaa", parent_team_id: null, is_root: true, created_at: "2026-01-01T00:00:00Z", member_count: 42 },
];

function withSeed(node: ReactNode, seed?: (qc: QueryClient) => void) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  seed?.(qc);
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
}

/** The levels of every heading the page renders, in document order. */
function levels(): number[] {
  return screen.getAllByRole("heading").map((h) => Number(h.tagName.slice(1)));
}

describe("the admin registers' heading outline", () => {
  it("opens the organizations register at the level below the shell's title", async () => {
    withSeed(<AdminOrgsPage />, (qc) => qc.setQueryData(["admin", "orgs"], ORGS));
    expect(await screen.findByRole("heading", { name: "Organizations" })).toBeInTheDocument();
    // The shell renders the h1; the page may not skip a level beneath it.
    expect(Math.min(...levels())).toBe(2);
  });

  it("opens the users register at the level below the shell's title", async () => {
    withSeed(<AdminUsersPage />);
    expect(await screen.findByRole("heading", { name: "Users" })).toBeInTheDocument();
    expect(Math.min(...levels())).toBe(2);
  });
});
