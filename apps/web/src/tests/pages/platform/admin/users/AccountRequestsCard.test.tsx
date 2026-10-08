// The support tool's account requests card on its real hooks against a stubbed server.

import { MemoryRouter } from "react-router-dom";
import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";
import { AccountRequestsCard } from "@/pages/platform/admin/users/AccountRequestsCard";

const USER_ID = "8f1c2b9e-0000-4000-8000-000000000001";
const BASE = `/admin/v1/users/${USER_ID}/account`;

let account: Record<string, unknown>;
let fetchSpy: ReturnType<typeof vi.fn>;

const json = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

const scheduled = (immediate: boolean) => ({
  id: "d-1",
  status: "scheduled",
  source: "support",
  requested_at: "2026-10-05T00:00:00Z",
  purge_after: immediate ? "2026-10-05T00:00:00Z" : "2026-10-19T00:00:00Z",
  blocked_reason: null,
});

async function route(req: Request): Promise<Response> {
  const p = new URL(req.url).pathname;
  if (p === BASE && req.method === "GET") return json(account);
  if (p === `${BASE}/deletion` && req.method === "POST") {
    const body = (await req.clone().json()) as { immediate: boolean };
    account = { ...account, deletion: scheduled(body.immediate) };
    return json(account.deletion, 202);
  }
  if (p === `${BASE}/deletion` && req.method === "DELETE") {
    account = { ...account, deletion: { ...scheduled(false), status: "cancelled" } };
    return json({ request: null });
  }
  return json({ detail: "unmatched" }, 404);
}

const posts = (path: string, method = "POST") =>
  fetchSpy.mock.calls.map(([r]) => r as Request).filter((r) => r.method === method && new URL(r.url).pathname === path);

const onDone = vi.fn();

function renderCard(isAdmin: boolean) {
  render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter>
        <AccountRequestsCard userId={USER_ID} isAdmin={isAdmin} onDone={onDone} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  account = { user_id: USER_ID, deleted_at: null, deletion: null };
  onDone.mockReset();
  fetchSpy = vi.fn(async (input: Request | string, init?: RequestInit) =>
    route(input instanceof Request ? input : new Request(input, init)),
  );
  vi.stubGlobal("fetch", fetchSpy);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("AccountRequestsCard", () => {
  it("shows support the requests and offers no action", async () => {
    renderCard(false);
    expect(await screen.findByText("Deletion")).toBeInTheDocument();
    expect(await screen.findByText("None")).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("lets an admin schedule a deletion with the grace period after confirming", async () => {
    renderCard(true);
    await userEvent.click(await screen.findByRole("button", { name: "Schedule deletion" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Schedule deletion" }));
    await waitFor(() => expect(posts(`${BASE}/deletion`)).toHaveLength(1));
    expect(await posts(`${BASE}/deletion`)[0]!.clone().json()).toEqual({ immediate: false });
    expect(await screen.findByText(/Scheduled for/)).toBeInTheDocument();
    expect(onDone).toHaveBeenCalledWith("Deletion scheduled.", true);
  });

  it("erases now only from a scheduled deletion, and can cancel it", async () => {
    account = { ...account, deletion: scheduled(false) };
    renderCard(true);
    await userEvent.click(await screen.findByRole("button", { name: "Erase now" }));
    await userEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Erase now" }));
    await waitFor(() => expect(posts(`${BASE}/deletion`)).toHaveLength(1));
    expect(await posts(`${BASE}/deletion`)[0]!.clone().json()).toEqual({ immediate: true });

    await userEvent.click(await screen.findByRole("button", { name: "Cancel deletion" }));
    await userEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Cancel deletion" }));
    await waitFor(() => expect(posts(`${BASE}/deletion`, "DELETE")).toHaveLength(1));
    expect(await screen.findByText("Last request cancelled")).toBeInTheDocument();
  });

  // The data export is the product's: the open card neither shows nor offers one.
  it("names no export and offers none to an admin", async () => {
    renderCard(true);
    expect(await screen.findByRole("button", { name: "Schedule deletion" })).toBeInTheDocument();
    expect(screen.queryByText("Latest export")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Request export" })).not.toBeInTheDocument();
  });

  it("offers nothing on an account already erased", async () => {
    account = { ...account, deleted_at: "2026-10-01T00:00:00Z" };
    renderCard(true);
    expect(await screen.findByText(/^Erased /)).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
});
