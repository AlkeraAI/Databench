import type { ReactNode } from "react";
import { QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useAdminUsers } from "@/api/admin/admin";
import {
  useBanDomain,
  useBanUser,
  useDomainBans,
  useLiftDomainBan,
  useLiftUserBan,
  useUserBans,
} from "@/api/admin/bans";
import { ApiError } from "@/api/errors";
import { createQueryClient } from "@/api/queryClient";

// The ban hooks driven through a REAL client (built by createQueryClient, so the shared
// MutationCache policy is live) with only `fetch` stubbed. What these pin is the wire
// contract a page can't see: the exact request each mutation sends, that a domain is
// sent EXACTLY as typed (normalization belongs to the server, and a client that
// "helpfully" lower-cased it would hide a server-side bug), that the server's refusal
// arrives as a structured ApiError carrying its own sentence, and that a ban refreshes
// the USERS register too — the `banned` column the list greys a row on.

let fetchSpy: ReturnType<typeof vi.fn>;
beforeEach(() => {
  fetchSpy = vi.fn();
  vi.stubGlobal("fetch", fetchSpy);
});
afterEach(() => vi.unstubAllGlobals());

const json = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

function wrap() {
  const qc = createQueryClient({ retry: false });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
  return { qc, wrapper };
}

/** openapi-fetch calls `fetch(new Request(url, init))`, so the one argument is a Request. */
async function lastCall() {
  const arg = fetchSpy.mock.calls[fetchSpy.mock.calls.length - 1][0] as Request;
  return { url: arg.url, method: arg.method, body: await arg.clone().text() };
}

const requestedUrls = (): string[] =>
  fetchSpy.mock.calls.map((c) => (c[0] as Request).url);

const USER_BAN = {
  id: "b-1",
  user_id: "u-1",
  user_email: "spam@farm.test",
  user_display_name: "Spam Farm",
  reason: "Account farming",
  created_at: "2026-09-01T10:00:00Z",
  created_by_id: "a-1",
  created_by_email: "ops@example.com",
  lifted_at: null,
  lifted_by_id: null,
  lifted_by_email: null,
  active: true,
};

const DOMAIN_BAN = {
  id: "d-1",
  domain: "farm.test",
  reason: "Disposable",
  created_at: "2026-09-01T10:00:00Z",
  created_by_id: "a-1",
  created_by_email: "ops@example.com",
  lifted_at: null,
  lifted_by_id: null,
  lifted_by_email: null,
  active: true,
};

describe("the ban registers", () => {
  it("reads the user bans off /admin/v1/bans/users", async () => {
    fetchSpy.mockResolvedValue(json([USER_BAN]));
    const { wrapper } = wrap();
    const { result } = renderHook(() => useUserBans(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect((await lastCall()).url).toContain("/admin/v1/bans/users");
    expect(result.current.data?.[0].user_email).toBe("spam@farm.test");
  });

  it("reads the domain bans off /admin/v1/bans/domains", async () => {
    fetchSpy.mockResolvedValue(json([DOMAIN_BAN]));
    const { wrapper } = wrap();
    const { result } = renderHook(() => useDomainBans(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect((await lastCall()).url).toContain("/admin/v1/bans/domains");
    expect(result.current.data?.[0].domain).toBe("farm.test");
  });
});

describe("useBanUser", () => {
  it("POSTs the user id and reason the caller supplied", async () => {
    fetchSpy.mockResolvedValue(json(USER_BAN, 201));
    const { wrapper } = wrap();
    const { result } = renderHook(() => useBanUser(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync({ userId: "u-1", reason: "Account farming" });
    });
    const { url, method, body } = await lastCall();
    expect(url).toContain("/admin/v1/bans/users");
    expect(method).toBe("POST");
    expect(JSON.parse(body)).toEqual({ user_id: "u-1", reason: "Account farming" });
  });

  it("sends an empty reason rather than omitting the field when none was given", async () => {
    fetchSpy.mockResolvedValue(json(USER_BAN, 201));
    const { wrapper } = wrap();
    const { result } = renderHook(() => useBanUser(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync({ userId: "u-1" });
    });
    expect(JSON.parse((await lastCall()).body)).toEqual({ user_id: "u-1", reason: "" });
  });

  it("surfaces the server's own refusal (409 already banned) as an ApiError", async () => {
    fetchSpy.mockResolvedValue(
      json({ error: { code: "conflict", message: "This account is already banned." } }, 409),
    );
    const { wrapper } = wrap();
    const { result } = renderHook(() => useBanUser(), { wrapper });
    let thrown: unknown;
    await act(async () => {
      thrown = await result.current.mutateAsync({ userId: "u-1" }).catch((e: unknown) => e);
    });
    expect(thrown).toBeInstanceOf(ApiError);
    expect((thrown as ApiError).status).toBe(409);
    expect((thrown as ApiError).message).toBe("This account is already banned.");
  });

  it("surfaces the 422 ban_refused message for a self-ban or a staff account", async () => {
    fetchSpy.mockResolvedValue(
      json({ error: { code: "ban_refused", message: "A platform staff account cannot be banned." } }, 422),
    );
    const { wrapper } = wrap();
    const { result } = renderHook(() => useBanUser(), { wrapper });
    let thrown: unknown;
    await act(async () => {
      thrown = await result.current.mutateAsync({ userId: "u-1" }).catch((e: unknown) => e);
    });
    expect((thrown as ApiError).code).toBe("ban_refused");
    expect((thrown as ApiError).message).toBe("A platform staff account cannot be banned.");
  });

  it("refreshes the users register too, so the list's banned column re-reads", async () => {
    fetchSpy.mockImplementation((req: Request) =>
      Promise.resolve(req.url.includes("/admin/v1/users") ? json([]) : json(USER_BAN, 201)),
    );
    const { wrapper } = wrap();
    const { result } = renderHook(
      () => ({ users: useAdminUsers(), ban: useBanUser() }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.users.isSuccess).toBe(true));
    const before = requestedUrls().filter((u) => u.includes("/admin/v1/users")).length;
    await act(async () => {
      await result.current.ban.mutateAsync({ userId: "u-1" });
    });
    await waitFor(() =>
      expect(requestedUrls().filter((u) => u.includes("/admin/v1/users")).length).toBeGreaterThan(before),
    );
  });
});

describe("useLiftUserBan", () => {
  it("DELETEs the ban at the user's path", async () => {
    fetchSpy.mockResolvedValue(new Response(null, { status: 204 }));
    const { wrapper } = wrap();
    const { result } = renderHook(() => useLiftUserBan(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync("u-1");
    });
    const { url, method } = await lastCall();
    expect(url).toContain("/admin/v1/bans/users/u-1");
    expect(method).toBe("DELETE");
  });
});

describe("useBanDomain", () => {
  it("POSTs the domain EXACTLY as typed — the server owns normalization", async () => {
    fetchSpy.mockResolvedValue(json(DOMAIN_BAN, 201));
    const { wrapper } = wrap();
    const { result } = renderHook(() => useBanDomain(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync({ domain: " @Farm.TEST ", reason: "Disposable" });
    });
    const { url, method, body } = await lastCall();
    expect(url).toContain("/admin/v1/bans/domains");
    expect(method).toBe("POST");
    expect(JSON.parse(body)).toEqual({ domain: " @Farm.TEST ", reason: "Disposable" });
  });

  it("surfaces the server's validation refusal for something that is not a domain", async () => {
    fetchSpy.mockResolvedValue(
      json(
        {
          error: {
            code: "validation_error",
            message: "The request failed validation.",
            details: { errors: [{ loc: ["body", "domain"], msg: "Value error, Enter a bare domain, e.g. acme.com" }] },
          },
        },
        422,
      ),
    );
    const { wrapper } = wrap();
    const { result } = renderHook(() => useBanDomain(), { wrapper });
    let thrown: unknown;
    await act(async () => {
      thrown = await result.current.mutateAsync({ domain: "not a domain" }).catch((e: unknown) => e);
    });
    // The field-level reason wins over the envelope's placeholder sentence.
    expect((thrown as ApiError).message).toBe("Enter a bare domain, e.g. acme.com");
  });

  it("refreshes the users register too (a domain ban flips accounts at that domain)", async () => {
    fetchSpy.mockImplementation((req: Request) =>
      Promise.resolve(req.url.includes("/admin/v1/users") ? json([]) : json(DOMAIN_BAN, 201)),
    );
    const { wrapper } = wrap();
    const { result } = renderHook(
      () => ({ users: useAdminUsers(), ban: useBanDomain() }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.users.isSuccess).toBe(true));
    const before = requestedUrls().filter((u) => u.includes("/admin/v1/users")).length;
    await act(async () => {
      await result.current.ban.mutateAsync({ domain: "farm.test" });
    });
    await waitFor(() =>
      expect(requestedUrls().filter((u) => u.includes("/admin/v1/users")).length).toBeGreaterThan(before),
    );
  });
});

describe("useLiftDomainBan", () => {
  it("DELETEs the ban at the domain's path", async () => {
    fetchSpy.mockResolvedValue(new Response(null, { status: 204 }));
    const { wrapper } = wrap();
    const { result } = renderHook(() => useLiftDomainBan(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync("farm.test");
    });
    const { url, method } = await lastCall();
    expect(url).toContain("/admin/v1/bans/domains/farm.test");
    expect(method).toBe("DELETE");
  });
});
