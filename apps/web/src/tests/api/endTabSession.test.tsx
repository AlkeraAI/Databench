// Every way a session ends in a tab goes through one teardown. Each case drives the real
// caller (the logout, leave and switch hooks, the landing, the unauthorized and session
// bridges) with only `fetch` stubbed, and asserts what the tab and the other tabs can see:
// the cache, the org the tab names on requests, what a second tab hears on the session
// channel, and where the tab navigates. The channel is a fake handed in through its seam;
// the second tab is a separate channel instance, as a real second tab would be.

import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { activeOrgId, enterOrg, forgetActiveOrg, orgChanged, setOrgNavigator } from "@/api/activeOrg";
import { meKey, useLogout, useSwitchOrg } from "@/api/auth";
import { fireUnauthorized } from "@/api/client";
import { NO_ORGANIZATION_PATH, useLandingCreateOrg, useLeaveOrg } from "@/api/orgs";
import { createQueryClient } from "@/api/queryClient";
import {
  SESSION_CHANNEL_NAME,
  setSessionChannelFactory,
  type SessionChannelLike,
} from "@/api/sessionChannel";
import { SessionBridge } from "@/app/boot/SessionBridge";
import { UnauthorizedBridge } from "@/app/boot/UnauthorizedBridge";

const ORG_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const ORG_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const OTHER_DATA = ["teams", "list"] as const;

/** A BroadcastChannel stand-in: every instance with the same name hears every OTHER
 *  instance's messages, as the real one does across tabs. */
class FakeChannel implements SessionChannelLike {
  static open: FakeChannel[] = [];
  readonly heard: unknown[] = [];
  private listeners = new Set<(event: MessageEvent) => void>();
  constructor(readonly name: string) {
    FakeChannel.open.push(this);
  }
  postMessage(message: unknown): void {
    for (const other of FakeChannel.open) {
      if (other !== this && other.name === this.name) other.deliver(message);
    }
  }
  addEventListener(_type: "message", listener: (event: MessageEvent) => void): void {
    this.listeners.add(listener);
  }
  removeEventListener(_type: "message", listener: (event: MessageEvent) => void): void {
    this.listeners.delete(listener);
  }
  private deliver(message: unknown): void {
    this.heard.push(message);
    for (const listener of this.listeners) listener(new MessageEvent("message", { data: message }));
  }
}

const json = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

const signedIn = () => ({ user: { org_team_id: ORG_B, org_name: "Beta" }, expires_at: new Date(Date.now() + 600_000).toISOString() });

let navigated: string[];
let restore: (url: string) => void;
let otherTab: FakeChannel;
let qc: QueryClient;

function serve(answers: Record<string, () => Response>) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: Request) => {
      const answer = answers[new URL(input.url).pathname];
      return answer ? answer() : json({}, 404);
    }),
  );
}

beforeEach(() => {
  navigated = [];
  FakeChannel.open = [];
  setSessionChannelFactory((name) => new FakeChannel(name));
  otherTab = new FakeChannel(SESSION_CHANNEL_NAME);
  restore = setOrgNavigator((url) => navigated.push(url));
  forgetActiveOrg();
  enterOrg(ORG_A, "Acme");
  qc = createQueryClient({ retry: false });
  qc.setQueryData(meKey, { org_team_id: ORG_A });
  qc.setQueryData(OTHER_DATA, ["held for Acme"]);
});

afterEach(() => {
  cleanup();
  setOrgNavigator(restore);
  setSessionChannelFactory(undefined);
  forgetActiveOrg();
  vi.unstubAllGlobals();
});

function wrap(node: React.ReactNode) {
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
}

function Act({ run, label }: { run: () => void; label: string }) {
  return (
    <button type="button" onClick={run}>
      {label}
    </button>
  );
}

function Logout() {
  const logout = useLogout();
  return <Act label="go" run={() => logout.mutate()} />;
}

function Leave() {
  const leave = useLeaveOrg();
  return <Act label="go" run={() => leave.mutate()} />;
}

function Switch() {
  const sw = useSwitchOrg();
  return <Act label="go" run={() => sw.mutate({ orgTeamId: ORG_B, target: "/files" })} />;
}

function LandingCreate() {
  const create = useLandingCreateOrg();
  return <Act label="go" run={() => create.mutate("Beta")} />;
}

const go = () => fireEvent.click(screen.getByRole("button", { name: "go" }));

describe("this tab ends its own session", () => {
  it("signing out drops the data and the org, keeps the signed-out identity and tells the other tabs", async () => {
    serve({ "/api/v1/auth/logout": () => json({}) });
    wrap(<Logout />);
    go();
    await waitFor(() => expect(otherTab.heard).toEqual([{ type: "logged_out" }]));
    expect(qc.getQueryData(OTHER_DATA)).toBeUndefined();
    expect(qc.getQueryData(meKey)).toBeNull();
    expect(activeOrgId()).toBeNull();
    // The route guard redirects; the teardown itself goes nowhere.
    expect(navigated).toEqual([]);
  });

  it("leaving the last org tells the other tabs and goes to the no-organization landing", async () => {
    serve({ "/api/v1/orgs/current/leave": () => json({ next_org_team_id: null }) });
    wrap(<Leave />);
    go();
    await waitFor(() => expect(navigated).toEqual([NO_ORGANIZATION_PATH]));
    expect(otherTab.heard).toEqual([{ type: "org_left" }]);
    expect(qc.getQueryData(OTHER_DATA)).toBeUndefined();
    expect(qc.getQueryData(meKey)).toBeNull();
    expect(activeOrgId()).toBeNull();
  });

  it("leaving for another org leaves the teardown to the switch that follows", async () => {
    serve({ "/api/v1/orgs/current/leave": () => json({ next_org_team_id: ORG_B }) });
    wrap(<Leave />);
    go();
    await waitFor(() => expect(qc.getMutationCache().getAll()[0]?.state.status).toBe("success"));
    expect(otherTab.heard).toEqual([]);
    expect(navigated).toEqual([]);
    expect(qc.getQueryData(OTHER_DATA)).toEqual(["held for Acme"]);
    expect(activeOrgId()).toBe(ORG_A);
  });

  it("switching drops the old org's data, enters the new org, tells the other tabs and reloads at the target", async () => {
    serve({ "/api/v1/auth/refresh/org": () => json(signedIn()) });
    wrap(<Switch />);
    go();
    await waitFor(() => expect(navigated).toEqual(["/files"]));
    expect(otherTab.heard).toEqual([{ type: "org_switched", org_team_id: ORG_B }]);
    expect(qc.getQueryData(OTHER_DATA)).toBeUndefined();
    expect(qc.getQueryData(meKey)).toBeUndefined();
    expect(activeOrgId()).toBe(ORG_B);
  });

  it("entering an org from the landing enters it and reloads at the overview, announcing nothing", async () => {
    forgetActiveOrg();
    serve({ "/api/v1/auth/refresh/org/new": () => json(signedIn()) });
    wrap(<LandingCreate />);
    go();
    await waitFor(() => expect(navigated).toEqual(["/"]));
    expect(otherTab.heard).toEqual([]);
    expect(qc.getQueryData(OTHER_DATA)).toBeUndefined();
    expect(activeOrgId()).toBe(ORG_B);
  });

  it("a refused credential signs the tab out where it stands", () => {
    wrap(<UnauthorizedBridge />);
    act(() => fireUnauthorized());
    expect(qc.getQueryData(OTHER_DATA)).toBeUndefined();
    expect(qc.getQueryData(meKey)).toBeNull();
    expect(activeOrgId()).toBeNull();
    expect(otherTab.heard).toEqual([]);
    expect(navigated).toEqual([]);
  });
});

describe("another tab ended the session", () => {
  it.each([
    ["signed out", { type: "logged_out" }, "/"],
    ["switched to another org", { type: "org_switched", org_team_id: ORG_B }, "/"],
    ["left its last org", { type: "org_left" }, NO_ORGANIZATION_PATH],
  ])("it %s: this tab drops its data and follows", (_why, message, landing) => {
    wrap(<SessionBridge />);
    act(() => otherTab.postMessage(message));
    expect(navigated).toEqual([landing]);
    expect(qc.getQueryData(OTHER_DATA)).toBeUndefined();
  });

  it("left its last org: this tab names no org either", () => {
    wrap(<SessionBridge />);
    act(() => otherTab.postMessage({ type: "org_left" }));
    expect(activeOrgId()).toBeNull();
    expect(qc.getQueryData(meKey)).toBeNull();
  });

  it("a switch into the org this tab already shows changes nothing", () => {
    wrap(<SessionBridge />);
    act(() => otherTab.postMessage({ type: "org_switched", org_team_id: ORG_A }));
    expect(navigated).toEqual([]);
    expect(qc.getQueryData(OTHER_DATA)).toEqual(["held for Acme"]);
  });

  it("the server says the session moved: the data is dropped before the tab reloads once", () => {
    wrap(<SessionBridge />);
    act(() => orgChanged("Beta"));
    expect(qc.getQueryData(OTHER_DATA)).toBeUndefined();
    expect(navigated).toEqual(["/"]);
  });
});
