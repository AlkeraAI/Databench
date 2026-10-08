// The open General tab with nothing installed: it reads the dashboard and the org's sign-in
// settings, asks for no knowledge sync or Slack document, and loads cleanly when every other
// route answers 404 (as the open backend does for a private one).

import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";
import { OrgBody } from "@/pages/organization/settings/OrgBody";
import { silentNotify } from "@/tests/fixtures/notify";

const DASH = { org: { id: "org_8Fk2", name: "Tideline Analytics", member_count: 12 }, teams: [], is_org_admin: true };
const SETTINGS = { allow_login_google: true, allow_login_github: true };

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("the open org settings General tab", () => {
  it("loads from open routes alone", async () => {
    const unserved: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = new URL(input instanceof Request ? input.url : String(input)).pathname;
        const json = (body: unknown, status = 200) =>
          new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
        if (path === "/api/v1/dashboard") return json(DASH);
        if (path === "/api/v1/org/settings") return json(SETTINGS);
        if (path === "/api/v1/auth/me") return json({ id: "u1", email: "vera@x.io", admin_team_ids: [] });
        if (path === "/api/v1/config") return json({ self_hosted: true });
        unserved.push(path);
        return json({ detail: "Not Found" }, 404);
      }),
    );
    render(
      <QueryClientProvider client={createQueryClient({ retry: false })}>
        <MemoryRouter>
          <OrgBody notify={silentNotify} />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByRole("checkbox", { name: "Allow sign-in with Google" })).toBeChecked();
    expect(screen.queryByText(/couldn.t load/i)).toBeNull();
    expect(screen.queryByText("Knowledge sync")).toBeNull();
    expect(unserved).toEqual([]);
  });
});
