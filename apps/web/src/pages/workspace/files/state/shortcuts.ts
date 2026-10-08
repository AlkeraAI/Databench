/**
 * The one keyboard table for the Files browser.
 *
 * Both the key dispatcher and the context menu read it: the menu shows the
 * label this table produces, so a binding can never drift from its caption.
 */

import type { Platform } from '@/lib/platform';

export type { Platform };

export type FilesAction =
  | 'open'
  | 'open-parent'
  | 'rename'
  | 'share'
  | 'trash'
  | 'copy'
  | 'cut'
  | 'paste'
  | 'undo'
  | 'redo'
  | 'new-folder'
  | 'search-folder'
  | 'search-drive'
  | 'quick-look'
  | 'focus-next'
  | 'focus-prev'
  | 'focus-first'
  | 'focus-last';

/** The parts of a keyboard event the table matches on. */
export interface KeyEventLike {
  readonly key: string;
  readonly shiftKey?: boolean;
  readonly metaKey?: boolean;
  readonly ctrlKey?: boolean;
  readonly altKey?: boolean;
}

export interface ShortcutBinding {
  readonly action: FilesAction;
  /** `KeyboardEvent.key`, matched case-insensitively. */
  readonly key: string;
  /** Cmd on macOS, Ctrl elsewhere. */
  readonly accel: boolean;
  readonly shift: boolean;
  /** Which platform the binding exists on at all. */
  readonly platform: Platform | 'all';
}

/**
 * Every Files keyboard binding, in menu order. Where the platforms differ the two
 * rows are listed separately rather than branching at match time, so the
 * context menu can render a platform's table by filtering this list.
 */
export const SHORTCUT_BINDINGS: readonly ShortcutBinding[] = [
  { action: 'open', key: 'Enter', accel: false, shift: false, platform: 'other' },
  { action: 'open', key: 'ArrowDown', accel: true, shift: false, platform: 'all' },
  { action: 'open-parent', key: 'ArrowUp', accel: true, shift: false, platform: 'all' },
  // macOS renames on Enter and opens with Cmd+Down; F2 renames everywhere.
  { action: 'rename', key: 'Enter', accel: false, shift: false, platform: 'mac' },
  { action: 'rename', key: 'F2', accel: false, shift: false, platform: 'all' },
  { action: 'share', key: 's', accel: true, shift: true, platform: 'all' },
  { action: 'trash', key: 'Backspace', accel: true, shift: false, platform: 'mac' },
  // The forward-delete key trashes everywhere: it has no text to delete in a
  // listing, and a person reaching for it on a Mac meant exactly this.
  { action: 'trash', key: 'Delete', accel: false, shift: false, platform: 'all' },
  { action: 'trash', key: 'Backspace', accel: false, shift: false, platform: 'other' },
  { action: 'copy', key: 'c', accel: true, shift: false, platform: 'all' },
  { action: 'cut', key: 'x', accel: true, shift: false, platform: 'all' },
  { action: 'paste', key: 'v', accel: true, shift: false, platform: 'all' },
  { action: 'undo', key: 'z', accel: true, shift: false, platform: 'all' },
  { action: 'redo', key: 'z', accel: true, shift: true, platform: 'all' },
  { action: 'new-folder', key: 'n', accel: true, shift: true, platform: 'all' },
  { action: 'search-folder', key: 'f', accel: true, shift: false, platform: 'all' },
  { action: 'search-drive', key: 'f', accel: true, shift: true, platform: 'all' },
  { action: 'quick-look', key: ' ', accel: false, shift: false, platform: 'all' },
  { action: 'focus-next', key: 'ArrowDown', accel: false, shift: false, platform: 'all' },
  { action: 'focus-prev', key: 'ArrowUp', accel: false, shift: false, platform: 'all' },
  { action: 'focus-first', key: 'Home', accel: false, shift: false, platform: 'all' },
  { action: 'focus-last', key: 'End', accel: false, shift: false, platform: 'all' },
];

/** The bindings that exist on one platform. */
export function bindingsFor(platform: Platform): readonly ShortcutBinding[] {
  return SHORTCUT_BINDINGS.filter((b) => b.platform === 'all' || b.platform === platform);
}

/** True when the event carries the platform's command modifier. */
export function hasAccel(event: KeyEventLike, platform: Platform): boolean {
  return platform === 'mac' ? event.metaKey === true : event.ctrlKey === true;
}

/**
 * True when the event carries the *other* platform's command modifier, which
 * must never stand in for the real one — Ctrl+C on macOS is not a copy.
 */
function hasForeignAccel(event: KeyEventLike, platform: Platform): boolean {
  return platform === 'mac' ? event.ctrlKey === true : event.metaKey === true;
}

export function resolveShortcut(event: KeyEventLike, platform: Platform): FilesAction | null {
  if (event.altKey === true) return null;
  if (hasForeignAccel(event, platform)) return null;
  const accel = hasAccel(event, platform);
  const shift = event.shiftKey === true;
  const key = event.key.toLowerCase();
  const match = bindingsFor(platform).find(
    (b) => b.key.toLowerCase() === key && b.accel === accel && b.shift === shift,
  );
  return match?.action ?? null;
}

const MAC_KEY_LABELS: Readonly<Record<string, string>> = {
  Enter: '↩',
  Backspace: '⌫',
  Delete: '⌦',
  ArrowUp: '↑',
  ArrowDown: '↓',
  Home: '↖',
  End: '↘',
  ' ': 'Space',
};

const OTHER_KEY_LABELS: Readonly<Record<string, string>> = {
  ArrowUp: 'Up',
  ArrowDown: 'Down',
  ' ': 'Space',
};

function labelKey(key: string, platform: Platform): string {
  const table = platform === 'mac' ? MAC_KEY_LABELS : OTHER_KEY_LABELS;
  return table[key] ?? (key.length === 1 ? key.toUpperCase() : key);
}

/** The caption the context menu shows beside an item, e.g. `⇧⌘N` or `Ctrl+Shift+N`. */
export function shortcutLabel(action: FilesAction, platform: Platform): string | null {
  const binding = bindingsFor(platform).find((b) => b.action === action);
  if (binding === undefined) return null;
  const key = labelKey(binding.key, platform);
  if (platform === 'mac') {
    return `${binding.shift ? '⇧' : ''}${binding.accel ? '⌘' : ''}${key}`;
  }
  const parts: string[] = [];
  if (binding.accel) parts.push('Ctrl');
  if (binding.shift) parts.push('Shift');
  parts.push(key);
  return parts.join('+');
}
