// The team masthead's primary action degrades to icon-only at the tightest panes
// (TeamDetail.module.css hides `.alk-btn__label` under a 420px container query). The visible text is
// then gone, so the button has to carry its name itself or a screen reader announces a bare "button".

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { toTeamsGraph, type TeamsWire } from "@/pages/organization/teams/data/adapt";
import { TeamDetail, type DetailActions } from "@/pages/organization/teams/detail/TeamDetail";

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify([]), { status: 200, headers: { "Content-Type": "application/json" } })),
  );
});
afterEach(() => {
  vi.unstubAllGlobals();
  cleanup();
});

const WIRE: TeamsWire = {
  teams: [
    { id: "root", name: "Org", parent_team_id: null, is_root: true, created_at: "2026-01-01T00:00:00Z", member_count: 1 },
    { id: "platform", name: "Platform", parent_team_id: "root", is_root: false, created_at: "2026-01-01T00:00:00Z", member_count: 1 },
  ],
  viewerId: "viewer",
  adminTeamIds: ["root", "platform"],
  selectedId: "platform",
  members: [
    {
      user_id: "viewer",
      team_id: "platform",
      team_name: "Platform",
      role: "admin",
      display_name: "Viewer",
      first_name: "Vera",
      last_name: "Ng",
      email: "viewer@example.com",
      created_at: "2026-01-01T00:00:00Z",
      effective_role: "admin",
      role_display: "Admin",
    },
  ],
  invites: [],
};

function actions(): DetailActions {
  return {
    addMember: vi.fn(),
    removeMember: vi.fn(),
    changeRole: vi.fn(),
    createSub: vi.fn(),
    renameTeam: vi.fn(),
    deleteTeam: vi.fn(),
    selectTeam: vi.fn(),
    revokeInvite: vi.fn(),
  } as unknown as DetailActions;
}

describe("team detail primary action", () => {
  it("keeps its accessible name when the label is hidden at phone width", () => {
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter>
          <TeamDetail
            graph={toTeamsGraph(WIRE)}
            teamId="platform"
            detail={{ status: "ready", errorMessage: null }}
            actions={actions()}
            onRetry={vi.fn()}
          />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    // The masthead's own button — the one whose visible label the narrow container query removes.
    const named = screen.getAllByRole("button", { name: "Add member" });
    expect(named.length).toBeGreaterThan(0);
    // The name must survive the label being stripped from the DOM the way the CSS strips it visually.
    const masthead = named[0];
    masthead.querySelector(".alk-btn__label")?.remove();
    expect(masthead.getAttribute("aria-label")).toBe("Add member");
  });
});
