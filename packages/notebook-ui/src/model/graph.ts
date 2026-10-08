// Questions the editor asks of the dependency graph: who a cell reads from,
// who reads from it, everything above or below it, and an order to draw or run
// cells in. An edge `[a, b]` says `b` reads a name `a` defines. Self edges are
// ignored; the engine never plans a cell as its own ancestor.

import type { GraphView } from "./types";

/** A cell's defs and refs, as `CellRuntime` carries them. */
export interface CellNames {
  defs: readonly string[];
  refs: readonly string[];
}

/** One end of an edge, with the names that flow along it. */
export interface CellLink {
  cell_id: string;
  names: string[];
}

export interface CellLinks {
  /** The cells this one reads from, in document order. */
  readsFrom: CellLink[];
  /** The cells that read from this one, in document order. */
  readBy: CellLink[];
}

export interface TopologicalOrder {
  /** Every cell, parents before children, ties broken by document order. */
  order: string[];
  /** The cells on or behind a cycle, which no order can satisfy. They follow
   *  the ordered cells, in document order. */
  cyclic: string[];
}

function documentIndex(order: readonly string[]): Map<string, number> {
  const index = new Map<string, number>();
  order.forEach((id, i) => {
    if (!index.has(id)) index.set(id, i);
  });
  return index;
}

/** The document order, falling back to first appearance in the edges for a
 *  cell the order does not name. */
function fullOrder(view: GraphView, order: readonly string[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  const add = (id: string): void => {
    if (!seen.has(id)) {
      seen.add(id);
      out.push(id);
    }
  };
  order.forEach(add);
  for (const [a, b] of view.edges) {
    add(a);
    add(b);
  }
  return out;
}

function adjacency(view: GraphView, direction: "parents" | "children"): Map<string, Set<string>> {
  const map = new Map<string, Set<string>>();
  for (const [a, b] of view.edges) {
    if (a === b) continue;
    const [from, to] = direction === "children" ? [a, b] : [b, a];
    let set = map.get(from);
    if (!set) {
      set = new Set();
      map.set(from, set);
    }
    set.add(to);
  }
  return map;
}

function sortByDocument(ids: Iterable<string>, view: GraphView, order: readonly string[]): string[] {
  const index = documentIndex(fullOrder(view, order));
  return [...ids].sort((x, y) => (index.get(x) ?? Infinity) - (index.get(y) ?? Infinity));
}

/** The cells `cellId` reads from directly, in document order. */
export function parentsOf(view: GraphView, cellId: string, order: readonly string[] = []): string[] {
  return sortByDocument(adjacency(view, "parents").get(cellId) ?? [], view, order);
}

/** The cells that read from `cellId` directly, in document order. */
export function childrenOf(view: GraphView, cellId: string, order: readonly string[] = []): string[] {
  return sortByDocument(adjacency(view, "children").get(cellId) ?? [], view, order);
}

function closure(view: GraphView, cellId: string, direction: "parents" | "children", order: readonly string[]): string[] {
  const adj = adjacency(view, direction);
  const seen = new Set<string>();
  const stack = [cellId];
  while (stack.length > 0) {
    const id = stack.pop() as string;
    for (const next of adj.get(id) ?? []) {
      if (next !== cellId && !seen.has(next)) {
        seen.add(next);
        stack.push(next);
      }
    }
  }
  return sortByDocument(seen, view, order);
}

/** Every ancestor of `cellId` (not the cell itself, even on a cycle), in
 *  document order. */
export function upstreamOf(view: GraphView, cellId: string, order: readonly string[] = []): string[] {
  return closure(view, cellId, "parents", order);
}

/** Every descendant of `cellId` (not the cell itself, even on a cycle), in
 *  document order. */
export function downstreamOf(view: GraphView, cellId: string, order: readonly string[] = []): string[] {
  return closure(view, cellId, "children", order);
}

/** Parents before children; among cells free at the same time, the one
 *  earlier in the document goes first. */
export function topologicalOrder(view: GraphView, order: readonly string[]): TopologicalOrder {
  const all = fullOrder(view, order);
  const index = documentIndex(all);
  const children = adjacency(view, "children");
  const indegree = new Map<string, number>(all.map((id) => [id, 0]));
  for (const set of children.values()) {
    for (const child of set) indegree.set(child, (indegree.get(child) ?? 0) + 1);
  }
  // A small ready list kept sorted by document index: notebooks are hundreds
  // of cells, not millions.
  const ready = all.filter((id) => indegree.get(id) === 0);
  const out: string[] = [];
  while (ready.length > 0) {
    const id = ready.shift() as string;
    out.push(id);
    for (const child of children.get(id) ?? []) {
      const left = (indegree.get(child) ?? 0) - 1;
      indegree.set(child, left);
      if (left === 0) {
        const at = ready.findIndex((other) => (index.get(other) ?? 0) > (index.get(child) ?? 0));
        ready.splice(at === -1 ? ready.length : at, 0, child);
      }
    }
  }
  const placed = new Set(out);
  const cyclic = all.filter((id) => !placed.has(id));
  return { order: [...out, ...cyclic], cyclic };
}

/** The edges the engine would draw from each cell's defs and refs, for when
 *  no graph has arrived yet. A name several cells define links to all of them. */
export function deriveGraph(cells: readonly { id: string }[], names: Readonly<Record<string, CellNames | undefined>>): GraphView {
  const definers = new Map<string, string[]>();
  for (const cell of cells) {
    for (const name of names[cell.id]?.defs ?? []) {
      const list = definers.get(name) ?? [];
      list.push(cell.id);
      definers.set(name, list);
    }
  }
  const edges: [string, string][] = [];
  const seen = new Set<string>();
  for (const cell of cells) {
    for (const name of names[cell.id]?.refs ?? []) {
      for (const definer of definers.get(name) ?? []) {
        const key = `${definer}\u0000${cell.id}`;
        if (definer !== cell.id && !seen.has(key)) {
          seen.add(key);
          edges.push([definer, cell.id]);
        }
      }
    }
  }
  return { edges, errors: {} };
}

/** What `cellId` reads and from whom, and who reads what from it. The names on
 *  an edge are the parent's defs the child refs; an edge whose names are not
 *  known (defs or refs missing) still appears, with no names. */
export function cellLinks(
  view: GraphView,
  cellId: string,
  names: Readonly<Record<string, CellNames | undefined>>,
  order: readonly string[] = [],
): CellLinks {
  const shared = (parent: string, child: string): string[] => {
    const refs = new Set(names[child]?.refs ?? []);
    return (names[parent]?.defs ?? []).filter((name) => refs.has(name));
  };
  return {
    readsFrom: parentsOf(view, cellId, order).map((parent) => ({ cell_id: parent, names: shared(parent, cellId) })),
    readBy: childrenOf(view, cellId, order).map((child) => ({ cell_id: child, names: shared(cellId, child) })),
  };
}
