// @vitest-environment node
//
// The chat workspace stores the tabs a person left open, by kind. The kinds a
// build knows are registered here rather than switched on, so a tab kind added
// later needs no edit to the tab strip — and, more importantly, so a stored tab
// whose kind this build has never heard of is KEPT rather than dropped: a person
// on an older client must not silently lose the tabs a newer one opened.

import { describe, expect, it } from "vitest";

import {
  MAX_WORKSPACE_TABS,
  UNKNOWN_TAB_KIND_NOTICE,
  registerTabKind,
  registeredTabKinds,
  tabKindFor,
  type WorkspaceTab,
} from "@/pages/workspace/chat/workspace/tabKinds";

const Nothing = (): null => null;

const tab = (over: Partial<WorkspaceTab> = {}): WorkspaceTab => ({
  id: "t1",
  kind: "file",
  node_id: "1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f",
  name: "q3.csv",
  path: "q3.csv",
  params: {},
  ...over,
});

describe("tabKinds", () => {
  it("answers nothing for a kind that was never registered", () => {
    expect(tabKindFor("hologram")).toBeUndefined();
  });

  it("a registered kind is found by its own name and by nothing else", () => {
    registerTabKind({ kind: "hologram", label: () => "Hologram", Component: Nothing });
    expect(tabKindFor("hologram")?.kind).toBe("hologram");
    expect(tabKindFor("Hologram")).toBeUndefined();
    expect(tabKindFor("")).toBeUndefined();
  });

  it("registering the same kind twice replaces it", () => {
    // Hot reload re-imports a module, and a lane that moves a kind's renderer
    // should not have to unregister first. Last registration wins.
    registerTabKind({ kind: "twice", label: () => "first", Component: Nothing });
    registerTabKind({ kind: "twice", label: () => "second", Component: Nothing });
    expect(tabKindFor("twice")?.label(tab({ kind: "twice" }))).toBe("second");
    expect(registeredTabKinds().filter((k) => k.kind === "twice")).toHaveLength(1);
  });

  it("a kind names a tab from the tab, and from the item when it has one", () => {
    registerTabKind({
      kind: "named",
      label: (t, item) => item?.name ?? t.name,
      Component: Nothing,
    });
    const kind = tabKindFor("named");
    expect(kind?.label(tab({ kind: "named", name: "stored.csv" }))).toBe("stored.csv");
    expect(
      kind?.label(tab({ kind: "named", name: "stored.csv" }), { name: "renamed.csv" } as never),
    ).toBe("renamed.csv");
  });

  it("a kind may declare itself pinned or a singleton, and defaults to neither", () => {
    // The Files tab is both: it can never be closed, and opening it twice must
    // activate the one that is already there.
    registerTabKind({
      kind: "files",
      pinned: true,
      singleton: true,
      label: () => "Files",
      Component: Nothing,
    });
    registerTabKind({ kind: "plain", label: () => "Plain", Component: Nothing });
    expect(tabKindFor("files")).toMatchObject({ pinned: true, singleton: true });
    expect(tabKindFor("plain")?.pinned ?? false).toBe(false);
    expect(tabKindFor("plain")?.singleton ?? false).toBe(false);
  });

  it("the census lists what this build registered, in registration order", () => {
    const before = registeredTabKinds().map((k) => k.kind);
    registerTabKind({ kind: "last", label: () => "Last", Component: Nothing });
    expect(registeredTabKinds().map((k) => k.kind)).toEqual([...before, "last"]);
  });

  it("an unknown kind has a sentence to render, so the tab survives the reader", () => {
    // The alternative — dropping the tab — is a silent data loss the person
    // only notices later, on the client that could still open it.
    expect(UNKNOWN_TAB_KIND_NOTICE).toBe("This tab needs a newer version");
  });

  it("the ceiling on stored tabs is the one the server enforces", () => {
    expect(MAX_WORKSPACE_TABS).toBe(32);
  });
});
