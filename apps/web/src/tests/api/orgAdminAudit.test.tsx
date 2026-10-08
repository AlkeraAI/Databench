import type { ReactNode } from "react";
import { act } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { auditCsvUrl, useAuditVerify, useOrgAudit } from "@/api/orgAdminAudit";

// The audit hooks driven through a REAL QueryClient with only `fetch` (the network boundary)
// stubbed — pinning the wire contract the page can't see: which endpoint each hook hits, that
// active filters land as query params (and absent ones don't), and that verification runs only
// when asked. The CSV export is a plain link, so its URL builder is pinned here too.

const PAGE_WIRE = { events: [], total: 0, offset: 0, limit: 50 };

let fetchSpy: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchSpy = vi.fn();
  vi.stubGlobal("fetch", fetchSpy);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

const json = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

/** The stubbed request that hit a path (a Request, per openapi-fetch). */
function requestTo(path: string): { url: URL; method: string } {
  const req = fetchSpy.mock.calls.map((c) => c[0] as Request).find((r) => r.url.includes(path));
  if (!req) throw new Error(`no request to ${path}`);
  return { url: new URL(req.url), method: req.method };
}

describe("useOrgAudit", () => {
  it("carries active filters as query params", async () => {
    fetchSpy.mockResolvedValue(json(PAGE_WIRE));
    const { result } = renderHook(
      () => useOrgAudit(50, 25, { action: "agent.", actor_email: "ada@acme.com" }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    const { url, method } = requestTo("/api/v1/org/audit-events");
    expect(method).toBe("GET");
    expect(url.searchParams.get("offset")).toBe("50");
    expect(url.searchParams.get("limit")).toBe("25");
    expect(url.searchParams.get("action")).toBe("agent.");
    expect(url.searchParams.get("actor_email")).toBe("ada@acme.com");
  });

  it("omits unset filters from the wire entirely", async () => {
    fetchSpy.mockResolvedValue(json(PAGE_WIRE));
    const { result } = renderHook(() => useOrgAudit(0, 50, {}), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    const { url } = requestTo("/api/v1/org/audit-events");
    for (const key of ["action", "actor_email", "created_after", "created_before"]) {
      expect(url.searchParams.has(key)).toBe(false);
    }
  });
});

describe("useAuditVerify", () => {
  it("never fetches on mount — the walk runs only when triggered", async () => {
    fetchSpy.mockResolvedValue(json({ ok: true, checked: 3, head_hash: "abc" }));
    const { result } = renderHook(() => useAuditVerify(), { wrapper });
    expect(fetchSpy).not.toHaveBeenCalled();
    await act(async () => {
      await result.current.mutateAsync();
    });
    const { url, method } = requestTo("/api/v1/org/audit-events/verify");
    expect(method).toBe("GET");
    expect(url.pathname.endsWith("/verify")).toBe(true);
    await waitFor(() => expect(result.current.data?.ok).toBe(true));
    expect(result.current.data?.checked).toBe(3);
  });
});

describe("auditCsvUrl", () => {
  const base = auditCsvUrl();

  it("bare: the export path with no query string", () => {
    expect(base.endsWith("/api/v1/org/audit-events/export.csv")).toBe(true);
    expect(base).not.toContain("?");
  });

  it("active filters land as encoded query params on the same path", () => {
    const url = new URL(auditCsvUrl({ action: "agent.", created_after: "2026-06-01T00:00:00.000Z" }));
    expect(url.pathname).toBe(new URL(base).pathname);
    expect(url.searchParams.get("action")).toBe("agent.");
    expect(url.searchParams.get("created_after")).toBe("2026-06-01T00:00:00.000Z");
  });

  it("unset and empty filters are omitted, never stringified", () => {
    expect(auditCsvUrl({ action: undefined, actor_email: "" })).toBe(base);
  });

  it("a + in an email survives the round-trip", () => {
    const url = new URL(auditCsvUrl({ actor_email: "ada+audit@acme.com" }));
    expect(url.searchParams.get("actor_email")).toBe("ada+audit@acme.com");
  });
});
