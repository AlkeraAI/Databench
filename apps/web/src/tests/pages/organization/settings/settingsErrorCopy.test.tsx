// What a failed settings write says out loud.
//
// A save can fail two ways, and only one of them has a sentence a reader can
// use. The server writes one ("You no longer administer this organization"); a
// network failure does not — the browser throws a TypeError whose message,
// "Failed to fetch", is addressed to a developer. Showing it as product copy is
// the bug this file pins: the page has a written fallback for exactly that case
// and it must be the one that reaches the toast.
//
// Driven through the REAL hooks and a real createQueryClient, with only `fetch`
// stubbed — the toast text asserted is the one the page would really flash.

import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";
import { errText } from "@/pages/organization/settings/fields";
import { OrgBody } from "@/pages/organization/settings/OrgBody";
import { recordingNotify } from "@/tests/fixtures/notify";

const DASH = {
  org: { id: "org_8Fk2", name: "Tideline Analytics", member_count: 12 },
  teams: [{}, {}],
  is_org_admin: true,
};
const SETTINGS = { allow_login_google: true, allow_login_github: true };
const SYNC = {
  sync_enabled: true,
  default_promotion: "private",
  classification_strictness: "standard",
  auto_promote_policy: "off",
  pull_cadence_seconds: 60,
};

/** The written fallback the page passes for a sign-in-method save. */
const FALLBACK = "Could not save sign-in methods.";
/** What the browser throws when the request never reaches a server. */
const NETWORK_FAILURE = new TypeError("Failed to fetch");

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

/** A server that serves the page's three documents and fails every PUT the given way. */
function stubServer(onPut: () => Promise<Response>) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const isRequest = typeof Request !== "undefined" && input instanceof Request;
      const url = isRequest ? (input as Request).url : String(input);
      const method = (isRequest ? (input as Request).method : (init?.method ?? "GET")).toUpperCase();
      if (method === "PUT") return onPut();
      if (url.includes("/org/slack")) return json({ connection: null, install_mode: "unavailable" });
      if (url.includes("/sync-settings")) return json(SYNC);
      if (url.includes("/org/settings")) return json(SETTINGS);
      return json(DASH);
    }),
  );
}

function renderBody() {
  const recorded = recordingNotify();
  const { notify } = recorded;
  render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter>
        <OrgBody notify={notify} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return recorded;
}

const googleSwitch = () =>
  screen.getByRole("checkbox", { name: "Allow sign-in with Google" }) as HTMLInputElement;

/** A sign-in method changes only after its confirmation, so the write under test starts here. */
async function confirmTheFlip(): Promise<void> {
  await screen.findByRole("dialog", { name: "Stop allowing sign-in with Google?" });
  await userEvent.click(screen.getByRole("button", { name: "Stop allowing" }));
}

beforeEach(() => {
  vi.clearAllMocks();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("a failed settings save says what the page wrote, not what the engine threw", () => {
  it("toasts the written fallback when the request never reaches the server", async () => {
    stubServer(() => Promise.reject(NETWORK_FAILURE));
    const { errors: toasts, successes } = renderBody();
    await waitFor(() => expect(googleSwitch().checked).toBe(true));

    await userEvent.click(googleSwitch());
    await confirmTheFlip();

    await waitFor(() => expect(toasts).toEqual([FALLBACK]));
    // A refusal is reported as an error, never under a success check.
    expect(successes).toEqual([]);
    expect(toasts).not.toContain(NETWORK_FAILURE.message);
    // The control that could not be saved goes back to what the org actually has.
    await waitFor(() => expect(googleSwitch().checked).toBe(true));
  });

  it("still shows the sentence the server wrote when there is one", async () => {
    const refused = "You no longer administer this organization.";
    stubServer(() => Promise.resolve(json({ error: { code: "forbidden", message: refused } }, 403)));
    const { errors: toasts, successes } = renderBody();
    await waitFor(() => expect(googleSwitch().checked).toBe(true));

    await userEvent.click(googleSwitch());
    await confirmTheFlip();

    await waitFor(() => expect(toasts).toEqual([refused]));
    expect(successes).toEqual([]);
  });
});

describe("errText", () => {
  it("keeps a server sentence and drops every message written for a developer", () => {
    // The two shapes a settings mutation can throw, plus the shapes a thrown
    // non-Error takes — only the first carries copy meant for a reader.
    const fallback = "Could not save that.";
    expect(errText(new TypeError("Failed to fetch"), fallback)).toBe(fallback);
    expect(errText(new Error("Cannot read properties of undefined"), fallback)).toBe(fallback);
    expect(errText("boom", fallback)).toBe(fallback);
    expect(errText(undefined, fallback)).toBe(fallback);
  });
});
