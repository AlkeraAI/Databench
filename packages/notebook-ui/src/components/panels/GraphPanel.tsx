// The dependency graph as a layered drawing: each cell sits one row below the
// deepest cell it reads from. Filters narrow it to what a selected cell reads
// from or what reads from it; cells with graph errors are marked.

import { useId, useMemo, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import { cellLinks, downstreamOf, topologicalOrder, upstreamOf } from "../../model/graph";
import type { CellNames } from "../../model/graph";
import type { DocCell, GraphView } from "../../model/types";
import { cellLabel, graphErrorLabel } from "./labels";
import "./panels.css";

export type GraphFilter = "all" | "upstream" | "downstream";

export interface GraphPanelProps {
  cells: readonly Pick<DocCell, "id" | "name">[];
  graph: GraphView;
  /** Defs and refs per cell, to name what flows along each edge. */
  names?: Readonly<Record<string, CellNames | undefined>>;
  /** The cell the filters are relative to, drawn as current. */
  selectedId?: string | null;
  onJump: (cellId: string) => void;
}

const NODE_W = 140;
const NODE_H = 30;
const GAP_X = 16;
const GAP_Y = 28;

interface Node {
  id: string;
  label: string;
  errors: string[];
  x: number;
  y: number;
}

function layout(ids: readonly string[], graph: GraphView): Map<string, { layer: number; slot: number }> {
  const visible = new Set(ids);
  const edges = graph.edges.filter(([a, b]) => visible.has(a) && visible.has(b) && a !== b);
  const { order } = topologicalOrder({ edges, errors: {} }, ids);
  const placedAt = new Map(order.map((id, i) => [id, i]));
  const layer = new Map<string, number>();
  for (const id of order) {
    let depth = 0;
    for (const [a, b] of edges) {
      // On a cycle, only parents already placed count, so the walk ends.
      if (b === id && (placedAt.get(a) ?? 0) < (placedAt.get(id) ?? 0)) depth = Math.max(depth, (layer.get(a) ?? 0) + 1);
    }
    layer.set(id, depth);
  }
  const slots = new Map<number, number>();
  const out = new Map<string, { layer: number; slot: number }>();
  for (const id of order) {
    const l = layer.get(id) ?? 0;
    const slot = slots.get(l) ?? 0;
    slots.set(l, slot + 1);
    out.set(id, { layer: l, slot });
  }
  return out;
}

export function GraphPanel({ cells, graph, names = {}, selectedId = null, onJump }: GraphPanelProps) {
  const [filter, setFilter] = useState<GraphFilter>("all");
  const idPrefix = useId();
  const buttons = useRef(new Map<string, HTMLButtonElement>());
  const order = useMemo(() => cells.map((c) => c.id), [cells]);
  const selected = selectedId !== null && order.includes(selectedId) ? selectedId : null;
  const active: GraphFilter = selected ? filter : "all";

  const visibleIds = useMemo(() => {
    if (active === "all" || !selected) return order;
    const related = new Set(active === "upstream" ? upstreamOf(graph, selected, order) : downstreamOf(graph, selected, order));
    related.add(selected);
    return order.filter((id) => related.has(id));
  }, [active, selected, graph, order]);

  const { nodes, edges, width, height, sequence } = useMemo(() => {
    const positions = layout(visibleIds, graph);
    const byId = new Map(cells.map((c, i) => [c.id, { cell: c, index: i }]));
    const list: Node[] = [];
    let maxX = 0;
    let maxY = 0;
    for (const id of visibleIds) {
      const at = positions.get(id);
      const entry = byId.get(id);
      if (!at || !entry) continue;
      const x = at.slot * (NODE_W + GAP_X);
      const y = at.layer * (NODE_H + GAP_Y);
      maxX = Math.max(maxX, x + NODE_W);
      maxY = Math.max(maxY, y + NODE_H);
      list.push({ id, label: cellLabel(entry.cell, entry.index), errors: graph.errors[id] ?? [], x, y });
    }
    const nodeById = new Map(list.map((n) => [n.id, n]));
    const lines = graph.edges
      .filter(([a, b]) => a !== b && nodeById.has(a) && nodeById.has(b))
      .map(([a, b]) => ({ from: nodeById.get(a) as Node, to: nodeById.get(b) as Node }));
    const sorted = [...list].sort((p, q) => p.y - q.y || p.x - q.x).map((n) => n.id);
    return { nodes: list, edges: lines, width: maxX, height: maxY, sequence: sorted };
  }, [visibleIds, graph, cells]);

  const edgeNames = (from: string, to: string): string[] =>
    cellLinks(graph, to, names, order).readsFrom.find((link) => link.cell_id === from)?.names ?? [];

  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>, id: string): void => {
    const at = sequence.indexOf(id);
    let next: string | undefined;
    if (event.key === "ArrowDown" || event.key === "ArrowRight") next = sequence[at + 1];
    else if (event.key === "ArrowUp" || event.key === "ArrowLeft") next = sequence[at - 1];
    else if (event.key === "Home") next = sequence[0];
    else if (event.key === "End") next = sequence[sequence.length - 1];
    else return;
    event.preventDefault();
    if (next) buttons.current.get(next)?.focus();
  };

  const filters: [GraphFilter, string][] = [
    ["all", "All"],
    ["upstream", "Upstream"],
    ["downstream", "Downstream"],
  ];

  return (
    <section className="nb-panel" aria-label="Graph">
      <div className="nb-panel__row" role="group" aria-label="Show">
        {filters.map(([value, label]) => (
          <button
            key={value}
            type="button"
            className="nb-chip"
            aria-pressed={active === value}
            disabled={value !== "all" && !selected}
            onClick={() => setFilter(value)}
          >
            {label}
          </button>
        ))}
      </div>
      {nodes.length === 0 ? (
        <p className="nb-panel__empty">No cells</p>
      ) : (
        <div className="nb-graph">
          <div className="nb-graph__canvas" style={{ width, height }}>
            <svg className="nb-graph__edges" width={width} height={height} aria-hidden="true">
              {edges.map(({ from, to }) => {
                const x1 = from.x + NODE_W / 2;
                const y1 = from.y + NODE_H;
                const x2 = to.x + NODE_W / 2;
                const y2 = to.y;
                const mid = (y1 + y2) / 2;
                const lit = selected !== null && (from.id === selected || to.id === selected);
                const flowing = edgeNames(from.id, to.id);
                return (
                  <path
                    key={`${from.id}->${to.id}`}
                    data-edge={`${from.id}->${to.id}`}
                    className={lit ? "nb-graph__edge nb-graph__edge--active" : "nb-graph__edge"}
                    d={`M ${x1} ${y1} C ${x1} ${mid}, ${x2} ${mid}, ${x2} ${y2}`}
                  >
                    {flowing.length > 0 ? <title>{flowing.join(", ")}</title> : null}
                  </path>
                );
              })}
            </svg>
            <ul className="nb-graph__nodes">
              {sequence.map((id) => {
                const node = nodes.find((n) => n.id === id) as Node;
                const problem = node.errors.map(graphErrorLabel).join(". ");
                return (
                  <li key={id}>
                    <button
                      ref={(el) => {
                        if (el) buttons.current.set(id, el);
                        else buttons.current.delete(id);
                      }}
                      type="button"
                      className={node.errors.length > 0 ? "nb-graph__node nb-graph__node--error" : "nb-graph__node"}
                      style={{ left: node.x, top: node.y, width: NODE_W, height: NODE_H }}
                      aria-current={id === selected ? "true" : undefined}
                      aria-describedby={problem ? `${idPrefix}-${id}` : undefined}
                      title={problem || undefined}
                      onClick={() => onJump(id)}
                      onKeyDown={(event) => onKeyDown(event, id)}
                    >
                      {node.errors.length > 0 ? (
                        <span className="nb-graph__mark" aria-hidden="true">
                          !
                        </span>
                      ) : null}
                      {node.label}
                    </button>
                    {problem ? (
                      <span id={`${idPrefix}-${id}`} className="nb-visually-hidden">
                        {problem}
                      </span>
                    ) : null}
                  </li>
                );
              })}
            </ul>
          </div>
        </div>
      )}
    </section>
  );
}
