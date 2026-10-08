import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { components } from "@alkera/sdk";

import { meKey } from "@/api/auth";
import { keys } from "@/api/keys";
import { createQueryClient } from "@/api/queryClient";
import { AdminOrgDetailPage, TAB_LABELS } from "@/pages/platform/admin/orgs/AdminOrgDetailPage";
import { STORAGE_LABELS } from "@/pages/platform/admin/orgs/StorageCard";

// The Storage band on the org-detail register — the only place an org's Files
// ceiling is written, and the ONLY way an Enterprise org (whose plan sets no
// figure) ever gets one. Driven through the real page, the real hooks and a real
// policy-bearing client with only `fetch` stubbed, so what is pinned is the
// operator's whole path: the reading names where the ceiling comes from, and each
// of the three writes puts a different, non-interchangeable request on the wire.

type Org = components["schemas"]["OrgRead"];
type User = components["schemas"]["UserRead"];
type Storage = components["schemas"]["OrgStorageRead"];

const ORG_ID = "org-under-test";
const GB = 1_000_000_000;
const TB = 1_000_000_000_000;

const ORG: Org = {
  name: "Tideline Analytics",
  id: ORG_ID,
  parent_team_id: null,
  is_root: true,
  created_at: "2026-01-01T00:00:00Z",
  member_count: 3,
};

const onPlan: Storage = {
  org_id: ORG_ID,
  plan_tier: "pro",
  storage_limit_bytes: 5 * TB,
  storage_limit_source: "plan",
  storage_used_bytes: 12 * GB,
  override_set: false,
  updated_at: null,
};

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

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

/** Render the register with the viewer's grade and the org's storage document
 *  served over `fetch`, returning the recorded calls so a test can assert the
 *  exact request the UI sent. `writeAnswer` lets a case make the write fail. */
function renderStorage({
  role = "alkera_admin" as User["platform_role"],
  storage = onPlan,
  writeAnswer,
  readAnswer,
  retry = false,
}: {
  role?: User["platform_role"];
  storage?: Storage;
  writeAnswer?: () => Response;
  /** Answers the storage READ in place of the served document. */
  readAnswer?: () => Response;
  /** Build the client with the portal's real retry policy rather than none. */
  retry?: boolean;
} = {}) {
  const calls: { url: string; method: string; body: unknown }[] = [];
  let served = storage;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const req = input instanceof Request ? input : null;
      const url = req ? req.url : String(input);
      const method = (req ? req.method : init?.method) ?? "GET";
      const raw = req ? await req.clone().text() : init?.body ? String(init.body) : "";
      calls.push({ url, method, body: raw ? JSON.parse(raw) : null });
      if (!url.includes("/storage")) {
        return new Response(JSON.stringify({ detail: `unstubbed ${url}` }), { status: 500 });
      }
      if (method !== "GET") {
        if (writeAnswer) return writeAnswer();
        const body = calls[calls.length - 1].body as { limit_bytes?: number | null } | null;
        served =
          method === "DELETE"
            ? onPlan
            : {
                ...served,
                storage_limit_bytes: body?.limit_bytes ?? null,
                storage_limit_source: "override",
                override_set: true,
              };
        return new Response(JSON.stringify(served), { status: 200, headers: { "content-type": "application/json" } });
      }
      if (readAnswer) return readAnswer();
      return new Response(JSON.stringify(served), { status: 200, headers: { "content-type": "application/json" } });
    }),
  );

  const qc = createQueryClient(retry ? undefined : { retry: false });
  qc.setQueryData(meKey, staff(role));
  qc.setQueryData(keys.admin.org(ORG_ID), ORG);
  qc.setQueryData(keys.admin.orgMembers(ORG_ID), []);
  // The sibling bands on the billing tab read their own endpoints; seed them so
  // this test exercises only the storage band.
  qc.setQueryData(keys.admin.enterpriseOrgs, []);
  qc.setQueryData(keys.admin.billingOrg(ORG_ID), { pool_balances: [], team_pools: [], members: [] });
  qc.setQueryData(keys.admin.recurringGrantsOrg(ORG_ID), []);
  qc.setQueryData(keys.admin.computeOrg(ORG_ID), []);
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[`/admin/orgs/${ORG_ID}`]}>
        <Routes>
          <Route path="/admin/orgs/:orgId" element={<AdminOrgDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { calls };
}

const openBilling = () => fireEvent.click(screen.getByRole("tab", { name: TAB_LABELS.overview }));

/** The storage card, found by its heading — the billing tab holds several cards
 *  and a figure must be read from this one, not a neighbour. */
async function storageCard(): Promise<HTMLElement> {
  const heading = await screen.findByRole("heading", { name: STORAGE_LABELS.title });
  return heading.closest(".alk-card") as HTMLElement;
}

/** The write the UI sent, once one has been sent. */
async function write(calls: { url: string; method: string; body: unknown }[], method: string) {
  return waitFor(() => {
    const found = calls.find((c) => c.method === method && c.url.includes("/storage"));
    expect(found).toBeDefined();
    return found!;
  });
}

/** Save the figure currently in the editor, through the confirmation. */
async function saveThroughConfirm() {
  fireEvent.click(screen.getByRole("button", { name: STORAGE_LABELS.save }));
  const dialog = await screen.findByRole("dialog");
  fireEvent.click(within(dialog).getByRole("button", { name: STORAGE_LABELS.save }));
}

describe("Admin org detail — the org's storage ceiling", () => {
  // The figure in the editor is not the figure the org lives under until the question is answered:
  // dismissing it must leave the server holding the old ceiling.
  it("writes nothing when the ceiling question is dismissed", async () => {
    const { calls } = renderStorage();
    openBilling();
    await storageCard();

    fireEvent.click(screen.getByRole("button", { name: STORAGE_LABELS.save }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

    expect(calls.some((c) => c.method !== "GET" && c.url.includes("/storage"))).toBe(false);
    // The editor is untouched, so the operator can answer again without re-entering the figure.
    expect(within(await storageCard()).getByRole("button", { name: STORAGE_LABELS.save })).toBeInTheDocument();
  });

  it("reads a plan-default ceiling as the plan, its tier and its figure", async () => {
    renderStorage();
    openBilling();
    const card = await storageCard();
    expect(within(card).getByText("Plan default · Pro · 5 TB")).toBeInTheDocument();
    expect(within(card).getByText("5 TB")).toBeInTheDocument();
    expect(within(card).getByText("12 GB")).toBeInTheDocument();
  });

  it("says an operator wrote the ceiling when one did", async () => {
    renderStorage({
      storage: { ...onPlan, storage_limit_bytes: 2 * TB, storage_limit_source: "override", override_set: true },
    });
    openBilling();
    const card = await storageCard();
    expect(within(card).getByText("Set by an operator")).toBeInTheDocument();
    expect(within(card).queryByText(/Plan default/)).not.toBeInTheDocument();
  });

  it("distinguishes an explicit unlimited from an org that simply has no ceiling", async () => {
    // The two render the same figure (none) and are opposite states: an unlimited
    // override ignores the plan forever, a deployment default follows it. The
    // source line is the only thing on screen that tells them apart.
    renderStorage({
      storage: { ...onPlan, storage_limit_bytes: null, storage_limit_source: "override", override_set: true },
    });
    openBilling();
    const overridden = await storageCard();
    expect(within(overridden).getByText("Unlimited (set by an operator)")).toBeInTheDocument();

    cleanup();
    vi.unstubAllGlobals();
    renderStorage({
      storage: { ...onPlan, storage_limit_bytes: 100 * GB, storage_limit_source: "default", override_set: false },
    });
    openBilling();
    const fallback = await storageCard();
    expect(within(fallback).getByText("Deployment default")).toBeInTheDocument();
  });

  it("sends the entered figure scaled by its unit, and shows the byte count first", async () => {
    const { calls } = renderStorage();
    openBilling();
    const card = await storageCard();

    fireEvent.change(within(card).getByLabelText(STORAGE_LABELS.editLabel), { target: { value: "2" } });
    fireEvent.click(within(card).getByRole("radio", { name: "TB" }));
    // The exact commitment is on screen BEFORE the save — this is where a
    // mistyped ceiling is caught, and the count is the decimal one the figure
    // means: 2 TB is 2,000,000,000,000 bytes, not 2 x 2^40.
    expect(within(card).getByText("2,000,000,000,000 bytes")).toBeInTheDocument();

    await saveThroughConfirm();

    const put = await write(calls, "PUT");
    expect(put.url).toContain(`/admin/v1/orgs/${ORG_ID}/storage`);
    expect(put.body).toEqual({ limit_bytes: 2_000_000_000_000 });
  });

  it("offers the unit as a radio group, so the page's tabs are only its own", async () => {
    // A unit picked beside an amount is a value, not a view: announced as tabs, GB and TB read as
    // two more of the page's sections.
    renderStorage();
    openBilling();
    const card = await storageCard();
    const units = within(card).getByRole("radiogroup", { name: `Unit for ${STORAGE_LABELS.editLabel}` });
    expect(within(units).getAllByRole("radio").map((r) => [r.textContent, r.getAttribute("aria-checked")])).toEqual([
      ["GB", "false"],
      ["TB", "true"],
    ]);
    expect(screen.queryByRole("tab", { name: "GB" })).toBeNull();
    expect(screen.queryByRole("tab", { name: "TB" })).toBeNull();
  });

  it("sends a null ceiling — not a zero, and not a DELETE — for Unlimited", async () => {
    const { calls } = renderStorage();
    openBilling();
    const card = await storageCard();

    fireEvent.click(within(card).getByLabelText(STORAGE_LABELS.unlimitedSwitch));
    await saveThroughConfirm();

    const put = await write(calls, "PUT");
    expect(put.body).toEqual({ limit_bytes: null });
    expect(calls.some((c) => c.method === "DELETE")).toBe(false);
  });

  it("drops the override with a DELETE when the operator hands the org back to its plan", async () => {
    const { calls } = renderStorage({
      storage: { ...onPlan, storage_limit_bytes: 2 * TB, storage_limit_source: "override", override_set: true },
    });
    openBilling();
    const card = await storageCard();

    fireEvent.click(within(card).getByRole("button", { name: STORAGE_LABELS.usePlan }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: STORAGE_LABELS.usePlan }));

    const del = await write(calls, "DELETE");
    expect(del.url).toContain(`/admin/v1/orgs/${ORG_ID}/storage`);
    expect(calls.some((c) => c.method === "PUT")).toBe(false);
  });

  it("offers no plan fallback on an org that has no override to drop", async () => {
    renderStorage();
    openBilling();
    const card = await storageCard();
    expect(within(card).getByRole("button", { name: STORAGE_LABELS.usePlan })).toBeDisabled();
  });

  it("shows the server's own refusal rather than a generic failure", async () => {
    renderStorage({
      writeAnswer: () =>
        new Response(JSON.stringify({ error: { code: "storage_limit_below_usage", message: "This org already stores 12 GB." } }), {
          status: 409,
          headers: { "content-type": "application/json" },
        }),
    });
    openBilling();
    const card = await storageCard();

    fireEvent.change(within(card).getByLabelText(STORAGE_LABELS.editLabel), { target: { value: "1" } });
    await saveThroughConfirm();

    expect(await screen.findByText("This org already stores 12 GB.")).toBeInTheDocument();
  });

  it("keeps the ceiling read-only for a support-grade staffer and says why", async () => {
    renderStorage({ role: "alkera_support" });
    openBilling();
    const card = await storageCard();
    expect(within(card).getByText("12 GB")).toBeInTheDocument();
    expect(within(card).getByText(STORAGE_LABELS.adminOnly)).toBeInTheDocument();
    expect(within(card).queryByRole("button", { name: STORAGE_LABELS.save })).not.toBeInTheDocument();
    expect(within(card).queryByRole("button", { name: STORAGE_LABELS.usePlan })).not.toBeInTheDocument();
  });
  it("states a refused read as platform-admin only, at once, without asking again", async () => {
    const { calls } = renderStorage({
      role: "alkera_support",
      retry: true,
      readAnswer: () =>
        new Response(JSON.stringify({ detail: "Platform admin only" }), {
          status: 403,
          headers: { "content-type": "application/json" },
        }),
    });
    openBilling();
    const card = await storageCard();
    expect(within(card).getByText(STORAGE_LABELS.readRefused)).toBeInTheDocument();
    expect(screen.queryByText(/couldn.t load this org.s storage/i)).not.toBeInTheDocument();
    expect(within(card).queryByRole("button", { name: /try again/i })).not.toBeInTheDocument();
    expect(calls.filter((c) => c.method === "GET" && c.url.includes("/storage"))).toHaveLength(1);
  });
});

describe("Admin org detail — the storage figure is read as typed", () => {
  // Stripping non-digits would save "-1" as 1 GB and "1e3" as 13 GB, so the text stays as typed
  // and is refused with a reason. The byte line under the field is the reason line, so an
  // operator reads it before Save is even reachable.
  it.each([
    ["-1", "Enter a plain number, like 12.50 (digits and a point only)."],
    ["1e3", "Enter a plain number, like 12.50 (digits and a point only)."],
    ["0", "Enter an amount above 0, or use Clear to remove the limit."],
  ])("keeps %s in the field, refuses to save it and says why", async (typed, reason) => {
    const { calls } = renderStorage();
    openBilling();
    const card = await storageCard();
    const field = within(card).getByLabelText(STORAGE_LABELS.editLabel);
    fireEvent.change(field, { target: { value: typed } });
    expect(field).toHaveValue(typed);
    expect(within(card).getByText(reason)).toBeInTheDocument();
    expect(within(card).getByRole("button", { name: STORAGE_LABELS.save })).toBeDisabled();
    expect(calls.some((c) => c.method !== "GET" && c.url.includes("/storage"))).toBe(false);
  });

  it("refuses a figure past the route's bound with the bound named, instead of a 500 after the fact", async () => {
    renderStorage();
    openBilling();
    const card = await storageCard();
    fireEvent.click(within(card).getByRole("radio", { name: "TB" }));
    fireEvent.change(within(card).getByLabelText(STORAGE_LABELS.editLabel), { target: { value: "99999999999" } });
    expect(within(card).getByText("At most 1000 PB.")).toBeInTheDocument();
    expect(within(card).getByRole("button", { name: STORAGE_LABELS.save })).toBeDisabled();
  });

  it("shows the exact byte count for a figure it will save", async () => {
    renderStorage();
    openBilling();
    const card = await storageCard();
    fireEvent.change(within(card).getByLabelText(STORAGE_LABELS.editLabel), { target: { value: "2" } });
    expect(within(card).getByText("2,000,000,000,000 bytes")).toBeInTheDocument();
    expect(within(card).getByRole("button", { name: STORAGE_LABELS.save })).toBeEnabled();
  });
});
