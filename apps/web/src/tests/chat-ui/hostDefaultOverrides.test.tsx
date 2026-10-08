// The VS Code webview injects a default stylesheet that paints every `code`
// element with the editor's preformat ground + padding and grounds every
// `blockquote` — so a fence rendered inside the webview boxed each line (the
// fence-wash defect) until prose.css neutralized the host default, and the
// path band's button variant re-inherited the body font size through the
// button's font-shorthand reset until shared.css restated the band metric.
//
// COMPUTED-STYLE form: the real package stylesheets are loaded into jsdom
// beside a simulated host-default sheet (literal sentinel values standing in
// for the --vscode-* vars, loaded FIRST like the real webview loads its
// defaults), the real components render, and getComputedStyle must show the
// package rules winning. Two limits of this environment, both verified here:
// jsdom does not resolve inherit keywords or var() to final values (those
// assertions pin the winning declaration instead), and jsdom cascades
// conflicting declarations by SHEET ORDER, ignoring specificity — so this
// suite proves the win under the real webview's actual load order only. The
// specificity win that holds under any order is a browser fact outside
// jsdom's reach; the on-screen proof was the round-8 EDH pixel pass.

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { PathBand, Prose } from "@alkera/ui";

const CHAT_SRC = resolve(dirname(fileURLToPath(import.meta.url)), "../../../../../packages/ui/src/chat");
const BASE_CSS = readFileSync(resolve(CHAT_SRC, "theme/base.css"), "utf8");
const SHARED_CSS = readFileSync(resolve(CHAT_SRC, "tools/shared.css"), "utf8");

// The shape of VS Code's injected webview defaults for code/blockquote/button,
// with an opaque sentinel where the host puts its theme vars — a package rule
// that loses the cascade leaves the sentinel visible. The stand-in declares
// every property the package neutralization resets (ground, padding, ink,
// mono, size), so a dropped reset line surfaces as its sentinel. The color is
// arbitrary and deliberately matches nothing in any real sheet.
const HOST_SENTINEL = "rgb(250, 1, 2)";
const HOST_MONO = "HostCodeMono";
const HOST_DEFAULT_CSS = [
  "body { font-size: 14px; }", // pins-source: hand-written stand-in for VS Code's injected webview defaults (the fixture this suite exists to beat); nothing importable ships them
  `code { background-color: ${HOST_SENTINEL}; color: ${HOST_SENTINEL}; font-family: ${HOST_MONO}; padding: 1px 3px; border-radius: 4px; font-size: 14px; }`, // pins-source: the host's code default, same stand-in
  `blockquote { background: ${HOST_SENTINEL}; }`,
  "button { font-size: 13px; }",
].join("\n");

const styles: HTMLStyleElement[] = [];

function inject(css: string): void {
  const el = document.createElement("style");
  el.textContent = css;
  document.head.appendChild(el);
  styles.push(el);
}

beforeEach(() => {
  // Host defaults FIRST — how the real webview loads them (default sheet in
  // <head>, the bundle's styles after). The order is load-bearing here: jsdom
  // resolves the conflict by order, so a reversed injection would flip these
  // results even though a real browser's specificity would not.
  inject(HOST_DEFAULT_CSS);
  inject(BASE_CSS);
  inject(SHARED_CSS);
});

afterEach(() => {
  cleanup();
  for (const el of styles.splice(0)) el.remove();
});

const style = (el: Element): CSSStyleDeclaration => window.getComputedStyle(el);

function renderProse(): { code: Element; quote: Element } {
  const markdown = "> paper, not a box\n\n```python\nx = 1\n```\n";
  const { container } = render(
    <div className="chat-root">
      <Prose content={markdown} />
    </div>,
  );
  const code = container.querySelector("pre code");
  const quote = container.querySelector("blockquote");
  if (!code || !quote) throw new Error("the fence or quote did not render");
  return { code, quote };
}

function renderBands(): { button: Element; label: Element } {
  const { container } = render(
    <div className="chat-root">
      <div data-tool="write">
        <PathBand path="/repo/src/fib.py" onOpen={() => {}} />
        <PathBand path="/repo/src/fib.py" />
      </div>
    </div>,
  );
  const button = container.querySelector("button.chat-tool-band--path"); // pins-source: the markup-to-stylesheet class contract under test (shared.tsx / shared.css)
  const label = container.querySelector("div.chat-tool-band--path"); // pins-source: same contract
  if (!button || !label) throw new Error("the band variants did not render");
  return { button, label };
}

describe("fence + quote host-default neutralization (theme/base.css)", () => {
  it("strips the host's code box for the fence's own metric", () => {
    const { code } = renderProse();
    // The package's background reset beats the host's opaque preformat ground.
    expect(style(code).backgroundColor).toBe("rgba(0, 0, 0, 0)"); // pins-source: CSSOM's frozen serialization of transparent
    // The host's 1px 3px box dies; the fence's own padding lives on the <pre>.
    expect(style(code).paddingTop).toBe("0px");
    expect(style(code).paddingRight).toBe("0px");
    expect(style(code).paddingBottom).toBe("0px");
    expect(style(code).paddingLeft).toBe("0px");
    // jsdom reports the winning DECLARATION for a keyword: the package's
    // inherit (the fence's own size, ink, and mono in a browser) -- never the
    // host's 14px, preformat ink, or editor mono.
    expect(style(code).fontSize).toBe("inherit");
    expect(style(code).color).toBe("inherit");
    expect(style(code).fontFamily).toBe("inherit");
  });

  it("keeps the quote on bare paper", () => {
    const { quote } = renderProse();
    expect(style(quote).backgroundColor).toBe("rgba(0, 0, 0, 0)"); // pins-source: CSSOM's frozen serialization of transparent
  });
});

describe("path-band button metric (shared.css)", () => {
  it("matches the label variant over its font-shorthand reset", () => {
    const { button, label } = renderBands();
    const labelSize = style(label).fontSize;
    const labelLine = style(label).lineHeight;
    // Guard first: an unparsed stylesheet would leave both values empty and a
    // bare equality green.
    expect(labelSize).not.toBe("");
    expect(labelLine).not.toBe("");
    // The defect this catches: the button's font-shorthand reset outranked the
    // band's font-size, so the button band sat a step above the label band.
    expect(style(button).fontSize).toBe(labelSize);
    expect(style(button).fontSize).not.toBe("13px");
    expect(style(button).fontSize).not.toBe("14px");
    expect(style(button).lineHeight).toBe(labelLine);
  });
});
