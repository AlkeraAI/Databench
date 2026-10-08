// The trail is the path the session walked, so a deep link has one segment and
// nothing above it to press; and on a narrow line a long trail cut every segment
// to a few letters, the folder the reader was in included. The way up is a
// control the surface drives from the folder's own parent, and a trail deeper
// than three folds its middle, opening in place on request.
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { Breadcrumbs, TRAIL_FOLDS_PAST, type Crumb } from "@/pages/workspace/files/Breadcrumbs";

const crumb = (id: string, name: string): Crumb => ({ id, name });
const DEEP: Crumb[] = [
  crumb("nd_home", "home"),
  crumb("nd_a", "Analyses"),
  crumb("nd_b", "2026"),
  crumb("nd_c", "Q3"),
  crumb("nd_d", "charts"),
];

describe("the way up", () => {
  it("is absent when the surface offers none", () => {
    render(<Breadcrumbs segments={DEEP.slice(0, 2)} />);
    expect(screen.queryByRole("button", { name: /^Up( to |$)/u })).toBeNull();
  });

  it("presses through to the surface under the name it gave", async () => {
    const user = userEvent.setup();
    const go = vi.fn();
    render(<Breadcrumbs segments={DEEP.slice(0, 2)} up={{ title: "Up to Home", go }} />);
    await user.click(screen.getByRole("button", { name: "Up to Home" }));
    expect(go).toHaveBeenCalledTimes(1);
  });

  it("stays on the line, disabled, where there is nothing above", () => {
    render(<Breadcrumbs segments={DEEP.slice(0, 1)} up={{ title: "Up" }} />);
    expect(screen.getByRole("button", { name: "Up" })).toBeDisabled();
  });
});

describe("a deep trail", () => {
  it("keeps its first and last segments and folds the ones between", () => {
    render(<Breadcrumbs segments={DEEP} onNavigate={() => {}} />);
    const trail = screen.getByRole("navigation", { name: "Breadcrumb" });
    expect(within(trail).getByRole("button", { name: "Home" })).toBeInTheDocument();
    expect(within(trail).getByText("charts")).toBeInTheDocument();
    for (const name of ["Analyses", "2026", "Q3"]) {
      expect(within(trail).queryByText(name)).toBeNull();
    }
    const fold = within(trail).getByRole("button", { name: "Show the 3 folders between" });
    // The folded names are one pointer away.
    expect(fold).toHaveAttribute("title", "Analyses / 2026 / Q3");
  });

  it("opens in place when the fold is pressed, and every segment is then a link", async () => {
    const user = userEvent.setup();
    const onNavigate = vi.fn();
    render(<Breadcrumbs segments={DEEP} onNavigate={onNavigate} />);
    const trail = screen.getByRole("navigation", { name: "Breadcrumb" });
    await user.click(within(trail).getByRole("button", { name: "Show the 3 folders between" }));
    await user.click(within(trail).getByRole("button", { name: "2026" }));
    expect(onNavigate).toHaveBeenCalledWith("nd_b");
    expect(within(trail).queryByRole("button", { name: /folders between/u })).toBeNull();
  });

  it("closes the fold again once the reader is in another folder", async () => {
    const user = userEvent.setup();
    const { rerender } = render(<Breadcrumbs segments={DEEP} onNavigate={() => {}} />);
    const trail = screen.getByRole("navigation", { name: "Breadcrumb" });
    await user.click(within(trail).getByRole("button", { name: "Show the 3 folders between" }));
    rerender(<Breadcrumbs segments={[...DEEP, crumb("nd_e", "png")]} onNavigate={() => {}} />);
    expect(within(trail).getByRole("button", { name: "Show the 4 folders between" })).toBeInTheDocument();
  });

  it("does not fold a trail of three", () => {
    render(<Breadcrumbs segments={DEEP.slice(0, TRAIL_FOLDS_PAST)} onNavigate={() => {}} />);
    const trail = screen.getByRole("navigation", { name: "Breadcrumb" });
    expect(within(trail).queryByRole("button", { name: /folders between/u })).toBeNull();
    expect(within(trail).getByText("2026")).toBeInTheDocument();
  });
});

/* The narrow-line priority is a layout contract, and jsdom neither lays out nor
 * cascades CSS, so the declarations it rests on are read off the sheet that
 * states them, the way the chat pane's height contract is checked. */
describe("on a narrow line", () => {
  const css = readFileSync(
    join(process.cwd(), "src/pages/workspace/files/treegrid.css"),
    "utf8",
  ).replace(/\/\*[\s\S]*?\*\//gu, "");
  const rule = (selector: string): string => {
    const at = css.indexOf(`${selector} {`);
    expect(at, `no rule for \`${selector}\``).toBeGreaterThan(-1);
    const open = css.indexOf("{", at);
    return css.slice(open + 1, css.indexOf("}", open));
  };
  const shrink = (declarations: string): number => {
    const flex = /flex:\s*\d+\s+(\d+)/u.exec(declarations);
    expect(flex, "a flex shorthand with a shrink factor").not.toBeNull();
    return Number(flex?.[1]);
  };

  it("the folder the reader is in gives up its name last", () => {
    const others = shrink(rule(".alk-files-crumbs__item:not(:last-child)"));
    const current = shrink(rule(".alk-files-crumbs__item:last-child"));
    expect(others).toBeGreaterThan(current);
  });
});
