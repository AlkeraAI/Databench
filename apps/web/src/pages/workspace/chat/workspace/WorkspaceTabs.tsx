// One group's strip of open tabs, spelled from the stored document.
//
// It is the one place a stored tab becomes something a reader can see, and it
// asks the kind registry rather than deciding anything itself: what a tab is
// CALLED, and whether it can be closed at all, are the kind's answers. A tab of
// a kind this build has never heard of still gets a row here under its stored
// name — dropping it from the strip would hide a tab the reader's other client
// can still open, and the panel beside it is where the build says it is behind.

import type { ReactNode } from "react";

import { TabStrip, type TabStripDrag, type TabStripItem, type TabStripMarker } from "@alkera/ui";

import type { Item } from "@/api/files";

import { tabKindFor, type WorkspaceTab } from "./tabKinds";
import { FILES_TAB_ID } from "./workspaceStore";

export interface WorkspaceTabsProps {
  tabs: WorkspaceTab[];
  activeId: string | null;
  /** Tabs whose file changed while the reader was reading something else. */
  updated: readonly string[];
  /** Tabs whose file is no longer there. */
  gone: readonly string[];
  /** The live node behind a tab, where one has been read. The stored name is
   *  the floor; a file renamed on the machine reads as its new name here. */
  itemOf?: (tab: WorkspaceTab) => Item | undefined;
  onActivate: (id: string) => void;
  onClose: (id: string) => void;
  /** A tab was double-clicked: keep it. */
  onPin?: (id: string) => void;
  /** How the strip's tabs are dragged, when they are. */
  drag?: TabStripDrag;
  /** The strip's accessible name; one group's strip among several names its
   *  group. */
  label?: string;
  trailing?: ReactNode;
}

/** What a tab wears. A file that has gone outranks a file that merely changed:
 *  the reader can still act on the second, and only the first needs answering. */
function markerFor(
  id: string,
  updated: readonly string[],
  gone: readonly string[],
): TabStripMarker | null {
  if (gone.includes(id)) return "gone";
  if (updated.includes(id)) return "updated";
  return null;
}

export function WorkspaceTabs({
  tabs,
  activeId,
  updated,
  gone,
  itemOf,
  onActivate,
  onClose,
  onPin,
  drag,
  label = "Open files",
  trailing,
}: WorkspaceTabsProps) {
  const items: TabStripItem[] = tabs.map((tab) => {
    const kind = tabKindFor(tab.kind);
    const name = kind ? kind.label(tab, itemOf?.(tab)) : tab.name;
    return {
      id: tab.id,
      label: name,
      // The folder browser is pinned by the store whatever the registry says,
      // so a close button on it would be a control that does nothing.
      pinned: tab.id === FILES_TAB_ID || kind?.pinned === true,
      marker: markerFor(tab.id, updated, gone),
      // The path is chat-relative, so it names the file without naming the
      // machine it sits on or the folders above the chat.
      title: tab.path ?? name,
      transient: tab.transient === true,
    };
  });

  return (
    <TabStrip
      label={label}
      tabs={items}
      activeId={activeId}
      onActivate={onActivate}
      onClose={onClose}
      onPin={onPin}
      drag={drag}
      trailing={trailing}
    />
  );
}
