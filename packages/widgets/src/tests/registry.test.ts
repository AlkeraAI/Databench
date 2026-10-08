// `window.__alkRegister`: the one name the frame's scripts share. Each test
// loads the bundle's modules afresh against a clean window.
import { beforeEach, describe, expect, it, vi } from "vitest";

type Seen = [string, string, Record<string, unknown>][];

const g = globalThis as unknown as Record<string, unknown>;

beforeEach(() => {
  vi.resetModules();
  delete g.__alkRegister;
});

describe("the registry", () => {
  it("chains to a function the frame host defined first, so both see each call", async () => {
    const seen: Seen = [];
    g.__alkRegister = (name: string, version: string, exports: Record<string, unknown>) => seen.push([name, version, exports]);
    const registry = await import("../registry");
    registry.register("lib", "1.2.0", { x: 1 });
    expect(seen).toEqual([["lib", "1.2.0", { x: 1 }]]);
    expect(registry.registration("lib")).toEqual({ name: "lib", version: "1.2.0", exports: { x: 1 } });
  });

  it("installs once: a second copy of the module shares the same registrations", async () => {
    const first = await import("../registry");
    first.register("lib", "1", { a: 1 });
    vi.resetModules();
    const second = await import("../registry");
    expect(second.registration("lib")?.exports).toEqual({ a: 1 });
    second.register("other", "1", {});
    expect(first.registration("other")).toBeDefined();
  });

  it("wakes a wait when the name registers, and only that name", async () => {
    const registry = await import("../registry");
    const wait = registry.whenRegistered("wanted", 1_000);
    registry.register("unrelated", "1", { v: 0 });
    (g.__alkRegister as (n: string, v: string, e: object) => void)("wanted", "3", { v: 3 });
    await expect(wait).resolves.toEqual({ name: "wanted", version: "3", exports: { v: 3 } });
    await expect(registry.whenRegistered("never", 10)).rejects.toThrow("never did not register");
  });
});

describe("the bundle entry", () => {
  it("registers as alkera-widgets with its version and places nothing else on window", async () => {
    let api: Record<string, unknown> | null = null;
    let version = "";
    g.__alkRegister = (name: string, v: string, exports: Record<string, unknown>) => {
      if (name === "alkera-widgets") {
        api = exports;
        version = v;
      }
    };
    const before = new Set(Object.keys(g));
    await import("../frame-entry");
    expect(version).toBe("0.0.0-test");
    expect(typeof (api as Record<string, unknown> | null)?.start).toBe("function");
    expect(Object.keys(g).filter((k) => !before.has(k))).toEqual([]);
  });
});
