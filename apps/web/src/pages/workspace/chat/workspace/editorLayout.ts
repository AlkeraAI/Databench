// How the editor groups beside a chat are arranged.
//
// A workspace is split the way an editor splits: a group sits beside another
// (a row) or above another (a column), and either half can be split again the
// other way. That is a tree, and it is kept as one: a leaf names a group, an
// inner node lays its children out along one axis with a share of the space
// each. Everything here is pure (a layout in, a layout out), so every rule
// about splitting, collapsing and neighbours is reachable from a plain test,
// and the store only decides WHEN to apply them.
//
// The tree is also what is stored, so it is read defensively: a stored layout
// is data another build wrote, and anything this build cannot make sense of
// reads as "no layout", which the store answers by laying the groups the tabs
// name out in one row.

/** How many groups a workspace may hold. Past four the pane beside a chat is a
 *  grid of slivers nobody can read. */
export const MAX_GROUPS = 4;

/** The smallest share of its split a group may be dragged down to. */
export const MIN_SHARE = 0.12;

/** The group every tab belongs to when nothing says otherwise: the one a
 *  document written before groups existed is read into. */
export const DEFAULT_GROUP_ID = "main";

export type Axis = "row" | "column";

/** Where a new group goes relative to the one it is split from. */
export type Side = "left" | "right" | "up" | "down";

export type LayoutNode = LayoutLeaf | LayoutSplit;

export interface LayoutLeaf {
  kind: "group";
  id: string;
}

export interface LayoutSplit {
  kind: "split";
  axis: Axis;
  children: LayoutNode[];
  /** One share per child, summing to 1. */
  sizes: number[];
}

/** The path from the root to a node: the child index taken at each split. */
export type LayoutPath = readonly number[];

export function leaf(id: string): LayoutLeaf {
  return { kind: "group", id };
}

/** The groups in reading order: left to right, top to bottom. This is the
 *  order "group 1..4" means. */
export function groupOrder(node: LayoutNode): string[] {
  if (node.kind === "group") return [node.id];
  return node.children.flatMap(groupOrder);
}

function axisOf(side: Side): Axis {
  return side === "left" || side === "right" ? "row" : "column";
}

function after(side: Side): boolean {
  return side === "right" || side === "down";
}

/** Shares that sum to 1, each at least the floor, from whatever was given. A
 *  share list of the wrong length, or one with a non-number in it, is equal
 *  shares: a stored layout is never trusted to add up. */
export function normalShares(sizes: readonly unknown[], count: number): number[] {
  if (count <= 0) return [];
  const usable =
    sizes.length === count &&
    sizes.every((size) => typeof size === "number" && Number.isFinite(size) && size > 0);
  if (!usable) return Array.from({ length: count }, () => 1 / count);
  const numbers = sizes as number[];
  const total = numbers.reduce((sum, size) => sum + size, 0);
  const floor = Math.min(MIN_SHARE, 1 / count);
  let shares = numbers.map((size) => size / total);
  // Shares under the floor are raised to it and the rest give up the space in
  // proportion; raising one can push another under, so it settles in passes.
  const floored = shares.map(() => false);
  for (let pass = 0; pass < count; pass += 1) {
    const raised = shares.map((share, index) => !floored[index] && share < floor - 1e-12);
    if (!raised.some(Boolean)) break;
    raised.forEach((low, index) => {
      if (low) floored[index] = true;
    });
    const lows = floored.filter(Boolean).length;
    const freeSum = shares.reduce((sum, share, index) => (floored[index] ? sum : sum + share), 0);
    const room = 1 - lows * floor;
    shares = shares.map((share, index) => (floored[index] ? floor : (share / freeSum) * room));
  }
  return shares;
}

/** A split whose children are tidied: a split with one child IS that child, and
 *  a child laid out along the same axis as its parent is folded into it, so the
 *  tree never holds a row inside a row. */
function tidy(node: LayoutNode): LayoutNode {
  if (node.kind === "group") return node;
  const children: LayoutNode[] = [];
  const sizes: number[] = [];
  node.children.forEach((raw, index) => {
    const child = tidy(raw);
    const share = node.sizes[index] ?? 1 / node.children.length;
    if (child.kind === "split" && child.axis === node.axis) {
      child.children.forEach((grandchild, inner) => {
        children.push(grandchild);
        sizes.push(share * (child.sizes[inner] ?? 1 / child.children.length));
      });
    } else {
      children.push(child);
      sizes.push(share);
    }
  });
  if (children.length === 1) return children[0] as LayoutNode;
  return { kind: "split", axis: node.axis, children, sizes: normalShares(sizes, children.length) };
}

/** The layout with `newId` placed on `side` of `groupId`, taking half of the
 *  space that group had. A group that is not in the layout leaves it as it is. */
export function splitAt(root: LayoutNode, groupId: string, newId: string, side: Side): LayoutNode {
  const axis = axisOf(side);
  const placed = leaf(newId);
  const visit = (node: LayoutNode): LayoutNode => {
    if (node.kind === "group") {
      if (node.id !== groupId) return node;
      return {
        kind: "split",
        axis,
        children: after(side) ? [node, placed] : [placed, node],
        sizes: [0.5, 0.5],
      };
    }
    // A split along the same axis takes the new group as a sibling, halving
    // only the share of the group it was split from.
    const index = node.children.findIndex((child) => child.kind === "group" && child.id === groupId);
    if (index >= 0 && node.axis === axis) {
      const share = node.sizes[index] ?? 1 / node.children.length;
      const children = [...node.children];
      const sizes = [...node.sizes];
      const at = after(side) ? index + 1 : index;
      sizes[index] = share / 2;
      children.splice(at, 0, placed);
      sizes.splice(at, 0, share / 2);
      return { ...node, children, sizes };
    }
    return { ...node, children: node.children.map(visit) };
  };
  return tidy(visit(root));
}

/** The layout without `groupId`; its space goes to the neighbours it shared a
 *  split with. `null` when it was the only group. */
export function removeGroup(root: LayoutNode, groupId: string): LayoutNode | null {
  const visit = (node: LayoutNode): LayoutNode | null => {
    if (node.kind === "group") return node.id === groupId ? null : node;
    const children: LayoutNode[] = [];
    const sizes: number[] = [];
    node.children.forEach((child, index) => {
      const kept = visit(child);
      if (kept === null) return;
      children.push(kept);
      sizes.push(node.sizes[index] ?? 1 / node.children.length);
    });
    if (children.length === 0) return null;
    return { ...node, children, sizes: normalShares(sizes, children.length) };
  };
  const next = visit(root);
  return next === null ? null : tidy(next);
}

/** The layout with the split at `path` given new shares. */
export function resizeAt(root: LayoutNode, path: LayoutPath, sizes: readonly number[]): LayoutNode {
  if (path.length === 0) {
    if (root.kind !== "split") return root;
    return { ...root, sizes: normalShares(sizes, root.children.length) };
  }
  if (root.kind !== "split") return root;
  const [head, ...rest] = path;
  if (head === undefined || root.children[head] === undefined) return root;
  const children = root.children.map((child, index) =>
    index === head ? resizeAt(child, rest, sizes) : child,
  );
  return { ...root, children };
}

/** Move the boundary after child `index` of the split at `path` by `delta` (a
 *  share of the split), keeping both sides above the floor. */
export function nudgeAt(root: LayoutNode, path: LayoutPath, index: number, delta: number): LayoutNode {
  const split = nodeAt(root, path);
  if (split === null || split.kind !== "split") return root;
  return resizeAt(root, path, nudgeShares(split.sizes, index, delta));
}

/** `sizes` with the boundary after `index` moved by `delta`: only the two
 *  shares either side of it change, and neither goes below the floor. */
export function nudgeShares(sizes: readonly number[], index: number, delta: number): number[] {
  const next = [...sizes];
  const left = next[index];
  const right = next[index + 1];
  if (left === undefined || right === undefined) return next;
  const pair = left + right;
  const floor = Math.min(MIN_SHARE, pair / 2);
  const moved = Math.min(Math.max(left + delta, floor), pair - floor);
  next[index] = moved;
  next[index + 1] = pair - moved;
  return next;
}

export function nodeAt(root: LayoutNode, path: LayoutPath): LayoutNode | null {
  let node: LayoutNode = root;
  for (const index of path) {
    if (node.kind !== "split") return null;
    const child: LayoutNode | undefined = node.children[index];
    if (child === undefined) return null;
    node = child;
  }
  return node;
}

/** The splits from the root down to `groupId`, each with the index taken. */
function trail(root: LayoutNode, groupId: string): { split: LayoutSplit; index: number }[] | null {
  if (root.kind === "group") return root.id === groupId ? [] : null;
  for (let index = 0; index < root.children.length; index += 1) {
    const child = root.children[index] as LayoutNode;
    const below = trail(child, groupId);
    if (below !== null) return [{ split: root, index }, ...below];
  }
  return null;
}

/** The group next to `groupId` on `side`, as a reader would point at it: the
 *  nearest split along that axis with something on that side, and the group in
 *  it nearest the boundary. `null` at the edge of the workspace. */
export function neighbour(root: LayoutNode, groupId: string, side: Side): string | null {
  const steps = trail(root, groupId);
  if (steps === null) return null;
  const axis = axisOf(side);
  for (let depth = steps.length - 1; depth >= 0; depth -= 1) {
    const step = steps[depth];
    if (step === undefined || step.split.axis !== axis) continue;
    const next = after(side) ? step.index + 1 : step.index - 1;
    const target = step.split.children[next];
    if (target === undefined) continue;
    const order = groupOrder(target);
    // The group nearest the shared edge: the first one going forward, the
    // last one going back.
    return (after(side) ? order[0] : order[order.length - 1]) ?? null;
  }
  return null;
}

/** The layout holding exactly `groups`: groups it names that are not wanted are
 *  removed, wanted groups it does not name are added in a row at the end, and
 *  anything past the ceiling is dropped (the caller folds those groups' tabs
 *  into one that is kept). */
export function reconcile(root: LayoutNode | null, groups: readonly string[]): LayoutNode {
  const wanted = [...new Set(groups)].slice(0, MAX_GROUPS);
  const first = wanted[0] ?? DEFAULT_GROUP_ID;
  let layout: LayoutNode | null = root;
  if (layout !== null) {
    for (const id of groupOrder(layout)) {
      if (!wanted.includes(id)) layout = layout === null ? null : removeGroup(layout, id);
    }
  }
  if (layout === null) layout = leaf(first);
  for (const id of wanted) {
    if (groupOrder(layout).includes(id)) continue;
    const order = groupOrder(layout);
    layout = splitAt(layout, order[order.length - 1] ?? first, id, "right");
  }
  return layout;
}

// -- the stored form ----------------------------------------------------------

/** One node as it is stored. Short keys: the document has a hard size ceiling
 *  and the layout rides in every save. */
export type LayoutWire = { g: string } | { split: Axis; children: LayoutWire[]; sizes: number[] };

export function toWire(node: LayoutNode): LayoutWire {
  if (node.kind === "group") return { g: node.id };
  return {
    split: node.axis,
    children: node.children.map(toWire),
    // Three places is a tenth of a pixel on any pane this will ever be.
    sizes: node.sizes.map((size) => Math.round(size * 1000) / 1000),
  };
}

/** A stored node, or `null` when it is not one this build can read: a group
 *  named twice, a split with nothing in it, an axis it has never heard of. */
export function fromWire(raw: unknown): LayoutNode | null {
  const seen = new Set<string>();
  const read = (value: unknown, depth: number): LayoutNode | null => {
    if (depth > MAX_GROUPS || value === null || typeof value !== "object") return null;
    const record = value as Record<string, unknown>;
    if (typeof record.g === "string" && record.g !== "") {
      if (seen.has(record.g)) return null;
      seen.add(record.g);
      return leaf(record.g);
    }
    if ((record.split === "row" || record.split === "column") && Array.isArray(record.children)) {
      const children: LayoutNode[] = [];
      for (const child of record.children) {
        const parsed = read(child, depth + 1);
        if (parsed === null) return null;
        children.push(parsed);
      }
      if (children.length === 0) return null;
      const sizes = Array.isArray(record.sizes) ? record.sizes : [];
      return { kind: "split", axis: record.split, children, sizes: normalShares(sizes, children.length) };
    }
    return null;
  };
  const parsed = read(raw, 0);
  if (parsed === null || groupOrder(parsed).length > MAX_GROUPS) return null;
  return tidy(parsed);
}
