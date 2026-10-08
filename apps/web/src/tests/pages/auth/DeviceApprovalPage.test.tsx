import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DeviceApprovalPage } from "@/pages/auth/DeviceApprovalPage";

// The device-grant consent page, driven through the REAL useCurrentUser + useDeviceInfo
// queries and the approve/deny mutations against a REAL QueryClient — only `fetch` is
// stubbed. We assert the observable contract: a signed-out visitor is bounced to /login
// carrying the full path so the code survives the round-trip; a transient info failure
// never reads as "expired" nor blocks approval; a definite 404 lands on the expired state;
// approve/deny POST and show their terminal cards; and a bare /device visit renders the
// manual entry form, whose input normalizes to the backend's exact-match XXXX-XXXX shape.

const meUser = {
  id: "11111111-1111-1111-1111-111111111111",
  email: "user@example.com",
  first_name: "Test",
  last_name: "User",
  display_name: "Test User",
  org_team_id: "22222222-2222-2222-2222-222222222222",
  email_verified_at: "2026-05-01T00:00:00Z",
  email_verification_required: false,
  created_at: "2026-05-01T00:00:00Z",
};

const deviceInfo = {
  client_id: "alkera-cli",
  client_name: "Alkera CLI",
  scope: "cli",
  expires_at: "2030-01-01T00:00:00Z",
  user_code: "WXYZ-1234",
};

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function urlOf(input: unknown): string {
  if (typeof input === "string") return input;
  if (input && typeof input === "object" && "url" in input) return String((input as { url: unknown }).url);
  return String(input);
}

type Handler = (url: string, init?: RequestInit) => Response;
let fetchSpy: ReturnType<typeof vi.fn>;

function setup(handler: Handler): void {
  fetchSpy = vi.fn((input: unknown, init?: RequestInit) => Promise.resolve(handler(urlOf(input), init)));
  vi.stubGlobal("fetch", fetchSpy);
}

function LoginProbe() {
  const returnTo = new URLSearchParams(useLocation().search).get("return_to") ?? "(none)";
  return <div data-testid="login-from">{returnTo}</div>;
}

function renderAt(search: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[`/device${search}`]}>
        <Routes>
          <Route path="/device" element={<DeviceApprovalPage />} />
          <Route path="/login" element={<LoginProbe />} />
          <Route path="/" element={<div>DASHBOARD</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  fetchSpy = vi.fn();
  vi.stubGlobal("fetch", fetchSpy);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("DeviceApprovalPage", () => {
  it("bounces a signed-out visitor to /login carrying the full path incl. user_code", async () => {
    setup((url) => {
      if (url.includes("/api/v1/auth/me")) return json(401, { detail: "no session" });
      return json(404, {});
    });
    renderAt("?user_code=WXYZ-1234");
    // The code MUST survive the login round-trip, else the one-click consent breaks.
    expect(await screen.findByTestId("login-from")).toHaveTextContent("/device?user_code=WXYZ-1234");
  });

  it("renders the consent screen with the client name, code, and account", async () => {
    setup((url) => {
      if (url.includes("/api/v1/auth/me")) return json(200, meUser);
      if (url.includes("/api/v1/auth/device/info")) return json(200, deviceInfo);
      return json(404, {});
    });
    renderAt("?user_code=WXYZ-1234");

    // Wait for the confirm state specifically — its code reads "Device code …", whereas the
    // loading state (same heading) shows "Loading the device code".
    expect(await screen.findByRole("img", { name: /device code WXYZ-1234/i })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /authorize a device/i })).toBeInTheDocument();
    expect(screen.getByText("user@example.com")).toBeInTheDocument();
    expect(screen.getByText(/Alkera CLI/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^approve$/i })).toBeEnabled();
  });

  it("keeps Approve enabled on a transient info failure and never reads as expired", async () => {
    setup((url) => {
      if (url.includes("/api/v1/auth/me")) return json(200, meUser);
      if (url.includes("/api/v1/auth/device/info"))
        return json(503, { error: { code: "upstream_error", message: "down" } });
      return json(404, {});
    });
    renderAt("?user_code=WXYZ-1234");

    expect(await screen.findByText(/couldn't load this device code/i)).toBeInTheDocument();
    expect(screen.queryByText(/expired/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^approve$/i })).toBeEnabled();
  });

  it("lands on the expired state when the info lookup 404s", async () => {
    setup((url) => {
      if (url.includes("/api/v1/auth/me")) return json(200, meUser);
      if (url.includes("/api/v1/auth/device/info"))
        return json(404, { error: { code: "not_found", message: "gone" } });
      return json(404, {});
    });
    renderAt("?user_code=WXYZ-1234");

    expect(await screen.findByRole("heading", { name: /this code has expired/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^approve$/i })).not.toBeInTheDocument();
  });

  it("renders the manual entry form on a bare /device visit, Continue disabled", async () => {
    setup((url) => {
      if (url.includes("/api/v1/auth/me")) return json(200, meUser);
      return json(404, {});
    });
    renderAt("");

    // Await the textbox, not the heading — the loading state shares the same title.
    expect(await screen.findByRole("textbox", { name: /device code/i })).toHaveValue("");
    expect(screen.getByRole("heading", { name: /authorize a device/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /continue/i })).toBeDisabled();
  });

  it("bounces a signed-out bare /device visit through /login and back", async () => {
    setup((url) => {
      if (url.includes("/api/v1/auth/me")) return json(401, { detail: "no session" });
      return json(404, {});
    });
    renderAt("");
    expect(await screen.findByTestId("login-from")).toHaveTextContent("/device");
  });

  // The backend lookup is an EXACT match on the canonical XXXX-XXXX shape, so manual
  // input must be normalized as typed: uppercase, separators dropped, hyphen re-inserted,
  // and a pasted approval link reduced to just its code.
  it.each([
    ["rpxh7j6p", "RPXH-7J6P"],
    [" wxyz-1234 ", "WXYZ-1234"],
    ["https://app.example.com/device?user_code=WXYZ-1234", "WXYZ-1234"],
  ])("normalizes manual input %j to its canonical code", async (typed, canonical) => {
    setup((url) => {
      if (url.includes("/api/v1/auth/me")) return json(200, meUser);
      return json(404, {});
    });
    renderAt("");

    const input = await screen.findByRole("textbox", { name: /device code/i });
    fireEvent.change(input, { target: { value: typed } });
    expect(input).toHaveValue(canonical);
    expect(screen.getByRole("button", { name: /continue/i })).toBeEnabled();
  });

  it("keeps Continue disabled on a partial code and never fires the info lookup", async () => {
    setup((url) => {
      if (url.includes("/api/v1/auth/me")) return json(200, meUser);
      return json(404, {});
    });
    renderAt("");

    const input = await screen.findByRole("textbox", { name: /device code/i });
    fireEvent.change(input, { target: { value: "RPX" } });
    expect(input).toHaveValue("RPX");
    expect(screen.getByRole("button", { name: /continue/i })).toBeDisabled();
    // Submitting the form anyway (Enter) must be a no-op — still on the form, no lookup.
    fireEvent.submit(input.closest("form")!);
    expect(screen.getByRole("textbox", { name: /device code/i })).toBeInTheDocument();
    expect(fetchSpy.mock.calls.some(([u]) => urlOf(u).includes("/device/info"))).toBe(false);
  });

  it("submits a typed code and flows into the consent screen", async () => {
    setup((url) => {
      if (url.includes("/api/v1/auth/me")) return json(200, meUser);
      if (url.includes("/api/v1/auth/device/info")) return json(200, deviceInfo);
      return json(404, {});
    });
    renderAt("");

    const input = await screen.findByRole("textbox", { name: /device code/i });
    fireEvent.change(input, { target: { value: "wxyz1234" } });
    // Submit the FORM, not the button — pins Enter-to-submit.
    fireEvent.submit(input.closest("form")!);

    expect(await screen.findByRole("img", { name: /device code WXYZ-1234/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^approve$/i })).toBeEnabled();
    expect(
      fetchSpy.mock.calls.some(([u]) => urlOf(u).includes("user_code=WXYZ-1234")),
    ).toBe(true);
  });

  it("recovers from a wrong code — the expired state offers a way back to the form", async () => {
    setup((url) => {
      if (url.includes("/api/v1/auth/me")) return json(200, meUser);
      if (url.includes("/api/v1/auth/device/info"))
        return json(404, { error: { code: "not_found", message: "gone" } });
      return json(404, {});
    });
    renderAt("");

    const input = await screen.findByRole("textbox", { name: /device code/i });
    fireEvent.change(input, { target: { value: "AAAA2222" } });
    fireEvent.submit(input.closest("form")!);

    expect(await screen.findByRole("heading", { name: /this code has expired/i })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /enter a different code/i }));
    expect(await screen.findByRole("textbox", { name: /device code/i })).toHaveValue("");
  });

  it("approves → POSTs /device/approve and shows the approved card", async () => {
    setup((url) => {
      if (url.includes("/api/v1/auth/me")) return json(200, meUser);
      if (url.includes("/api/v1/auth/device/info")) return json(200, deviceInfo);
      if (url.includes("/api/v1/auth/device/approve")) return json(200, { message: "device approved" });
      return json(404, {});
    });
    renderAt("?user_code=WXYZ-1234");

    fireEvent.click(await screen.findByRole("button", { name: /^approve$/i }));
    expect(await screen.findByRole("heading", { name: /device approved/i })).toBeInTheDocument();
    expect(fetchSpy.mock.calls.some(([u]) => urlOf(u).includes("/api/v1/auth/device/approve"))).toBe(true);
  });

  it("denies → POSTs /device/deny and shows the denied card", async () => {
    setup((url) => {
      if (url.includes("/api/v1/auth/me")) return json(200, meUser);
      if (url.includes("/api/v1/auth/device/info")) return json(200, deviceInfo);
      if (url.includes("/api/v1/auth/device/deny")) return json(200, { message: "device denied" });
      return json(404, {});
    });
    renderAt("?user_code=WXYZ-1234");

    fireEvent.click(await screen.findByRole("button", { name: /^deny$/i }));
    expect(await screen.findByRole("heading", { name: /sign-in denied/i })).toBeInTheDocument();
    expect(fetchSpy.mock.calls.some(([u]) => urlOf(u).includes("/api/v1/auth/device/deny"))).toBe(true);
  });

  describe("choosing which org the device acts in", () => {
    const orgA = "22222222-2222-2222-2222-222222222222";
    const orgB = "33333333-3333-3333-3333-333333333333";
    const twoOrgs = {
      active_org_team_id: orgA,
      memberships: [
        { org_team_id: orgB, org_name: "Beta Labs", role: "member", sso_required: false },
        { org_team_id: orgA, org_name: "Acme", role: "admin", sso_required: false },
      ],
    };
    const oneOrg = { active_org_team_id: orgA, memberships: [twoOrgs.memberships[1]] };

    function serve(memberships: unknown, approve: () => Response = () => json(200, { message: "ok" })) {
      setup((url) => {
        if (url.includes("/api/v1/auth/memberships")) return json(200, memberships);
        if (url.includes("/api/v1/auth/me")) return json(200, meUser);
        if (url.includes("/api/v1/auth/device/info")) return json(200, deviceInfo);
        if (url.includes("/api/v1/auth/device/approve")) return approve();
        return json(404, {});
      });
    }

    async function approveBody(): Promise<unknown> {
      const call = fetchSpy.mock.calls.find(([u]) => urlOf(u).includes("/api/v1/auth/device/approve"));
      expect(call).toBeDefined();
      const [input, init] = call as [unknown, RequestInit | undefined];
      const raw = input instanceof Request ? await input.clone().text() : String(init?.body ?? "");
      return JSON.parse(raw);
    }

    it("offers no choice to a person in one org and approves into the session's org", async () => {
      serve(oneOrg);
      renderAt("?user_code=WXYZ-1234");
      fireEvent.click(await screen.findByRole("button", { name: /^approve$/i }));
      expect(await screen.findByRole("heading", { name: /device approved/i })).toBeInTheDocument();
      expect(screen.queryByText("Organization")).toBeNull();
      expect(await approveBody()).toEqual({ user_code: "WXYZ-1234" });
    });

    it("pre-selects the org the link names and approves into it", async () => {
      serve(twoOrgs);
      renderAt(`?user_code=WXYZ-1234&org=${orgB}`);
      const picker = await screen.findByRole("button", { name: /organization/i });
      expect(picker).toHaveTextContent("Beta Labs");
      fireEvent.click(screen.getByRole("button", { name: /^approve$/i }));
      expect(await screen.findByRole("heading", { name: /device approved/i })).toBeInTheDocument();
      expect(await approveBody()).toEqual({ user_code: "WXYZ-1234", org_team_id: orgB });
    });

    it("defaults to the session's org when the link names none", async () => {
      serve(twoOrgs);
      renderAt("?user_code=WXYZ-1234");
      expect(await screen.findByRole("button", { name: /organization/i })).toHaveTextContent("Acme");
      fireEvent.click(screen.getByRole("button", { name: /^approve$/i }));
      expect(await screen.findByRole("heading", { name: /device approved/i })).toBeInTheDocument();
      expect(await approveBody()).toEqual({ user_code: "WXYZ-1234", org_team_id: orgA });
    });

    it("refuses to approve into an org the person is not in", async () => {
      serve(twoOrgs);
      renderAt("?user_code=WXYZ-1234&org=44444444-4444-4444-4444-444444444444");
      expect(await screen.findByText("You don't have access to that organization.")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: /^approve$/i })).toBeDisabled();
    });

    it("offers the org's single sign-on when approving needs it", async () => {
      const assign = vi.fn();
      vi.stubGlobal("location", { ...window.location, assign });
      const loginUrl = "http://api.test/api/v1/auth/sso/x/login?return_to=%2Fdevice";
      serve(twoOrgs, () =>
        json(403, {
          error: {
            code: "sso_required",
            message: "Sign in with your organization's single sign-on, then approve again.",
            details: { login_url: loginUrl },
          },
        }),
      );
      renderAt(`?user_code=WXYZ-1234&org=${orgB}`);
      fireEvent.click(await screen.findByRole("button", { name: /^approve$/i }));
      fireEvent.click(await screen.findByRole("button", { name: "Continue with single sign-on" }));
      expect(assign).toHaveBeenCalledWith(loginUrl);
    });
  });
});
