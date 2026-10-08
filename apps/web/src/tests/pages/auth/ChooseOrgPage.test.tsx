import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { enterOrg, forgetActiveOrg, setOrgNavigator } from "@/api/activeOrg";
import { createQueryClient } from "@/api/queryClient";
import { setSessionChannelFactory } from "@/api/sessionChannel";
import { ChooseOrgPage } from "@/pages/auth/ChooseOrgPage";

// The org chooser, through the real memberships read and the real switch mutation with only
// `fetch` stubbed: it lists each org with the person's role, marks the ones that take their
// single sign-on, switches to the one picked and lands on the requested page, and lets a
// person with one org straight through.

const ORG_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const ORG_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const ORG_C = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";

const threeOrgs = {
  active_org_team_id: ORG_A,
  memberships: [
    { org_team_id: ORG_B, org_name: "Beta Labs", role: "member", sso_required: false, last_active_at: null },
    { org_team_id: ORG_A, org_name: "Acme", role: "admin", sso_required: false, last_active_at: null },
    { org_team_id: ORG_C, org_name: "Corp", role: "member", sso_required: true, last_active_at: null },
  ],
};

const json = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

let navigated: string[];
let switched: unknown[];
let restore: (url: string) => void;

function serve(memberships: unknown) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: Request) => {
      const path = new URL(input.url).pathname;
      if (path === "/api/v1/auth/memberships") return json(memberships);
      if (path === "/api/v1/auth/refresh/org") {
        switched.push(await input.clone().json());
        return json({ user: { org_team_id: ORG_B }, expires_at: new Date(Date.now() + 600_000).toISOString() });
      }
      return json({}, 404);
    }),
  );
}

function renderAt(search: string) {
  return render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter initialEntries={[`/choose-org${search}`]}>
        <Routes>
          <Route path="/choose-org" element={<ChooseOrgPage />} />
          <Route path="/" element={<p>the overview</p>} />
          <Route path="/files" element={<p>the files</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  navigated = [];
  switched = [];
  setSessionChannelFactory(null);
  restore = setOrgNavigator((url) => navigated.push(url));
  forgetActiveOrg();
  enterOrg(ORG_A);
});

afterEach(() => {
  cleanup();
  setOrgNavigator(restore);
  setSessionChannelFactory(undefined);
  forgetActiveOrg();
  vi.unstubAllGlobals();
});

describe("ChooseOrgPage", () => {
  it("says a failed read of the orgs in the generic sentence, never the client's diagnostic", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("", { status: 500 })));
    renderAt("");
    expect(await screen.findByText("Something went wrong on our end. Try again.")).toBeInTheDocument();
    expect(screen.queryByText(/could not load your organizations/)).toBeNull();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("says a failed switch in the generic sentence and stays", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: Request) =>
        new URL(input.url).pathname === "/api/v1/auth/memberships"
          ? json(threeOrgs)
          : new Response("", { status: 503 }),
      ),
    );
    renderAt("");
    fireEvent.click(await screen.findByRole("button", { name: /Beta Labs/ }));
    expect(await screen.findByText("Something went wrong on our end. Try again.")).toBeInTheDocument();
    expect(screen.queryByText(/could not switch organizations/)).toBeNull();
    expect(navigated).toEqual([]);
  });

  it("lists every org with the role there, and marks the single sign-on ones", async () => {
    serve(threeOrgs);
    renderAt("");
    expect(screen.getByRole("heading", { name: "Choose an organization" })).toBeInTheDocument();
    const list = await screen.findByRole("list", { name: "Your organizations" });
    const rows = within(list).getAllByRole("button").map((b) => b.textContent);
    expect(rows).toEqual(["Beta LabsMember", "AcmeAdmin", "CorpSingle sign-on"]);
  });

  it("switches to the org picked and lands where the sign-in was going", async () => {
    serve(threeOrgs);
    renderAt(`?return_to=${encodeURIComponent("/files")}`);
    fireEvent.click(await screen.findByRole("button", { name: /Beta Labs/ }));
    await waitFor(() => expect(navigated).toEqual(["/files"]));
    expect(switched).toEqual([{ org_team_id: ORG_B }]);
  });

  it("continues without a switch when the org picked is the one the session is in", async () => {
    serve(threeOrgs);
    renderAt("");
    fireEvent.click(await screen.findByRole("button", { name: /Acme/ }));
    expect(navigated).toEqual(["/"]);
    expect(switched).toEqual([]);
  });

  it("refuses a return path off the app's own origin", async () => {
    serve(threeOrgs);
    renderAt(`?return_to=${encodeURIComponent("//evil.example")}`);
    fireEvent.click(await screen.findByRole("button", { name: /Acme/ }));
    expect(navigated).toEqual(["/"]);
  });

  it("lets a person with one org straight through", async () => {
    serve({ active_org_team_id: ORG_A, memberships: [threeOrgs.memberships[1]] });
    renderAt("");
    expect(await screen.findByText("the overview")).toBeInTheDocument();
  });
});

// An org that provisioned the person waits for them to join: "Pending" with Join, never a row
// that switches. Joining activates it and then offers the switch into exactly that org.
describe("ChooseOrgPage with pending memberships", () => {
  const ORG_P = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";
  const pendingRow = {
    org_team_id: ORG_P,
    org_name: "Pending Co",
    role: "member",
    sso_required: false,
    last_active_at: null,
    status: "pending",
  };
  const oneActiveOnePending = {
    active_org_team_id: ORG_A,
    memberships: [{ ...threeOrgs.memberships[1], status: "active" }, pendingRow],
  };
  let joined: unknown[];
  let joinAnswer: () => Response;
  let memberships: unknown;
  let multiOrgEnabled: boolean;

  beforeEach(() => {
    joined = [];
    memberships = oneActiveOnePending;
    multiOrgEnabled = true;
    joinAnswer = () => json({ ...pendingRow, status: "active" });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: Request) => {
        const path = new URL(input.url).pathname;
        if (path === "/api/v1/auth/memberships") return json(memberships);
        if (path === "/api/v1/config") return json({ self_hosted: false, multi_org_enabled: multiOrgEnabled });
        if (path === "/api/v1/auth/memberships/join") {
          joined.push(await input.clone().json());
          return joinAnswer();
        }
        if (path === "/api/v1/auth/refresh/org") {
          switched.push(await input.clone().json());
          return json({ user: { org_team_id: ORG_P }, expires_at: new Date(Date.now() + 600_000).toISOString() });
        }
        return json({}, 404);
      }),
    );
  });

  it("shows a pending org even beside a single active one, and never as a switch", async () => {
    renderAt("");
    const list = await screen.findByRole("list", { name: "Your organizations" });
    expect(within(list).getByText("Pending")).toBeInTheDocument();
    // The pending row's only control is Join; the org name itself switches nothing.
    fireEvent.click(within(list).getByText("Pending Co"));
    expect(switched).toEqual([]);
    expect(within(list).queryByRole("button", { name: /^Pending Co/ })).toBeNull();
    expect(within(list).getByRole("button", { name: "Join Pending Co" })).toBeInTheDocument();
  });

  it("joins, then switches into the org joined", async () => {
    renderAt(`?return_to=${encodeURIComponent("/files")}`);
    fireEvent.click(await screen.findByRole("button", { name: "Join Pending Co" }));
    await waitFor(() => expect(joined).toEqual([{ org_team_id: ORG_P }]));
    fireEvent.click(await screen.findByRole("button", { name: "Switch to Pending Co" }));
    await waitFor(() => expect(navigated).toEqual(["/files"]));
    expect(switched).toEqual([{ org_team_id: ORG_P }]);
  });

  it("follows the org's single sign-on when joining needs it, and joins nothing here", async () => {
    joinAnswer = () =>
      json(
        {
          error: {
            code: "sso_required",
            message: "This organization requires single sign-on.",
            trace_id: "t",
            details: { login_url: "https://api.example.test/sso/p" },
          },
        },
        409,
      );
    renderAt("");
    fireEvent.click(await screen.findByRole("button", { name: "Join Pending Co" }));
    await waitFor(() => expect(navigated).toEqual(["https://api.example.test/sso/p"]));
    expect(screen.queryByRole("button", { name: "Switch to Pending Co" })).toBeNull();
  });

  it("says a refused join and offers no switch", async () => {
    joinAnswer = () =>
      json(
        { error: { code: "login_method_not_allowed", message: "This organization doesn't allow this sign-in method.", trace_id: "t" } },
        409,
      );
    renderAt("");
    fireEvent.click(await screen.findByRole("button", { name: "Join Pending Co" }));
    expect(await screen.findByText("This organization doesn't allow this sign-in method.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Switch to Pending Co" })).toBeNull();
    expect(navigated).toEqual([]);
  });

  it("says a join the server does not know", async () => {
    joinAnswer = () => json({ error: { code: "not_found", message: "Not found", trace_id: "t" } }, 404);
    renderAt("");
    fireEvent.click(await screen.findByRole("button", { name: "Join Pending Co" }));
    expect(await screen.findByText("Not found")).toBeInTheDocument();
  });

  it("with several orgs per person off, ignores a pending row and lets one org straight through", async () => {
    multiOrgEnabled = false;
    renderAt("");
    expect(await screen.findByText("the overview")).toBeInTheDocument();
    expect(screen.queryByText("Pending Co")).toBeNull();
    expect(joined).toEqual([]);
  });

  it("with several orgs per person off, lists only the orgs that can be entered", async () => {
    multiOrgEnabled = false;
    memberships = { ...threeOrgs, memberships: [...threeOrgs.memberships, pendingRow] };
    renderAt("");
    const list = await screen.findByRole("list", { name: "Your organizations" });
    expect(within(list).getAllByRole("button").map((b) => b.textContent)).toEqual([
      "Beta LabsMember",
      "AcmeAdmin",
      "CorpSingle sign-on",
    ]);
    expect(within(list).queryByText("Pending")).toBeNull();
  });
});
