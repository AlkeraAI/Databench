import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useAdminOrgs, useSetPlatformRoleMutation } from "@/api/admin/admin";
import { ApiError } from "@/api/errors";

// The admin hooks driven through a REAL QueryClient with only `fetch` stubbed. These pin the wire
// contract a regression would silently break: the list query hits the right endpoint and surfaces a
// structured ApiError on failure (so a page can render its error state), and the role mutation PATCHes
// the platform_role with the body the backend expects (a null demotes to a regular user).

let fetchSpy: ReturnType<typeof vi.fn>;
beforeEach(() => {
  fetchSpy = vi.fn();
  vi.stubGlobal("fetch", fetchSpy);
});
afterEach(() => vi.unstubAllGlobals());

const json = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

function wrap() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
  return { qc, wrapper };
}

// openapi-fetch invokes `fetch(new Request(url, init))`, so the first arg is a Request. Read the
// method + url off it, and await its body text (the Request consumes the init body into its stream).
async function lastCall() {
  const arg = fetchSpy.mock.calls[fetchSpy.mock.calls.length - 1][0];
  if (arg instanceof Request) {
    const body = await arg.clone().text();
    return { url: arg.url, method: arg.method, body };
  }
  const [url, init] = fetchSpy.mock.calls[fetchSpy.mock.calls.length - 1] as [string, RequestInit];
  return { url: String(url), method: init?.method ?? "GET", body: init?.body != null ? String(init.body) : "" };
}

describe("useAdminOrgs", () => {
  it("GETs the orgs register and returns the list", async () => {
    fetchSpy.mockResolvedValue(json([{ name: "Tideline", id: "x", parent_team_id: null, is_root: true, created_at: "2026-01-01T00:00:00Z", member_count: 3 }]));
    const { wrapper } = wrap();
    const { result } = renderHook(() => useAdminOrgs(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect((await lastCall()).url).toContain("/admin/v1/orgs");
    expect(result.current.data?.[0].name).toBe("Tideline");
  });

  it("surfaces a structured ApiError on a failed fetch (so the page can render its error state)", async () => {
    fetchSpy.mockResolvedValue(json({ error: { code: "forbidden", message: "not staff" } }, 403));
    const { wrapper } = wrap();
    const { result } = renderHook(() => useAdminOrgs(), { wrapper });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error).toBeInstanceOf(ApiError);
    expect((result.current.error as ApiError).status).toBe(403);
  });
});

describe("useSetPlatformRoleMutation", () => {
  it("PATCHes the user's platform_role with the role in the body", async () => {
    fetchSpy.mockResolvedValue(json({ id: "u1" }));
    const { wrapper } = wrap();
    const { result } = renderHook(() => useSetPlatformRoleMutation(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync({ userId: "u1", platformRole: "alkera_admin" });
    });
    const { url, method, body } = await lastCall();
    expect(url).toContain("/admin/v1/users/u1/platform_role");
    expect(method).toBe("PATCH");
    expect(JSON.parse(body)).toEqual({ platform_role: "alkera_admin" });
  });

  it("sends platform_role: null to demote a user to a regular account", async () => {
    fetchSpy.mockResolvedValue(json({ id: "u1" }));
    const { wrapper } = wrap();
    const { result } = renderHook(() => useSetPlatformRoleMutation(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync({ userId: "u1", platformRole: null });
    });
    expect(JSON.parse((await lastCall()).body)).toEqual({ platform_role: null });
  });
});
