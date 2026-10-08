// The org page's bands ride the URL, the sandbox limits card writes the two staff-set figures, and the
// Activity band renders the org's chats, errors and refusals, and audit trail — or one sentence each
// when there is nothing to show.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { components } from "@alkera/sdk";

import { meKey } from "@/api/auth";
import { createQueryClient } from "@/api/queryClient";
import { keys } from "@/api/keys";
import {
  ACTIVITY_LABELS,
  AdminOrgDetailPage,
  ISSUE_KIND_LABELS,
  SANDBOX_LABELS,
  TAB_LABELS,
  parseLimit,
} from "@/pages/platform/admin/orgs/AdminOrgDetailPage";
import { PORTAL_ROUTES } from "@/app/extensions/portal";

// A chat's machine links to the machine's page where the fleet console is installed.
PORTAL_ROUTES.register({ key: "admin.machines.detail", mount: "platformAdmin", path: "/admin/machines/:machineId", element: null });

type User = components["schemas"]["UserRead"];

const ORG_ID = "5d7c9a8e-0000-4000-8000-000000000001";
const ORG = { name: "Tideline", id: ORG_ID, parent_team_id: null, is_root: true, created_at: "2026-01-01T00:00:00Z", member_count: 3 };

const staff = (role: User["platform_role"]): User => ({
  email: "staff@example.com",
  first_name: "Sam",
  last_name: "Staff",
  id: "u-staff",
  org_team_id: "t-root",
  org_name: "Tideline",
  org_role: "member",
  membership_count: 1,
  display_name: "Sam Staff",
  platform_role: role,
  email_verification_required: false,
  has_password: true,
  mfa_enabled: false,
  created_at: "2026-01-01T00:00:00Z",
});

type Call = { url: string; method: string; body: unknown };

/** A server that answers the settings read/write and the three activity reads, and 404s the rest. */
function serve(role: User["platform_role"], routes: { settings?: Record<string, unknown>; chats?: unknown[]; errors?: unknown[]; audit?: unknown[] }) {
  const calls: Call[] = [];
  let settings = { allow_login_google: true, allow_login_github: true, sandbox_vcpu: null, sandbox_memory_mb: null, ...routes.settings };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const req = input as Request;
      const raw = await req.clone().text();
      const body: unknown = raw ? JSON.parse(raw) : null;
      calls.push({ url: req.url, method: req.method, body });
      const json = (b: unknown, status = 200) => new Response(JSON.stringify(b), { status, headers: { "content-type": "application/json" } });
      const path = new URL(req.url).pathname;
      if (path === "/api/v1/auth/me") return json(staff(role));
      if (path === `/admin/v1/orgs/${ORG_ID}`) return json(ORG);
      if (path.endsWith("/settings")) {
        if (req.method === "PUT") settings = { ...settings, ...(body as object) };
        return json(settings);
      }
      if (path.endsWith("/chats")) return json({ items: routes.chats ?? [] });
      if (path.endsWith("/errors")) return json({ items: routes.errors ?? [] });
      if (path.endsWith("/audit")) return json({ items: routes.audit ?? [], total: (routes.audit ?? []).length });
      return json({ detail: "Not found" }, 404);
    }),
  );
  return calls;
}

let location = "";
function LocationProbe() {
  location = useLocation().search;
  return null;
}

function renderPage(role: User["platform_role"], entry = `/admin/orgs/${ORG_ID}`) {
  const qc = createQueryClient({ retry: false });
  qc.setQueryData(meKey, staff(role));
  qc.setQueryData(keys.admin.org(ORG_ID), ORG);
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[entry]}>
        <Routes>
          <Route path="/admin/orgs/:orgId" element={<><AdminOrgDetailPage /><LocationProbe /></>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const selected = () => screen.getAllByRole("tab").find((t) => t.getAttribute("aria-selected") === "true")?.textContent;

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("the org page's bands ride the URL", () => {
  it.each([
    ["overview", TAB_LABELS.overview],
    ["members", TAB_LABELS.members],
    ["activity", TAB_LABELS.activity],
    ["settings", TAB_LABELS.settings],
  ])("?tab=%s opens %s for a platform admin", async (key, label) => {
    serve("alkera_admin", {});
    renderPage("alkera_admin", `/admin/orgs/${ORG_ID}?tab=${key}`);
    await screen.findByRole("heading", { name: ORG.name });
    expect(selected()).toBe(label);
  });

  it.each([
    ["an unknown band", "nonsense"],
    ["the admin-only band for support staff", "settings"],
  ])("falls back to Overview for %s", async (_what, key) => {
    serve("alkera_support", {});
    renderPage("alkera_support", `/admin/orgs/${ORG_ID}?tab=${key}`);
    await screen.findByRole("heading", { name: ORG.name });
    expect(selected()).toBe(TAB_LABELS.overview);
  });

  it("writes the chosen band into the URL", async () => {
    serve("alkera_admin", {});
    renderPage("alkera_admin");
    await screen.findByRole("heading", { name: ORG.name });
    fireEvent.click(screen.getByRole("tab", { name: TAB_LABELS.activity }));
    expect(new URLSearchParams(location).get("tab")).toBe("activity");
    expect(selected()).toBe(TAB_LABELS.activity);
  });
});

describe("parseLimit", () => {
  it.each([
    ["", null],
    ["  ", null],
    ["1", 1],
    ["64", 64],
    ["0", undefined],
    ["65", undefined],
    ["4.5", undefined],
    ["-2", undefined],
    ["eight", undefined],
  ])("reads %j as %s within 1..64", (raw, expected) => {
    expect(parseLimit(raw, [1, 64])).toBe(expected);
  });
});

describe("the sandbox limits card", () => {
  it("saves both figures and an emptied field resets to the box default", async () => {
    const calls = serve("alkera_admin", { settings: { sandbox_vcpu: 2, sandbox_memory_mb: 4096 } });
    renderPage("alkera_admin");
    const vcpu = await screen.findByLabelText(SANDBOX_LABELS.vcpu);
    const memory = screen.getByLabelText(SANDBOX_LABELS.memory);
    await waitFor(() => expect(vcpu).toHaveValue("2"));
    const save = screen.getByRole("button", { name: SANDBOX_LABELS.save });
    expect(save).toBeDisabled();

    fireEvent.change(vcpu, { target: { value: "8" } });
    fireEvent.change(memory, { target: { value: "" } });
    fireEvent.click(save);

    await waitFor(() => expect(calls.some((c) => c.method === "PUT")).toBe(true));
    const put = calls.find((c) => c.method === "PUT")!;
    expect(put.url).toContain(`/admin/v1/orgs/${ORG_ID}/settings`);
    expect(put.body).toEqual({ sandbox_vcpu: 8, sandbox_memory_mb: null });
    await waitFor(() => expect(screen.getByLabelText(SANDBOX_LABELS.vcpu)).toHaveValue("8"));
  });

  it("refuses an out-of-range figure before it reaches the server", async () => {
    const calls = serve("alkera_admin", {});
    renderPage("alkera_admin");
    const memory = await screen.findByLabelText(SANDBOX_LABELS.memory);
    fireEvent.change(memory, { target: { value: "100" } });
    expect(screen.getByText(SANDBOX_LABELS.invalid)).toHaveAttribute("role", "alert");
    const save = screen.getByRole("button", { name: SANDBOX_LABELS.save });
    expect(save).toBeDisabled();
    fireEvent.click(save);
    expect(calls.some((c) => c.method === "PUT")).toBe(false);
  });

  it("shows support staff the figures, read-only", async () => {
    serve("alkera_support", { settings: { sandbox_vcpu: 4, sandbox_memory_mb: null } });
    renderPage("alkera_support");
    expect(await screen.findByText(SANDBOX_LABELS.adminOnly)).toBeInTheDocument();
    expect(screen.queryByLabelText(SANDBOX_LABELS.vcpu)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: SANDBOX_LABELS.save })).not.toBeInTheDocument();
    expect(await screen.findByText("4 vCPU")).toBeInTheDocument();
    expect(screen.getByText(SANDBOX_LABELS.placeholder)).toBeInTheDocument();
  });
});

describe("the Activity band", () => {
  it("says so in one sentence when each list is empty", async () => {
    serve("alkera_support", {});
    renderPage("alkera_support", `/admin/orgs/${ORG_ID}?tab=activity`);
    expect(await screen.findByText(ACTIVITY_LABELS.chatsEmpty)).toBeInTheDocument();
    expect(await screen.findByText(ACTIVITY_LABELS.issuesEmpty)).toBeInTheDocument();
    expect(await screen.findByText(ACTIVITY_LABELS.auditEmpty)).toBeInTheDocument();
  });

  const chatOn = (over: Record<string, unknown>) => ({
    id: `c-${String(over.machine_status)}`,
    title: `Chat ${String(over.machine_status)}`,
    owner_email: "ana@tideline.example",
    machine_id: "m-1",
    machine_name: "tideline-box",
    mirror_state: null,
    machine_refusal: null,
    last_activity_at: "2026-09-24T10:00:00Z",
    created_at: "2026-09-20T10:00:00Z",
    ...over,
  });

  it.each([
    // The box said "awake" before it was released; the machine's state now is what the row says.
    [{ machine_status: "stranded", mirror_state: "awake" }, "stranded"],
    [{ machine_status: "unreachable", mirror_state: "awake" }, "unreachable"],
    [{ machine_status: "asleep", mirror_state: "asleep" }, "asleep"],
    [{ machine_status: "ready", mirror_state: "awake" }, "awake"],
    [{ machine_status: "ready", mirror_state: null }, "ready"],
    [{ machine_status: "ready", mirror_state: "awake", machine_refusal: "no credits" }, "refused"],
  ])("says what the machine is for a chat reading %j", async (over, word) => {
    serve("alkera_support", { chats: [chatOn(over)] });
    renderPage("alkera_support", `/admin/orgs/${ORG_ID}?tab=activity`);
    const link = await screen.findByRole("link", { name: "tideline-box" });
    expect(link.closest("td")?.textContent).toBe(`tideline-box${word}`);
  });

  it("links a chat's machine to that machine's own page", async () => {
    serve("alkera_support", { chats: [chatOn({ machine_status: "ready" })] });
    renderPage("alkera_support", `/admin/orgs/${ORG_ID}?tab=activity`);
    expect((await screen.findByRole("link", { name: "tideline-box" })).getAttribute("href")).toBe("/admin/machines/m-1");
  });

  it("lists the chats, the errors and refusals, and the audit events it is served", async () => {
    serve("alkera_support", {
      chats: [
        {
          id: "c-1",
          title: "Quarterly review",
          owner_email: "ana@tideline.example",
          machine_id: "m-1",
          machine_name: "tideline-box",
          machine_status: "ready",
          mirror_state: "awake",
          machine_refusal: null,
          last_activity_at: "2026-09-24T10:00:00Z",
          created_at: "2026-09-20T10:00:00Z",
        },
      ],
      errors: [
        { kind: "refusal", chat_id: "c-1", chat_title: "Quarterly review", detail: "The model refused", at: "2026-09-24T10:05:00Z" },
        { kind: "machine_refused", chat_id: "c-1", chat_title: "Quarterly review", detail: "no credits", at: "2026-09-24T10:01:00Z" },
      ],
      audit: [
        { id: "a-1", actor_email: "ops@example.com", action: "storage.org_limit_set", target: null, detail: null, created_at: "2026-09-24T09:00:00Z" },
      ],
    });
    renderPage("alkera_support", `/admin/orgs/${ORG_ID}?tab=activity`);
    expect(await screen.findByText("ana@tideline.example")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "tideline-box" })).toBeInTheDocument();
    expect(screen.getByText("awake")).toBeInTheDocument();
    expect(await screen.findByText(ISSUE_KIND_LABELS.refusal)).toBeInTheDocument();
    expect(screen.getByText(ISSUE_KIND_LABELS.machine_refused)).toBeInTheDocument();
    expect(screen.getByText("no credits")).toBeInTheDocument();
    expect(await screen.findByText("storage.org_limit_set")).toBeInTheDocument();
    expect(screen.queryByText(ACTIVITY_LABELS.chatsEmpty)).not.toBeInTheDocument();
  });
});
