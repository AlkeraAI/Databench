import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { activeOrgId, enterOrg, forgetActiveOrg, setOrgNavigator } from "@/api/activeOrg";
import { createQueryClient } from "@/api/queryClient";
import { NoOrganizationPage } from "@/pages/auth/NoOrganizationPage";

// The landing for a person who belongs to no org, through the real hooks with only `fetch`
// stubbed. Each action acts on the sign-in (the refresh cookie) with the header the server
// demands, enters the org it produces and reloads into it; every refusal is said; a pasted
// link is reduced to its token; and nothing here ever calls the plain refresh route, which
// would end the org-less sign-in.

const ORG_N = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";

const json = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const refusal = (status: number, code: string, message: string, details?: Record<string, unknown>) =>
  json({ error: { code, message, trace_id: "t", ...(details ? { details } : {}) } }, status);
const entered = () =>
  json(
    { user: { id: "u", email: "v@x.io", org_team_id: ORG_N, org_name: "Northwind" }, expires_at: new Date(Date.now() + 600_000).toISOString() },
    201,
  );

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
      if (path === "/api/v1/auth/me") return refusal(401, "unauthorized", "not authenticated");
      return json({}, 404);
    }),
  );
}

function Where() {
  const location = useLocation();
  return <p data-testid="where">{location.pathname + location.search}</p>;
}

function renderAt(path = "/no-organization") {
  return render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/no-organization" element={<NoOrganizationPage />} />
          <Route path="*" element={<Where />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const posted = (path: string) => sent.filter((r) => r.method === "POST" && new URL(r.url).pathname === path);

async function createOrg(name: string) {
  fireEvent.change(await screen.findByLabelText("Organization name"), { target: { value: name } });
  fireEvent.click(screen.getByRole("button", { name: "Create" }));
}

async function acceptLink(link: string) {
  fireEvent.change(await screen.findByLabelText("Invitation link"), { target: { value: link } });
  fireEvent.click(screen.getByRole("button", { name: "Accept" }));
}

beforeEach(() => {
  sent = [];
  navigated = [];
  answers = { "/api/v1/config": () => json({ self_hosted: false, multi_org_enabled: true }) };
  restore = setOrgNavigator((url) => navigated.push(url));
  forgetActiveOrg();
  serve();
});

afterEach(() => {
  // Not one request in any case reached the plain refresh route.
  expect(sent.filter((r) => new URL(r.url).pathname === "/api/v1/auth/refresh")).toHaveLength(0);
  cleanup();
  setOrgNavigator(restore);
  forgetActiveOrg();
  vi.unstubAllGlobals();
});

describe("NoOrganizationPage", () => {
  it("offers exactly creating an organization and accepting an invitation", async () => {
    renderAt();
    expect(await screen.findByRole("form", { name: "Create organization" })).toBeInTheDocument();
    expect(screen.getByRole("form", { name: "Accept an invitation" })).toBeInTheDocument();
    expect(screen.getAllByRole("button").map((b) => b.textContent)).toEqual(
      expect.arrayContaining(["Create", "Accept"]),
    );
  });

  it("creates an org on the sign-in, with the header, and reloads into it", async () => {
    answers["/api/v1/auth/refresh/org/new"] = entered;
    renderAt();
    await createOrg("  Northwind  ");
    await waitFor(() => expect(navigated).toEqual(["/"]));
    const [request] = posted("/api/v1/auth/refresh/org/new");
    expect(request.headers.get("X-Requested-With")).toBe("alkera");
    expect(await request.json()).toEqual({ name: "Northwind" });
    // The answer named the org entered; it is adopted, not read as a switch elsewhere.
    expect(activeOrgId()).toBe(ORG_N);
  });

  it("forgets the org this tab last held before entering the new one", async () => {
    enterOrg("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee");
    answers["/api/v1/auth/refresh/org/new"] = () => {
      const response = entered();
      response.headers.set("X-Alkera-Org", ORG_N);
      return response;
    };
    renderAt();
    await createOrg("Northwind");
    // One navigation, to the overview, with no "switched in another window" detour.
    await waitFor(() => expect(navigated).toEqual(["/"]));
  });

  it("asks for a name before creating", async () => {
    renderAt();
    fireEvent.click(await screen.findByRole("button", { name: "Create" }));
    expect(await screen.findByText("Name your organization")).toBeInTheDocument();
    expect(posted("/api/v1/auth/refresh/org/new")).toHaveLength(0);
  });

  it.each([
    [
      "the creation limit",
      () => refusal(429, "org_creation_limited", "You've created too many organizations recently. Try again later."),
      "You've created too many organizations recently. Try again later.",
    ],
    [
      "an unverified email",
      () => refusal(403, "email_verification_required", "Verify your email address to perform this action."),
      "Verify your email address to perform this action.",
    ],
    [
      "a name the server refuses",
      () =>
        json(
          {
            error: {
              code: "validation_error",
              message: "The request failed validation.",
              trace_id: "t",
              details: { errors: [{ type: "value_error", loc: ["body", "name"], msg: "Value error, Organization name cannot contain a web address" }] },
            },
          },
          422,
        ),
      "Organization name cannot contain a web address",
    ],
    ["an ended sign-in", () => refusal(401, "session_revoked", "session revoked"), "Your sign-in has ended. Sign in again."],
    ["a server without several orgs per person", () => refusal(404, "not_found", "Not found"), "Creating an organization isn't available."],
    ["a server failure", () => refusal(500, "internal_error", "An unexpected error occurred. Please try again."), "Something went wrong on our end. Try again."],
    ["a failure with no body", () => new Response("", { status: 502 }), "Something went wrong on our end. Try again."],
  ])("says so when creating meets %s, and stays", async (_label, answer, sentence) => {
    answers["/api/v1/auth/refresh/org/new"] = answer;
    renderAt();
    await createOrg("Northwind");
    expect(await screen.findByText(sentence)).toBeInTheDocument();
    // The client's own diagnostic ("could not create the organization (502)") is never shown.
    expect(screen.queryByText(/\(\d{3}\)/)).toBeNull();
    expect(navigated).toEqual([]);
  });

  it.each([
    ["a whole signup link", "https://app.example.com/signup?invite=tok-42", "tok-42"],
    ["a sign-in link", "/login?invite=tok-42", "tok-42"],
    ["the token alone", "  tok-42 ", "tok-42"],
  ])("accepts %s by its token and reloads into the org", async (_label, link, token) => {
    answers["/api/v1/auth/refresh/org/join"] = entered;
    renderAt();
    await acceptLink(link);
    await waitFor(() => expect(navigated).toEqual(["/"]));
    const [request] = posted("/api/v1/auth/refresh/org/join");
    expect(request.headers.get("X-Requested-With")).toBe("alkera");
    expect(await request.json()).toEqual({ token });
  });

  it("refuses to send a link that carries no token", async () => {
    renderAt();
    await acceptLink("https://app.example.com/signup");
    expect(await screen.findByText("Paste the invitation link or token")).toBeInTheDocument();
    expect(posted("/api/v1/auth/refresh/org/join")).toHaveLength(0);
  });

  it("fills in the invitation a sign-in carried here", async () => {
    answers["/api/v1/auth/refresh/org/join"] = entered;
    renderAt("/no-organization?invite=tok-carried");
    expect(await screen.findByLabelText("Invitation link")).toHaveValue("tok-carried");
    fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    await waitFor(() => expect(navigated).toEqual(["/"]));
    expect(await posted("/api/v1/auth/refresh/org/join")[0].json()).toEqual({ token: "tok-carried" });
  });

  it.each([
    ["closed", () => refusal(410, "invitation_expired", "This invitation has expired."), "This invitation has expired."],
    [
      "for another account",
      () => refusal(409, "invitation_other_account", "This invitation is for v***@x.io. Sign in with that email to accept."),
      "This invitation is for v***@x.io. Sign in with that email to accept.",
    ],
    ["unknown", () => refusal(404, "not_found", "Invitation not found"), "That invitation link isn't valid."],
  ])("says when the invitation is %s, and stays", async (_label, answer, sentence) => {
    answers["/api/v1/auth/refresh/org/join"] = answer;
    renderAt();
    await acceptLink("tok-1");
    expect(await screen.findByText(sentence)).toBeInTheDocument();
    expect(navigated).toEqual([]);
  });

  it("follows an org that wants its single sign-on first", async () => {
    answers["/api/v1/auth/refresh/org/join"] = () =>
      refusal(409, "sso_required", "This organization requires single sign-on.", {
        login_url: "https://api.example.test/api/v1/auth/sso/x/login",
      });
    renderAt();
    await acceptLink("tok-1");
    await waitFor(() => expect(navigated).toEqual(["https://api.example.test/api/v1/auth/sso/x/login"]));
    expect(screen.queryByText("This organization requires single sign-on.")).toBeNull();
  });

  it("sends someone already in an org to the overview", async () => {
    answers["/api/v1/auth/me"] = () => json({ id: "u", email: "v@x.io", first_name: "V", last_name: "N", org_team_id: ORG_N });
    renderAt();
    expect(await screen.findByTestId("where")).toHaveTextContent(/^\/$/);
  });

  it("sends a visitor to sign-in, offering nothing, with several orgs per person off", async () => {
    answers["/api/v1/config"] = () => json({ self_hosted: false, multi_org_enabled: false });
    renderAt("/no-organization?invite=tok-1");
    expect(await screen.findByTestId("where")).toHaveTextContent(/^\/login$/);
    expect(screen.queryByRole("button", { name: "Create" })).toBeNull();
    expect(sent.filter((r) => r.method === "POST")).toHaveLength(0);
  });
});
