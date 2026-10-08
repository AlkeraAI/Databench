// SplitPane — the IDE-style column layout: named panes separated by draggable
// gutters, one pane taking whatever is left.
//
// The component owns none of the sizes. It reports a drag (`onResize`) and a
// collapse (`onToggle`) and re-renders from the specs it is given, so the host
// decides what is remembered and where (per user, per chat, not at all). That
// keeps the layout pure presentation and lets a restored width be clamped by the
// host against the same specs that drew it.

import type { ReactNode } from "react";

/** A ceiling expressed either in pixels or as a share of the container. */
export type PaneMax = number | `${number}%`;

/** One column. Exactly one pane in a set carries `fill`: it takes the remaining
 *  width and needs no `size`. */
export interface PaneSpec {
  id: string;
  /** The pane that absorbs the leftover width. Only one per set. */
  fill?: true;
  /** The pane's width in pixels — its starting width, and where a double-click on
   *  its gutter returns it to. Meaningless on the filling pane. */
  size?: number;
  /** The narrowest the pane is drawn. A drag stops here; dragging well past it
   *  collapses the pane instead. */
  min: number;
  max?: PaneMax;
  /** Whether the pane can be hidden entirely, leaving a strip with a show button. */
  collapsible?: boolean;
  collapsed?: boolean;
  /** How the pane is named to a screen reader — on its gutter ("Resize Files
   *  panel") and on its show button ("Show Files panel"). */
  label: string;
}

export interface SplitPaneProps {
  /** Names the whole layout for assistive technology. */
  label: string;
  /** The columns, in visual order. `children` supplies one node per pane, in the
   *  same order. */
  panes: PaneSpec[];
  /** A drag or a keyboard resize settled on a new width, in pixels. */
  onResize(id: string, px: number): void;
  /** A pane was collapsed or shown again. */
  onToggle(id: string, collapsed: boolean): void;
  /** Pixels one arrow key moves a gutter. Defaults to 16. */
  step?: number;
  /** Pixels Shift + an arrow key moves a gutter. Defaults to 64. */
  bigStep?: number;
  /** How far past its minimum a collapsible pane must be dragged before it
   *  collapses. Defaults to 48. */
  collapseBelow?: number;
  /** One node per entry in `panes`, in the same order. */
  children: ReactNode;
}
