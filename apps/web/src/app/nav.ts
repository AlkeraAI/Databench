import type { IconName } from "./icons";
import { ExtensionError } from "@alkera/ui/extensions";
import { PORTAL_NAV, placeAfter, type PortalNav } from "./extensions/portal";

/** The portal's primary navigation — grouped destinations, each a route + glyph. Source of truth
 *  for the sidebar and a lookup the topbar names pages from. A `requires` gate hides a doorway the
 *  user can't pass (an org-admin or platform link); the route guards are the real enforcement.
 *
 *  `NAV` is the open portal's own sidebar; an installed extension adds its leaves and groups
 *  through `PORTAL_NAV`, and `portalNav()` is what the shell renders. */

export type NavGate = "orgAdmin" | "teamAdmin" | "platformStaff" | "platformAdmin";

export interface NavLeaf {
  label: string;
  to: string;
  icon: IconName;
  requires?: NavGate;
  /** Additionally hide unless this is a self-hosted deployment (the Deployment page). Composes
   *  with `requires` — both must pass. */
  requiresSelfHosted?: boolean;
  /** Match the route exactly (NavLink `end`) — for an index leaf like Admin → Overview (`/admin`)
   *  whose path is a prefix of its siblings. Defaults on for "/". */
  end?: boolean;
}
export interface NavParent {
  label: string;
  icon: IconName;
  children: NavLeaf[];
  requires?: NavGate;
}
export type NavNode = NavLeaf | NavParent;
export interface NavGroup {
  group: string;
  /** Whether the sidebar shows `group` as a heading over the items. The first
   *  group is everyone's own pages and reads plainly without one. */
  titled?: boolean;
  items: NavNode[];
  requires?: NavGate;
}

export const hasChildren = (n: NavNode): n is NavParent => "children" in n;

export const NAV: NavGroup[] = [
  {
    group: "Personal",
    titled: false,
    items: [
      { label: "Overview", to: "/", icon: "overview" },
      { label: "Chat", to: "/chat", icon: "chats" },
      // Files sits directly under Chat: a chat's attachments, results and boards all live in
      // the same tree the Files page browses, so the two are one step apart.
      { label: "Files", to: "/files", icon: "drive" },
      // No Templates leaf either: a chat template is a folder in the drive, so
      // Files IS the index of them and a second listing of the same rows only
      // splits the story. The per-object pages stay routed — Files opens one by
      // the node's object facet (`object.web_url`), which is `/templates/:id`
      // for a template and `/objects/:objectId` for what is left of the older
      // saved objects.
    ],
  },
  {
    group: "Organization",
    items: [
      { label: "Teams", to: "/teams", icon: "teams" },
      // ONE doorway to every org-wide control: knowledge sync's master switch, org billing,
      // single sign-on and the audit log are tabs of this page, so the sidebar carries one entry
      // instead of four and the leaf lights on every /settings/organization/* tab (no `end`).
      { label: "Org settings", to: "/settings/organization", icon: "settings", requires: "orgAdmin" },
      // Machines: an org admin manages every one, a team admin the ones their teams own. The
      // path sits under the settings area, so the leaf that names it more exactly is the one lit.
      { label: "Machines", to: "/settings/organization/machines", icon: "monitor", requires: "teamAdmin" },
      // The reader's own Preferences are NOT an Organization entry — they are
      // nobody's but the reader's, and they live in the account menu beside
      // Profile (see AppLayout's AccountMenu).
      // Org-admin surfaces — hidden for a plain member, gated server- and client-side. (Members live
      // on Teams; plan/billing lives on Manage plan.)
      // Deployment health + provider keys — a self-hosted install's ops surface (org admin only).
      {
        label: "Deployment",
        to: "/org/deployment",
        icon: "monitor",
        requires: "orgAdmin",
        requiresSelfHosted: true,
      },
    ],
  },
  {
    // Platform-staff console. Each register is a subtab under Admin in the sidebar — the disclosure
    // carries the descent rail (and, collapsed, keeps the sub-icons on the main icon axis).
    group: "Platform",
    requires: "platformStaff",
    items: [
      {
        label: "Admin",
        icon: "shieldCheck",
        children: [
          { label: "Ops", to: "/admin/ops", icon: "monitor" },
          { label: "Organizations", to: "/admin/orgs", icon: "building" },
          { label: "Users", to: "/admin/users", icon: "users" },
          // Account + domain bans are an escalation-class write: the routes answer a
          // support account 403, so the doorway is hidden from one rather than offered.
          { label: "Bans", to: "/admin/bans", icon: "lock", requires: "platformAdmin" },
          { label: "Audit log", to: "/admin/audit", icon: "note", requires: "platformAdmin" },
        ],
      },
    ],
  },
];

/** The sidebar with every installed extension's leaves and groups in place. Reads (and so
 *  freezes) `PORTAL_NAV`: call it while rendering, never at module scope. */
export function portalNav(): NavGroup[] {
  return composeNav(NAV, PORTAL_NAV.items());
}

/** `base` with each contribution inserted after its anchor, in registration order. */
export function composeNav(base: readonly NavGroup[], contributions: readonly PortalNav[]): NavGroup[] {
  const groups = placeAfter(
    base,
    contributions.flatMap((c) => (c.kind === "group" ? [{ item: c.group, after: c.after, label: c.key }] : [])),
    (group) => group.group,
  );
  for (const c of contributions) {
    if (c.kind === "leaf" && !groups.some((group) => group.group === c.group)) {
      throw new ExtensionError(`${c.key} joins the ${c.group} group, and there is no such group`);
    }
    const parents = groups.flatMap((group) => group.items.filter(hasChildren));
    if (c.kind === "child" && !parents.some((parent) => parent.label === c.parent)) {
      throw new ExtensionError(`${c.key} joins the ${c.parent} entry, and there is no such entry`);
    }
  }
  return groups.map((group) => {
    const leaves = contributions.flatMap((c) =>
      c.kind === "leaf" && c.group === group.group ? [{ item: c.leaf as NavNode, after: c.after, label: c.key }] : [],
    );
    const items = leaves.length === 0 ? group.items : placeAfter(group.items, leaves, navKey);
    return { ...group, items: items.map((item) => withChildren(item, contributions)) };
  });
}

/** A parent with every contributed child placed after its anchor. */
function withChildren(node: NavNode, contributions: readonly PortalNav[]): NavNode {
  if (!hasChildren(node)) return node;
  const children = contributions.flatMap((c) =>
    c.kind === "child" && c.parent === node.label ? [{ item: c.leaf, after: c.after, label: c.key }] : [],
  );
  return children.length === 0 ? node : { ...node, children: placeAfter(node.children, children, (leaf) => leaf.to) };
}

const navKey = (node: NavNode): string => (hasChildren(node) ? node.label : node.to);

export interface NavGates {
  orgAdmin: boolean;
  /** An org admin, or an admin of any team: who manages machines. */
  teamAdmin: boolean;
  platformStaff: boolean;
  /** The higher platform grade (alkera_admin). Support staff reach the console but
   *  not the surfaces whose endpoints answer them a 403. */
  platformAdmin: boolean;
  selfHosted: boolean;
}

const canSee = (gates: NavGates, requires?: NavGate): boolean => !requires || gates[requires];

/** Whether a leaf clears its role gate AND (if flagged) the self-hosted gate. */
const leafVisible = (gates: NavGates, leaf: NavLeaf): boolean =>
  canSee(gates, leaf.requires) && (!leaf.requiresSelfHosted || gates.selfHosted);

/** The nav a given role may see: drop any group, parent, or leaf whose gate the user can't pass,
 *  filter a parent's children by their own gates, then drop a parent (or group) left empty. UX
 *  only — the route guards still enforce real access. */
export function filterNav(nav: NavGroup[], gates: NavGates): NavGroup[] {
  return nav
    .filter((g) => canSee(gates, g.requires))
    .map((g) => ({
      ...g,
      items: g.items
        .map((item) =>
          hasChildren(item)
            ? { ...item, children: item.children.filter((child) => leafVisible(gates, child)) }
            : item,
        )
        .filter((item) =>
          hasChildren(item)
            ? canSee(gates, item.requires) && item.children.length > 0
            : leafVisible(gates, item),
        ),
    }))
    .filter((g) => g.items.length > 0);
}

/** Whether a more exact leaf than `leaf` claims the path: Org settings
 *  (`/settings/organization`) stays unlit on the Machines page under it, so the
 *  rail lights one leaf for one page. */
export function shadowedLeaf(leaf: NavLeaf, pathname: string, leaves: readonly NavLeaf[]): boolean {
  return leaves.some(
    (other) =>
      other.to !== leaf.to &&
      other.to.startsWith(`${leaf.to}/`) &&
      (pathname === other.to || pathname.startsWith(`${other.to}/`)),
  );
}
