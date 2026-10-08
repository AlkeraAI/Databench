// A preview is bounded wherever it is embedded.
//
// The registry decides who draws a file; the surface around it decides whether
// that renderer has a height to work inside. Two surfaces embed it — the tab
// beside a chat and the Files page's large preview, which is also what the
// `/files/:nodeId` address opens — and they had each answered the question
// differently. The chat pane offered a scrolling box, so a bar pinned inside the
// preview answered to the pane instead of its own table and landed mid-file,
// and a renderer with no scroller of its own was simply cut off at the pane's
// edge with no way to reach the rest of the file.
//
// So the promise is the same in both: the pane is a bounded flex column, it does
// not scroll itself, and the preview inside it takes the height it is offered.
// jsdom applies no stylesheet, so the promise is read from the CSS and the
// structure from the rendered DOM.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { PreviewSurface } from "@alkera/ui";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";

afterEach(cleanup);

const ROOT = resolve(__dirname, "../../..");
const WORKSPACE_CSS = readFileSync(
  resolve(ROOT, "pages/workspace/chat/workspace/workspace.css"),
  "utf8",
);
const FILES_CSS = readFileSync(resolve(ROOT, "pages/workspace/files/files-page.css"), "utf8");

/** Everything inside an at-rule (`@media`, `@supports`) removed, braces counted
 *  so a nested block cannot end the scan early. A conditional override of the
 *  same selector must not answer for the unconditional rule — a bound that is
 *  only right inside a media query would otherwise read as right everywhere. */
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

/** The declarations a selector carries. Every block naming it exactly is merged
 *  in file order, the way the browser resolves it. */
function declarationsFor(css: string, selector: string): Record<string, string> {
  const out: Record<string, string> = {};
  // Comments first: they sit between blocks, so a rule's "head" would otherwise
  // carry the paragraph written above it and match nothing.
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

/** The two embeddings, each named by the class its pane wears and the stylesheet
 *  that owns it. A third surface that embeds a preview adds a row here. */
const EMBEDDINGS = [
  {
    name: "the files pane beside a chat",
    css: WORKSPACE_CSS,
    pane: ".alk-ws-file__body",
    child: ".alk-ws-file__body > .alk-preview",
    column: ".alk-ws-file",
    // The rule that must say `overflow: hidden`, and so cut the pane's own
    // scrollbox out from under the preview. The pane says it itself here.
    bound: ".alk-ws-file__body",
  },
  {
    name: "the Files page preview, which /files/:nodeId opens",
    css: FILES_CSS,
    pane: ".files-preview__body",
    child: ".files-preview__body > .alk-preview",
    column: null,
    // The modal's body is what closes this one, and the pane inside it declares
    // no overflow at all — which is why the pane's own rule cannot be the thing
    // asserted, and an assertion that tolerated a missing value proved nothing.
    bound: ".files-preview .alk-modal__body",
  },
] as const;

/** An ancestor that scrolls is what a bar or a sticky header inside the preview
 *  anchors to instead of its own box, so no rule in the chain may open one. */
const SCROLLS = new Set(["auto", "scroll", "overlay"]);

const CSV = "panel,label,v1\n" + Array.from({ length: 297 }, (_r, i) => `p${i},l${i},${i}`).join("\n");

const SCRIPT = "import os\n".repeat(600);

function surface(mime: string, name: string, text: string, size: number) {
  return (
    <PreviewSurface
      facts={{ mime, name, size }}
      content={{ kind: "text", text }}
      version="etag-1"
      status="ready"
      onDownload={() => {}}
    />
  );
}

describe.each(EMBEDDINGS.map((e) => [e.name, e] as const))("%s", (_name, embedding) => {
  it("gives the preview a bounded box", () => {
    const pane = declarationsFor(embedding.css, embedding.pane);
    expect(pane, `${embedding.pane} has no rule`).not.toEqual({});
    expect(pane["flex"], "the pane does not take the height offered").toBe("1 1 auto");
    expect(pane["min-height"], "the pane floors at its content").toBe("0");
    expect(pane["display"], "the pane is not a column").toBe("flex");
    expect(pane["flex-direction"]).toBe("column");
  });

  it("closes the scrollbox a bar inside the preview would answer to", () => {
    // Named per embedding rather than defaulted: the chat pane says `hidden`
    // itself, the modal's body says it for the pane inside it, and a test that
    // accepted "no value" from either would have passed on the broken code.
    const bound = declarationsFor(embedding.css, embedding.bound);
    expect(bound, `${embedding.bound} has no rule`).not.toEqual({});
    expect(bound["overflow"], `${embedding.bound} does not close the scrollbox`).toBe("hidden");
  });

  it("opens no scroller of its own anywhere between the pane and the preview", () => {
    for (const selector of [embedding.pane, embedding.child, embedding.bound]) {
      const rule = declarationsFor(embedding.css, selector);
      for (const property of ["overflow", "overflow-y"]) {
        const value = rule[property];
        if (value === undefined) continue;
        expect(SCROLLS.has(value), `${selector} sets ${property}: ${value}`).toBe(false);
      }
    }
  });

  it("hands the height straight down to the preview", () => {
    const child = declarationsFor(embedding.css, embedding.child);
    expect(child, `${embedding.child} has no rule`).not.toEqual({});
    expect(child["flex"]).toBe("1 1 auto");
    expect(child["min-height"]).toBe("0");
  });

  if (embedding.column !== null) {
    it("is itself a bounded column, so the height reaches the pane at all", () => {
      const column = declarationsFor(embedding.css, embedding.column);
      expect(column["display"]).toBe("flex");
      expect(column["flex-direction"]).toBe("column");
      expect(column["min-height"]).toBe("0");
      expect(column["height"]).toBe("100%");
    });
  }

  it("draws a spreadsheet's pager outside the scrolling table", () => {
    const { container } = render(
      <div className={embedding.pane.replace(/^\./, "").split(" ")[0]}>
        {surface("text/csv", "panel-facts.csv", CSV, 15_872)}
      </div>,
    );
    const wrap = container.querySelector(".alk-datatable__wrap");
    const foot = container.querySelector(".alk-datatable__foot");
    expect(wrap, "no scrolling table").not.toBeNull();
    expect(foot, "no pager").not.toBeNull();
    expect(wrap!.contains(foot!), "the pager is inside the rows it is meant to sit under").toBe(
      false,
    );
    expect(foot!.parentElement).toBe(wrap!.parentElement);
  });

  it("sorts a numeric column by value from the keyboard alone", async () => {
    const user = userEvent.setup();
    const { container } = render(
      <div className={embedding.pane.replace(/^\./, "").split(" ")[0]}>
        {surface("text/csv", "panel-facts.csv", "panel,v1\na,3\nb,11\nc,1\n", 64)}
      </div>,
    );
    const header = screen.getByRole("button", { name: /^v1/ });
    header.focus();
    await user.keyboard("{Enter}");
    const values = [...container.querySelectorAll("tbody tr")].map((row) => {
      const cells = row.querySelectorAll("td:not(.alk-datatable__rownum)");
      return cells[cells.length - 1]?.textContent ?? "";
    });
    // By value, not by its letters — 11 after 3, not between 1 and 3.
    expect(values).toEqual(["1", "3", "11"]);
    expect(header.closest("th")?.getAttribute("aria-sort")).toBe("ascending");
  });

  it("draws a long source file inside a scroll region of its own", () => {
    const { container } = render(
      <div className={embedding.pane.replace(/^\./, "").split(" ")[0]}>
        {surface("text/plain", "build-dashboard.py", SCRIPT, 21_600)}
      </div>,
    );
    // The renderer's root IS the scroller for code; without it the pane's
    // `overflow: hidden` cut the file off at whatever line the pane ended on.
    expect(container.querySelector(".alk-preview-code"), "source has no scroll region").not.toBeNull();
  });
});
