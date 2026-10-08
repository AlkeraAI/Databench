/**
 * The Files page's folder line is ONE line.
 *
 * A crumb that stands for an object — a chat, walked into for its files — wears
 * the object's mark. The product reset draws every svg as a block, so a segment
 * that is itself a block put the mark on the first line and the name on the
 * second: the page's title bar read as two rows for every chat folder. The
 * segment is a row now, and the name is the box that gives width back.
 *
 * jsdom neither lays out nor cascades CSS, so the declarations the one-line
 * contract rests on are read off the sheet that states them.
 */

import { readFileSync } from "node:fs";
import { join, resolve } from "node:path";

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Breadcrumbs, type Crumb } from "@/pages/workspace/files/Breadcrumbs";
import { OBJECT_NAV_ICONS } from "@/lib/files/columns";

const WEB = process.cwd();
const sheet = (path: string): string => readFileSync(path, "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
/** The explorer's own sheet — loaded by every surface that mounts the browser. */
const TREEGRID_CSS = sheet(join(WEB, "src/pages/workspace/files/treegrid.css"));
/** The Files PAGE's sheet — loaded by the page and by nothing else. */
const PAGE_CSS = sheet(join(WEB, "src/pages/workspace/files/files-page.css"));
const RESET_CSS = sheet(resolve(WEB, "../../packages/ui/src/theme/reset.css"));

/** The declarations of the first rule whose selector list ends in `selector`. */
function rule(css: string, selector: string): string {
  const at = css.indexOf(`${selector} {`);
  expect(at, `no rule for \`${selector}\``).toBeGreaterThan(-1);
  const open = css.indexOf("{", at);
  return css.slice(open + 1, css.indexOf("}", open));
}

const TRAIL: readonly Crumb[] = [
  { id: "nd_home", name: "home" },
  // The mark a chat crumb actually wears, taken from the one table that says so.
  { id: "nd_chat", name: "Hi what model are you?", icon: OBJECT_NAV_ICONS.chat },
];

describe("the Files page's crumb keeps a mark on its name's line", () => {
  it("draws the segment as a row rather than as a block", () => {
    // A block segment is the bug: the mark, drawn as a block by the reset,
    // takes the first line and the name falls to the second.
    expect(rule(RESET_CSS, "canvas")).toMatch(/display:\s*block/);
    const segment = rule(TREEGRID_CSS, ".alk-files-crumbs__current");
    expect(segment).toMatch(/display:\s*flex/);
    expect(segment).not.toMatch(/display:\s*block/);
    expect(segment).toMatch(/align-items:\s*center/);
  });

  it("gives the mark no width to surrender and no line of its own", () => {
    const mark = rule(TREEGRID_CSS, ".alk-files-crumbs__current .alk-ic");
    expect(mark).not.toMatch(/display:\s*block/);
    expect(mark).toMatch(/display:\s*inline-block/);
    // Without this the glyph is squeezed to nothing when the line is tight,
    // rather than the name being cut.
    expect(mark).toMatch(/flex:\s*none/);
  });

  it("cuts the name, and only the name, when the line is narrower than it", () => {
    const name = rule(TREEGRID_CSS, ".alk-files-crumbs__name");
    expect(name).toMatch(/min-width:\s*0/);
    expect(name).toMatch(/overflow:\s*hidden/);
    expect(name).toMatch(/text-overflow:\s*ellipsis/);
    expect(name).toMatch(/white-space:\s*nowrap/);
  });

  it("states the segment once, in the sheet both surfaces load", () => {
    // The chat's Files tab does not load the page's sheet, and the page and the
    // tab must not drift into two spellings of one line.
    expect(PAGE_CSS).not.toMatch(/\.alk-files-crumbs__/);
    expect(TREEGRID_CSS).toMatch(/\.alk-files-crumbs__current/);
  });
});

describe("what the trail renders", () => {
  it("puts an object's mark inside the segment that carries its name", () => {
    render(<Breadcrumbs segments={TRAIL} />);
    const current = document.querySelector(".alk-files-crumbs__current");
    expect(current).not.toBeNull();
    // A mark drawn beside the segment is a mark that can be left on a line of
    // its own; it is the name's neighbour, inside the same box.
    expect(current?.querySelector("svg.alk-ic")).not.toBeNull();
    expect(current?.querySelector(".alk-files-crumbs__name")?.textContent).toBe(
      "Hi what model are you?",
    );
  });

  it("carries the whole name on hover, on a link segment as on the page's own", () => {
    render(<Breadcrumbs segments={TRAIL} onNavigate={() => {}} />);
    expect(document.querySelector(".alk-files-crumbs__current")).toHaveAttribute(
      "title",
      "Hi what model are you?",
    );
    // The root is re-cased for display, and hover says what the eye sees.
    expect(screen.getByRole("button", { name: "Home" })).toHaveAttribute("title", "Home");
  });

  it("gives a nameless-mark segment nothing extra to wrap", () => {
    render(<Breadcrumbs segments={[{ id: "nd_plain", name: "Reports" }]} />);
    const current = document.querySelector(".alk-files-crumbs__current");
    expect(current?.querySelector("svg")).toBeNull();
    expect(current?.querySelector(".alk-files-crumbs__name")?.textContent).toBe("Reports");
  });
});
