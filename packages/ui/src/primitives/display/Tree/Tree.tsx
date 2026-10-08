import { useCallback, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";

import { cx } from "../../cx";
import { ChevronRightIcon } from "../../icons";

/**
 * Tree — a selectable, collapsible index rendered as an ARIA `tree`.
 *
 * The org's team register and the knowledge subject rail are the same shape: a nested list you
 * navigate by selecting a node, each row a twist chevron, a label, and a trailing count. This owns
 * the shape — the drawn connectors, the expand/collapse animation, the selected-row treatment, and a
 * roving-tabindex keyboard model (arrows move / expand / collapse, Home/End, Enter/Space select).
 * Selection and expansion are both controlled: the caller owns `selectedId` + `expanded` and reacts
 * to `onSelect` / `onToggle`. A long label truncates to one line, so expanding never reflows a row.
 */

export interface TreeNode {
  id: string;
  /** The row's label. A string also names the twist's accessible action ("Expand Finance"). */
  label: ReactNode;
  /** Trailing content, right-aligned — typically a count. Its ink follows the row state (muted at
   *  rest, brand when selected). Wrap it yourself for mono / an aria-label. */
  trailing?: ReactNode;
  /** The emphasized (root) treatment — a semibold label even at rest. */
  emphasized?: boolean;
  /** Visible, navigable, and not selectable. A picker that hid the rows a person may not pick
   *  would hide the shape of the tree they are picking inside; this keeps the row on screen and
   *  refuses the selection instead. */
  disabled?: boolean;
  /** Why this row cannot be picked, announced with it. Only read when `disabled`. */
  disabledReason?: string;
  children?: TreeNode[];
}

export interface TreeProps {
  nodes: TreeNode[];
  /** The selected node's id — carries the brand wash. Selecting a row (click or Enter) also moves the
   *  roving tab stop there; changing `selectedId` from outside does not move keyboard focus. */
  selectedId?: string;
  /** The expanded node ids (controlled). */
  expanded: Set<string>;
  onSelect: (id: string) => void;
  onToggle: (id: string) => void;
  /** Accessible name for the tree. */
  ariaLabel: string;
  className?: string;
}

/** The label text, when it's a plain string — used for the twist's accessible action. */
function labelText(label: ReactNode): string {
  return typeof label === "string" ? label : "";
}

/** The twist's accessible action for a node ("Expand Finance" / "Collapse Finance"). Exported so
 *  callers/tests build the expected label from this one source instead of copying the wording. */
export function treeTwistLabel(open: boolean, name: string): string {
  return `${open ? "Collapse" : "Expand"}${name ? ` ${name}` : ""}`;
}

/** Pre-order list of every currently-visible (expanded-reachable) node id — the order the up/down
 *  arrow keys walk, and the basis for first-child / parent jumps. */
function visibleOrder(nodes: TreeNode[], expanded: Set<string>, out: string[] = []): string[] {
  for (const n of nodes) {
    out.push(n.id);
    if (n.children?.length && expanded.has(n.id)) visibleOrder(n.children, expanded, out);
  }
  return out;
}

/** Map of node id → its parent id (null at the root), for the ArrowLeft parent jump. */
function parentMap(nodes: TreeNode[], parent: string | null, out: Map<string, string | null>): void {
  for (const n of nodes) {
    out.set(n.id, parent);
    if (n.children?.length) parentMap(n.children, n.id, out);
  }
}

export function Tree({ nodes, selectedId, expanded, onSelect, onToggle, ariaLabel, className }: TreeProps) {
  const [focusId, setFocusId] = useState<string | undefined>(selectedId);
  const rowRefs = useRef(new Map<string, HTMLDivElement>());

  const focusRow = useCallback((id: string) => {
    setFocusId(id);
    rowRefs.current.get(id)?.focus();
  }, []);

  const setRowRef = useCallback((id: string, el: HTMLDivElement | null) => {
    if (el) rowRefs.current.set(id, el);
    else rowRefs.current.delete(id);
  }, []);

  // Selecting a row also parks the roving tab stop on it, so a click and the keyboard agree on where
  // Tab re-enters the tree.
  const selectRow = useCallback(
    (id: string, disabled?: boolean) => {
      setFocusId(id);
      if (!disabled) onSelect(id);
    },
    [onSelect],
  );

  // The walk order + parent lookup only change with the tree shape or its expansion — not on every
  // focus move — so derive them once per (nodes, expanded), not per keystroke re-render.
  const { order, parents, visible } = useMemo(() => {
    const o = visibleOrder(nodes, expanded);
    const p = new Map<string, string | null>();
    parentMap(nodes, null, p);
    return { order: o, parents: p, visible: new Set(o) };
  }, [nodes, expanded]);

  // The single roving tab stop must be a VISIBLE node — collapsed children stay rendered (so they can
  // animate) but are inert, so if the focused node was collapsed away the tree would have no reachable
  // tab stop. Fall back to the selection, then the first node.
  const tabStopId = focusId && visible.has(focusId) ? focusId : selectedId && visible.has(selectedId) ? selectedId : order[0];

  const onRowKey = (e: KeyboardEvent<HTMLDivElement>, node: TreeNode, hasKids: boolean, open: boolean) => {
    // The treeitem is the focusable element AND an ancestor of its child treeitems, so a child's
    // keydown bubbles here too — handle only the event on the focused item itself.
    if (e.target !== e.currentTarget) return;
    const idx = order.indexOf(node.id);
    switch (e.key) {
      case "Enter":
      case " ":
        e.preventDefault();
        if (!node.disabled) onSelect(node.id);
        break;
      case "ArrowDown":
        e.preventDefault();
        if (idx < order.length - 1) focusRow(order[idx + 1]);
        break;
      case "ArrowUp":
        e.preventDefault();
        if (idx > 0) focusRow(order[idx - 1]);
        break;
      case "ArrowRight":
        e.preventDefault();
        if (hasKids && !open) onToggle(node.id);
        else if (hasKids && open && idx < order.length - 1) focusRow(order[idx + 1]);
        break;
      case "ArrowLeft": {
        e.preventDefault();
        if (hasKids && open) onToggle(node.id);
        else {
          const parent = parents.get(node.id);
          if (parent) focusRow(parent);
        }
        break;
      }
      case "Home":
        e.preventDefault();
        focusRow(order[0]);
        break;
      case "End":
        e.preventDefault();
        focusRow(order[order.length - 1]);
        break;
    }
  };

  return (
    <div className={cx("alk-tree", className)} role="tree" aria-label={ariaLabel}>
      {nodes.map((n) => (
        <TreeItem
          key={n.id}
          node={n}
          level={1}
          selectedId={selectedId}
          tabStopId={tabStopId}
          expanded={expanded}
          onSelect={selectRow}
          onToggle={onToggle}
          onRowKey={onRowKey}
          setRowRef={setRowRef}
        />
      ))}
    </div>
  );
}

function TreeItem({
  node,
  level,
  selectedId,
  tabStopId,
  expanded,
  onSelect,
  onToggle,
  onRowKey,
  setRowRef,
}: {
  node: TreeNode;
  level: number;
  selectedId: string | undefined;
  tabStopId: string | undefined;
  expanded: Set<string>;
  onSelect: (id: string, disabled?: boolean) => void;
  onToggle: (id: string) => void;
  onRowKey: (e: KeyboardEvent<HTMLDivElement>, node: TreeNode, hasKids: boolean, open: boolean) => void;
  setRowRef: (id: string, el: HTMLDivElement | null) => void;
}) {
  const kids = node.children ?? [];
  const hasKids = kids.length > 0;
  const open = expanded.has(node.id);
  const active = selectedId === node.id;
  const name = labelText(node.label);

  return (
    // The treeitem is the focusable element: role, level, expanded/selected state, the roving tab
    // stop, and the keyboard model all sit here so a screen reader announces the node it lands on.
    // The inner `__row` is just the visible single line (hover / selection paint + connector anchor).
    <div
      className="alk-tree__node"
      role="treeitem"
      aria-level={level}
      aria-expanded={hasKids ? open : undefined}
      aria-selected={active}
      aria-disabled={node.disabled || undefined}
      ref={(el) => setRowRef(node.id, el)}
      tabIndex={node.id === tabStopId ? 0 : -1}
      onKeyDown={(e) => onRowKey(e, node, hasKids, open)}
    >
      <div
        className="alk-tree__row"
        data-active={active || undefined}
        data-emphasized={node.emphasized || undefined}
        data-disabled={node.disabled || undefined}
        title={node.disabled ? node.disabledReason : undefined}
        onClick={() => onSelect(node.id, node.disabled)}
      >
        {hasKids ? (
          <button
            type="button"
            className="alk-tree__twist"
            data-open={open || undefined}
            tabIndex={-1}
            aria-label={treeTwistLabel(open, name)}
            onClick={(e) => {
              e.stopPropagation();
              onToggle(node.id);
            }}
          >
            <ChevronRightIcon size={16} />
          </button>
        ) : (
          <span className="alk-tree__twist alk-tree__twist--leaf" aria-hidden="true" />
        )}
        <span className="alk-tree__name">{node.label}</span>
        {node.trailing != null ? <span className="alk-tree__count">{node.trailing}</span> : null}
      </div>
      {hasKids ? (
        <div className="alk-tree__children" data-open={open || undefined}>
          {/* always rendered so expand AND collapse animate (grid-rows 0fr→1fr); the closed subtree is
              inert so it's out of the tab order + the a11y tree, matching aria-expanded=false. */}
          <div className="alk-tree__children-inner" role="group" inert={!open || undefined}>
            {kids.map((child) => (
              <TreeItem
                key={child.id}
                node={child}
                level={level + 1}
                selectedId={selectedId}
                tabStopId={tabStopId}
                expanded={expanded}
                onSelect={onSelect}
                onToggle={onToggle}
                onRowKey={onRowKey}
                setRowRef={setRowRef}
              />
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}
