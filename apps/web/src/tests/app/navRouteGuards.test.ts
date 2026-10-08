import { describe, expect, it } from "vitest";

import appSource from "@/App.tsx?raw";
import { NAV, filterNav, hasChildren, type NavGates, type NavLeaf } from "@/app/nav";

// The sidebar hides a doorway its route guard would bounce the reader back from.
// The guard is the contract (App.tsx), so the paths the platform-admin guard wraps
// are read from the route table itself: a page moved behind the higher grade
// without its nav leaf following — or a leaf gated that its route no longer is —
// fails here, not in front of a support user.

/** The `path` of every route nested inside `<Route element={<RequirePlatformAdmin />}>`. */
function platformAdminPaths(source: string): Set<string> {
  const open = source.indexOf("<Route element={<RequirePlatformAdmin />}>");
  expect(open).toBeGreaterThan(-1);
  // Its children are self-closing routes, so the first closing tag after it is its own.
  const close = source.indexOf("</Route>", open);
  const block = source.slice(open, close);
  return new Set([...block.matchAll(/path="([^"]+)"/g)].map((m) => m[1]!));
}

function platformLeaves(): NavLeaf[] {
  const platform = NAV.find((g) => g.group === "Platform");
  return (platform?.items ?? []).flatMap((item) => (hasChildren(item) ? item.children : [item]));
}

const gates = (over: Partial<NavGates>): NavGates => ({
  orgAdmin: false,
  teamAdmin: false,
  platformStaff: false,
  platformAdmin: false,
  selfHosted: false,
  ...over,
});

function visibleAdminPaths(g: NavGates): string[] {
  const platform = filterNav(NAV, g).find((grp) => grp.group === "Platform");
  return (platform?.items ?? []).flatMap((i) => (hasChildren(i) ? i.children.map((c) => c.to) : [i.to]));
}

describe("platform nav gates follow the route guards", () => {
  const guarded = platformAdminPaths(appSource);

  it("reads the guarded routes, including the bans register and the audit log", () => {
    expect(guarded).toContain("/admin/bans");
    expect(guarded).toContain("/admin/audit");
  });

  it.each(platformLeaves().map((leaf) => [leaf.label, leaf] as const))(
    "%s is gated on the platform-admin grade exactly when its route is",
    (_label, leaf) => {
      expect(leaf.requires === "platformAdmin").toBe(guarded.has(leaf.to));
    },
  );

  it("a support account is offered no doorway its guard would bounce", () => {
    const offered = visibleAdminPaths(gates({ platformStaff: true }));
    expect(offered.length).toBeGreaterThan(0);
    expect(offered.filter((to) => guarded.has(to))).toEqual([]);
  });

  it("a platform admin is offered the bans register and the audit log", () => {
    const offered = visibleAdminPaths(gates({ platformStaff: true, platformAdmin: true }));
    expect(offered).toEqual(expect.arrayContaining(["/admin/bans", "/admin/audit"]));
  });
});
