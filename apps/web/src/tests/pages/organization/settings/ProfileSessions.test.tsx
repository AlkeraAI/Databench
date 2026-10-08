// Regression: revoking a session must update the UI — the revoked row disappears
// without a manual reload. The revoke hook (api/account.ts useRevokeSession) declares
// NOTHING: the refresh is entirely the shared MutationCache policy's doing, so this
// test fails if the policy stops invalidating (or stops awaiting) after a mutation.
// The confirm modal's toast also fires only after the refreshed list has landed.

import { MemoryRouter } from "react-router-dom";
import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";
import { ProfileBody } from "@/pages/organization/settings/ProfileBody";

const VIEWER = {
  id: "viewer",
  email: "vera@x.io",
  first_name: "Vera",
  last_name: "Ng",
  display_name: "Vera Ng",
  email_verified_at: "2026-01-01T00:00:00Z",
  email_verification_required: false,
  email_verification_deadline: null,
  has_password: true,
};

const escapeRe = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

const session = (jti: string, overrides: Record<string, unknown> = {}) => ({
  jti,
  token_type: "web",
  issued_at: "2026-06-01T00:00:00Z",
  expires_at: "2026-12-01T00:00:00Z",
  last_used_at: "2026-07-01T00:00:00Z",
  label: null,
  current: false,
  ...overrides,
});

// Mutable server state: the DELETE removes the row, so the post-mutation refetch
// observes the revocation exactly like the real backend.
let sessions: ReturnType<typeof session>[];

const json = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

function route(req: Request): Response {
  const p = new URL(req.url).pathname;
  if (p === "/api/v1/auth/me") return json(VIEWER);
  if (p === "/api/v1/auth/identities") return json({ identities: [] });
  if (p === "/api/v1/auth/mfa/status") return json({ enabled: false, backup_codes_remaining: 0 });
  if (p === "/api/v1/auth/sessions" && req.method === "GET") return json({ sessions });
  const revoke = p.match(/^\/api\/v1\/auth\/sessions\/([^/]+)$/);
  if (revoke && req.method === "DELETE") {
    sessions = sessions.filter((s) => s.jti !== revoke[1]);
    return json({ ok: true });
  }
  return json({ detail: `unmatched ${req.method} ${p}` }, 404);
}

let fetchSpy: ReturnType<typeof vi.fn>;

beforeEach(() => {
  sessions = [
    session("jti-current", { current: true }),
    session("jti-laptop", { label: "Work laptop" }),
    session("jti-cli", { token_type: "cli", label: "CI token" }),
  ];
  fetchSpy = vi.fn(async (input: Request | string, init?: RequestInit) =>
    route(input instanceof Request ? input : new Request(input, init)),
  );
  vi.stubGlobal("fetch", fetchSpy);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const sessionGets = () =>
  fetchSpy.mock.calls
    .map((c) => c[0] as Request)
    .filter((r) => new URL(r.url).pathname === "/api/v1/auth/sessions" && r.method === "GET");

function renderProfile() {
  const onToast = vi.fn();
  const notify = { success: onToast, error: (message: string) => onToast(`error: ${message}`) };
  render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter>
        <ProfileBody notify={notify} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { onToast };
}

// "Revoke the session you don't recognise" is what this panel is for, and it was a coin flip:
// nine unlabelled browser rows all rendered "Browser session · Last used 3 minutes ago" with
// nine identical Revoke buttons, while the API had been returning the user agent and the coarse
// network per row all along.
describe("Profile sessions — one row is distinguishable from the next", () => {
  const CHROME_MAC =
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36";
  const SAFARI_IOS =
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1";

  beforeEach(() => {
    sessions = [
      session("jti-current", { current: true, client: `${CHROME_MAC} · 203.0.113.0/24` }),
      session("jti-phone", { client: `${SAFARI_IOS} · 198.51.100.0/24` }),
      session("jti-other-mac", { client: `${CHROME_MAC} · 192.0.2.0/24` }),
    ];
  });

  it("names each device and where it was last seen, and names its Revoke control", async () => {
    renderProfile();

    expect(await screen.findByText("Safari on iOS")).toBeInTheDocument();
    expect(screen.getAllByText("Chrome on macOS")).toHaveLength(2);
    expect(screen.getByText(/198\.51\.100\.0\/24/)).toBeInTheDocument();

    // The current session is not revocable by accident — it shows the pill instead of a button.
    expect(screen.getByText("This device")).toBeInTheDocument();
    const names = screen
      .getAllByRole("button", { name: /^Revoke / })
      .map((b) => b.getAttribute("aria-label"));
    expect(names).toHaveLength(2);
    expect(new Set(names).size).toBe(2);
    expect(names.some((n) => n?.includes("192.0.2.0/24"))).toBe(true);
  });

  it("shows the absolute timestamps on the row, not only on hover", async () => {
    // A tooltip is not reachable by keyboard and is unreliable for screen readers, so the one
    // fact added for "is this session mine?" has to be text on the row and in the control's name.
    renderProfile();
    await screen.findByText("Safari on iOS");
    const signedIn = new Date("2026-06-01T00:00:00Z").toLocaleString();
    expect(screen.getAllByText(new RegExp(`Signed in ${escapeRe(signedIn)}`)).length).toBe(3);
    const revoke = screen.getAllByRole("button", { name: /^Revoke / });
    expect(revoke.every((b) => b.getAttribute("aria-label")?.includes("Signed in"))).toBe(true);
  });
});

describe("Profile sessions — revoke updates the UI", () => {
  it("revoking a session removes its row (no manual reload) and toasts after the fresh list landed", async () => {
    const user = userEvent.setup();
    const { onToast } = renderProfile();
    // Capture the refetch count AT toast time: the toast fires in the callsite onSuccess,
    // which react-query runs only AFTER the policy's awaited refetch — so the fresh list
    // must already have been fetched when the toast goes out. A fire-and-forget policy
    // (invalidate without returning the promise) fails this, not just the row assertion.
    let getsAtToast = -1;
    onToast.mockImplementation(() => {
      getsAtToast = sessionGets().length;
    });

    expect(await screen.findByText("Work laptop")).toBeInTheDocument();
    expect(screen.getByText("CI token")).toBeInTheDocument();

    // Each non-current row carries its own Revoke button (the current device shows a pill instead).
    const revokeButtons = screen.getAllByRole("button", { name: /^Revoke / });
    expect(revokeButtons).toHaveLength(2);
    await user.click(revokeButtons[0]);

    const modal = await screen.findByRole("dialog", { name: /revoke this session/i });
    await user.click(within(modal).getByRole("button", { name: "Revoke session" }));

    // The row disappears because the policy refetched the list — the server state changed,
    // nothing in the component hand-synced it.
    await waitFor(() => expect(screen.queryByText("Work laptop")).not.toBeInTheDocument());
    expect(screen.getByText("CI token")).toBeInTheDocument();
    expect(onToast).toHaveBeenCalledWith("Revoked Work laptop.");
    expect(getsAtToast).toBeGreaterThanOrEqual(2);
  });

  // The two questions on this panel both end a session someone is using, so neither may act on
  // the press that raises them. Backing out has to leave every row standing.
  it.each([
    {
      what: "a single session",
      open: async (user: ReturnType<typeof userEvent.setup>) => {
        await user.click(screen.getAllByRole("button", { name: /^Revoke / })[0]);
        return screen.findByRole("dialog", { name: /revoke this session/i });
      },
      path: "/api/v1/auth/sessions/",
    },
    {
      what: "every other session",
      open: async (user: ReturnType<typeof userEvent.setup>) => {
        await user.click(screen.getByRole("button", { name: "Sign out everywhere" }));
        return screen.findByRole("dialog", { name: /sign out everywhere/i });
      },
      path: "/api/v1/auth/logout-all",
    },
  ])("backing out of the question that ends $what sends nothing", async ({ open, path }) => {
    const user = userEvent.setup();
    renderProfile();
    expect(await screen.findByText("Work laptop")).toBeInTheDocument();

    const modal = await open(user);
    await user.click(within(modal).getByRole("button", { name: "Cancel" }));

    const wrote = fetchSpy.mock.calls
      .map(([input]) => (input instanceof Request ? input : new Request(input as string)))
      .filter((r) => r.method !== "GET" && r.url.includes(path));
    expect(wrote).toHaveLength(0);
    expect(screen.getByText("Work laptop")).toBeInTheDocument();
  });

  it("sign out everywhere refreshes the registry down to the current device", async () => {
    const user = userEvent.setup();
    renderProfile();

    expect(await screen.findByText("Work laptop")).toBeInTheDocument();
    // logout-all revokes every OTHER session server-side; mirror that in the stub.
    fetchSpy.mockImplementation(async (input: Request | string, init?: RequestInit) => {
      const req = input instanceof Request ? input : new Request(input, init);
      if (new URL(req.url).pathname === "/api/v1/auth/logout-all") {
        sessions = sessions.filter((s) => s.current);
        return json({ ok: true });
      }
      return route(req);
    });

    await user.click(screen.getByRole("button", { name: "Sign out everywhere" }));
    const modal = await screen.findByRole("dialog", { name: /sign out everywhere/i });
    await user.click(within(modal).getByRole("button", { name: "Sign out everywhere" }));

    await waitFor(() => expect(screen.queryByText("Work laptop")).not.toBeInTheDocument());
    await waitFor(() => expect(screen.queryByText("CI token")).not.toBeInTheDocument());
    expect(screen.getByText("This device")).toBeInTheDocument();
  });
});
