import { describe, expect, it } from "vitest";

import { NAV, filterNav, hasChildren, shadowedLeaf, type NavGates, type NavLeaf } from "@/app/nav";

// filterNav gates groups, parents, and a parent's individual children -- the platform console (a
// gated group), its per-grade leaves, and the synthetic empty-parent case pin the behavior. These
// read the open sidebar alone; where an extension's leaves land is pinned by the product's
// composition test.

const gates = (over: Partial<NavGates>): NavGates => ({
  orgAdmin: false,
  teamAdmin: false,
  platformStaff: false,
  platformAdmin: false,
  selfHosted: false,
  ...over,
});

describe("filterNav", () => {
  it("drops the platform-staff console for a non-staff user", () => {
    const groups = filterNav(NAV, gates({ orgAdmin: true })).map((g) => g.group);
    expect(groups).not.toContain("Platform");
  });

  it("keeps the platform console for staff", () => {
    const groups = filterNav(NAV, gates({ platformStaff: true })).map((g) => g.group);
    expect(groups).toContain("Platform");
  });

  // The bans routes answer a support account a 403, so the doorway must not be offered
  // to one — the leaf is gated on the higher platform grade, not on "is staff".
  const adminLeaves = (g: NavGates): string[] => {
    const platform = filterNav(NAV, g).find((grp) => grp.group === "Platform");
    const admin = platform?.items.find((i) => hasChildren(i) && i.label === "Admin");
    return admin && hasChildren(admin) ? admin.children.map((c) => c.label) : [];
  };

  it("offers the bans register to a platform admin", () => {
    expect(adminLeaves(gates({ platformStaff: true, platformAdmin: true }))).toContain("Bans");
  });

  it("hides the bans register from support staff, who keep the rest of the console", () => {
    const leaves = adminLeaves(gates({ platformStaff: true }));
    expect(leaves).not.toContain("Bans");
    expect(leaves).toContain("Users");
  });

  // The cloud fleet console ships with the product; the open console runs local and SSH
  // machines only.
  it("offers no cloud machine register or offerings catalog", () => {
    const leaves = adminLeaves(gates({ platformStaff: true, platformAdmin: true }));
    expect(leaves).not.toContain("Machines");
    expect(leaves).not.toContain("Machine offerings");
  });

  it("drops a parent left with no visible children", () => {
    // A synthetic group whose parent's every child is org-admin: a member sees an empty parent,
    // which must be dropped rather than rendered as a childless disclosure.
    const synthetic = [
      {
        group: "Test",
        items: [
          {
            label: "Parent",
            icon: "overview" as const,
            children: [{ label: "Only", to: "/x", icon: "overview" as const, requires: "orgAdmin" as const }],
          },
        ],
      },
    ];
    expect(filterNav(synthetic, gates({}))).toEqual([]);
  });
});

// Files is a plain member destination in the Workspace group, one step below Chat -- a chat's
// attachments and saved results live in the tree it browses.
describe("the Files leaf", () => {
  const workspace = (nav: ReturnType<typeof filterNav>) => nav.find((g) => g.group === "Personal");

  it("sits directly under Chat for a plain member", () => {
    const items = workspace(filterNav(NAV, gates({})))?.items ?? [];
    const labels = items.filter((i) => !hasChildren(i)).map((i) => i.label);
    expect(labels.indexOf("Files")).toBe(labels.indexOf("Chat") + 1);
  });

  it("routes to /files with the drive glyph and no role gate", () => {
    const leaf = NAV.flatMap((g) => g.items)
      .filter((i): i is NavLeaf => !hasChildren(i))
      .find((i) => i.label === "Files");
    expect(leaf).toMatchObject({ to: "/files", icon: "drive" });
    expect(leaf?.requires).toBeUndefined();
    expect(leaf?.requiresSelfHosted).toBeUndefined();
  });

  it("survives every gate combination -- no role can lose it", () => {
    for (const over of [{}, { orgAdmin: true }, { platformStaff: true }, { orgAdmin: true, selfHosted: true }]) {
      const labels = (workspace(filterNav(NAV, gates(over)))?.items ?? []).map((i) => i.label);
      expect(labels).toContain("Files");
    }
  });
});



describe("the Machines leaf", () => {
  const orgLeaves = (over: Parameters<typeof gates>[0]): NavLeaf[] =>
    (filterNav(NAV, gates(over)).find((g) => g.group === "Organization")?.items ?? []).filter(
      (i): i is NavLeaf => !hasChildren(i),
    );

  it.each([
    ["a plain member", {}, false],
    ["a team admin", { teamAdmin: true }, true],
    ["an org admin, who is a team admin of the root", { orgAdmin: true, teamAdmin: true }, true],
  ] as const)("is offered to %s: %s", (_who, over, offered) => {
    expect(orgLeaves(over).some((l) => l.label === "Machines")).toBe(offered);
  });

  it("lights alone on its pages; Org settings stays lit on the rest of the settings area", () => {
    const leaves = orgLeaves({ orgAdmin: true, teamAdmin: true });
    const settings = leaves.find((l) => l.label === "Org settings")!;
    const machines = leaves.find((l) => l.label === "Machines")!;
    expect(shadowedLeaf(settings, "/settings/organization/machines", leaves)).toBe(true);
    expect(shadowedLeaf(settings, "/settings/organization/machines/m1", leaves)).toBe(true);
    expect(shadowedLeaf(settings, "/settings/organization/billing", leaves)).toBe(false);
    // A sibling whose path merely starts with the same letters is not under it.
    expect(shadowedLeaf(settings, "/settings/organization/machinesx", leaves)).toBe(false);
    expect(shadowedLeaf(machines, "/settings/organization/machines/m1", leaves)).toBe(false);
  });
});
