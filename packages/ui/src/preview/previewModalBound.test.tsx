// The dialog a file is previewed in is bounded by the VIEWPORT, and the preview
// inside it is what scrolls.
//
// A bound like `min(94vh, 100%)` does not work: that `100%` is a percentage of
// the scrim's auto-sized grid row, whose height is decided by this very box, so
// it is cyclic, resolves as `auto`, and takes the whole `min()` with it. A long
// file then grows the dialog past the screen and, because the dialog grows
// rather than its body, the content runs off the bottom with no scrollbar. The
// bound belongs to the wrapper, not any renderer, so it is pinned once, here.
//
// jsdom lays nothing out and applies no stylesheet, so this is two halves: the
// structure the components build, and the stylesheet's own promise about it.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { Modal } from "../primitives/overlays/Modal/Modal";

import { PreviewSurface } from "./PreviewSurface";
import type { PreviewContent, PreviewFacts, PreviewProps } from "./types";

afterEach(cleanup);

const MODAL_CSS = readFileSync(
  resolve(__dirname, "../primitives/overlays/Modal/modal.css"),
  "utf8",
);

/** Everything inside an at-rule (`@media`, `@supports`) removed, braces counted
 *  so a nested block cannot end the scan early.
 *
 *  Without this the narrow-window override of the SAME selector merges over the
 *  unconditional rule and answers for it — which would have let this file pass
 *  against the very bug it exists to catch. */
function withoutAtRules(css: string): string {
  let out = "";
  let index = 0;
  while (index < css.length) {
    if (css[index] !== "@") {
      out += css[index];
      index += 1;
      continue;
    }
    const open = css.indexOf("{", index);
    if (open === -1) {
      out += css.slice(index);
      break;
    }
    let depth = 0;
    let scan = open;
    for (; scan < css.length; scan += 1) {
      if (css[scan] === "{") depth += 1;
      else if (css[scan] === "}") {
        depth -= 1;
        if (depth === 0) break;
      }
    }
    index = scan + 1;
  }
  return out;
}

/** The declarations a selector carries unconditionally, comments stripped and
 *  blocks merged in file order — the way the browser resolves them. */
function declarationsFor(css: string, selector: string): Record<string, string> {
  const out: Record<string, string> = {};
  const source = withoutAtRules(css.replace(/\/\*[\s\S]*?\*\//g, ""));
  for (const [, heads, body] of source.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    if (
      !heads
        .split(",")
        .map((head) => head.trim())
        .includes(selector)
    )
      continue;
    for (const declaration of body.split(";")) {
      const colon = declaration.indexOf(":");
      if (colon === -1) continue;
      out[declaration.slice(0, colon).trim()] = declaration.slice(colon + 1).trim();
    }
  }
  return out;
}

function props(facts: PreviewFacts, content: PreviewContent): PreviewProps {
  return { facts, content, version: "etag-1", status: "ready", onDownload: () => {} };
}

/** Preview kinds, each long enough that an unbounded dialog would
 *  run off the screen. The scroll region is the renderer's own. */
const KINDS = [
  {
    name: "a note",
    file: { mime: "text/markdown", name: "TEMPLATE.md", size: 4_096 },
    text: "# Title\n\n".concat("A paragraph of the note.\n\n".repeat(400)),
    scroller: ".alk-preview-markdown",
  },
  {
    name: "a plain-text file",
    file: { mime: "text/plain", name: "check-bars.txt", size: 450 },
    text: "a bar of text\n".repeat(400),
    scroller: ".alk-preview-text__body",
  },
  {
    name: "source",
    file: { mime: "text/plain", name: "build-dashboard.py", size: 21_600 },
    text: "import os\n".repeat(600),
    scroller: ".alk-preview-code",
  },
  {
    name: "a spreadsheet",
    file: { mime: "text/csv", name: "geo-panel-facts.csv", size: 15_872 },
    text: "panel,label,v1\n".concat(
      Array.from({ length: 297 }, (_row, index) => `p${index},l${index},${index}`).join("\n"),
    ),
    scroller: ".alk-datatable__wrap",
  },
] as const;

describe("the dialog a file is previewed in", () => {
  it("is anchored to the viewport, not to whatever is behind it", () => {
    const scrim = declarationsFor(MODAL_CSS, ".alk-modal-scrim");
    expect(scrim["position"], "the scrim is not viewport-anchored").toBe("fixed");
    expect(scrim["inset"]).toBe("0");
    // An oversized dialog centred in a scrim that cannot scroll puts its title
    // out of reach above the top edge.
    expect(scrim["overflow"], "an oversized dialog would be unreachable").toBe("auto");
  });

  it.each([
    ['.alk-modal[data-size="full"]', "height"],
    ['.alk-modal[data-size="full"]', "max-height"],
  ])("bounds %s's %s against the viewport alone", (selector, property) => {
    const rule = declarationsFor(MODAL_CSS, selector);
    const value = rule[property];
    expect(value, `${selector} declares no ${property}`).toBeDefined();
    // A percentage here is a percentage of a box sized BY this
    // box, so it resolves as `auto` and the bound silently disappears.
    expect(value).not.toMatch(/%/);
    expect(value, `${property} is not measured against the viewport`).toMatch(/dvh|svh|vh/);
  });

  it("keeps the base bound for every other size", () => {
    const base = declarationsFor(MODAL_CSS, ".alk-modal");
    expect(base["max-height"]).toBe("calc(100dvh - 2 * var(--alkSpace6))");
    expect(base["display"]).toBe("flex");
    expect(base["flex-direction"]).toBe("column");
  });

  it("gives the body the room left over and lets it shrink to nothing", () => {
    const body = declarationsFor(MODAL_CSS, ".alk-modal__body");
    expect(body["flex"]).toBe("1 1 auto");
    // Without this the body floors at its content and pushes the dialog past
    // its own max-height instead of scrolling inside it.
    expect(body["min-height"]).toBe("0");
  });
});

describe.each(KINDS.map((kind) => [kind.name, kind] as const))(
  "%s opened in the preview dialog",
  (_name, kind) => {
    function open() {
      return render(
        <Modal open onClose={() => {}} title={kind.file.name} size="full" className="files-preview">
          <div className="files-preview__body">
            <PreviewSurface {...props(kind.file, { kind: "text", text: kind.text })} />
          </div>
        </Modal>,
      );
    }

    it("puts its scroll region inside the dialog's body", () => {
      open();
      const dialog = document.querySelector('.alk-modal[data-size="full"]');
      expect(dialog, "the preview did not open at the full size").not.toBeNull();
      const body = dialog?.querySelector(".alk-modal__body");
      expect(body, "the dialog has no body").not.toBeNull();
      const scroller = body?.querySelector(kind.scroller);
      expect(scroller, `${kind.scroller} is not inside the dialog's body`).not.toBeNull();
    });

    it("draws the header outside that scroll region, so it cannot scroll away", () => {
      open();
      const head = document.querySelector(".alk-modal__head");
      const scroller = document.querySelector(kind.scroller);
      expect(head, "the dialog has no header").not.toBeNull();
      expect(scroller).not.toBeNull();
      expect(head?.contains(scroller!), "the header is inside the scroller").toBe(false);
      expect(scroller?.contains(head!), "the scroller swallowed the header").toBe(false);
    });
  },
);
