import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { components } from "@alkera/sdk";

import { meKey } from "@/api/auth";
import { count } from "@/pages/platform/admin/shared/format";
import {
  AdminOrgDetailPage,
  DANGER_LABELS,
  SIGN_IN_PROVIDERS,
  TAB_LABELS,
  GONE_ORG,
  deleteConfirmTitle,
  deletedTitle,
  providerToggleLabel,
} from "@/pages/platform/admin/orgs/AdminOrgDetailPage";

// The org-detail register, driven through its REAL hooks with the org, its settings, and the viewer's
// platform role seeded into the query cache. These pin the tidy the operator console got: the sign-in
// providers carry their brand marks (reachable by an accessible name that names the provider) and a
// toggle wired to the settings PUT, and the danger-zone rename/delete now live INSIDE the Settings band
// — there is no separate "Danger zone" tab. A support-grade staffer (no admin role) never sees the
// Settings band at all. Every searched string is derived from the component's own exported constants or
// from the seeded fixture, never a copied literal.

type Org = components["schemas"]["OrgRead"];
type User = components["schemas"]["UserRead"];
type OrgSettings = components["schemas"]["OrgSettingsRead"];

const ORG_ID = "a1b2c3d4-aaaa";

const ORG: Org = {
  name: "Tideline Analytics",
  id: ORG_ID,
  parent_team_id: null,
  is_root: true,
  created_at: "2026-01-01T00:00:00Z",
  member_count: 42,
};

// The provider flags the two toggles read + write, keyed off the component's own catalogue so the test
// never hard-codes "allow_login_google" / "allow_login_github".
const GOOGLE = SIGN_IN_PROVIDERS.find((p) => p.name === "Google")!;
const GITHUB = SIGN_IN_PROVIDERS.find((p) => p.name === "GitHub")!;

const admin = (role: User["platform_role"]): User => ({
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

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function renderDetail({
  role,
  settings = { [GOOGLE.key]: true, [GITHUB.key]: false } as unknown as OrgSettings,
  memberCount = ORG.member_count,
  name = ORG.name,
}: {
  role: User["platform_role"];
  settings?: OrgSettings;
  memberCount?: number;
  name?: string;
}) {
  // staleTime Infinity makes the seeded cache authoritative: no background refetch (which would
  // "fetch failed" in jsdom and flip a query to error) races the assertions.
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  qc.setQueryData(meKey, admin(role));
  qc.setQueryData(["admin", "orgs", ORG_ID], { ...ORG, name, member_count: memberCount });
  qc.setQueryData(["admin", "orgs", ORG_ID, "settings"], settings);
  qc.setQueryData(["admin", "orgs", ORG_ID, "members"], []);
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[`/admin/orgs/${ORG_ID}`]}>
        <Routes>
          <Route path="/admin/orgs/:orgId" element={<AdminOrgDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return qc;
}

const orgHeading = () => screen.findByRole("heading", { name: ORG.name });
const settingsTab = () => screen.getByRole("tab", { name: TAB_LABELS.settings });
const providerToggle = (name: string) => screen.getByRole("checkbox", { name: providerToggleLabel(name) });
const queryProviderToggle = (name: string) =>
  screen.queryByRole("checkbox", { name: providerToggleLabel(name) });
const deleteButton = () => screen.getByRole("button", { name: DANGER_LABELS.delete });
/** What actually went over the wire, by method — openapi-fetch passes one Request per call. */
const sent = (spy: { mock: { calls: unknown[][] } }, method: string): Request[] =>
  spy.mock.calls.map((c) => c[0] as Request).filter((r) => r.method === method);
const queryDeleteButton = () => screen.queryByRole("button", { name: DANGER_LABELS.delete });

describe("AdminOrgDetailPage", () => {
  it("offers every band to a platform admin, Settings last — and no separate Danger zone tab", async () => {
    renderDetail({ role: "alkera_admin" });
    await orgHeading();
    const tabs = screen.getAllByRole("tab").map((t) => t.textContent);
    expect(tabs).toEqual(Object.values(TAB_LABELS));
    expect(screen.queryByRole("tab", { name: /danger zone/i })).not.toBeInTheDocument();
  });

  it("sets the sections in a strip that scrolls on a narrow screen rather than cutting each label short", async () => {
    // jsdom does not lay out, so the width contract is pinned on the stylesheet the strip wears: the
    // control keeps its own content width (every label whole) and the strip scrolls sideways.
    renderDetail({ role: "alkera_admin" });
    await orgHeading();
    const strip = screen.getByTestId("org-sections");
    expect(within(strip).getByRole("tablist", { name: "Organization sections" })).toBeTruthy();
    const css = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "../../../../../pages/platform/admin/orgs/AdminOrgDetailPage.module.css"), "utf8");
    const rule = (selector: string) => {
      const at = css.indexOf(`${selector} {`);
      expect(at).toBeGreaterThan(-1);
      return css.slice(at, css.indexOf("}", at));
    };
    expect(rule(".sections")).toMatch(/overflow-x:\s*auto/);
    expect(rule(".sections > :global(.alk-seg)")).toMatch(/width:\s*max-content/);
    expect(rule(".sections > :global(.alk-seg)")).toMatch(/max-width:\s*none/);
  });

  it("keeps the danger controls behind the Settings tab, off the default Overview", async () => {
    renderDetail({ role: "alkera_admin" });
    await orgHeading();
    // Overview is the default band and carries the providers; the danger zone is not reachable yet.
    expect(screen.getByRole("tab", { name: TAB_LABELS.overview })).toHaveAttribute("aria-selected", "true");
    expect(providerToggle(GOOGLE.name)).toBeInTheDocument();
    expect(queryDeleteButton()).not.toBeInTheDocument();
    fireEvent.click(settingsTab());
    expect(deleteButton()).toBeInTheDocument();
    expect(queryProviderToggle(GOOGLE.name)).not.toBeInTheDocument();
  });

  it("shows each provider's brand mark and reflects the seeded allow flags on the Overview", async () => {
    renderDetail({ role: "alkera_admin", settings: { [GOOGLE.key]: true, [GITHUB.key]: false } as unknown as OrgSettings });
    await orgHeading();

    // Every provider in the catalogue renders a toggle whose checked state mirrors its seeded flag, and
    // a brand mark (an SVG) in the same row — the read side of the wiring, driven off the constant so a
    // new provider added to the catalogue is covered without editing the test.
    for (const p of SIGN_IN_PROVIDERS) {
      const toggle = providerToggle(p.name);
      expect(toggle).toBeInstanceOf(HTMLInputElement);
      expect(toggle.closest(`[data-provider="${p.name}"]`)?.querySelector("svg")).toBeInTheDocument();
    }
    expect(providerToggle(GOOGLE.name)).toBeChecked();
    expect(providerToggle(GITHUB.name)).not.toBeChecked();

    // The danger actions sit in the Settings band.
    fireEvent.click(settingsTab());
    expect(screen.getByRole("button", { name: DANGER_LABELS.rename })).toBeInTheDocument();
    expect(deleteButton()).toBeInTheDocument();
  });

  it("saves the org's login setting when a provider toggle is flipped", async () => {
    // A blocked provider (GitHub off) toggled ON must PUT the org settings with that flag flipped true —
    // the write side of the wiring. A no-op onChange would leave this fetch un-fired.
    // Only the settings route answers; the Overview's other cards read a 404 and show their error.
    const fetchMock = vi.fn((input: RequestInfo | URL, _init?: RequestInit) =>
      Promise.resolve(
        (input as Request).url.endsWith("/settings")
          ? new Response(JSON.stringify({ [GOOGLE.key]: true, [GITHUB.key]: true } as unknown as OrgSettings), {
              status: 200,
              headers: { "content-type": "application/json" },
            })
          : new Response(JSON.stringify({ detail: "Not found" }), { status: 404, headers: { "content-type": "application/json" } }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    renderDetail({ role: "alkera_admin", settings: { [GOOGLE.key]: true, [GITHUB.key]: false } as unknown as OrgSettings });
    await orgHeading();

    fireEvent.click(providerToggle(GITHUB.name));

    // openapi-fetch invokes fetch with a single Request object; read the wire
    // shape off the WRITE, not off whichever read the band happened to fire
    // first — a sibling card's GET must not decide what this asserts.
    const writeOf = () =>
      fetchMock.mock.calls
        .map((c) => c[0] as unknown as Request)
        .find((r) => r.method === "PUT");
    await waitFor(() => expect(writeOf()).toBeDefined());
    const req = writeOf()!;
    expect(req.url).toContain(`/admin/v1/orgs/${ORG_ID}/settings`);
    // The body carries the flipped flag (true), NOT the flag left alone.
    expect(await req.clone().json()).toEqual({ [GITHUB.key]: true });

    // The success handler surfaces a confirmation toast (status role), proving onSuccess ran end to end.
    expect(await screen.findByRole("status")).toBeInTheDocument();
  });

  it("opens a delete confirmation that names the org from the Settings band", async () => {
    renderDetail({ role: "alkera_admin" });
    await orgHeading();
    fireEvent.click(settingsTab());
    fireEvent.click(deleteButton());
    const dialog = await screen.findByRole("dialog");
    // The confirmation names the exact org being destroyed — title built from the component's own
    // formatter over the seeded fixture, so neither the format nor the org name is a copied literal.
    expect(within(dialog).getByText(deleteConfirmTitle(ORG.name))).toBeInTheDocument();
  });

  // Deleting an org takes every team, member and chat in it, and nothing brings them back. The
  // question is gated on the org's own name so it cannot be answered by muscle memory.
  it("holds the delete shut until the org's name is typed back", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(() => Promise.resolve(new Response(null, { status: 204 })));
    vi.stubGlobal("fetch", fetchMock);
    renderDetail({ role: "alkera_admin" });
    await orgHeading();
    fireEvent.click(settingsTab());
    fireEvent.click(deleteButton());

    const dialog = await screen.findByRole("dialog");
    const key = within(dialog).getByRole("button", { name: DANGER_LABELS.delete });
    const field = within(dialog).getByLabelText(`Type ${ORG.name} to confirm`);
    expect(key).toBeDisabled();

    await user.type(field, ORG.name.slice(0, -1));
    expect(key).toBeDisabled();
    await user.click(key);
    expect(sent(fetchMock, "DELETE")).toHaveLength(0);

    await user.type(field, ORG.name.slice(-1));
    await waitFor(() => expect(key).toBeEnabled());
    await user.click(key);
    await waitFor(() => expect(sent(fetchMock, "DELETE")).toHaveLength(1));
  });

  // A signup that never finished its profile leaves an org with no name. Gated on that empty name,
  // the question stood open with nothing typed, one click from deleting every member.
  it.each([
    ["", "an empty name"],
    ["   ", "a blank name"],
  ])("gates an org with %j (%s) on its short id, and names it by that id", async (name) => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(() => Promise.resolve(new Response(null, { status: 204 })));
    vi.stubGlobal("fetch", fetchMock);
    renderDetail({ role: "alkera_admin", name });
    const label = `Unnamed org (${ORG_ID.slice(0, 8)})`;
    expect(await screen.findByRole("heading", { name: label, level: 1 })).toBeInTheDocument();
    fireEvent.click(settingsTab());
    fireEvent.click(deleteButton());

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(deleteConfirmTitle(label))).toBeInTheDocument();
    const key = within(dialog).getByRole("button", { name: DANGER_LABELS.delete });
    expect(key).toBeDisabled();
    await user.click(key);
    expect(sent(fetchMock, "DELETE")).toHaveLength(0);

    await user.type(within(dialog).getByLabelText(`Type ${ORG_ID.slice(0, 8)} to confirm`), ORG_ID.slice(0, 8));
    await waitFor(() => expect(key).toBeEnabled());
    await user.click(key);
    await waitFor(() => expect(sent(fetchMock, "DELETE")).toHaveLength(1));
    expect(new URL(sent(fetchMock, "DELETE")[0].url).pathname).toBe(`/admin/v1/orgs/${ORG_ID}`);
  });

  it("says the org was deleted, not that the link is stale, once the delete lands", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(() => Promise.resolve(new Response(null, { status: 204 })));
    vi.stubGlobal("fetch", fetchMock);
    renderDetail({ role: "alkera_admin" });
    await orgHeading();
    fireEvent.click(settingsTab());
    fireEvent.click(deleteButton());
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText(`Type ${ORG.name} to confirm`), ORG.name);
    await user.click(within(dialog).getByRole("button", { name: DANGER_LABELS.delete }));

    expect(await screen.findByText(deletedTitle(ORG.name))).toBeInTheDocument();
    expect(screen.queryByText(GONE_ORG.title)).toBeNull();
    expect(screen.getByRole("link", { name: GONE_ORG.action }).getAttribute("href")).toBe("/admin/orgs");
  });

  it("asks for the name without the spaces around it", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(() => Promise.resolve(new Response(null, { status: 204 })));
    vi.stubGlobal("fetch", fetchMock);
    renderDetail({ role: "alkera_admin", name: `  ${ORG.name} ` });
    await orgHeading();
    fireEvent.click(settingsTab());
    fireEvent.click(deleteButton());
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText(`Type ${ORG.name} to confirm`), ORG.name);
    await waitFor(() => expect(within(dialog).getByRole("button", { name: DANGER_LABELS.delete })).toBeEnabled());
  });

  it("backing out of the delete leaves the org standing", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(() => Promise.resolve(new Response(null, { status: 204 })));
    vi.stubGlobal("fetch", fetchMock);
    renderDetail({ role: "alkera_admin" });
    await orgHeading();
    fireEvent.click(settingsTab());
    fireEvent.click(deleteButton());
    await screen.findByRole("dialog");

    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Cancel" }));

    expect(sent(fetchMock, "DELETE")).toHaveLength(0);
    expect(deleteButton()).toBeInTheDocument();
  });

  it("hides the Settings band from a support-grade staffer — no tab, no providers, no danger actions", async () => {
    renderDetail({ role: "alkera_support" });
    await orgHeading();
    const tabs = screen.getAllByRole("tab").map((t) => t.textContent);
    expect(tabs).toEqual(Object.values(TAB_LABELS).filter((label) => label !== TAB_LABELS.settings));
    expect(screen.queryByRole("tab", { name: TAB_LABELS.settings })).not.toBeInTheDocument();
    // The Settings-only controls aren't merely hidden behind a tab — they're absent from the tree.
    expect(queryProviderToggle(GOOGLE.name)).not.toBeInTheDocument();
    expect(queryDeleteButton()).not.toBeInTheDocument();
  });
});

// The pill beside the org name carries the roster size. The page builds that reading inline rather
// than through an exported formatter, so the noun is the one string here spelled out; the figure is
// still derived through `count`, the register's shared reading, so the separator follows the viewer's
// locale rather than a hard-coded comma.
describe("the roster-size pill", () => {
  it.each([
    [1, "member"],
    [42, "members"],
    [1234, "members"],
  ])("reads a roster of %i with the noun %s", async (memberCount, noun) => {
    renderDetail({ role: "alkera_support", memberCount });
    await orgHeading();
    expect(screen.getByText(`${count(memberCount)} ${noun}`)).toBeInTheDocument();
  });

  it("never pluralizes a roster of one", async () => {
    // The asymmetric guard: an always-plural reading, or a naive "member(s)", fails here.
    renderDetail({ role: "alkera_support", memberCount: 1 });
    await orgHeading();
    expect(screen.queryByText("1 members")).not.toBeInTheDocument();
  });
});
