import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { activeOrgId, enterOrg, forgetActiveOrg, setOrgNavigator } from "@/api/activeOrg";
import { GENERIC_FAILURE } from "@/api/errors";
import { createQueryClient } from "@/api/queryClient";
import { setSessionChannelFactory } from "@/api/sessionChannel";
import { LeaveOrgRow } from "@/pages/organization/settings/LeaveOrgRow";
import { RAW_FAILURES, expectNoRawFailureText } from "@/tests/fixtures/rawFailures";

// "Leave organization", through the real hooks with only `fetch` stubbed: offered only where the
// public config says the server runs with several orgs per person, confirmed before anything is
// sent, then a switch into exactly the org the server names next, or the no-organization landing
// when it names none. The last-admin refusal is said and nothing moves.

const ORG_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const ORG_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

const json = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

let sent: Request[];
let navigated: string[];
let restore: (url: string) => void;
let multiOrgEnabled: boolean;
let leaveAnswer: () => Response;

function serve() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: Request) => {
      sent.push(input.clone());
      const path = new URL(input.url).pathname;
      if (path === "/api/v1/config") return json({ self_hosted: false, multi_org_enabled: multiOrgEnabled });
      if (path === "/api/v1/orgs/current/leave") return leaveAnswer();
      if (path === "/api/v1/auth/refresh/org") {
        return json({ user: { org_team_id: ORG_B }, expires_at: new Date(Date.now() + 600_000).toISOString() });
      }
      return json({}, 404);
    }),
  );
}

const posted = (path: string) => sent.filter((r) => r.method === "POST" && new URL(r.url).pathname === path);

function renderRow() {
  render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter>
        <LeaveOrgRow orgName="Acme" />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function leave() {
  fireEvent.click(await screen.findByRole("button", { name: "Leave" }));
  // Nothing is sent until the dialog is answered.
  expect(posted("/api/v1/orgs/current/leave")).toHaveLength(0);
  const dialog = await screen.findByRole("dialog", { name: "Leave Acme?" });
  fireEvent.click(within(dialog).getByRole("button", { name: "Leave" }));
}

beforeEach(() => {
  sent = [];
  navigated = [];
  multiOrgEnabled = true;
  leaveAnswer = () => json({ next_org_team_id: ORG_B });
  setSessionChannelFactory(null);
  restore = setOrgNavigator((url) => navigated.push(url));
  forgetActiveOrg();
  enterOrg(ORG_A);
  serve();
});

afterEach(() => {
  cleanup();
  setOrgNavigator(restore);
  setSessionChannelFactory(undefined);
  forgetActiveOrg();
  vi.unstubAllGlobals();
});

describe("LeaveOrgRow", () => {
  it("is not offered, and asks nothing, with several orgs per person off", async () => {
    multiOrgEnabled = false;
    renderRow();
    await waitFor(() => expect(sent.some((r) => new URL(r.url).pathname === "/api/v1/config")).toBe(true));
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.queryByText("Leave organization")).toBeNull();
    expect(sent.map((r) => new URL(r.url).pathname)).toEqual(["/api/v1/config"]);
  });

  it("leaves after the confirm, then switches into the org the server names", async () => {
    renderRow();
    await leave();
    await waitFor(() => expect(navigated).toEqual(["/"]));
    expect(posted("/api/v1/orgs/current/leave")).toHaveLength(1);
    expect(await posted("/api/v1/auth/refresh/org")[0].json()).toEqual({ org_team_id: ORG_B });
  });

  it("goes to the no-organization landing when no org is left, and renews nothing", async () => {
    leaveAnswer = () => json({ next_org_team_id: null });
    renderRow();
    await leave();
    await waitFor(() => expect(navigated).toEqual(["/no-organization"]));
    expect(posted("/api/v1/auth/refresh/org")).toHaveLength(0);
    expect(sent.filter((r) => new URL(r.url).pathname === "/api/v1/auth/refresh")).toHaveLength(0);
    expect(activeOrgId()).toBeNull();
  });

  it("says the last-admin refusal and moves nowhere", async () => {
    leaveAnswer = () =>
      json(
        {
          error: {
            code: "last_admin",
            message: "You're the only admin of this organization. Make someone else an admin before you leave.",
            trace_id: "t",
          },
        },
        409,
      );
    renderRow();
    await leave();
    expect(
      await screen.findByText("You're the only admin of this organization. Make someone else an admin before you leave."),
    ).toBeInTheDocument();
    expect(navigated).toEqual([]);
    expect(posted("/api/v1/auth/refresh/org")).toHaveLength(0);
  });

  it.each(RAW_FAILURES)("says %s in the general sentence, never in its own words", async (_label, answer) => {
    leaveAnswer = answer;
    renderRow();
    await leave();
    expect(await screen.findByText(GENERIC_FAILURE)).toBeInTheDocument();
    expectNoRawFailureText();
    expect(navigated).toEqual([]);
  });
});
