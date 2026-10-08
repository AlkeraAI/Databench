/**
 * Every Files shortcut, pressed on a real element through the page's own hook.
 *
 * `state/shortcuts.test.ts` proves the pure table resolves each chord; this
 * sweep proves the page *binding* — that each resolved action reaches its
 * handler exactly once, that it consumes the keystroke, and the negative twin
 * of each rule that suppresses it (a text field has focus, the page is
 * disabled, an unbound action is left for the browser).
 */

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import {
  SHORTCUT_BINDINGS,
  type FilesAction,
  type Platform,
} from "@/pages/workspace/files/state/shortcuts";
import {
  isTypingTarget,
  useFilesShortcuts,
  type FilesShortcutHandlers,
} from "@/pages/workspace/files/useFilesShortcuts";

const PLATFORMS: readonly Platform[] = ["mac", "other"];

interface HarnessProps {
  readonly platform: Platform;
  readonly handlers: FilesShortcutHandlers;
  readonly enabled?: boolean;
  readonly extra?: React.ReactNode;
}

function Harness({ platform, handlers, enabled, extra }: HarnessProps): React.ReactElement {
  const { onKeyDown } = useFilesShortcuts({ platform, handlers, enabled });
  return (
    <div data-testid="surface" onKeyDown={onKeyDown}>
      <div data-testid="row" tabIndex={0} />
      {extra}
    </div>
  );
}

/** One spy per action in the table, so a chord that fires the wrong one is visible. */
function spyAll(): { handlers: FilesShortcutHandlers; fired: () => FilesAction[] } {
  const calls: FilesAction[] = [];
  const handlers: Record<string, () => void> = {};
  for (const binding of SHORTCUT_BINDINGS) {
    handlers[binding.action] = () => {
      calls.push(binding.action);
    };
  }
  return { handlers: handlers as FilesShortcutHandlers, fired: () => calls };
}

interface Chord {
  readonly key: string;
  readonly metaKey: boolean;
  readonly ctrlKey: boolean;
  readonly shiftKey: boolean;
}

function chordFor(
  binding: (typeof SHORTCUT_BINDINGS)[number],
  platform: Platform,
): Chord {
  return {
    key: binding.key,
    metaKey: binding.accel && platform === "mac",
    ctrlKey: binding.accel && platform !== "mac",
    shiftKey: binding.shift,
  };
}

const CASES = PLATFORMS.flatMap((platform) =>
  SHORTCUT_BINDINGS.filter((b) => b.platform === "all" || b.platform === platform).map(
    (binding) => ({ platform, binding }),
  ),
);

describe("every Files shortcut reaches the page", () => {
  it.each(CASES.map((c) => [`${c.platform}: ${c.binding.action}`, c] as const))(
    "%s fires its handler once and takes the keystroke",
    (_name, { platform, binding }) => {
      const { handlers, fired } = spyAll();
      render(<Harness platform={platform} handlers={handlers} />);
      const consumed = !fireEvent.keyDown(
        screen.getByTestId("surface"),
        chordFor(binding, platform),
      );
      expect(fired()).toEqual([binding.action]);
      expect(consumed).toBe(true);
    },
  );

  it.each(CASES.map((c) => [`${c.platform}: ${c.binding.action}`, c] as const))(
    "%s is left for the browser when the page registers no handler",
    (_name, { platform, binding }) => {
      render(<Harness platform={platform} handlers={{}} />);
      const consumed = !fireEvent.keyDown(
        screen.getByTestId("surface"),
        chordFor(binding, platform),
      );
      expect(consumed).toBe(false);
    },
  );

  it.each(CASES.map((c) => [`${c.platform}: ${c.binding.action}`, c] as const))(
    "%s does nothing while a modal owns the keyboard",
    (_name, { platform, binding }) => {
      const { handlers, fired } = spyAll();
      render(<Harness platform={platform} handlers={handlers} enabled={false} />);
      fireEvent.keyDown(screen.getByTestId("surface"), chordFor(binding, platform));
      expect(fired()).toEqual([]);
    },
  );

  it.each(CASES.map((c) => [`${c.platform}: ${c.binding.action}`, c] as const))(
    "%s belongs to the text field the caller is typing in",
    (_name, { platform, binding }) => {
      const { handlers, fired } = spyAll();
      render(
        <Harness
          platform={platform}
          handlers={handlers}
          extra={<input data-testid="rename" defaultValue="notes.txt" />}
        />,
      );
      fireEvent.keyDown(screen.getByTestId("rename"), chordFor(binding, platform));
      expect(fired()).toEqual([]);
    },
  );

  it("dispatches exactly the actions the table declares, and no others", () => {
    const seen = new Set<FilesAction>();
    for (const { platform, binding } of CASES) {
      const { handlers, fired } = spyAll();
      const view = render(<Harness platform={platform} handlers={handlers} />);
      fireEvent.keyDown(screen.getByTestId("surface"), chordFor(binding, platform));
      for (const action of fired()) seen.add(action);
      view.unmount();
    }
    expect([...seen].sort()).toEqual([...new Set(SHORTCUT_BINDINGS.map((b) => b.action))].sort());
  });
});

describe("which targets count as typing", () => {
  it.each([
    ["a text input", "<input type='text' />", true],
    ["an untyped input", "<input />", true],
    ["a search input", "<input type='search' />", true],
    ["a textarea", "<textarea></textarea>", true],
    ["a select", "<select></select>", true],
    ["a checkbox", "<input type='checkbox' />", false],
    ["a radio", "<input type='radio' />", false],
    ["a submit input", "<input type='submit' />", false],
    ["a button input", "<input type='button' />", false],
    ["a plain button", "<button></button>", false],
    ["a treegrid row", "<div tabindex='0'></div>", false],
  ])("%s: %s", (_label, html, expected) => {
    const host = document.createElement("div");
    host.innerHTML = html;
    expect(isTypingTarget(host.firstElementChild)).toBe(expected);
  });

  it("a contenteditable surface is typing", () => {
    // jsdom does not implement `isContentEditable`, so the flag the browser
    // would compute from the attribute is supplied here instead.
    const editable = document.createElement("div");
    Object.defineProperty(editable, "isContentEditable", { value: true });
    expect(isTypingTarget(editable)).toBe(true);
  });

  it("is false for a target that is not an element at all", () => {
    expect(isTypingTarget(null)).toBe(false);
    expect(isTypingTarget(document)).toBe(false);
  });
});

describe("a chord outside the table", () => {
  it.each(PLATFORMS)("%s: an unbound key fires nothing and is not consumed", (platform) => {
    const { handlers, fired } = spyAll();
    render(<Harness platform={platform} handlers={handlers} />);
    const consumed = !fireEvent.keyDown(screen.getByTestId("surface"), {
      key: "q",
      metaKey: false,
      ctrlKey: false,
      shiftKey: false,
    });
    expect(fired()).toEqual([]);
    expect(consumed).toBe(false);
  });

  it.each(CASES.map((c) => [`${c.platform}: ${c.binding.action}`, c] as const))(
    "%s does not fire with Alt held",
    (_name, { platform, binding }) => {
      const { handlers, fired } = spyAll();
      render(<Harness platform={platform} handlers={handlers} />);
      fireEvent.keyDown(screen.getByTestId("surface"), {
        ...chordFor(binding, platform),
        altKey: true,
      });
      expect(fired()).toEqual([]);
    },
  );

  it.each(CASES.filter((c) => c.binding.accel).map((c) => [`${c.platform}: ${c.binding.action}`, c] as const))(
    "%s does not accept the other platform's command modifier",
    (_name, { platform, binding }) => {
      const { handlers, fired } = spyAll();
      render(<Harness platform={platform} handlers={handlers} />);
      const foreign = chordFor(binding, platform === "mac" ? "other" : "mac");
      fireEvent.keyDown(screen.getByTestId("surface"), { ...foreign, key: binding.key });
      expect(fired()).toEqual([]);
    },
  );
});

describe("the page's own guard rails", () => {
  it("never calls a handler twice for one keystroke", () => {
    const open = vi.fn();
    render(<Harness platform="mac" handlers={{ open }} />);
    fireEvent.keyDown(screen.getByTestId("surface"), { key: "ArrowDown", metaKey: true });
    expect(open).toHaveBeenCalledTimes(1);
  });
});
