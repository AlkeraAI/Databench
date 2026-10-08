import { describe, expect, it, vi } from "vitest";

import { ExtensionError, ExtensionPoint, installExtensions, installedExtensions } from "./extensions";

// The composition contract every open seam relies on: a point keeps registration order,
// refuses a second item under one key, and refuses any registration once something has
// read it (a reader that already rendered would never see the late item).

describe("ExtensionPoint", () => {
  it("returns items in registration order", () => {
    const point = new ExtensionPoint<{ key: string }>("test.order");
    point.register({ key: "b" });
    point.register({ key: "a" });
    expect(point.items().map((i) => i.key)).toEqual(["b", "a"]);
  });

  it("refuses a second item under a key already registered", () => {
    const point = new ExtensionPoint<{ key: string; n: number }>("test.dup");
    point.register({ key: "a", n: 1 });
    expect(() => point.register({ key: "a", n: 2 })).toThrow(ExtensionError);
    expect(point.items()).toEqual([{ key: "a", n: 1 }]);
  });

  it("freezes on the first read and refuses a late registration", () => {
    const point = new ExtensionPoint<{ key: string }>("test.freeze");
    expect(point.frozen).toBe(false);
    point.register({ key: "early" });
    expect(point.items()).toHaveLength(1);
    expect(point.frozen).toBe(true);
    expect(() => point.register({ key: "late" })).toThrow(/already read/);
    expect(point.items().map((i) => i.key)).toEqual(["early"]);
  });

  it("hands each reader its own copy, so a reader cannot add to the point", () => {
    const point = new ExtensionPoint<{ key: string }>("test.copy");
    point.register({ key: "a" });
    (point.items() as { key: string }[]).push({ key: "smuggled" });
    expect(point.items().map((i) => i.key)).toEqual(["a"]);
  });
});

describe("installExtensions", () => {
  it("installs each extension once, in order, and a repeat install is a no-op", () => {
    const calls: string[] = [];
    const first = { name: "test.install.first", install: vi.fn(() => calls.push("first")) };
    const second = { name: "test.install.second", install: vi.fn(() => calls.push("second")) };
    installExtensions([first, second]);
    installExtensions([first, second]);
    expect(calls).toEqual(["first", "second"]);
    expect(installedExtensions()).toEqual(expect.arrayContaining([first.name, second.name]));
  });

  it("refuses a different extension under a name already installed", () => {
    installExtensions([{ name: "test.install.taken", install: () => {} }]);
    const impostor = { name: "test.install.taken", install: vi.fn() };
    expect(() => installExtensions([impostor])).toThrow(ExtensionError);
    expect(impostor.install).not.toHaveBeenCalled();
  });
});
