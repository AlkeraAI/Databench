// The open end of the account settings, on its real hooks against a stubbed server. The server
// state is mutable, so a POST that schedules a deletion is what the next GET reads back, exactly
// as the shared MutationCache policy refreshes it in the product.
//
// Nothing is installed here, as in the open build: the page is the deletion section alone. The
// product's export section has its own suite beside the private code that registers it.

import { MemoryRouter } from "react-router-dom";
import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";
import type { CurrentUser } from "@/api/auth";
import { AccountLifecycleSections } from "@/pages/organization/settings/AccountLifecycleSections";

const USER = { id: "u-1", email: "quill@tideline.example", first_name: "Quill", last_name: "Lee" } as unknown as CurrentUser;

type Plan = {
  orgs: Array<Record<string, unknown>>;
  blockers: Array<Record<string, unknown>>;
  forfeited_credit_nanos: number;
  can_proceed: boolean;
  grace_days: number;
  reauth: "password" | "recent_sign_in";
  mfa_required: boolean;
};

const PLAN: Plan = {
  orgs: [
    { org_id: "o-1", org_name: "Tideline", fate: "leave", transfer_to_name: "Dana Admin", shared_items: 3, private_items: 1 },
    { org_id: "o-2", org_name: "Quill Labs", fate: "close", transfer_to_name: null, shared_items: 0, private_items: 4 },
  ],
  blockers: [],
  forfeited_credit_nanos: 2_500_000_000,
  can_proceed: true,
  grace_days: 14,
  reauth: "password",
  mfa_required: false,
};

let plan: Plan;
let scheduled: Record<string, unknown> | null;
let deletionRefusal: { status: number; code: string } | null;
let fetchSpy: ReturnType<typeof vi.fn>;

const json = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

async function route(req: Request): Promise<Response> {
  const p = new URL(req.url).pathname;
  if (p === "/api/v1/me/account/deletion/plan") return json(plan);
  if (p === "/api/v1/me/account/deletion" && req.method === "GET") return json({ request: scheduled });
  if (p === "/api/v1/me/account/deletion" && req.method === "POST") {
    if (deletionRefusal) {
      return json({ error: { code: deletionRefusal.code, message: "refused", trace_id: "t" } }, deletionRefusal.status);
    }
    scheduled = {
      id: "d-1",
      status: "scheduled",
      source: "self",
      requested_at: "2026-10-05T00:00:00Z",
      purge_after: "2026-10-19T00:00:00Z",
      blocked_reason: null,
    };
    return json(scheduled, 202);
  }
  if (p === "/api/v1/me/account/deletion" && req.method === "DELETE") {
    scheduled = null;
    return json({ request: null });
  }
  return json({ detail: `unmatched ${req.method} ${p}` }, 404);
}

const calls = (method: string, path: string) =>
  fetchSpy.mock.calls
    .map(([input]) => input as Request)
    .filter((r) => r.method === method && new URL(r.url).pathname === path);

const notify = { success: vi.fn(), error: vi.fn() };

function renderSections() {
  return render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter>
        <AccountLifecycleSections user={USER} notify={notify as never} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  plan = structuredClone(PLAN);
  scheduled = null;
  deletionRefusal = null;
  notify.success.mockReset();
  notify.error.mockReset();
  fetchSpy = vi.fn(async (input: Request | string, init?: RequestInit) =>
    route(input instanceof Request ? input : new Request(input, init)),
  );
  vi.stubGlobal("fetch", fetchSpy);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("the open account settings", () => {
  it("offers no data export, and the deletion section is all it draws", async () => {
    renderSections();
    expect(await screen.findByRole("button", { name: "Delete account" })).toBeInTheDocument();
    expect(screen.queryByText("Export your data")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Request export" })).not.toBeInTheDocument();
    const asked = fetchSpy.mock.calls.map(([input]) => new URL((input as Request).url).pathname);
    expect(asked.filter((path) => path.includes("/exports"))).toEqual([]);
  });
});

describe("delete account", () => {
  async function openDialog() {
    renderSections();
    await userEvent.click(await screen.findByRole("button", { name: "Delete account" }));
    return screen.findByRole("dialog", { name: "Delete your account?" });
  }

  it("shows each organization's fate and what is forfeited before anything is confirmed", async () => {
    const dialog = await openDialog();
    const list = await within(dialog).findByRole("list", { name: "What happens" });
    const lines = within(list).getAllByRole("listitem").map((li) => li.textContent);
    expect(lines).toContain("You leave Tideline. 3 shared items move to Dana Admin. 1 private item is deleted.");
    expect(lines).toContain("Quill Labs closes with your account. Everything in it is deleted.");
    expect(lines).toContain("Unused credit of $2.50 is forfeited.");
    expect(lines).toContain("Your account is deleted in 14 days. You can cancel until then.");
    expect(calls("POST", "/api/v1/me/account/deletion")).toHaveLength(0);
  });

  it("confirms only with the password and the email typed exactly, then schedules", async () => {
    const dialog = await openDialog();
    const confirm = within(dialog).getByRole("button", { name: "Delete account" });
    await userEvent.type(await within(dialog).findByLabelText("Current password"), "hunter2-long");
    expect(confirm).toBeDisabled();
    await userEvent.type(within(dialog).getByLabelText("Type quill@tideline.example to confirm"), "quill@tideline.example");
    expect(confirm).toBeEnabled();
    await userEvent.click(confirm);

    await waitFor(() => expect(calls("POST", "/api/v1/me/account/deletion")).toHaveLength(1));
    const body = await calls("POST", "/api/v1/me/account/deletion")[0]!.clone().json();
    expect(body).toEqual({ confirm_email: "quill@tideline.example", current_password: "hunter2-long", mfa_code: null });
    await waitFor(() => expect(notify.success).toHaveBeenCalledWith(expect.stringMatching(/^Your account will be deleted on /)));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Cancel deletion" })).toBeInTheDocument();
  });

  it("keeps the confirm disabled for anything but the exact email", async () => {
    const dialog = await openDialog();
    await userEvent.type(await within(dialog).findByLabelText("Current password"), "pw");
    const typed = within(dialog).getByLabelText("Type quill@tideline.example to confirm");
    for (const wrong of ["quill@tideline.example.org", "QUILL@tideline.example"]) {
      await userEvent.clear(typed);
      await userEvent.type(typed, wrong);
      expect(within(dialog).getByRole("button", { name: "Delete account" })).toBeDisabled();
    }
  });

  it("lists blockers and offers no confirmation while they stand", async () => {
    plan.can_proceed = false;
    plan.blockers = [{ code: "last_admin", org_id: "o-1", org_name: "Tideline", message: "Make another member an admin of Tideline." }];
    const dialog = await openDialog();
    expect(await within(dialog).findByText("Settle these first")).toBeInTheDocument();
    expect(within(dialog).getByText("Make another member an admin of Tideline.")).toBeInTheDocument();
    expect(within(dialog).queryByLabelText("Current password")).not.toBeInTheDocument();
    expect(within(dialog).queryByLabelText("Type quill@tideline.example to confirm")).not.toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Delete account" })).toBeDisabled();
  });

  it("asks for an authenticator code when the account has one", async () => {
    plan.mfa_required = true;
    const dialog = await openDialog();
    await userEvent.type(await within(dialog).findByLabelText("Current password"), "pw");
    await userEvent.type(within(dialog).getByLabelText("Type quill@tideline.example to confirm"), "quill@tideline.example");
    const confirm = within(dialog).getByRole("button", { name: "Delete account" });
    expect(confirm).toBeDisabled();
    await userEvent.type(within(dialog).getByLabelText("Authenticator code"), "123456");
    expect(confirm).toBeEnabled();
  });

  it("asks an account without a password for a recent sign-in instead", async () => {
    plan.reauth = "recent_sign_in";
    const dialog = await openDialog();
    expect(await within(dialog).findByText("You need to have signed in within the last 10 minutes.")).toBeInTheDocument();
    expect(within(dialog).queryByLabelText("Current password")).not.toBeInTheDocument();
    await userEvent.type(within(dialog).getByLabelText("Type quill@tideline.example to confirm"), "quill@tideline.example");
    expect(within(dialog).getByRole("button", { name: "Delete account" })).toBeEnabled();
  });

  it.each([
    ["current_password_invalid", "That password isn't correct."],
    ["reauth_required", "Sign out and sign in again, then delete your account within 10 minutes."],
  ])("says what was refused (%s) and keeps the dialog open", async (code, sentence) => {
    deletionRefusal = { status: 403, code };
    const dialog = await openDialog();
    const password = await within(dialog).findByLabelText("Current password");
    await userEvent.type(password, "wrong-pw");
    await userEvent.type(within(dialog).getByLabelText("Type quill@tideline.example to confirm"), "quill@tideline.example");
    await userEvent.click(within(dialog).getByRole("button", { name: "Delete account" }));
    expect(await within(dialog).findByText(sentence)).toBeInTheDocument();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    if (code === "current_password_invalid") expect(password).toHaveValue("");
  });

  it("shows a scheduled deletion's date and cancels it", async () => {
    scheduled = {
      id: "d-1",
      status: "scheduled",
      source: "self",
      requested_at: "2026-10-05T00:00:00Z",
      purge_after: "2026-10-19T00:00:00Z",
      blocked_reason: null,
    };
    renderSections();
    expect(await screen.findByText(/Your account will be deleted on/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Cancel deletion" }));
    await waitFor(() => expect(calls("DELETE", "/api/v1/me/account/deletion")).toHaveLength(1));
    await waitFor(() => expect(notify.success).toHaveBeenCalledWith("Your account will not be deleted."));
    expect(await screen.findByRole("button", { name: "Delete account" })).toBeInTheDocument();
  });

  it("says a held deletion is on hold", async () => {
    scheduled = {
      id: "d-1",
      status: "scheduled",
      source: "self",
      requested_at: "2026-10-05T00:00:00Z",
      purge_after: "2026-10-19T00:00:00Z",
      blocked_reason: "last_admin",
    };
    renderSections();
    expect(await screen.findByText(/It is on hold until what blocks it is settled/)).toBeInTheDocument();
  });
});
