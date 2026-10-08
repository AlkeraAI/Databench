import { afterEach, describe, expect, it } from 'vitest';

import {
  type FilesAction,
  type KeyEventLike,
  type Platform,
  SHORTCUT_BINDINGS,
  bindingsFor,
  hasAccel,
  resolveShortcut,
  shortcutLabel,
} from '@/pages/workspace/files/state/shortcuts';
import { detectPlatform } from '@/lib/platform';

/** An event with the platform's command modifier held. */
function accel(platform: Platform, event: KeyEventLike): KeyEventLike {
  return platform === 'mac' ? { ...event, metaKey: true } : { ...event, ctrlKey: true };
}

/** An event with the *other* platform's command modifier held. */
function foreignAccel(platform: Platform, event: KeyEventLike): KeyEventLike {
  return platform === 'mac' ? { ...event, ctrlKey: true } : { ...event, metaKey: true };
}

const PLATFORMS: readonly Platform[] = ['mac', 'other'];

describe('hasAccel — which modifier is the command key', () => {
  it('macOS reads Cmd and not Ctrl', () => {
    expect(hasAccel({ key: 'c', metaKey: true }, 'mac')).toBe(true);
    expect(hasAccel({ key: 'c', ctrlKey: true }, 'mac')).toBe(false);
  });

  it('Windows and Linux read Ctrl and not Cmd', () => {
    expect(hasAccel({ key: 'c', ctrlKey: true }, 'other')).toBe(true);
    expect(hasAccel({ key: 'c', metaKey: true }, 'other')).toBe(false);
  });

  it('an event with no modifiers has no command key on either platform', () => {
    expect(hasAccel({ key: 'c' }, 'mac')).toBe(false);
    expect(hasAccel({ key: 'c' }, 'other')).toBe(false);
  });
});

describe('resolveShortcut — bindings shared by both platforms', () => {
  const shared: readonly { key: string; shift: boolean; action: FilesAction }[] = [
    { key: 'c', shift: false, action: 'copy' },
    { key: 'x', shift: false, action: 'cut' },
    { key: 'v', shift: false, action: 'paste' },
    { key: 'z', shift: false, action: 'undo' },
    { key: 'z', shift: true, action: 'redo' },
    { key: 'n', shift: true, action: 'new-folder' },
    { key: 'f', shift: false, action: 'search-folder' },
    { key: 'f', shift: true, action: 'search-drive' },
    { key: 'ArrowDown', shift: false, action: 'open' },
    { key: 'ArrowUp', shift: false, action: 'open-parent' },
  ];

  for (const platform of PLATFORMS) {
    for (const { key, shift, action } of shared) {
      it(`${platform}: accel${shift ? '+shift' : ''}+${key} is ${action}`, () => {
        expect(resolveShortcut(accel(platform, { key, shiftKey: shift }), platform)).toBe(action);
      });

      it(`${platform}: ${key} without the command modifier is not ${action}`, () => {
        expect(resolveShortcut({ key, shiftKey: shift }, platform)).not.toBe(action);
      });

      it(`${platform}: the other platform's modifier does not stand in for ${action}`, () => {
        expect(resolveShortcut(foreignAccel(platform, { key, shiftKey: shift }), platform)).toBeNull();
      });

      it(`${platform}: Alt held blocks ${action}`, () => {
        expect(
          resolveShortcut({ ...accel(platform, { key, shiftKey: shift }), altKey: true }, platform),
        ).toBeNull();
      });
    }
  }

  it('mac: Cmd+Shift+Z is redo, not undo', () => {
    expect(resolveShortcut({ key: 'z', metaKey: true, shiftKey: true }, 'mac')).toBe('redo');
    expect(resolveShortcut({ key: 'z', metaKey: true }, 'mac')).toBe('undo');
  });

  it('other: Ctrl+Shift+F searches the drive and Ctrl+F the folder', () => {
    expect(resolveShortcut({ key: 'f', ctrlKey: true, shiftKey: true }, 'other')).toBe(
      'search-drive',
    );
    expect(resolveShortcut({ key: 'f', ctrlKey: true }, 'other')).toBe('search-folder');
  });

  it('an uppercase key from a held Shift still matches', () => {
    expect(resolveShortcut({ key: 'N', ctrlKey: true, shiftKey: true }, 'other')).toBe('new-folder');
  });
});

describe('resolveShortcut — where the platforms differ', () => {
  it('mac: Enter renames', () => {
    expect(resolveShortcut({ key: 'Enter' }, 'mac')).toBe('rename');
  });

  it('other: Enter opens', () => {
    expect(resolveShortcut({ key: 'Enter' }, 'other')).toBe('open');
  });

  it('F2 renames on both platforms', () => {
    expect(resolveShortcut({ key: 'F2' }, 'mac')).toBe('rename');
    expect(resolveShortcut({ key: 'F2' }, 'other')).toBe('rename');
  });

  it('mac: Cmd+Down opens, since Enter is taken by rename', () => {
    expect(resolveShortcut({ key: 'ArrowDown', metaKey: true }, 'mac')).toBe('open');
  });

  it('mac: Cmd+Backspace trashes', () => {
    expect(resolveShortcut({ key: 'Backspace', metaKey: true }, 'mac')).toBe('trash');
  });

  it('mac: a bare Backspace does not trash', () => {
    expect(resolveShortcut({ key: 'Backspace' }, 'mac')).toBeNull();
  });

  it('mac: the forward-delete key trashes, as Cmd+Backspace does', () => {
    expect(resolveShortcut({ key: 'Delete' }, 'mac')).toBe('trash');
  });

  it('mac: Cmd+Backspace stays the caption the menu shows', () => {
    expect(shortcutLabel('trash', 'mac')).toBe('⌘⌫');
  });

  it('other: Delete and Backspace both trash', () => {
    expect(resolveShortcut({ key: 'Delete' }, 'other')).toBe('trash');
    expect(resolveShortcut({ key: 'Backspace' }, 'other')).toBe('trash');
  });

  it('other: Ctrl+Backspace is not the trash binding', () => {
    expect(resolveShortcut({ key: 'Backspace', ctrlKey: true }, 'other')).toBeNull();
  });
});

describe('resolveShortcut — treegrid navigation and quick look', () => {
  it.each(PLATFORMS)('%s: bare arrows, Home and End move the focus', (platform) => {
    expect(resolveShortcut({ key: 'ArrowDown' }, platform)).toBe('focus-next');
    expect(resolveShortcut({ key: 'ArrowUp' }, platform)).toBe('focus-prev');
    expect(resolveShortcut({ key: 'Home' }, platform)).toBe('focus-first');
    expect(resolveShortcut({ key: 'End' }, platform)).toBe('focus-last');
  });

  it.each(PLATFORMS)('%s: Space is quick look and accel+Space is not', (platform) => {
    expect(resolveShortcut({ key: ' ' }, platform)).toBe('quick-look');
    expect(resolveShortcut(accel(platform, { key: ' ' }), platform)).toBeNull();
  });

  it.each(PLATFORMS)('%s: an unbound key resolves to nothing', (platform) => {
    expect(resolveShortcut({ key: 'q' }, platform)).toBeNull();
    expect(resolveShortcut({ key: 'Tab' }, platform)).toBeNull();
  });
});

describe('the shortcut table itself', () => {
  it('resolves every binding on the platform it belongs to', () => {
    for (const platform of PLATFORMS) {
      for (const binding of bindingsFor(platform)) {
        const event: KeyEventLike = binding.accel
          ? accel(platform, { key: binding.key, shiftKey: binding.shift })
          : { key: binding.key, shiftKey: binding.shift };
        expect(resolveShortcut(event, platform)).toBe(binding.action);
      }
    }
  });

  it('never binds the same chord to two actions on one platform', () => {
    for (const platform of PLATFORMS) {
      const chords = bindingsFor(platform).map(
        (b) => `${b.key.toLowerCase()}|${String(b.accel)}|${String(b.shift)}`,
      );
      expect(new Set(chords).size).toBe(chords.length);
    }
  });

  it('keeps a mac-only binding out of the Windows and Linux table', () => {
    expect(bindingsFor('other').some((b) => b.platform === 'mac')).toBe(false);
    expect(SHORTCUT_BINDINGS.some((b) => b.platform === 'mac')).toBe(true);
  });
});

describe('shortcutLabel — what the context menu shows', () => {
  it.each([
    { action: 'copy' as const, mac: '⌘C', other: 'Ctrl+C' },
    { action: 'new-folder' as const, mac: '⇧⌘N', other: 'Ctrl+Shift+N' },
    { action: 'search-drive' as const, mac: '⇧⌘F', other: 'Ctrl+Shift+F' },
    { action: 'quick-look' as const, mac: 'Space', other: 'Space' },
  ])('$action reads $mac on macOS and $other elsewhere', ({ action, mac, other }) => {
    expect(shortcutLabel(action, 'mac')).toBe(mac);
    expect(shortcutLabel(action, 'other')).toBe(other);
  });

  it('rename and trash read differently per platform', () => {
    expect(shortcutLabel('rename', 'mac')).toBe('↩');
    expect(shortcutLabel('rename', 'other')).toBe('F2');
    expect(shortcutLabel('trash', 'mac')).toBe('⌘⌫');
    expect(shortcutLabel('trash', 'other')).toBe('Delete');
  });

  it('every action reachable on a platform has a label there', () => {
    for (const platform of PLATFORMS) {
      for (const binding of bindingsFor(platform)) {
        expect(shortcutLabel(binding.action, platform)).not.toBeNull();
      }
    }
  });
});

/** Pin what `detectPlatform` reads, one signal at a time. */
function stubNavigator(over: {
  uaDataPlatform?: string | undefined;
  platform?: string;
  userAgent?: string;
}): void {
  Object.defineProperty(window.navigator, 'userAgentData', {
    value: over.uaDataPlatform === undefined ? undefined : { platform: over.uaDataPlatform },
    configurable: true,
  });
  Object.defineProperty(window.navigator, 'platform', {
    value: over.platform ?? '',
    configurable: true,
  });
  Object.defineProperty(window.navigator, 'userAgent', {
    value: over.userAgent ?? 'Mozilla/5.0 (X11; Linux x86_64)',
    configurable: true,
  });
}

describe('which platform the accelerator is read on', () => {
  const real = {
    uaData: Object.getOwnPropertyDescriptor(window.navigator, 'userAgentData'),
    platform: Object.getOwnPropertyDescriptor(window.navigator, 'platform'),
    userAgent: Object.getOwnPropertyDescriptor(window.navigator, 'userAgent'),
  };

  afterEach(() => {
    for (const [name, descriptor] of [
      ['userAgentData', real.uaData],
      ['platform', real.platform],
      ['userAgent', real.userAgent],
    ] as const) {
      if (descriptor === undefined) delete (window.navigator as unknown as Record<string, unknown>)[name];
      else Object.defineProperty(window.navigator, name, descriptor);
    }
  });

  it('reads the modern signal', () => {
    stubNavigator({ uaDataPlatform: 'macOS' });
    expect(detectPlatform()).toBe('mac');
  });

  it('reads the older one when there is no modern signal', () => {
    stubNavigator({ platform: 'MacIntel' });
    expect(detectPlatform()).toBe('mac');
  });

  it('falls back to the user-agent string when neither platform field says anything', () => {
    stubNavigator({ userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)' });
    expect(detectPlatform()).toBe('mac');
  });

  it('is not shadowed by a modern signal it does not recognise', () => {
    // A hint the table cannot read is not evidence this is NOT a Mac. Taking the
    // first signal present and stopping there bound the whole Files browser to
    // Ctrl on a Mac: Cmd+A selected nothing, Cmd+click replaced the selection
    // instead of toggling, and the row menu advertised Ctrl+C.
    stubNavigator({
      uaDataPlatform: 'Unknown',
      platform: 'MacIntel',
      userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)',
    });
    expect(detectPlatform()).toBe('mac');
  });

  it('leaves a machine that is not a Mac on Ctrl', () => {
    stubNavigator({
      uaDataPlatform: 'Windows',
      platform: 'Win32',
      userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
    });
    expect(detectPlatform()).toBe('other');
  });
});
