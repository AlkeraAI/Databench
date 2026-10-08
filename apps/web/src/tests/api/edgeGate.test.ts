// The one visit an edge sign-in gate needs, and when the app must not make it.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  apiIsCrossOrigin,
  bounceThroughEdgeGate,
  type EdgeGateDeps,
  GATE_RETURN_PATH,
  noteApiReachable,
} from "@/api/edgeGate";

const API = "https://api.staging.example";

function memoryStorage(kept = true): EdgeGateDeps["storage"] {
  const items = new Map<string, string>();
  return {
    get: (k) => items.get(k) ?? null,
    set: (k, v) => {
      items.set(k, v);
      return kept;
    },
    remove: (k) => void items.delete(k),
  };
}

function deps(overrides: Partial<EdgeGateDeps> = {}): EdgeGateDeps & { assign: ReturnType<typeof vi.fn> } {
  const assign = vi.fn();
  return {
    apiBaseUrl: API,
    location: {
      origin: "https://app.staging.example",
      pathname: "/workspace/chats/abc",
      search: "?tab=files",
      hash: "#top",
      assign,
    },
    storage: memoryStorage(),
    ...overrides,
    assign,
  };
}

const read = (method = "GET") => new Request(`${API}/api/v1/config`, { method });

describe("bounceThroughEdgeGate", () => {
  it("sends the browser through the API host once, back to where it was", () => {
    const d = deps();
    expect(bounceThroughEdgeGate(read(), d)).toBe(true);
    expect(d.assign).toHaveBeenCalledTimes(1);
    const target = new URL(d.assign.mock.calls[0]![0] as string);
    expect(target.origin).toBe(API);
    expect(target.pathname).toBe(GATE_RETURN_PATH);
    expect(target.searchParams.get("next")).toBe("/workspace/chats/abc?tab=files#top");
  });

  it("bounces at most once per page load, so a real outage stays visible", () => {
    const d = deps();
    expect(bounceThroughEdgeGate(read(), d)).toBe(true);
    expect(bounceThroughEdgeGate(read(), d)).toBe(false);
    expect(bounceThroughEdgeGate(read("HEAD"), d)).toBe(false);
    expect(d.assign).toHaveBeenCalledTimes(1);
  });

  it("bounces again after the API has answered in between (the gate's cookie lapsed)", () => {
    const d = deps();
    expect(bounceThroughEdgeGate(read(), d)).toBe(true);
    noteApiReachable(d);
    expect(bounceThroughEdgeGate(read(), d)).toBe(true);
    expect(d.assign).toHaveBeenCalledTimes(2);
  });

  it("never leaves the page for a write", () => {
    const d = deps();
    for (const method of ["POST", "PUT", "PATCH", "DELETE"]) {
      expect(bounceThroughEdgeGate(read(method), d)).toBe(false);
    }
    expect(d.assign).not.toHaveBeenCalled();
    expect(bounceThroughEdgeGate(read(), d)).toBe(true);
  });

  it("does nothing when the API is same-origin: the page itself came through any gate", () => {
    const d = deps({ apiBaseUrl: "https://app.staging.example" });
    expect(apiIsCrossOrigin(d)).toBe(false);
    expect(bounceThroughEdgeGate(read(), d)).toBe(false);
    expect(d.assign).not.toHaveBeenCalled();
  });

  it("does nothing when the browser would not keep the memory of the visit, rather than risk a loop", () => {
    const d = deps({ storage: memoryStorage(false) });
    expect(bounceThroughEdgeGate(read(), d)).toBe(false);
    expect(bounceThroughEdgeGate(read(), d)).toBe(false);
    expect(d.assign).not.toHaveBeenCalled();
  });

  it("treats an unparseable API base as same-origin", () => {
    const d = deps({ apiBaseUrl: "not a url" });
    expect(apiIsCrossOrigin(d)).toBe(false);
    expect(bounceThroughEdgeGate(read(), d)).toBe(false);
  });
});

describe("the API client", () => {
  const realLocation = window.location;
  let assign: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.resetModules();
    vi.stubEnv("VITE_API_BASE_URL", API);
    assign = vi.fn();
    Object.defineProperty(window, "location", {
      configurable: true,
      value: {
        origin: "http://localhost:3000",
        pathname: "/dashboard",
        search: "",
        hash: "",
        assign,
        href: "http://localhost:3000/dashboard",
      },
    });
    window.sessionStorage.clear();
  });

  afterEach(() => {
    Object.defineProperty(window, "location", { configurable: true, value: realLocation });
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it("bounces when a read fails before any response, and only once", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );
    const { api } = await import("@/api/client");
    await expect(api.GET("/api/v1/config")).rejects.toBeInstanceOf(TypeError);
    expect(assign).toHaveBeenCalledTimes(1);
    expect(assign.mock.calls[0]![0]).toBe(`${API}${GATE_RETURN_PATH}?next=%2Fdashboard`);
    await expect(api.GET("/api/v1/config")).rejects.toBeInstanceOf(TypeError);
    expect(assign).toHaveBeenCalledTimes(1);
  });

  it("does not bounce on an answered request, whatever its status", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("{}", { status: 503, headers: { "content-type": "application/json" } })),
    );
    const { api } = await import("@/api/client");
    const result = await api.GET("/api/v1/config");
    expect(result.response.status).toBe(503);
    expect(assign).not.toHaveBeenCalled();
  });
});
