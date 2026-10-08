// The chat pane is sized by its PARENT, and only the transcript scrolls.
//
// The browser portal seats the surface UNDER a page masthead, so a root that
// declared `height: 100vh` would overflow the page by the header's height and
// push the composer's bottom edge off screen.
//
// The contract that keeps both hosts honest is structural, and it lives in the
// stylesheets: the surface states no viewport height at all, every box between
// the host's height and the transcript is a flex column that may shrink
// (`min-height: 0`), and exactly one box scrolls. jsdom does not lay out or
// cascade CSS — it can measure none of this — so the check reads the sheets and
// pins the declarations the layout depends on.

import { readdirSync, readFileSync } from "node:fs";
import { join, resolve } from "node:path";

import { describe, expect, it } from "vitest";

const WEB = process.cwd();
const UI_SRC = resolve(WEB, "../../packages/ui/src");

/** A sheet with its comments stripped — the prose explains the trap it avoids
 *  (\"never 100vh here\"), so only the declarations may be searched for one. */
const read = (path: string): string => readFileSync(path, "utf8").replace(/\/\*[\s\S]*?\*\//g, "");

/** The declarations of one rule, by its exact selector. */
function block(css: string, selector: string): string {
  const start = css.indexOf(`\n${selector} {`);
  expect(start, `no rule for \`${selector}\``).toBeGreaterThan(-1);
  const open = css.indexOf("{", start);
  const close = css.indexOf("}", open);
  return css.slice(open + 1, close);
}

const surfaceCss = read(join(WEB, "src/pages/workspace/chat/chat-surface.css"));
const pageCss = read(join(WEB, "src/pages/workspace/chat/chat-page.css"));
const panelCss = read(join(UI_SRC, "chat/panel/panel.css"));
const webviewCss = read(join(WEB, "src/webview/ide-surface.css"));

describe("the chat surface takes its height from its host", () => {
  it("states no viewport height of its own", () => {
    expect(surfaceCss).not.toMatch(/\d+(vh|dvh|svh|lvh)/);
  });

  it("fills the parent as a column that may shrink", () => {
    const shell = block(surfaceCss, ".chat-shell-fill");
    expect(shell).toMatch(/height:\s*100%/);
    expect(shell).toMatch(/min-height:\s*0/);
    expect(shell).toMatch(/flex-direction:\s*column/);
  });

  it("lets the panel shrink below its content", () => {
    expect(block(surfaceCss, ".chat-shell-fill > .chat-panel")).toMatch(/min-height:\s*0/);
  });
});

describe("the portal page hands the surface a bounded height", () => {
  it("fills the routed content box rather than the viewport", () => {
    const page = block(pageCss, ".chat-page");
    expect(page).toMatch(/height:\s*100%/);
    expect(page).toMatch(/min-height:\s*0/);
    expect(pageCss).not.toMatch(/\d+(vh|dvh|svh|lvh)/);
  });

  it("keeps the surface cell shrinkable", () => {
    expect(block(pageCss, ".chat-page__surface")).toMatch(/min-height:\s*0/);
  });

  // The files pane is the second thing the page now hands a height to, and it
  // is the one most likely to be given a tall child: a PDF, a full-page HTML
  // report, an image at 1:1. If this column cannot shrink below its content the
  // whole page grows to fit it and the composer goes off the bottom — the same
  // failure as a `100vh`, arriving through a different door.
  it("keeps the files column shrinkable too", () => {
    const pane = block(pageCss, ".chat-page__workspace");
    expect(pane).toMatch(/min-height:\s*0/);
    expect(pane).toMatch(/flex-direction:\s*column/);
  });
});

describe("the workspace sheets measure against their pane, never the window", () => {
  const dir = join(WEB, "src/pages/workspace/chat/workspace");
  const sheets = readdirSync(dir).filter((name) => name.endsWith(".css"));

  it("has sheets to check", () => {
    // A rename that emptied the directory would otherwise make the check below
    // pass by having nothing to look at.
    expect(sheets.length).toBeGreaterThan(0);
  });

  it.each(sheets)("%s states no viewport length", (name) => {
    expect(read(join(dir, name))).not.toMatch(/\d+(vh|dvh|svh|lvh)/);
  });

  it("gives the pane and its panel a floor of nothing", () => {
    const workspaceCss = read(join(dir, "workspace.css"));
    expect(block(workspaceCss, ".alk-ws")).toMatch(/min-height:\s*0/);
    expect(block(workspaceCss, ".alk-ws__panel")).toMatch(/min-height:\s*0/);
  });
});

describe("only the transcript scrolls", () => {
  it("the panel is a full-height column", () => {
    const panel = block(panelCss, ".chat-root .chat-panel");
    expect(panel).toMatch(/height:\s*100%/);
    expect(panel).toMatch(/flex-direction:\s*column/);
    expect(panel).not.toMatch(/overflow/);
  });

  it("the tape is the one scroll container, and it may shrink", () => {
    const scroll = block(panelCss, ".chat-root .chat-scroll");
    expect(scroll).toMatch(/flex:\s*1/);
    expect(scroll).toMatch(/min-height:\s*0/);
    expect(scroll).toMatch(/overflow-y:\s*auto/);
  });
});

describe("the webview states the height the surface fills", () => {
  it("puts the viewport height on the webview root, not inside the surface", () => {
    const root = block(webviewCss, ".idew-root");
    expect(root).toMatch(/height:\s*100dvh/);
    expect(root).toMatch(/min-height:\s*0/);
    expect(block(webviewCss, ".idew-root > *")).toMatch(/min-height:\s*0/);
  });

  it("stamps that root on the element the surfaces mount into", () => {
    expect(read(join(WEB, "src/webview/vscodeApp.tsx"))).toContain('className="idew-root"');
  });
});
