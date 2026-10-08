/**
 * The keyboard table, bound to the page.
 *
 * The table itself is `state/shortcuts.ts` — a pure matcher this hook only
 * dispatches. Four rules make the binding safe to hang off the whole page:
 *
 *  * a text field wins. While a rename box or the search field has focus the
 *    keystroke belongs to it, so nothing here fires — otherwise F2 inside the
 *    rename input would restart the rename and Space would never type a space.
 *  * a control wins its own activation. Enter and Space on a button, a link or a
 *    checkbox press that control: the table's Enter (open, or rename on a Mac)
 *    and Space (quick look) are the listing's, and taking them from New folder
 *    or the Grid toggle left those reachable only by mouse.
 *  * an action nobody handles is not consumed. The treegrid owns arrows and
 *    Home/End; the page registers no handler for them and the event travels on
 *    with its default intact.
 *  * the listing is one tab stop. Tab from a row leaves the listing for the next
 *    control after it (Shift+Tab for the one before it) instead of walking the
 *    controls inside every row, which is what makes a 20,000-row grid tabbable
 *    at all. Everywhere else Tab is the browser's own, so the bar above the
 *    listing, its column headers and the details pane keep their natural order.
 */

import { useCallback, type KeyboardEvent as ReactKeyboardEvent } from "react";

import { resolveShortcut, type FilesAction, type Platform } from "./state/shortcuts";

/** One callback per action the page implements. An absent action is left alone. */
export type FilesShortcutHandlers = Partial<Record<FilesAction, () => void>>;

export interface FilesShortcutOptions {
  readonly platform: Platform;
  readonly handlers: FilesShortcutHandlers;
  /** False while a modal owns the keyboard; the page still renders. */
  readonly enabled?: boolean;
}

export interface FilesShortcutBinding {
  readonly onKeyDown: (event: ReactKeyboardEvent<HTMLElement>) => void;
}

/** True when the keystroke belongs to a text field the caller is typing in. */
export function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (target.isContentEditable) return true;
  const tag = target.tagName;
  if (tag === "TEXTAREA" || tag === "SELECT") return true;
  if (tag !== "INPUT") return false;
  const type = (target as HTMLInputElement).type;
  // A checkbox or a radio is not a text field: Space there is the control's own.
  return type !== "checkbox" && type !== "radio" && type !== "button" && type !== "submit";
}

/** The surfaces that take the keyboard from the page while they are up: the row
 *  menu and the chooser a chat row opens, every dialog built on the shared modal
 *  (the file preview, Move to…, Copy to…, Share…, Save as template…), and the
 *  confirmations. A `role="dialog"` WITHOUT `aria-modal` is not one of them — the
 *  details pane is drawn that way under 900 px and its keys are still the page's. */
const OVERLAY_SURFACES = '[role="menu"],[role="alertdialog"],[role="dialog"][aria-modal="true"]';

/** The controls that answer Enter and Space themselves. */
const ACTIVATING_CONTROL =
  'button, a[href], summary, input, select, textarea, [role="button"], [role="link"], [role="menuitem"], [role="tab"], [role="checkbox"], [role="radio"], [role="switch"], [role="option"]';

/** True when an unmodified Enter or Space would press the focused control. */
export function isControlActivation(event: {
  key: string;
  target: EventTarget | null;
  metaKey?: boolean;
  ctrlKey?: boolean;
  altKey?: boolean;
}): boolean {
  if (event.key !== "Enter" && event.key !== " ") return false;
  if (event.metaKey === true || event.ctrlKey === true || event.altKey === true) return false;
  return event.target instanceof Element && event.target.matches(ACTIVATING_CONTROL);
}

/** What a browser stops on with Tab, before the checks a selector cannot make. */
const TABBABLE =
  'a[href], button, input, select, textarea, summary, [tabindex], [contenteditable="true"]';

function isTabbable(element: HTMLElement): boolean {
  if (element.tabIndex < 0) return false;
  if ((element as HTMLButtonElement).disabled === true) return false;
  if (element instanceof HTMLInputElement && element.type === "hidden") return false;
  return element.closest("[hidden], [inert]") === null;
}

/** The body of a listing a keystroke landed in: a row or a cell of a treegrid,
 *  never its column headers, which are ordinary buttons in the tab order. */
function listingOf(target: Element): Element | null {
  const listing = target.closest('[role="treegrid"], [role="grid"]');
  if (listing === null) return null;
  if (target.closest('[role="columnheader"]') !== null) return null;
  return listing;
}

/**
 * Move focus past the rows of `listing`, to the next tabbable control after
 * `from` (or before it, backwards) that is not itself inside the rows. False
 * when there is nothing on that side, so the browser's own Tab can leave the page.
 */
function leaveListing(listing: Element, from: Element, backwards: boolean): boolean {
  const inRows = (element: Element) =>
    listing.contains(element) && element.closest('[role="columnheader"]') === null;
  const candidates = [...document.querySelectorAll<HTMLElement>(TABBABLE)].filter(
    (element) => isTabbable(element) && !inRows(element),
  );
  const before = (element: Element) =>
    (element.compareDocumentPosition(from) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0;
  const target = backwards
    ? candidates.filter(before).at(-1)
    : candidates.find((element) => !before(element) && element !== from);
  if (target === undefined) return false;
  target.focus();
  return true;
}

export function useFilesShortcuts(options: FilesShortcutOptions): FilesShortcutBinding {
  const { platform, handlers, enabled = true } = options;

  const onKeyDown = useCallback(
    (event: ReactKeyboardEvent<HTMLElement>) => {
      if (!enabled) return;
      // A key typed in an overlay is not the page's. Tab would throw focus out
      // of a surface that traps it, and Delete would
      // trash the row behind it. Both kinds reach here: React bubbles a
      // PORTALLED child's event through the COMPONENT tree, so the file preview
      // arrives even though it is mounted on `document.body`, and an overlay the
      // page draws in place — the chooser a chat row opens, a confirmation in
      // the details pane — is a plain DOM child of this very element. So the
      // test is what the keystroke landed IN, not where that surface is mounted.
      if (!event.currentTarget.contains(event.target as Node)) return;
      const target = event.target;
      if (target instanceof Element && target.closest(OVERLAY_SURFACES) !== null) return;
      if (isTypingTarget(event.target)) return;

      if (event.key === "Tab" && !event.metaKey && !event.ctrlKey && !event.altKey) {
        const listing = target instanceof Element ? listingOf(target) : null;
        if (listing !== null && target instanceof Element) {
          if (leaveListing(listing, target, event.shiftKey)) event.preventDefault();
        }
        return;
      }
      if (isControlActivation(event)) return;

      const action = resolveShortcut(event, platform);
      if (action === null) return;
      const handler = handlers[action];
      if (handler === undefined) return;
      event.preventDefault();
      handler();
    },
    [enabled, platform, handlers],
  );

  return { onKeyDown };
}
