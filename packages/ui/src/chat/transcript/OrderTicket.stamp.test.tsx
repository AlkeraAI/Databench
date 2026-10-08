// The time under the reader's own message: present, but out of the way.
//
// The stamp is the one thing on a user turn nobody came back for, so it holds
// its space and stays invisible until the pointer — or the keyboard, which has
// no pointer to lend — reaches the message it belongs to.
//
// Two of the three facts that makes true are invisible to a markup assertion,
// so this reads the real cascade instead: the sheets injected in the order the
// bundle emits them (a component's own stylesheet rides its import, the theme's
// base layer arrives after), which is the order in which the `.chat-num` mono
// utility gets the last word on any rule that merely ties with it.

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";

import { render } from "@testing-library/react";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { OrderTicket } from "./OrderTicket";
import { TranscriptBlock } from "./TranscriptBlock";

const AT = "12:04 AM";

/** Read a stylesheet by its path relative to this test. vitest's CSS pipeline
 *  empties `?raw` imports, so the test's own path is the anchor. */
function readCss(...segments: string[]): string {
  const testPath = expect.getState().testPath;
  if (!testPath) throw new Error("vitest testPath unavailable, cannot locate the stylesheet");
  return readFileSync(join(dirname(testPath), ...segments), "utf8");
}

/** Every rule in the injected cascade, flattened out of its media block. */
function rules(sheet: CSSStyleSheet): CSSStyleRule[] {
  const found: CSSStyleRule[] = [];
  const walk = (list: CSSRuleList): void => {
    for (const rule of Array.from(list)) {
      if (rule instanceof CSSStyleRule) found.push(rule);
      else if (rule instanceof CSSMediaRule) walk(rule.cssRules);
    }
  };
  walk(sheet.cssRules);
  return found;
}

/** A selector's weight, the three counts folded into one comparable number:
 *  ids, then classes/attributes/pseudo-classes, then element names. */
function specificity(selector: string): number {
  const count = (pattern: RegExp): number => (selector.match(pattern) ?? []).length;
  const classes = count(/\.[\w-]+|\[[^\]]+\]|:(?!:)[\w-]+/g);
  return count(/#[\w-]+/g) * 10000 + classes * 100 + count(/(^|[\s>+~])[a-z][\w-]*/gi);
}

function mount(pending?: boolean): HTMLElement {
  const { container } = render(
    <div className="chat-root">
      <TranscriptBlock kind="user">
        <OrderTicket text="Refactor the chat UX packages." at={AT} pending={pending} />
      </TranscriptBlock>
    </div>,
  );
  return container;
}

describe("the time under a user message", () => {
  let sheet: HTMLStyleElement;
  beforeAll(() => {
    sheet = document.createElement("style");
    // Bundle order: the component's own sheet (pulled by its import) precedes
    // the theme layer the host imports as `@alkera/ui/chat/styles`.
    sheet.textContent = readCss("transcript.css") + readCss("..", "theme", "base.css");
    document.head.append(sheet);
  });
  afterAll(() => sheet.remove());

  const stamp = (container: HTMLElement): HTMLElement =>
    container.querySelector(".chat-ticket__at") as HTMLElement;

  /** The one rule in the cascade that sizes what `selector` names. A `var()`
   *  value never reaches cssstyle's typed properties, so this reads cssText. */
  const sizeRule = (selector: string): CSSStyleRule => {
    const found = rules(sheet.sheet as CSSStyleSheet).find(
      (rule) => rule.selectorText.includes(selector) && rule.cssText.includes("font-size:"),
    );
    if (!found) throw new Error(`no rule sizes ${selector}`);
    return found;
  };

  it("is in the document, carrying the time, whether or not anyone is looking", () => {
    // Invisible is not absent: the stamp stays readable to a screen reader and
    // to anything that reads the transcript's text.
    expect(stamp(mount())).toHaveTextContent(AT);
  });

  it("is hidden until the message is reached, without giving up its space", () => {
    const at = getComputedStyle(stamp(mount()));
    expect(at.opacity, "the stamp starts invisible").toBe("0");
    // display:none / visibility:hidden would both do it — and both would move
    // the tape the instant a pointer crossed a message.
    expect(at.display).not.toBe("none");
    expect(at.visibility).not.toBe("hidden");
  });

  it("comes back for a pointer on the message and for focus inside it", () => {
    // jsdom simulates neither, so the reveal is read off the cascade: both
    // states, on the message's own box rather than the full-width block, and
    // both restoring the stamp rather than only one of them.
    const revealers = rules(sheet.sheet as CSSStyleSheet)
      .filter((rule) => rule.selectorText.includes(".chat-ticket__at") && rule.style.opacity === "1")
      .map((rule) => rule.selectorText)
      .join(" ");
    expect(revealers).toContain(".chat-ticket-wrap:hover");
    expect(revealers).toContain(".chat-ticket-wrap:focus-within");
  });

  it("is stamped a step under the mono utility it also carries", () => {
    // `.chat-num` gives the stamp tabular figures AND `--chat-fs-mono`, which
    // reads at the size of the message itself. The record step is the notch
    // below it, and it only lands if the stamp's rule OUTRANKS that utility —
    // a tie loses, base.css being emitted last. jsdom resolves a cascade by
    // document order alone, so the ranking is read off the sheet rather than
    // off the element.
    const sized = sizeRule(".chat-ticket__at");
    expect(sized.cssText).toContain("font-size: var(--chat-fs-record)");
    expect(specificity(sized.selectorText)).toBeGreaterThan(specificity(sizeRule(".chat-num").selectorText));
  });

  it("does not animate where motion is refused", () => {
    const reduced = rules(sheet.sheet as CSSStyleSheet).some(
      (rule) =>
        rule.parentRule instanceof CSSMediaRule &&
        rule.parentRule.conditionText.includes("prefers-reduced-motion") &&
        rule.selectorText.includes(".chat-ticket__at") &&
        rule.style.transition === "none",
    );
    expect(reduced).toBe(true);
  });

  it("carries no stamp at all while the message is still going out", () => {
    // A time the turn never started at would be a lie, hover or no hover.
    expect(stamp(mount(true))).toBeNull();
  });
});
