import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/errors";

// The live seam classifier (useLiveTeamsState): it maps the React Query results to a page status +
// a detail status. The one correctness point that must not regress: the roster endpoint is
// team-admin gated, so its 403 is an EXPECTED answer that resolves to a first-class `forbidden`
// detail — NOT an `error`. We mock the hooks (the network boundary) and assert the classification
// through the resolved state, never internals.

type FakeQuery = { data?: unknown; isError?: boolean; isLoading?: boolean; error?: unknown; refetch?: () => unknown };
const hooks = {
  teams: { data: [], isError: false } as FakeQuery,
  members: { data: undefined, isError: false, isLoading: false } as FakeQuery,
  invitations: { data: [], isError: false } as FakeQuery,
  viewer: { data: { id: "viewer" } } as FakeQuery,
};

vi.mock("@/api/auth", () => ({ useCurrentUser: () => hooks.viewer }));
vi.mock("@/api/teams", () => ({
  useTeams: () => hooks.teams,
  useTeamMembers: () => hooks.members,
  useTeamInvitations: () => hooks.invitations,
}));
// Ancestor rosters fan out through useQueries — none needed for the classification under test.
vi.mock("@tanstack/react-query", async (orig) => ({ ...(await orig<object>()), useQueries: () => [] }));

const { useLiveTeamsSync, useTeamsData, resetTeamsStore, useTeamsStore } = await import("@/pages/organization/teams/data/provider");

const ORG = [
  { id: "root", name: "Org", parent_team_id: null, is_root: true, created_at: "2026-01-01T00:00:00Z", member_count: 1 },
  { id: "platform", name: "Platform", parent_team_id: "root", is_root: false, created_at: "2026-01-01T00:00:00Z", member_count: 1 },
];

afterEach(cleanup);
beforeEach(() => {
  resetTeamsStore();
  hooks.teams = { data: ORG, isError: false, refetch: () => {} };
  hooks.members = { data: undefined, isError: false, isLoading: false, refetch: () => {} };
  hooks.invitations = { data: [], isError: false, refetch: () => {} };
  // An org admin: admin of the root and, by descent, of everything under it.
  hooks.viewer = { data: { id: "viewer", admin_team_ids: ["root", "platform"] } };
});

// The page runs the live source for its selection and reads the resolved state back from the store.
// `selected` defaults to the explicit "platform" pick; passing `undefined` exercises the seam's
// default-selection branch (`targetId = selectedId ?? rootId`).
function Probe({ selected = "platform" as string | undefined }: { selected?: string | undefined }) {
  useLiveTeamsSync(selected);
  const { status, detail } = useTeamsData();
  return (
    <div>
      <span data-testid="status">{status}</span>
      <span data-testid="detail">{detail.status}</span>
    </div>
  );
}
const statusOf = () => [screen.getByTestId("status").textContent, screen.getByTestId("detail").textContent];

describe("useLiveTeamsState classification", () => {
  it("maps a 403 on the roster to a forbidden detail, not an error", () => {
    hooks.members = { isError: true, error: new ApiError(403, null, "forbidden"), refetch: () => {} };
    render(<Probe />);
    expect(statusOf()).toEqual(["ready", "forbidden"]); // tree still loads; only the detail is gated
  });

  it("maps a non-403 roster failure to an error detail", () => {
    hooks.members = { isError: true, error: new ApiError(503, null, "down"), refetch: () => {} };
    render(<Probe />);
    expect(statusOf()).toEqual(["ready", "error"]);
  });

  it("resolves to ready once the roster lands", () => {
    hooks.members = { data: [], isLoading: false, refetch: () => {} };
    render(<Probe />);
    expect(statusOf()).toEqual(["ready", "ready"]);
  });

  it("is loading while the roster is still in flight", () => {
    // Drive the store OFF its initial `loading` first, so a passing "loading" assertion proves the
    // writer actively re-derived it from an in-flight roster — not that the store never moved. (The
    // page status resolves to `ready` once the list + viewer land; only the detail is still loading.)
    act(() =>
      useTeamsStore.getState().setResolved({
        status: "ready",
        graph: null,
        detail: { status: "ready", errorMessage: null },
        errorMessage: null,
        retry: () => {},
      }),
    );
    hooks.members = { data: undefined, isLoading: true, refetch: () => {} };
    render(<Probe />);
    expect(statusOf()).toEqual(["ready", "loading"]);
  });

  it("resolves the ORG ROOT's roster when no team is selected (the default landing selection)", () => {
    // No explicit selection → the seam must fall back to the root team (`is_root`) and resolve ITS
    // roster, not sit in loading forever. A regression dropping the `?? rootId` default leaves
    // targetId undefined → the page stays `loading`, which this catches.
    hooks.members = { data: [], isLoading: false, refetch: () => {} };
    render(<Probe selected={undefined} />);
    expect(statusOf()).toEqual(["ready", "ready"]);
  });

  it("resolves a team the viewer does not administer to forbidden, whatever the roster would say", () => {
    // `admin_team_ids` is the server's answer to "may this viewer read that roster"; a team outside
    // it is the forbidden surface without asking (a 403 answered in advance), even though a roster
    // is sitting in the cache.
    hooks.viewer = { data: { id: "viewer", admin_team_ids: ["platform"] } };
    hooks.members = { data: [], isLoading: false, refetch: () => {} };
    render(<Probe selected="root" />);
    expect(statusOf()).toEqual(["ready", "forbidden"]);
  });

  it("fails the whole page only when the team LIST itself errors", () => {
    hooks.teams = { isError: true, error: new ApiError(500, null, "nope"), refetch: () => {} };
    render(<Probe />);
    expect(screen.getByTestId("status").textContent).toBe("error");
  });
});
