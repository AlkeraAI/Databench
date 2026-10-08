import { afterEach, describe, expect, it } from "vitest";

import { accountKey, clearStorageMirror, safeLocalStorage, safeSessionStorage } from "./storage";

type StoreName = "localStorage" | "sessionStorage";

const real: Record<StoreName, PropertyDescriptor | undefined> = {
  localStorage: Object.getOwnPropertyDescriptor(globalThis, "localStorage"),
  sessionStorage: Object.getOwnPropertyDescriptor(globalThis, "sessionStorage"),
};

/** Make the whole store unreachable the way a blocked context does: naming it
 *  throws, before any key is read. */
function blockStore(name: StoreName): void {
  Object.defineProperty(globalThis, name, {
    configurable: true,
    get() {
      throw new DOMException("The operation is insecure.", "SecurityError");
    },
  });
}

/** Swap in a store whose named methods throw — a quota that has filled, or a
 *  browser that turns hostile mid-session. */
function breakMethods(name: StoreName, methods: readonly ("getItem" | "setItem")[]): void {
  const underlying = real[name]?.get?.call(globalThis) ?? real[name]?.value;
  const broken = Object.create(underlying) as Record<string, unknown>;
  for (const method of methods) {
    broken[method] = () => {
      throw new DOMException("The quota has been exceeded.", "QuotaExceededError");
    };
  }
  Object.defineProperty(globalThis, name, { configurable: true, value: broken });
}

function restore(): void {
  for (const name of ["localStorage", "sessionStorage"] as const) {
    const descriptor = real[name];
    if (descriptor) Object.defineProperty(globalThis, name, descriptor);
  }
}

afterEach(() => {
  restore();
  clearStorageMirror();
  globalThis.localStorage.clear();
  globalThis.sessionStorage.clear();
});

describe("safeLocalStorage on a browser that works", () => {
  it("round-trips a string and forgets it on remove", () => {
    const store = safeLocalStorage();
    expect(store.set("alkera.test.k", "v")).toBe(true);
    expect(store.get("alkera.test.k")).toBe("v");
    expect(store.has("alkera.test.k")).toBe(true);
    expect(store.keys()).toContain("alkera.test.k");
    store.remove("alkera.test.k");
    expect(store.get("alkera.test.k")).toBeNull();
    expect(store.has("alkera.test.k")).toBe(false);
    expect(store.keys()).not.toContain("alkera.test.k");
  });

  it("round-trips JSON and reads an unparsable value as nothing stored", () => {
    const store = safeLocalStorage();
    expect(store.writeJson("alkera.test.j", { a: [1, 2] })).toBe(true);
    expect(store.readJson("alkera.test.j")).toEqual({ a: [1, 2] });

    store.set("alkera.test.j", "{not json");
    expect(store.readJson("alkera.test.j")).toBeNull();
    expect(store.readJson("alkera.test.never-written")).toBeNull();
  });

  it("refuses a value JSON cannot express without writing anything", () => {
    const store = safeLocalStorage();
    const cyclic: Record<string, unknown> = {};
    cyclic.self = cyclic;
    expect(store.writeJson("alkera.test.cycle", cyclic)).toBe(false);
    expect(store.get("alkera.test.cycle")).toBeNull();
    // `undefined` has no JSON encoding at all; nothing is stored for it either.
    expect(store.writeJson("alkera.test.undef", undefined)).toBe(false);
    expect(store.get("alkera.test.undef")).toBeNull();
  });
});

describe("safeLocalStorage where the browser refuses the store outright", () => {
  it("reads as nothing stored instead of throwing", () => {
    blockStore("localStorage");
    const store = safeLocalStorage();
    expect(() => store.get("alkera.test.blocked")).not.toThrow();
    expect(store.get("alkera.test.blocked")).toBeNull();
    expect(store.readJson("alkera.test.blocked")).toBeNull();
    expect(() => store.remove("alkera.test.blocked")).not.toThrow();
    expect(() => store.keys()).not.toThrow();
    expect(store.has("alkera.test.blocked")).toBe(false);
  });

  it("keeps the value for this page session so the feature still works", () => {
    blockStore("localStorage");
    const store = safeLocalStorage();
    expect(store.set("alkera.test.session-only", "grid")).toBe(false);
    expect(store.get("alkera.test.session-only")).toBe("grid");
    expect(store.readJson("alkera.test.session-only")).toBeNull();
    expect(store.keys()).toContain("alkera.test.session-only");

    store.remove("alkera.test.session-only");
    expect(store.get("alkera.test.session-only")).toBeNull();
  });

  it("round-trips JSON through memory too", () => {
    blockStore("localStorage");
    const store = safeLocalStorage();
    expect(store.writeJson("alkera.test.json-only", { view: "grid" })).toBe(false);
    expect(store.readJson("alkera.test.json-only")).toEqual({ view: "grid" });
  });
});

describe("safeLocalStorage where the browser refuses one write", () => {
  it("holds a quota-refused value in memory rather than reading back the old one", () => {
    const store = safeLocalStorage();
    store.set("alkera.test.quota", "old");

    breakMethods("localStorage", ["setItem"]);
    expect(store.set("alkera.test.quota", "new")).toBe(false);
    // The browser still holds "old"; the reader is looking at "new", and that
    // is what has to come back.
    expect(store.get("alkera.test.quota")).toBe("new");

    restore();
    expect(store.set("alkera.test.quota", "newer")).toBe(true);
    // Once the browser takes it the memory copy is dropped rather than
    // shadowing every later read with a value the store no longer holds.
    expect(store.get("alkera.test.quota")).toBe("newer");
    expect(globalThis.localStorage.getItem("alkera.test.quota")).toBe("newer");
  });

  it("reads as nothing stored when the read itself throws", () => {
    const store = safeLocalStorage();
    store.set("alkera.test.unreadable", "v");
    breakMethods("localStorage", ["getItem"]);
    expect(store.get("alkera.test.unreadable")).toBeNull();
    expect(store.readJson("alkera.test.unreadable")).toBeNull();
  });
});

describe("safeSessionStorage", () => {
  it("is a separate store from the long-lived one", () => {
    safeLocalStorage().set("alkera.test.kind", "local");
    safeSessionStorage().set("alkera.test.kind", "session");
    expect(safeLocalStorage().get("alkera.test.kind")).toBe("local");
    expect(safeSessionStorage().get("alkera.test.kind")).toBe("session");
  });

  it("survives a blocked store the same way, without touching the other one", () => {
    safeLocalStorage().set("alkera.test.other", "kept");
    blockStore("sessionStorage");
    expect(safeSessionStorage().set("alkera.test.s", "v")).toBe(false);
    expect(safeSessionStorage().get("alkera.test.s")).toBe("v");
    expect(safeLocalStorage().get("alkera.test.other")).toBe("kept");
  });
});

describe("accountKey", () => {
  it("names the family, the person and the org", () => {
    expect(accountKey("u1", "o1", "chat.last")).toBe("alkera.chat.last:u1:o1");
  });

  it.each([
    ["another org", ["u1", "o2", "chat.last"]],
    ["another person", ["u2", "o1", "chat.last"]],
    ["another family", ["u1", "o1", "chat.layout"]],
    ["no org", ["u1", "", "chat.last"]],
  ] as const)("keys %s apart", (_case, [user, org, name]) => {
    expect(accountKey(user, org, name)).not.toBe(accountKey("u1", "o1", "chat.last"));
  });

  it("keeps a value written in one org out of another", () => {
    const store = safeLocalStorage();
    store.set(accountKey("u1", "o1", "chat.last"), "c1");

    expect(store.get(accountKey("u1", "o2", "chat.last"))).toBeNull();
    expect(store.get(accountKey("u1", "", "chat.last"))).toBeNull();
    expect(store.get(accountKey("u1", "o1", "chat.last"))).toBe("c1");
  });
});
