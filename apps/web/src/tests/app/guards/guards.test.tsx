import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RequireOrgAdmin } from "@/app/guards/RequireOrgAdmin";
import { RequirePlatformAdmin } from "@/app/guards/RequirePlatformAdmin";
import { RequirePlatformStaff } from "@/app/guards/RequirePlatformStaff";
import { RequireSelfHosted } from "@/app/guards/RequireSelfHosted";

// The three permission guards, driven through their REAL data hooks against a REAL
// QueryClient — only `fetch` is stubbed. Each guard renders ONE of three things and
// the test asserts which: the loading gate while the probe is pending, the protected
// child when the role check passes, and the redirect when it fails — to "/" for the org and
// staff guards, to "/admin" for the stricter platform-admin guard (a support user it rejects
// still reaches the admin home). RequireOrgAdmin gates on /api/v1/dashboard's `is_org_admin`;
// the two platform guards gate on /api/v1/auth/me's `platform_role`. The fail and the loading
// cases are what a wrong
// implementation slips through (an over-eager guard renders the child too early, or a
// missing field reads as "allowed"), so each guard pins all three.

const DASH = {
  user: { id: "u-1" },
  org: { id: "org-1", name: "Acme" },
  teams: [],
  pending_invitations: [],
  is_org_admin: true,
};
const USER = {
  id: "u-1",
  first_name: "Ada",
  last_name: "Lovelace",
  email: "ada@acme.com",
  email_verification_required: false,
  has_password: true,
  created_at: "2026-01-01T00:00:00Z",
};

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

let fetchSpy: ReturnType<typeof vi.fn>;
beforeEach(() => {
  fetchSpy = vi.fn();
  vi.stubGlobal("fetch", fetchSpy);
});
afterEach(() => vi.unstubAllGlobals());

function renderGuard(guard: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/secret"]}>
        <Routes>
          <Route element={guard}>
            <Route path="/secret" element={<div>admin surface</div>} />
          </Route>
          <Route path="/" element={<div>dashboard home</div>} />
          <Route path="/admin" element={<div>admin home</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("RequireOrgAdmin", () => {
  it("renders the child when the dashboard reports is_org_admin", async () => {
    fetchSpy.mockResolvedValue(json(200, { ...DASH, is_org_admin: true }));
    renderGuard(<RequireOrgAdmin />);
    expect(await screen.findByText("admin surface")).toBeInTheDocument();
  });

  it("redirects to the dashboard when is_org_admin is false", async () => {
    fetchSpy.mockResolvedValue(json(200, { ...DASH, is_org_admin: false }));
    renderGuard(<RequireOrgAdmin />);
    expect(await screen.findByText("dashboard home")).toBeInTheDocument();
    expect(screen.queryByText("admin surface")).not.toBeInTheDocument();
  });

  it("holds the loading gate (neither destination) while the probe is pending", async () => {
    fetchSpy.mockReturnValue(new Promise<Response>(() => {}));
    renderGuard(<RequireOrgAdmin />);
    expect(await screen.findByRole("status", { name: /checking your access/i })).toBeInTheDocument();
    expect(screen.queryByText("admin surface")).not.toBeInTheDocument();
    expect(screen.queryByText("dashboard home")).not.toBeInTheDocument();
  });
});

describe("RequirePlatformStaff", () => {
  it("renders the child for any platform_role (support)", async () => {
    fetchSpy.mockResolvedValue(json(200, { ...USER, platform_role: "alkera_support" }));
    renderGuard(<RequirePlatformStaff />);
    expect(await screen.findByText("admin surface")).toBeInTheDocument();
  });

  it("redirects when the user has no platform_role", async () => {
    fetchSpy.mockResolvedValue(json(200, { ...USER, platform_role: null }));
    renderGuard(<RequirePlatformStaff />);
    expect(await screen.findByText("dashboard home")).toBeInTheDocument();
    expect(screen.queryByText("admin surface")).not.toBeInTheDocument();
  });
});

describe("RequirePlatformAdmin", () => {
  it("renders the child for alkera_admin", async () => {
    fetchSpy.mockResolvedValue(json(200, { ...USER, platform_role: "alkera_admin" }));
    renderGuard(<RequirePlatformAdmin />);
    expect(await screen.findByText("admin surface")).toBeInTheDocument();
  });

  it("redirects a support-only role to the admin home (stricter than RequirePlatformStaff)", async () => {
    // Support passes RequirePlatformStaff but must NOT pass the admin-only gate; it lands on the
    // admin home it can still see, not the protected child.
    fetchSpy.mockResolvedValue(json(200, { ...USER, platform_role: "alkera_support" }));
    renderGuard(<RequirePlatformAdmin />);
    expect(await screen.findByText("admin home")).toBeInTheDocument();
    expect(screen.queryByText("admin surface")).not.toBeInTheDocument();
  });

  it("redirects when the user has no platform_role at all", async () => {
    fetchSpy.mockResolvedValue(json(200, { ...USER, platform_role: null }));
    renderGuard(<RequirePlatformAdmin />);
    expect(await screen.findByText("admin home")).toBeInTheDocument();
  });
});

// A gate that could not ASK is not a gate that said no.
//
// Each of these guards sits OUTSIDE the app shell, so whatever it renders is the whole page. When
// the read behind one failed, two things happened and neither was the truth: the probe's retry
// ladder held an empty page with a single skeleton bar on it, and the settled failure then read as
// "no permission" and bounced the reader to a dashboard with no explanation. A failure to establish
// a role has to say so, and offer the one action that asks again.
describe("a gate whose read fails", () => {
  const cases = [
    { name: "RequireOrgAdmin", guard: <RequireOrgAdmin />, landing: "dashboard home" },
    { name: "RequirePlatformStaff", guard: <RequirePlatformStaff />, landing: "dashboard home" },
    { name: "RequirePlatformAdmin", guard: <RequirePlatformAdmin />, landing: "admin home" },
  ];

  it.each(cases)("$name says so instead of bouncing", async ({ guard, landing }) => {
    fetchSpy.mockResolvedValue(json(500, { detail: "boom" }));
    renderGuard(guard);

    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load this page");
    expect(screen.queryByText(landing)).not.toBeInTheDocument();
    expect(screen.queryByText("admin surface")).not.toBeInTheDocument();
  });

  it.each(cases)("$name lets the reader ask again", async ({ guard }) => {
    fetchSpy.mockResolvedValue(json(500, { detail: "boom" }));
    renderGuard(guard);
    await screen.findByRole("alert");

    fetchSpy.mockResolvedValue(
      json(200, { ...DASH, ...USER, platform_role: "alkera_admin", self_hosted: true }),
    );
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));

    expect(await screen.findByText("admin surface")).toBeInTheDocument();
  });

  it("says what happened rather than guessing the request never arrived", async () => {
    fetchSpy.mockResolvedValue(json(503, { detail: "unavailable" }));
    renderGuard(<RequireOrgAdmin />);

    expect(await screen.findByRole("alert")).toHaveTextContent("The server could not answer.");
  });

  it("RequireSelfHosted still reads an unanswerable config as the hosted shape", async () => {
    // The odd one out, and on purpose: `usePublicConfig` resolves a failed read to the shipped
    // default instead of raising, so this gate never sees an error and the deployment shape it
    // could not confirm is the hosted one. A retry plate here would offer a choice about a
    // decision already made, safely, without asking — so the bounce is the contract, and this
    // pins it rather than leaving the difference to be read as an oversight.
    fetchSpy.mockResolvedValue(json(500, { detail: "boom" }));
    renderGuard(<RequireSelfHosted />);

    expect(await screen.findByText("dashboard home")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByText("admin surface")).not.toBeInTheDocument();
  });

  it("RequireSelfHosted admits the page when the config says self-hosted", async () => {
    fetchSpy.mockResolvedValue(json(200, { self_hosted: true }));
    renderGuard(<RequireSelfHosted />);

    expect(await screen.findByText("admin surface")).toBeInTheDocument();
  });

  it("a read that ANSWERS no still bounces", async () => {
    // The carve-out covers only a failure to ask: a 200 saying the reader is not an admin is an
    // answer, and it must keep behaving exactly as it did.
    fetchSpy.mockResolvedValue(json(200, { ...DASH, is_org_admin: false }));
    renderGuard(<RequireOrgAdmin />);
    expect(await screen.findByText("dashboard home")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
