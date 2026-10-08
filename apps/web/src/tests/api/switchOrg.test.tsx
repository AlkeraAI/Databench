// Switching orgs, and every other tab following. `useSwitchOrg` posts to the switch route
// with its CSRF header, drops what the tab holds, tells the other tabs and reloads; an org
// that wants its single sign-on first sends the tab there. `SessionBridge` reloads a tab when
// another tab announces a switch or a sign-out. The BroadcastChannel is a fake handed in
// through the channel seam; `fetch` is stubbed; navigation is captured.

import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { activeOrgId, enterOrg, forgetActiveOrg, setOrgNavigator } from "@/api/activeOrg";
import { useLogout, useSwitchOrg } from "@/api/auth";
import { createQueryClient } from "@/api/queryClient";
import {
  setSessionChannelFactory,
  type SessionChannelLike,
  type SessionMessage,
} from "@/api/sessionChannel";
import { SessionBridge } from "@/app/boot/SessionBridge";

const ORG_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const ORG_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

/** A BroadcastChannel stand-in: every instance with the same name hears every OTHER
 *  instance's messages, which is exactly what the real one does across tabs. */
class FakeChannel implements SessionChannelLike {
  static open: FakeChannel[] = [];
  readonly posted: unknown[] = [];
  private listeners = new Set<(event: MessageEvent) => void>();
  constructor(readonly name: string) {
    FakeChannel.open.push(this);
  }
  postMessage(message: unknown): void {
    this.posted.push(message);
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
    for (const listener of this.listeners) listener(new MessageEvent("message", { data: message }));
  }
}

const json = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

let navigated: string[];
let sent: Request[];
let restore: (url: string) => void;

function serve(handler: (request: Request) => Response) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: Request) => {
      sent.push(input);
      return handler(input);
    }),
  );
}

beforeEach(() => {
  navigated = [];
  sent = [];
  FakeChannel.open = [];
  setSessionChannelFactory((name) => new FakeChannel(name));
  restore = setOrgNavigator((url) => navigated.push(url));
  forgetActiveOrg();
  enterOrg(ORG_A, "Acme");
});

afterEach(() => {
  cleanup();
  setOrgNavigator(restore);
  setSessionChannelFactory(undefined);
  forgetActiveOrg();
  vi.unstubAllGlobals();
});

function wrap(qc: QueryClient, node: React.ReactNode) {
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
}

function Switcher({ target }: { target?: string }) {
  const sw = useSwitchOrg();
  return (
    <>
      <button type="button" onClick={() => sw.mutate({ orgTeamId: ORG_B, target })}>
        switch
      </button>
      {sw.isError ? <p>failed</p> : null}
    </>
  );
}

describe("useSwitchOrg", () => {
  it("posts with the CSRF header, drops the cache, tells the other tabs and reloads at the target", async () => {
    serve(() =>
      json({ user: { org_team_id: ORG_B, org_name: "Beta" }, expires_at: new Date(Date.now() + 600_000).toISOString() }),
    );
    const qc = createQueryClient({ retry: false });
    qc.setQueryData(["auth", "me"], { org_team_id: ORG_A });
    const otherTab = new FakeChannel("alkera-session");
    wrap(qc, <Switcher target="/files" />);

    fireEvent.click(screen.getByRole("button", { name: "switch" }));

    await waitFor(() => expect(navigated).toEqual(["/files"]));
    const request = sent.find((r) => new URL(r.url).pathname === "/api/v1/auth/refresh/org");
    expect(request?.headers.get("X-Requested-With")).toBe("alkera");
    expect(await request?.clone().json()).toEqual({ org_team_id: ORG_B });
    expect(qc.getQueryData(["auth", "me"])).toBeUndefined();
    const announced = FakeChannel.open.filter((c) => c !== otherTab).flatMap((c) => c.posted);
    expect(announced).toEqual([{ type: "org_switched", org_team_id: ORG_B }]);
    expect(activeOrgId()).toBe(ORG_B);
  });

  it("goes to the org's single sign-on when the switch answers with one", async () => {
    const loginUrl = "http://api.test/api/v1/auth/sso/b/login?return_to=%2F%3Fswitch_org%3Db";
    serve(() =>
      json({ error: { code: "sso_required", message: "SSO", details: { login_url: loginUrl } } }, 409),
    );
    const qc = createQueryClient({ retry: false });
    wrap(qc, <Switcher />);
    fireEvent.click(screen.getByRole("button", { name: "switch" }));
    await waitFor(() => expect(navigated).toEqual([loginUrl]));
    expect(FakeChannel.open.flatMap((c) => c.posted)).toEqual([]);
    expect(activeOrgId()).toBe(ORG_A);
  });

  it("stays put on a plain refusal", async () => {
    serve(() => json({ error: { code: "not_found", message: "Not found" } }, 404));
    const qc = createQueryClient({ retry: false });
    wrap(qc, <Switcher />);
    fireEvent.click(screen.getByRole("button", { name: "switch" }));
    expect(await screen.findByText("failed")).toBeInTheDocument();
    expect(navigated).toEqual([]);
  });
});

describe("signing out", () => {
  it("tells the other tabs", async () => {
    serve(() => json({ message: "Logged out" }));
    const qc = createQueryClient({ retry: false });
    function SignOut() {
      const logout = useLogout();
      return (
        <button type="button" onClick={() => logout.mutate()}>
          out
        </button>
      );
    }
    wrap(qc, <SignOut />);
    fireEvent.click(screen.getByRole("button", { name: "out" }));
    await waitFor(() =>
      expect(FakeChannel.open.flatMap((c) => c.posted)).toEqual([{ type: "logged_out" }]),
    );
  });
});

describe("SessionBridge", () => {
  function post(message: SessionMessage) {
    act(() => new FakeChannel("alkera-session").postMessage(message));
  }

  it.each<[string, SessionMessage]>([
    ["a switch to another org", { type: "org_switched", org_team_id: ORG_B }],
    ["a sign-out", { type: "logged_out" }],
  ])("reloads this tab when another tab announces %s", (_label, message) => {
    const qc = createQueryClient({ retry: false });
    qc.setQueryData(["auth", "me"], { org_team_id: ORG_A });
    wrap(qc, <SessionBridge />);
    post(message);
    expect(navigated).toEqual(["/"]);
    expect(qc.getQueryData(["auth", "me"])).toBeUndefined();
  });

  it("ignores a switch into the org this tab already shows", () => {
    const qc = createQueryClient({ retry: false });
    wrap(qc, <SessionBridge />);
    post({ type: "org_switched", org_team_id: ORG_A });
    expect(navigated).toEqual([]);
  });

  it("ignores a message that is not a session announcement", () => {
    const qc = createQueryClient({ retry: false });
    wrap(qc, <SessionBridge />);
    act(() => new FakeChannel("alkera-session").postMessage({ type: "something_else" }));
    expect(navigated).toEqual([]);
  });

  it("does nothing, and does not throw, in a browser without BroadcastChannel", () => {
    setSessionChannelFactory(null);
    const qc = createQueryClient({ retry: false });
    expect(() => wrap(qc, <SessionBridge />)).not.toThrow();
  });
});
