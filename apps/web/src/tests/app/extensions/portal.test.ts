import { ExtensionError } from "@alkera/ui/extensions";
import { describe, expect, it } from "vitest";

import { placeAfter, type PortalNav } from "@/app/extensions/portal";
import { composeNav, type NavGroup } from "@/app/nav";

// Where an extension's contribution lands. Every portal list an extension joins (the
// sidebar, the org settings strip, the admin org tabs, a team's plates) is placed by
// anchor, so a contribution names what it follows and never an index.

const key = (s: { key: string }) => s.key;
const at = (key: string, after: string | null) => ({ item: { key }, after, label: key });

describe("placeAfter", () => {
  const base = [{ key: "a" }, { key: "b" }, { key: "c" }];

  it("puts a contribution right after its anchor", () => {
    expect(placeAfter(base, [at("x", "a")], key).map(key)).toEqual(["a", "x", "b", "c"]);
  });

  it("puts a contribution with no anchor first", () => {
    expect(placeAfter(base, [at("x", null)], key).map(key)).toEqual(["x", "a", "b", "c"]);
  });

  it("keeps registration order between two contributions to one anchor", () => {
    expect(placeAfter(base, [at("x", "a"), at("y", "a")], key).map(key)).toEqual(["a", "x", "y", "b", "c"]);
  });

  it("can anchor on an earlier contribution", () => {
    expect(placeAfter(base, [at("x", "c"), at("y", "x")], key).map(key)).toEqual(["a", "b", "c", "x", "y"]);
  });

  it("refuses an anchor that names nothing", () => {
    expect(() => placeAfter(base, [at("x", "nowhere")], key)).toThrow(ExtensionError);
  });

  it("leaves the base list untouched", () => {
    placeAfter(base, [at("x", "a")], key);
    expect(base.map(key)).toEqual(["a", "b", "c"]);
  });
});

describe("composeNav", () => {
  const base: NavGroup[] = [
    {
      group: "Personal",
      items: [
        { label: "Overview", to: "/", icon: "overview" },
        { label: "Chat", to: "/chat", icon: "chats" },
      ],
    },
    { group: "Organization", items: [{ label: "Teams", to: "/teams", icon: "teams" }] },
  ];
  const labels = (nav: NavGroup[], group: string) =>
    nav.find((g) => g.group === group)?.items.map((i) => i.label) ?? [];

  it("places a leaf in its group after the leaf it names", () => {
    const leaf: PortalNav = {
      key: "plug",
      kind: "leaf",
      group: "Personal",
      after: "/",
      leaf: { label: "Plug", to: "/plug", icon: "plug" },
    };
    const nav = composeNav(base, [leaf]);
    expect(labels(nav, "Personal")).toEqual(["Overview", "Plug", "Chat"]);
    expect(labels(nav, "Organization")).toEqual(["Teams"]);
  });

  it("places a group after the group it names", () => {
    const group: PortalNav = {
      key: "extra",
      kind: "group",
      after: "Personal",
      group: { group: "Extra", items: [{ label: "E", to: "/e", icon: "plug" }] },
    };
    expect(composeNav(base, [group]).map((g) => g.group)).toEqual(["Personal", "Extra", "Organization"]);
  });

  it("refuses a leaf for a group that does not exist", () => {
    const stray: PortalNav = {
      key: "stray",
      kind: "leaf",
      group: "Nowhere",
      after: null,
      leaf: { label: "S", to: "/s", icon: "plug" },
    };
    expect(() => composeNav(base, [stray])).toThrow(ExtensionError);
  });

  it("returns the base unchanged with nothing contributed", () => {
    expect(composeNav(base, [])).toEqual(base);
  });
});
