// Every preview scrolls inside itself, and nothing in it floats over the file.
//
// Two bugs came out of the same missing fact. A CSV's pager was laid out under a
// box the rows spilled out of, so the bar drew across row 11 with the rows
// showing through it; and a 600-line script had no scroll region at all, so the
// pane's own `overflow: hidden` cut it off at line 50. Both are the renderer
// failing to own a bounded column: a bar that is a flex item cannot land in the
// middle of a table, and a body that is a bounded scroller cannot be cut off.
//
// jsdom lays nothing out and applies no stylesheet, so this is pinned in two
// halves — the structure the component builds, read from the rendered DOM, and
// the stylesheet's own promise about that structure, read from the CSS files.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";

import { DataTable } from "../resources/DataTable";

import { PreviewSurface } from "./PreviewSurface";
import type { PreviewContent, PreviewFacts, PreviewProps } from "./types";

afterEach(cleanup);

const PREVIEW_CSS = readFileSync(resolve(__dirname, "preview.css"), "utf8");
const DATATABLE_CSS = readFileSync(
  resolve(__dirname, "../resources/DataTable/datatable.css"),
  "utf8",
);
const BLOBVIEW_CSS = readFileSync(
  resolve(__dirname, "../resources/BlobView/blobview.css"),
  "utf8",
);
const TOKENS_CSS = readFileSync(resolve(__dirname, "../theme/tokens.css"), "utf8");

/** Everything inside an at-rule (`@media`, `@supports`) removed, braces counted
 *  so a nested block cannot end the scan early. A conditional override of the
 *  same selector must not answer for the unconditional rule. */
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

/** The declarations a selector carries, as `property: value`. Every block whose
 *  selector list names `selector` exactly is merged, in file order, so a rule
 *  split across two blocks reads the way the browser resolves it. */
export function declarationsFor(css: string, selector: string): Record<string, string> {
  const out: Record<string, string> = {};
  // Comments first: they sit between blocks, so a rule's "head" would otherwise
  // carry the paragraph written above it and match nothing. Then the at-rules —
  // a conditional override of the SAME selector would otherwise merge over the
  // unconditional rule and answer for it, which is how a bound that was only
  // right inside a media query could read as right everywhere.
  const source = withoutAtRules(css.replace(/\/\*[\s\S]*?\*\//g, ""));
  const blocks = source.matchAll(/([^{}]+)\{([^{}]*)\}/g);
  for (const [, heads, body] of blocks) {
    const named = heads
      .split(",")
      .map((head) => head.trim())
      .includes(selector);
    if (!named) continue;
    for (const declaration of body.split(";")) {
      const colon = declaration.indexOf(":");
      if (colon === -1) continue;
      out[declaration.slice(0, colon).trim()] = declaration.slice(colon + 1).trim();
    }
  }
  return out;
}

/** A box that takes the height its parent offers and may shrink below its
 *  content — the two halves of "bounded". Without `min-height: 0` a flex item
 *  floors at its content and the scroller below it never gets a height to
 *  scroll within. */
function expectBounded(css: string, selector: string): void {
  const rule = declarationsFor(css, selector);
  expect(rule, `${selector} has no rule at all`).not.toEqual({});
  expect(rule["flex"], `${selector} does not take the height offered`).toBe("1 1 auto");
  expect(rule["min-height"], `${selector} floors at its content`).toBe("0");
}

function facts(over: Partial<PreviewFacts> = {}): PreviewFacts {
  return { mime: "text/plain", name: "file.txt", size: 400, ...over };
}

function props(over: Partial<PreviewProps> = {}): PreviewProps {
  return {
    facts: facts(),
    content: { kind: "text", text: "a,b\n1,2\n" } as PreviewContent,
    version: "etag-1",
    status: "ready",
    onDownload: () => {},
    ...over,
  };
}

/** Rows enough that a page of the grid is taller than any cap — the shape the
 *  pager bug needed to show itself. */
function manyRows(count: number): string {
  const lines = ["panel,label,v1"];
  for (let row = 0; row < count; row += 1) lines.push(`p${row},l${row},${row}`);
  return lines.join("\n");
}

/** Every kind the default registry draws, with the file that routes to it and
 *  the element inside it that is meant to be the scroller. A renderer whose
 *  whole pane is the scroll region names its own root. */
const KINDS = [
  {
    name: "source",
    id: "code",
    facts: facts({ mime: "text/plain", name: "build-dashboard.py", size: 21_600 }),
    content: { kind: "text", text: "import os\n".repeat(600) } as PreviewContent,
    scroller: ".alk-preview-code",
  },
  {
    name: "a note",
    id: "markdown",
    facts: facts({ mime: "text/markdown", name: "notes.md" }),
    content: { kind: "text", text: "# Title\n\nbody\n" } as PreviewContent,
    scroller: ".alk-preview-markdown",
  },
  {
    name: "plain text",
    id: "text",
    facts: facts({ mime: "text/plain", name: "log.txt" }),
    content: { kind: "text", text: "line\n".repeat(400) } as PreviewContent,
    scroller: ".alk-preview-text__body",
  },
  {
    name: "a spreadsheet",
    id: "csv",
    facts: facts({ mime: "text/csv", name: "geo-panel-facts.csv", size: 15_872 }),
    content: { kind: "text", text: manyRows(297) } as PreviewContent,
    scroller: ".alk-datatable__wrap",
  },
  {
    name: "a picture",
    id: "image",
    facts: facts({ mime: "image/png", name: "chart.png" }),
    content: { kind: "blob", url: "blob:chart" } as PreviewContent,
    scroller: ".alk-preview-image__stage",
  },
  {
    name: "a framed document",
    id: "pdf",
    facts: facts({ mime: "application/pdf", name: "report.pdf" }),
    content: { kind: "frame", url: "blob:report" } as PreviewContent,
    scroller: ".alk-preview-frame",
  },
] as const;

describe("every renderer the registry draws owns a bounded scroll region", () => {
  it.each(KINDS.map((kind) => [kind.name, kind] as const))(
    "%s draws its scroller inside the pane",
    (_name, kind) => {
      const { container } = render(
        <PreviewSurface {...props({ facts: kind.facts, content: kind.content })} />,
      );
      const pane = container.querySelector(".alk-preview");
      expect(pane, "the surface did not draw a pane").not.toBeNull();
      expect(pane?.getAttribute("data-renderer")).toBe(kind.id);
      const scroller = pane?.querySelector(kind.scroller);
      expect(scroller, `${kind.id} has no ${kind.scroller} to scroll in`).not.toBeNull();
    },
  );

  it.each([
    [".alk-preview-code", "source"],
    [".alk-preview-markdown", "a note"],
    [".alk-preview-text__body", "plain text"],
    [".alk-preview-image__stage", "a picture"],
  ])("%s is a bounded scroller in the stylesheet (%s)", (selector) => {
    const rule = declarationsFor(PREVIEW_CSS, selector);
    expect(rule["overflow"], `${selector} does not scroll`).toBe("auto");
    expect(rule["min-height"], `${selector} floors at its content`).toBe("0");
  });

  it("gives every scroller a thumb that is visible in BOTH schemes", () => {
    // Five rules spend this name. It was declared once, in the dark scheme — a
    // pale blue-grey that on the light scheme's near-white ground is very nearly
    // the ground, so the scrollbars these panes rely on all but vanished there.
    const declared = [...TOKENS_CSS.matchAll(/--alkScrollbarThumb:\s*([^;]+);/g)].map((hit) =>
      hit[1].trim(),
    );
    expect(declared.length, "the thumb is declared in only one scheme").toBeGreaterThanOrEqual(2);
    expect(new Set(declared).size, "both schemes declare the same colour").toBe(declared.length);
  });

  it("the pane itself is the bound — it never scrolls the page", () => {
    const pane = declarationsFor(PREVIEW_CSS, ".alk-preview");
    expect(pane["overflow"]).toBe("hidden");
    expect(pane["height"]).toBe("100%");
    expect(pane["min-height"]).toBe("0");
  });

  it.each([
    [".alk-preview-code"],
    [".alk-preview-markdown"],
    [".alk-preview-csv"],
    [".alk-preview-text"],
    [".alk-preview-image"],
    [".alk-preview-frame"],
  ])("%s takes the height the pane offers", (selector) => {
    expectBounded(PREVIEW_CSS, selector);
  });
});

describe("the spreadsheet's pager", () => {
  const csv = KINDS.find((kind) => kind.id === "csv")!;

  function renderCsv(): HTMLElement {
    return render(<PreviewSurface {...props({ facts: csv.facts, content: csv.content })} />)
      .container;
  }

  it("is a sibling of the scrolling table, never a child of it", () => {
    const container = renderCsv();
    const wrap = container.querySelector(".alk-datatable__wrap");
    const foot = container.querySelector(".alk-datatable__foot");
    expect(wrap).not.toBeNull();
    expect(foot, "the pager is not drawn at all").not.toBeNull();
    // Inside the scroller it would ride up the rows on every wheel tick; the
    // bug drew it under a box the rows spilled out of, which is the same fault
    // seen from the other side.
    expect(wrap!.contains(foot!), "the pager is inside the scrolling table").toBe(false);
    expect(foot!.parentElement).toBe(wrap!.parentElement);
    // ...and after it, so it is the floor of the pane rather than a lid.
    const after = wrap!.compareDocumentPosition(foot!) & Node.DOCUMENT_POSITION_FOLLOWING;
    expect(after, "the pager is drawn above the table").toBeTruthy();
  });

  it("sits on an opaque ground, so rows cannot read through it", () => {
    const foot = declarationsFor(DATATABLE_CSS, ".alk-datatable__foot");
    expect(foot["flex"], "the pager can be pushed off its place").toBe("0 0 auto");
    expect(foot["background"], "the pager is transparent").toBe("var(--alkRaisedBg)");
  });

  it("leaves the table a bounded box of its own to scroll in", () => {
    // If `is-fill` lifted the cap, the scroll would go to whatever ancestor had
    // one, and the same grid would behave differently in each surface.
    const wrap = declarationsFor(DATATABLE_CSS, ".alk-datatable__wrap.is-fill");
    expect(wrap["max-height"], "the filled table is still capped").toBe("none");
    expect(wrap["overflow"], "the filled table still spills").toBe("auto");
    expect(wrap["min-height"]).toBe("0");
    expect(wrap["flex"]).toBe("1 1 auto");
    const root = declarationsFor(DATATABLE_CSS, ".alk-datatable.is-fill");
    expect(root["flex"]).toBe("1 1 auto");
    expect(root["min-height"]).toBe("0");
  });

  it("has no cap left that a paged table could fall back into", () => {
    // The 320px rule this bug came out of had no reachable caller once `is-fill`
    // became the bound: every paged grid in the product is also filled, so the
    // rule could only ever contradict the one beside it.
    expect(declarationsFor(DATATABLE_CSS, ".alk-datatable__wrap.is-paged")).toEqual({});
  });

  it("binds a filled grid that does not page — the full blob view", () => {
    // `fill` without `pageSize` is the saved-result FullView. It took the same
    // `overflow: visible` and is bound by the same rule now, and its host hands
    // it a height to use rather than letting it grow past the box.
    const body = declarationsFor(BLOBVIEW_CSS, ".alk-blobv__body");
    expect(body["display"], "the full view's body is not a column").toBe("flex");
    expect(body["flex-direction"]).toBe("column");
    expect(body["min-height"]).toBe("0");
    const { container } = render(
      <DataTable columns={["panel", "v1"]} rows={[["a", "1"]]} rowNumbers fill />,
    );
    const root = container.querySelector(".alk-datatable");
    expect(root?.classList.contains("is-fill"), "the root does not fill").toBe(true);
    expect(container.querySelector(".alk-datatable__wrap.is-fill")).not.toBeNull();
    // Un-paged, so there is no pager to sit under the rows.
    expect(container.querySelector(".alk-datatable__foot")).toBeNull();
  });
});

describe("sorting a spreadsheet", () => {
  const csv = KINDS.find((kind) => kind.id === "csv")!;

  function renderSortable(text: string) {
    return render(
      <PreviewSurface
        {...props({ facts: csv.facts, content: { kind: "text", text } as PreviewContent })}
      />,
    );
  }

  it("is the column header, not a row of keys floating over the table", () => {
    const { container } = renderSortable(manyRows(297));
    // No separate sort strip sits above the header row.
    expect(container.querySelector(".alk-preview-csv__sort")).toBeNull();
    const header = container.querySelector("thead th:not(.alk-datatable__rownum)");
    // The shared control the rest of the product's tables sort with, not one of
    // the grid's own.
    expect(header?.querySelector(".alk-sortheader"), "the header does not sort").not.toBeNull();
    // `aria-sort` is the cell's to carry, and `aria-pressed` alongside it would
    // announce a toggle that is not what a sort is.
    expect(header?.getAttribute("aria-sort")).toBe("none");
    expect(header?.querySelector("[aria-pressed]")).toBeNull();
  });

  it("marks the sorted cell, and only that one", async () => {
    const user = userEvent.setup();
    const { container } = renderSortable("panel,label,v1\nb,two,2\na,one,1\n");
    await user.click(screen.getByRole("button", { name: /^label/ }));
    // Keyed by the column's own label — the state SortHeader folds into the
    // accessible name is asserted below, where a name reads it properly.
    const sorted = [...container.querySelectorAll("thead th[aria-sort]")].map((cell) => [
      cell.querySelector(".alk-sortheader")?.firstChild?.textContent,
      cell.getAttribute("aria-sort"),
    ]);
    expect(sorted).toEqual([
      ["panel", "none"],
      ["label", "ascending"],
      ["v1", "none"],
    ]);
    // Exactly one column says so out loud, and it is that one.
    expect(screen.getByRole("button", { name: /sorted ascending/ })).toHaveAccessibleName(
      "label sorted ascending",
    );
  });

  it("sorts from the keyboard, so the header is reachable without a mouse", async () => {
    const user = userEvent.setup();
    const { container } = renderSortable("panel,label,v1\nb,two,2\na,one,1\n");
    const header = screen.getByRole("button", { name: /^panel/ });
    header.focus();
    expect(header).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(firstColumn(container)).toEqual(["a", "b"]);
    await user.keyboard("{ }");
    expect(firstColumn(container), "space did not turn the sort around").toEqual(["b", "a"]);
  });

  it("orders a numeric column by value, so 11 follows 3 rather than 1", async () => {
    const user = userEvent.setup();
    const { container } = renderSortable("panel,label,v1\na,one,3\nb,two,11\nc,three,1\n");
    await user.click(screen.getByRole("button", { name: /^v1/ }));
    expect(lastColumn(container)).toEqual(["1", "3", "11"]);
  });
});

/** The first data cell of every drawn row, past the row-number gutter. */
function firstColumn(container: HTMLElement): string[] {
  return [...container.querySelectorAll("tbody tr")].map(
    (row) => row.querySelectorAll("td:not(.alk-datatable__rownum)")[0]?.textContent ?? "",
  );
}

function lastColumn(container: HTMLElement): string[] {
  return [...container.querySelectorAll("tbody tr")].map((row) => {
    const cells = row.querySelectorAll("td:not(.alk-datatable__rownum)");
    return cells[cells.length - 1]?.textContent ?? "";
  });
}
