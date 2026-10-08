import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { enterOrg, forgetActiveOrg, setOrgNavigator } from "@/api/activeOrg";
import { GENERIC_FAILURE } from "@/api/errors";
import { createQueryClient } from "@/api/queryClient";
import { setSessionChannelFactory } from "@/api/sessionChannel";
import { LinkSsoPage, SSO_LINK_EXPIRED } from "@/pages/auth/LinkSsoPage";
import { expectNoRawFailureText, serverFellOver } from "@/tests/fixtures/rawFailures";

// The single sign-on link page, through the real hooks with only `fetch` stubbed: it names the
// masked email and the org, links on "Link" and offers the switch into exactly that org when the
// link also joined it, says the server's message when it did not, says every refusal, and
// discards the request on "Cancel".

const ORG_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const ORG_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

const json = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const refusal = (status: number, code: string, message: string) =>
  json({ error: { code, message, trace_id: "t" } }, status);

const LINK = { org_team_id: ORG_B, org_name: "Beta Labs", email_masked: "v***@x.io" };

let sent: Request[];
let navigated: string[];
let restore: (url: string) => void;
let answers: Record<string, () => Response>;

function serve() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: Request) => {
      sent.push(input.clone());
      const path = new URL(input.url).pathname;
      const answer = answers[path];
      if (answer) return answer();
      if (path === "/api/v1/auth/refresh/org") {
        return json({ user: { org_team_id: ORG_B }, expires_at: new Date(Date.now() + 600_000).toISOString() });
      }
      return json({}, 404);
    }),
  );
}

function Where() {
  const location = useLocation();
  return <p data-testid="where">{location.pathname + location.search}</p>;
}

function renderAt(path = `/link-sso?org=${ORG_B}`) {
  return render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/link-sso" element={<LinkSsoPage />} />
          <Route path="*" element={<Where />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const posted = (path: string) => sent.filter((r) => r.method === "POST" && new URL(r.url).pathname === path);

beforeEach(() => {
  sent = [];
  navigated = [];
  answers = {
    "/api/v1/auth/sso-link": () => json(LINK),
    "/api/v1/auth/sso-link/cancel": () => json({ cancelled: true }),
    "/api/v1/auth/logout": () => json({ message: "ok" }),
  };
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

describe("LinkSsoPage", () => {
  it("asks whether to link the masked email to the org's single sign-on", async () => {
    renderAt();
    expect(
      await screen.findByRole("heading", { name: "Link v***@x.io to Beta Labs's single sign-on?" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Link" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeInTheDocument();
  });

  it("links, then switches into the org the link joined", async () => {
    answers["/api/v1/auth/sso-link/confirm"] = () =>
      json({ linked: true, joined: true, org_team_id: ORG_B, message: null });
    renderAt();
    fireEvent.click(await screen.findByRole("button", { name: "Link" }));
    fireEvent.click(await screen.findByRole("button", { name: "Switch to Beta Labs" }));
    await waitFor(() => expect(navigated).toEqual(["/"]));
    expect(posted("/api/v1/auth/sso-link/confirm")).toHaveLength(1);
    expect(await posted("/api/v1/auth/refresh/org")[0].json()).toEqual({ org_team_id: ORG_B });
  });

  it("says what the org must do when the link joined nothing, and offers no switch", async () => {
    answers["/api/v1/auth/sso-link/confirm"] = () =>
      json({ linked: true, joined: false, org_team_id: ORG_B, message: "Ask an admin of Beta Labs to invite you." });
    renderAt();
    fireEvent.click(await screen.findByRole("button", { name: "Link" }));
    expect(await screen.findByText("Ask an admin of Beta Labs to invite you.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Switch to Beta Labs" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByTestId("where")).toHaveTextContent(/^\/$/);
    expect(posted("/api/v1/auth/refresh/org")).toHaveLength(0);
  });

  it.each([
    ["sso_subject_linked", 409, "That sign-on identity is already linked to another account."],
    ["sso_provider_linked", 409, "This account is already linked to that organization's sign-on."],
    ["account_deactivated", 403, "This account is deactivated."],
  ])("says the %s refusal and links nothing", async (code, status, message) => {
    answers["/api/v1/auth/sso-link/confirm"] = () => refusal(status, code, message);
    renderAt();
    fireEvent.click(await screen.findByRole("button", { name: "Link" }));
    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Switch to Beta Labs" })).toBeNull();
  });

  it("says the link expired when the request is gone at confirm", async () => {
    answers["/api/v1/auth/sso-link/confirm"] = () => refusal(404, "not_found", "Not found");
    renderAt();
    fireEvent.click(await screen.findByRole("button", { name: "Link" }));
    expect(await screen.findByText(SSO_LINK_EXPIRED)).toBeInTheDocument();
  });

  it("says the link expired when nothing is parked for this browser", async () => {
    answers["/api/v1/auth/sso-link"] = () => refusal(404, "not_found", "Not found");
    renderAt();
    expect(await screen.findByText(SSO_LINK_EXPIRED)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Link" })).toBeNull();
  });

  it("says a server that fell over in the general sentence, never in its own words", async () => {
    answers["/api/v1/auth/sso-link"] = serverFellOver;
    renderAt();
    expect(await screen.findByText(GENERIC_FAILURE)).toBeInTheDocument();
    expectNoRawFailureText();
  });

  it("shows another account's refusal and signs out back to sign-in returning here", async () => {
    const message = "You're signed in as another account. Sign in as v***@x.io to link it.";
    answers["/api/v1/auth/sso-link"] = () => refusal(409, "sso_link_other_account", message);
    renderAt();
    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Link" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
    await waitFor(() => expect(posted("/api/v1/auth/logout")).toHaveLength(1));
    expect(await screen.findByTestId("where")).toHaveTextContent(
      `/login?return_to=${encodeURIComponent(`/link-sso?org=${ORG_B}`)}`,
    );
  });

  it("discards the request on Cancel and goes to the overview", async () => {
    renderAt();
    fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));
    expect(await screen.findByTestId("where")).toHaveTextContent(/^\/$/);
    expect(posted("/api/v1/auth/sso-link/cancel")).toHaveLength(1);
    expect(posted("/api/v1/auth/sso-link/confirm")).toHaveLength(0);
  });
});
