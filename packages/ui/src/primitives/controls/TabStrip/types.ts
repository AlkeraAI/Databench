// TabStrip — a row of closable tabs over one panel, the way an editor shows the
// files you have open.
//
// Unlike `Tabs` (a fixed set of sections that never changes), a strip's tabs come
// and go: they are opened by something the reader did elsewhere, they can be
// closed, one can be pinned so it always leads, and one can be marked — its file
// changed under it, or is no longer there. A tab can also be a preview (opened
// in passing, drawn in italics, and kept by a double click), and a strip can let
// its tabs be dragged: along itself, or out to another strip. The strip owns none
// of that state; it reports the activation, the close, the keep and the drop and
// draws what it is given.

import type { ReactNode } from "react";

/** Why a tab is wearing a dot: `updated` — the file changed while the tab was not
 *  the one being read; `gone` — the file it shows is no longer there. */
export type TabStripMarker = "updated" | "gone";

export interface TabStripItem {
  id: string;
  /** The visible name. Long names ellipsize, so `title` carries the whole one. */
  label: string;
  icon?: ReactNode;
  /** A pinned tab leads the strip and has no close button. */
  pinned?: boolean;
  marker?: TabStripMarker | null;
  /** The hover text — the full path or name when `label` is a shortened form. */
  title?: string;
  /** A preview tab: drawn in italics until it is kept. */
  transient?: boolean;
}

/** How a strip's tabs are dragged. Only a drag carrying `type` is dropped here,
 *  so two kinds of strip on one page never take each other's tabs. */
export interface TabStripDrag {
  /** The data type a dragged tab carries. */
  type: string;
  /** What a tab of this strip carries when it is dragged. */
  payload(id: string): string;
  /** A tab carrying `payload` was dropped before the tab at `index` (the end
   *  when `index` is the number of tabs). The index counts every tab drawn, the
   *  dragged one included when it is from this strip. */
  onDrop(payload: string, index: number): void;
  onDragStart?(id: string): void;
  onDragEnd?(): void;
}

export interface TabStripProps {
  /** Names the strip for assistive technology. */
  label: string;
  tabs: TabStripItem[];
  /** The tab whose panel is drawn. `null` where the strip is empty. */
  activeId: string | null;
  onActivate(id: string): void;
  onClose(id: string): void;
  /** A tab was double-clicked: keep it, if it is a preview. */
  onPin?(id: string): void;
  /** Lets the tabs be dragged. Absent, they are not. */
  drag?: TabStripDrag;
  /** Controls set after the last tab — an overflow menu, a new-tab button. */
  trailing?: ReactNode;
}
