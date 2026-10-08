// A file WIDER than the screen must still open on the screen.
//
// The dialog is a grid item in the scrim, and a grid item's automatic minimum
// size is its CONTENT's min-content width, which beats the `width` the dialog
// asks for. Unbounded, a wide table widens the dialog, the scrim's auto-sized
// column grows with it, and `place-items: center` puts the item's centre far
// off screen.
//
// The dialog is bounded by the VIEWPORT and the table scrolls inside it. Both
// halves are pinned: jsdom lays nothing out and applies no stylesheet, so the
// structure is read from the rendered DOM and the bound is read from the CSS.

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
const DATATABLE_CSS = readFileSync(
  resolve(__dirname, "../resources/DataTable/datatable.css"),
  "utf8",
);

/** Everything inside an at-rule removed, braces counted so a nested block cannot
 *  end the scan early — a narrow-window override of the same selector must not
 *  answer for the unconditional rule. */
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

/** 40 columns and enough rows to matter — the shape that put the dialog off the
 *  screen. Each cell is long enough that no browser could fit the row. */
const COLUMNS = 40;
const WIDE_CSV = [
  Array.from({ length: COLUMNS }, (_c, index) => `measure_column_${index}`).join(","),
  ...Array.from({ length: 60 }, (_r, row) =>
    Array.from({ length: COLUMNS }, (_c, index) => `value-${row}-${index}-padded`).join(","),
  ),
].join("\n");

const FACTS: PreviewFacts = { mime: "text/csv", name: "qa-wide.csv", size: 1_700_000 };

function props(content: PreviewContent): PreviewProps {
  return { facts: FACTS, content, version: "etag-1", status: "ready", onDownload: () => {} };
}

function openWideCsv() {
  return render(
    <Modal open onClose={() => {}} title={FACTS.name} size="full" className="files-preview">
      <div className="files-preview__body">
        <PreviewSurface {...props({ kind: "text", text: WIDE_CSV })} />
      </div>
    </Modal>,
  );
}

describe("a spreadsheet wider than the screen", () => {
  it("opens inside the scrim that positions it", () => {
    openWideCsv();
    const scrim = document.querySelector(".alk-modal-scrim");
    const dialog = document.querySelector('.alk-modal[data-size="full"]');
    expect(scrim, "the preview did not open").not.toBeNull();
    expect(dialog, "the preview did not open at the full size").not.toBeNull();
    // The scrim is what anchors the dialog to the viewport. A dialog outside it
    // is positioned by the page behind it, which scrolls.
    expect(scrim?.contains(dialog!)).toBe(true);
  });

  it("scrolls the table inside its own container rather than widening the dialog", () => {
    openWideCsv();
    const dialog = document.querySelector('.alk-modal[data-size="full"]');
    const wrap = dialog?.querySelector(".alk-datatable__wrap");
    expect(wrap, "the grid is not inside the dialog").not.toBeNull();
    const rule = declarationsFor(DATATABLE_CSS, ".alk-datatable__wrap");
    // Without a scroller of its own the 40 columns are laid out at full width
    // and the box they are in has to grow to hold them.
    expect(rule["overflow"], "the grid hands its overflow to an ancestor").toBe("auto");
    expect(declarationsFor(DATATABLE_CSS, ".alk-datatable__wrap.is-fill")["overflow"]).toBe("auto");
  });

  it("floors the dialog's minimum width at zero, so its content cannot widen it", () => {
    const dialog = declarationsFor(MODAL_CSS, ".alk-modal");
    // The whole bug. `min-width: auto` on a grid item resolves to the content's
    // min-content width and OVERRIDES `width`, so a wide table pushed the dialog
    // past the scrim and the centring carried it off the screen.
    expect(dialog["min-width"], "the dialog's content can still widen it").toBe("0");
  });

  it("bounds the dialog's width against the viewport it has to fit in", () => {
    const dialog = declarationsFor(MODAL_CSS, ".alk-modal");
    const value = dialog["max-width"];
    expect(value, "the dialog declares no max-width").toBeDefined();
    // A percentage here is a percentage of the scrim's auto-sized grid track — a
    // track sized by this very box — so it cannot bound anything.
    expect(value).not.toMatch(/%/);
    expect(value, "the width is not measured against the viewport").toMatch(/vw/);
  });

  it("keeps that bound clear of the scrim's own gutter", () => {
    const scrim = declarationsFor(MODAL_CSS, ".alk-modal-scrim");
    const dialog = declarationsFor(MODAL_CSS, ".alk-modal");
    // The gutter is one value used twice: the scrim pads by it and the dialog
    // subtracts it. Spelled separately they drift, and the dialog either leaves a
    // stripe of scrim unused or overhangs it by a few pixels.
    expect(scrim["padding"]).toBe("var(--alk-modal-gutter)");
    expect(dialog["max-width"]).toContain("var(--alk-modal-gutter)");
  });
});
