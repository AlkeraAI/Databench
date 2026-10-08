// How wide the chat page's side panes are on THIS machine.
//
// The rail lists every chat, so how wide it is is one answer for the account.
// The files pane is about the chat in front of the reader: one conversation is
// a column of prose and another is three files being edited side by side, and a
// reader who widened the pane for the second does not want the first to open
// that way. So the pane is remembered per chat — bounded, because a key per
// chat would otherwise grow for as long as the browser lives — and a chat this
// browser has never laid out opens at the last width the reader settled on
// anywhere, which is a better guess than the default.
//
// Storage itself is the shared, guarded one (`app/usePersistedSize`): it throws
// outright in some embedded and privacy contexts, comes back empty in a private
// window, and may hold a value written by a build that laid the page out
// differently. All of those read as "this browser has not been told yet".

import { accountKey } from "@alkera/ui/storage";

import type { AccountScope } from "../../../../lib/accountScope";
import {
  boundsOf,
  clampSize,
  hasSize,
  readPaneSize,
  readSize,
  sizeStore,
  writeSize,
  type LruBound,
  type PaneSize,
  type SizeBounds,
} from "../../../../app/usePersistedSize";


/** The shape this build writes. A document stamped with anything else was
 *  written by a build whose panes are not these panes, so it is not read. */
const VERSION = 1;

/** One pane's range, in CSS pixels. `size` is where it opens. */
export type PaneBounds = SizeBounds;

/** The rail that lists chats. */
export const RAIL_BOUNDS: PaneBounds = { min: 176, max: 480, size: 256 };

/** The workspace pane. `max` is the ceiling on a wide screen; a caller that
 *  knows the viewport narrows it to 60% of the width it actually has. */
export const PANE_BOUNDS: PaneBounds = { min: 320, max: 960, size: 576 };

export type PaneLayout = PaneSize;

export interface ChatLayout {
  v: typeof VERSION;
  rail: PaneLayout;
  pane: PaneLayout;
}

export interface LayoutOptions {
  /** Narrow the rail's range — the viewport is the only thing that does. */
  rail?: Partial<PaneBounds>;
  /** Narrow the workspace pane's range, typically to 60% of the viewport. */
  pane?: Partial<PaneBounds>;
  /**
   * What the workspace pane does on a browser that has never been told: open on
   * a wide screen, folded away on a narrow one. Only ever consulted when there
   * is nothing stored — a reader who folded the pane away on a wide screen
   * comes back to it folded away.
   */
  paneCollapsedByDefault?: boolean;
  /**
   * The chat whose pane this is. The pane is remembered against it; without one
   * (the chat list, a chat that has not resolved yet) only the account's
   * running default is read and written.
   */
  chatId?: string | null;
}

/** How many chats' pane widths this browser keeps. A reader who moves between a
 *  handful of conversations finds each as they left it; the rest fall off the
 *  end rather than accumulating a key per chat for the life of the browser. */
export const PANES_KEPT = 40;

/** Where the account's layout document is kept, per person and org. A layout
 *  stored before keys named the org is ignored, not migrated: it is a
 *  convenience, and the panes simply open at their defaults once. */
const keyFor = (scope: AccountScope): string => accountKey(scope.userId, scope.orgId, "chat.layout");

/** Where one chat's pane is remembered, and the namespace bounding all of them.
 *  Namespaced by account as well as chat: two people on one machine do not share
 *  a layout, and signing out of one must not evict the other's. */
const paneKeyFor = (userId: string, chatId: string): string => `chat.pane:${userId}:${chatId}`;
const paneLruFor = (userId: string): LruBound => ({
  namespace: `chat.pane:${userId}`,
  keep: PANES_KEPT,
});

/** What this browser remembers for this account, already clamped to the ranges
 *  the caller can actually lay out in. Never throws and never answers a width a
 *  pane cannot take.
 *
 *  The pane is read for the chat named in `options`; a chat this browser has
 *  never laid out falls back to the account's running default, which is the last
 *  width the reader settled on in any chat. */
export function readLayout(
  scope: AccountScope | null | undefined,
  options: LayoutOptions = {},
): ChatLayout {
  const rail = boundsOf(RAIL_BOUNDS, options.rail);
  const pane = boundsOf(PANE_BOUNDS, options.pane);
  const paneCollapsed = options.paneCollapsedByDefault ?? false;

  // An unreadable store and an unparsable value are the same fact: this browser
  // has nothing to say about the layout.
  const parsed: unknown = scope ? sizeStore().readJson(keyFor(scope)) : null;
  const document =
    parsed && typeof parsed === "object" && (parsed as { v?: unknown }).v === VERSION
      ? (parsed as Record<string, unknown>)
      : {};

  const fallback = readPaneSize(document.pane, pane, paneCollapsed);
  const perChat =
    scope && options.chatId
      ? readSize(paneKeyFor(scope.userId, options.chatId), PANE_BOUNDS, {
          bounds: options.pane,
          collapsedByDefault: fallback.collapsed,
        })
      : null;
  // A chat with nothing of its own is not a chat opened at the default: it is a
  // chat opened the way this reader last arranged one.
  const stored = scope && options.chatId ? hasSize(paneKeyFor(scope.userId, options.chatId)) : false;

  return {
    v: VERSION,
    rail: readPaneSize(document.rail, rail, false),
    pane: stored && perChat ? perChat : fallback,
  };
}

/** Remember this layout for this account, and this pane for this chat. Clamped
 *  on the way in as well as on the way out, so a width that was squeezed by a
 *  narrow window is not stored as a preference the reader never expressed. */
export function writeLayout(
  scope: AccountScope | null | undefined,
  layout: ChatLayout,
  options: LayoutOptions = {},
): void {
  if (!scope) return;
  const document: ChatLayout = {
    v: VERSION,
    rail: {
      collapsed: layout.rail.collapsed,
      width: clampSize(layout.rail.width, boundsOf(RAIL_BOUNDS, options.rail)),
    },
    // The account's copy of the pane is the running default a chat with nothing
    // of its own opens at.
    pane: {
      collapsed: layout.pane.collapsed,
      width: clampSize(layout.pane.width, boundsOf(PANE_BOUNDS, options.pane)),
    },
  };
  // A store that will not take the write costs this reader the memory of their
  // layout and nothing else.
  sizeStore().writeJson(keyFor(scope), document);
  if (options.chatId) {
    writeSize(paneKeyFor(scope.userId, options.chatId), layout.pane, PANE_BOUNDS, {
      bounds: options.pane,
      lru: paneLruFor(scope.userId),
    });
  }
}


/** How long this browser's copy of the layout may lag the screen.
 *
 *  A drag settles a width on every pointer frame, and `localStorage.setItem` is
 *  synchronous: writing each of those frames spends the same main thread that
 *  has to redraw the columns under the cursor, and serializes the whole document
 *  dozens of times to store a width the reader passed through on the way to the
 *  one they meant. The width they meant is the one the gesture stops on, so the
 *  page lays out from every frame and the browser is told once the frames stop.
 */
export const LAYOUT_COMMIT_MS = 120;

/** Where a layout goes to be remembered — once, on the width a gesture settled
 *  on, rather than on each of the widths it crossed. */
export interface LayoutWriter {
  /** The layout as of this frame. Written once the frames stop coming. */
  remember(scope: AccountScope | null | undefined, layout: ChatLayout, options?: LayoutOptions): void;
  /** Write whatever is still owed, now: the page is going away, and a width the
   *  reader chose a moment before leaving is still a width they chose. */
  flush(): void;
}

export function createLayoutWriter(delay: number = LAYOUT_COMMIT_MS): LayoutWriter {
  let timer: ReturnType<typeof setTimeout> | null = null;
  let owed: (() => void) | null = null;
  const settle = (): void => {
    timer = null;
    const write = owed;
    owed = null;
    write?.();
  };
  return {
    remember(scope, layout, options = {}): void {
      owed = () => writeLayout(scope, layout, options);
      if (timer !== null) clearTimeout(timer);
      timer = setTimeout(settle, delay);
    },
    flush(): void {
      if (timer !== null) clearTimeout(timer);
      settle();
    },
  };
}
