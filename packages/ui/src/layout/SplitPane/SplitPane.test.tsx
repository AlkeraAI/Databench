import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { useState, type ReactNode } from "react";
import { afterEach, describe, expect, it, vi, type Mock } from "vitest";

import { SPLIT_GUTTER_PX, SPLIT_STRIP_PX, SplitPane } from "./SplitPane";
import type { PaneSpec } from "./types";

// SplitPane is the IDE column layout the chat workspace is drawn in: a collapsible
// chat rail, the transcript, and a collapsible panel on the right. Everything a
// person can do to a column — drag it, nudge it with the keyboard, collapse it past
// its minimum, bring it back, reset it — has to be reachable without a mouse and has
// to be announced, so the assertions here are the separator's own `aria-value*` and
// the calls the host is handed, never an inline style jsdom does not compute.

afterEach(cleanup);

// jsdom ships no PointerEvent, so a fired `pointerdown` arrives with a null
// `clientX` and a null `button` and every drag reads as a no-op. Give it the one
// the browser has — a mouse event that also carries a pointer id.
class JsdomPointerEvent extends MouseEvent {
  readonly pointerId: number;
  constructor(type: string, init: PointerEventInit = {}) {
    super(type, init);
    this.pointerId = init.pointerId ?? 1;
  }
}
vi.stubGlobal("PointerEvent", JsdomPointerEvent);

const RAIL_MAX = 480;
const WORKSPACE_MAX_SHARE = 0.6;

const RAIL: PaneSpec = {
  id: "rail",
  min: 176,
  max: RAIL_MAX,
  size: 256,
  collapsible: true,
  label: "Chats",
};
const CHAT: PaneSpec = { id: "chat", fill: true, min: 360, label: "Chat" };
const WORKSPACE: PaneSpec = {
  id: "workspace",
  min: 320,
  max: `${WORKSPACE_MAX_SHARE * 100}%`,
  size: 576,
  collapsible: true,
  label: "Files panel",
};
const LAYOUT = "Chat workspace";
const STEP = 16;
const BIG_STEP = 64;
const COLLAPSE_BELOW = 48;

/** A container wide enough that no pane is squeezed, so a test that is about
 *  dragging is not also about the squeeze. */
const ROOMY = 1600;

interface Spies {
  onResize: Mock<(id: string, px: number) => void>;
  onToggle: Mock<(id: string, collapsed: boolean) => void>;
}

function spies(): Spies {
  return { onResize: vi.fn<(id: string, px: number) => void>(), onToggle: vi.fn<(id: string, collapsed: boolean) => void>() };
}

/** The host the component is designed for: it owns the widths and the collapsed
 *  flags, so every assertion below goes through a real re-render from new specs
 *  rather than through state the component secretly kept. */
function Harness({ onResize, onToggle, dir }: Spies & { dir?: "rtl" }): ReactNode {
  const [sizes, setSizes] = useState<Record<string, number>>({
    rail: RAIL.size!,
    workspace: WORKSPACE.size!,
  });
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const panes: PaneSpec[] = [
    { ...RAIL, size: sizes.rail, collapsed: collapsed.rail },
    CHAT,
    { ...WORKSPACE, size: sizes.workspace, collapsed: collapsed.workspace },
  ];
  const pane = (
    <SplitPane
      label={LAYOUT}
      panes={panes}
      step={STEP}
      bigStep={BIG_STEP}
      collapseBelow={COLLAPSE_BELOW}
      onResize={(id, px) => {
        onResize(id, px);
        setSizes((s) => ({ ...s, [id]: px }));
      }}
      onToggle={(id, next) => {
        onToggle(id, next);
        setCollapsed((s) => ({ ...s, [id]: next }));
      }}
    >
      <div>rail content</div>
      <div>chat content</div>
      <div>files content</div>
    </SplitPane>
  );
  return dir ? <div dir={dir}>{pane}</div> : pane;
}

/** Pin the container's width for the run: the percent ceiling and the squeeze both
 *  read it, and jsdom lays nothing out. */
function stubWidth(px: number): void {
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
    width: px,
    height: 800,
    top: 0,
    left: 0,
    right: px,
    bottom: 800,
    x: 0,
    y: 0,
    toJSON: () => ({}),
  } as DOMRect);
}

function gutter(label: string): HTMLElement {
  return screen.getByRole("separator", { name: `Resize ${label}` });
}

function now(label: string): number {
  return Number(gutter(label).getAttribute("aria-valuenow"));
}

/** One pointer drag, from the gutter's own grab point through a list of client x. */
function drag(el: HTMLElement, from: number, to: number[]): void {
  fireEvent.pointerDown(el, { clientX: from, pointerId: 1, button: 0 });
  for (const x of to) fireEvent.pointerMove(window, { clientX: x, pointerId: 1 });
  fireEvent.pointerUp(window, { pointerId: 1 });
}

describe("SplitPane keyboard resizing", () => {
  it("nudges a left pane wider with ArrowRight and reports the new width", async () => {
    const s = spies();
    render(<Harness {...s} />);
    gutter(RAIL.label).focus();
    await userEvent.keyboard("{ArrowRight}");
    expect(s.onResize).toHaveBeenCalledWith(RAIL.id, RAIL.size! + STEP);
    // The host re-rendered from the reported width, and the separator announces it.
    expect(now(RAIL.label)).toBe(RAIL.size! + STEP);
    expect(s.onToggle).not.toHaveBeenCalled();
  });

  it("nudges a left pane narrower with ArrowLeft", async () => {
    const s = spies();
    render(<Harness {...s} />);
    gutter(RAIL.label).focus();
    await userEvent.keyboard("{ArrowLeft}");
    expect(s.onResize).toHaveBeenCalledWith(RAIL.id, RAIL.size! - STEP);
    expect(now(RAIL.label)).toBe(RAIL.size! - STEP);
  });

  it("moves by the big step when Shift is held", async () => {
    const s = spies();
    render(<Harness {...s} />);
    gutter(RAIL.label).focus();
    await userEvent.keyboard("{Shift>}{ArrowRight}{/Shift}");
    expect(s.onResize).toHaveBeenCalledWith(RAIL.id, RAIL.size! + BIG_STEP);
    await userEvent.keyboard("{Shift>}{ArrowLeft}{/Shift}");
    expect(s.onResize).toHaveBeenLastCalledWith(RAIL.id, RAIL.size!);
  });

  it("sends Home to the minimum and End to the maximum", async () => {
    const s = spies();
    render(<Harness {...s} />);
    gutter(RAIL.label).focus();
    await userEvent.keyboard("{Home}");
    expect(s.onResize).toHaveBeenLastCalledWith(RAIL.id, RAIL.min);
    expect(now(RAIL.label)).toBe(RAIL.min);
    await userEvent.keyboard("{End}");
    expect(s.onResize).toHaveBeenLastCalledWith(RAIL.id, RAIL_MAX);
    expect(now(RAIL.label)).toBe(RAIL_MAX);
  });

  it("grows a RIGHT pane with ArrowLeft and shrinks it with ArrowRight", async () => {
    stubWidth(ROOMY);
    const s = spies();
    render(<Harness {...s} />);
    gutter(WORKSPACE.label).focus();
    await userEvent.keyboard("{ArrowLeft}");
    expect(s.onResize).toHaveBeenLastCalledWith(WORKSPACE.id, WORKSPACE.size! + STEP);
    await userEvent.keyboard("{ArrowRight}");
    expect(s.onResize).toHaveBeenLastCalledWith(WORKSPACE.id, WORKSPACE.size!);
    expect(now(WORKSPACE.label)).toBe(WORKSPACE.size!);
  });

  it("reads a percent ceiling against the container it is drawn in", async () => {
    stubWidth(ROOMY);
    const s = spies();
    render(<Harness {...s} />);
    const sixtyPercent = Math.round(WORKSPACE_MAX_SHARE * ROOMY);
    expect(Number(gutter(WORKSPACE.label).getAttribute("aria-valuemax"))).toBe(sixtyPercent);
    gutter(WORKSPACE.label).focus();
    await userEvent.keyboard("{End}");
    expect(s.onResize).toHaveBeenLastCalledWith(WORKSPACE.id, sixtyPercent);
  });

  it("does not report a width past an edge it is already sitting on", async () => {
    const s = spies();
    render(<Harness {...s} />);
    gutter(RAIL.label).focus();
    await userEvent.keyboard("{Home}");
    s.onResize.mockClear();
    await userEvent.keyboard("{ArrowLeft}{Home}");
    expect(s.onResize).not.toHaveBeenCalled();
  });

  it("collapses and restores a collapsible pane from the keyboard", async () => {
    const s = spies();
    render(<Harness {...s} />);
    gutter(RAIL.label).focus();
    await userEvent.keyboard("{Enter}");
    expect(s.onToggle).toHaveBeenCalledWith(RAIL.id, true);
    expect(screen.queryByText("rail content")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: `Show ${RAIL.label}` }));
    expect(s.onToggle).toHaveBeenLastCalledWith(RAIL.id, false);
    expect(screen.getByText("rail content")).toBeInTheDocument();
  });

  it("leaves a pane that cannot collapse alone on Enter and Space", async () => {
    const s = spies();
    const fixed: PaneSpec[] = [
      { id: "side", min: 100, size: 200, label: "Side" },
      { id: "body", fill: true, min: 100, label: "Body" },
    ];
    render(
      <SplitPane label={LAYOUT} panes={fixed} onResize={s.onResize} onToggle={s.onToggle}>
        <div>side</div>
        <div>body</div>
      </SplitPane>,
    );
    gutter("Side").focus();
    await userEvent.keyboard("{Enter} ");
    expect(s.onToggle).not.toHaveBeenCalled();
    expect(screen.getByText("side")).toBeInTheDocument();
  });
});

describe("SplitPane pointer resizing", () => {
  it("follows the pointer and reports the width it settled on", () => {
    stubWidth(ROOMY);
    const s = spies();
    render(<Harness {...s} />);
    drag(gutter(RAIL.label), 600, [640]);
    expect(s.onResize).toHaveBeenLastCalledWith(RAIL.id, RAIL.size! + 40);
    expect(now(RAIL.label)).toBe(RAIL.size! + 40);
  });

  it("clamps a drag to the pane's own ceiling and floor", () => {
    stubWidth(ROOMY);
    const s = spies();
    render(<Harness {...s} />);
    drag(gutter(RAIL.label), 600, [4000]);
    expect(s.onResize).toHaveBeenLastCalledWith(RAIL.id, RAIL_MAX);
    // Just inside the collapse threshold: it floors at the minimum, never below.
    drag(gutter(RAIL.label), 600, [600 - (RAIL_MAX - RAIL.min) - 20]);
    expect(s.onResize).toHaveBeenLastCalledWith(RAIL.id, RAIL.min);
    expect(s.onToggle).not.toHaveBeenCalled();
  });

  it("collapses once when dragged well past the minimum, and restores on the way back", () => {
    stubWidth(ROOMY);
    const s = spies();
    render(<Harness {...s} />);
    const past = 600 - (RAIL.size! - RAIL.min) - COLLAPSE_BELOW - 1;
    drag(gutter(RAIL.label), 600, [past, past - 100, 600]);
    expect(s.onToggle.mock.calls).toEqual([
      [RAIL.id, true],
      [RAIL.id, false],
    ]);
  });

  it("drags a RIGHT pane wider when the pointer moves left", () => {
    stubWidth(ROOMY);
    const s = spies();
    render(<Harness {...s} />);
    drag(gutter(WORKSPACE.label), 900, [860]);
    expect(s.onResize).toHaveBeenLastCalledWith(WORKSPACE.id, WORKSPACE.size! + 40);
  });

  it("returns a pane to the width it was first given on a double-click", async () => {
    const s = spies();
    render(<Harness {...s} />);
    gutter(RAIL.label).focus();
    await userEvent.keyboard("{Shift>}{ArrowRight}{/Shift}");
    expect(now(RAIL.label)).toBe(RAIL.size! + BIG_STEP);
    fireEvent.doubleClick(gutter(RAIL.label));
    expect(s.onResize).toHaveBeenLastCalledWith(RAIL.id, RAIL.size);
    expect(now(RAIL.label)).toBe(RAIL.size);
  });

  it("marks the layout while a drag is in flight and unmarks it after", () => {
    stubWidth(ROOMY);
    const s = spies();
    render(<Harness {...s} />);
    const bar = gutter(RAIL.label);
    const root = screen.getByRole("group", { name: LAYOUT });
    expect(root).not.toHaveAttribute("data-dragging");
    fireEvent.pointerDown(bar, { clientX: 600, pointerId: 1, button: 0 });
    expect(root).toHaveAttribute("data-dragging");
    fireEvent.pointerUp(window, { pointerId: 1 });
    expect(root).not.toHaveAttribute("data-dragging");
  });
});

describe("SplitPane squeezing", () => {
  it("squeezes the rightmost collapsible pane rather than collapsing it", () => {
    // 256 + 360 + 576 + two gutters is 1204: the files pane alone can give
    // the 204 px this container is short.
    stubWidth(1000);
    const s = spies();
    render(<Harness {...s} />);
    const root = screen.getByRole("group", { name: LAYOUT });
    expect(root).toHaveAttribute("data-squeezed", WORKSPACE.id);
    // The pane it squeezed is drawn narrower than the host asked for, and never
    // narrower than its own minimum.
    expect(now(WORKSPACE.label)).toBeLessThan(WORKSPACE.size!);
    expect(now(WORKSPACE.label)).toBeGreaterThanOrEqual(WORKSPACE.min);
    expect(now(RAIL.label)).toBe(RAIL.size);
    expect(s.onToggle).not.toHaveBeenCalled();
    expect(s.onResize).not.toHaveBeenCalled();
  });

  it("takes what the rightmost pane cannot give from the next pane leftward", () => {
    // 1204 px asked of 900: the files pane gives down to its 320 px minimum
    // (256 px), and the remaining 48 px come off the rail, so the conversation
    // keeps its own minimum instead of being squeezed below it.
    stubWidth(900);
    const s = spies();
    render(<Harness {...s} />);
    const root = screen.getByRole("group", { name: LAYOUT });
    expect(now(WORKSPACE.label)).toBe(WORKSPACE.min);
    expect(now(RAIL.label)).toBe(RAIL.size! - 48);
    const chat = 900 - now(WORKSPACE.label) - now(RAIL.label) - 2 * SPLIT_GUTTER_PX;
    expect(chat).toBe(CHAT.min);
    // The pane named as squeezed is still the first to give.
    expect(root).toHaveAttribute("data-squeezed", WORKSPACE.id);
    expect(s.onToggle).not.toHaveBeenCalled();
    expect(s.onResize).not.toHaveBeenCalled();
  });

  it("never draws a side pane below its own minimum, however short the container", () => {
    stubWidth(600);
    render(<Harness {...spies()} />);
    expect(now(WORKSPACE.label)).toBe(WORKSPACE.min);
    expect(now(RAIL.label)).toBe(RAIL.min);
  });

  it("leaves every pane alone when the container has room", () => {
    stubWidth(ROOMY);
    const s = spies();
    render(<Harness {...s} />);
    const root = screen.getByRole("group", { name: LAYOUT });
    expect(root).not.toHaveAttribute("data-squeezed");
    expect(now(WORKSPACE.label)).toBe(WORKSPACE.size);
  });
});

describe("SplitPane accessibility", () => {
  it("names the layout, every gutter and every show button", () => {
    render(<Harness {...spies()} />);
    expect(screen.getByRole("group", { name: LAYOUT })).toBeInTheDocument();
    for (const spec of [RAIL, WORKSPACE]) {
      const bar = gutter(spec.label);
      expect(bar).toHaveAttribute("aria-orientation", "vertical");
      expect(bar).toHaveAttribute("tabindex", "0");
      expect(Number(bar.getAttribute("aria-valuemin"))).toBe(spec.min);
    }
    // The filling pane has no gutter of its own — it is what the others leave.
    expect(screen.queryByRole("separator", { name: `Resize ${CHAT.label}` })).toBeNull();
  });

  it("points each gutter at the pane it actually resizes", () => {
    render(<Harness {...spies()} />);
    const controlled = (label: string, text: string): void => {
      const id = gutter(label).getAttribute("aria-controls");
      expect(id).toBeTruthy();
      const pane = document.getElementById(id!);
      expect(pane).not.toBeNull();
      expect(pane!.textContent).toBe(text);
    };
    controlled(RAIL.label, "rail content");
    controlled(WORKSPACE.label, "files content");
  });

  it("offers a show button in place of a collapsed pane's gutter", async () => {
    const s = spies();
    render(<Harness {...s} />);
    gutter(WORKSPACE.label).focus();
    await userEvent.keyboard("{Enter}");
    expect(screen.queryByRole("separator", { name: `Resize ${WORKSPACE.label}` })).toBeNull();
    expect(screen.getByRole("button", { name: `Show ${WORKSPACE.label}` })).toBeInTheDocument();
    expect(screen.queryByText("files content")).toBeNull();
    // The rail is untouched by its neighbour collapsing.
    expect(screen.getByText("rail content")).toBeInTheDocument();
  });
});

describe("SplitPane in a right-to-left layout", () => {
  it("mirrors the arrow keys", async () => {
    const s = spies();
    render(<Harness {...s} dir="rtl" />);
    gutter(RAIL.label).focus();
    await userEvent.keyboard("{ArrowRight}");
    expect(s.onResize).toHaveBeenLastCalledWith(RAIL.id, RAIL.size! - STEP);
    await userEvent.keyboard("{ArrowLeft}");
    expect(s.onResize).toHaveBeenLastCalledWith(RAIL.id, RAIL.size);
  });

  it("mirrors the drag direction", () => {
    stubWidth(ROOMY);
    const s = spies();
    render(<Harness {...s} dir="rtl" />);
    drag(gutter(RAIL.label), 600, [560]);
    expect(s.onResize).toHaveBeenLastCalledWith(RAIL.id, RAIL.size! + 40);
  });
});

describe("the SplitPane stylesheet", () => {
  const HERE = dirname(fileURLToPath(import.meta.url));
  const sheet = readFileSync(join(HERE, "splitpane.css"), "utf8");
  const theme = join(HERE, "../../theme");

  it("holds no viewport-height unit", () => {
    // The layout is drawn inside a page that already owns its height; a vh unit
    // here would ignore that and grow past it on a mobile browser's chrome.
    expect(sheet).not.toMatch(/\b[\d.]+(?:vh|dvh|svh|lvh)\b/);
  });

  it("reads only custom properties the design system declares", () => {
    const declared = new Set<string>();
    for (const file of ["tokens.css", "motion.css"]) {
      const css = readFileSync(join(theme, file), "utf8");
      for (const m of css.matchAll(/(--alk[A-Za-z0-9-]*)\s*:/g)) declared.add(m[1]!);
    }
    const own = new Set([...sheet.matchAll(/(--alk[A-Za-z0-9-]*)\s*:/g)].map((m) => m[1]!));
    const read = [...sheet.matchAll(/var\(\s*(--alk[A-Za-z0-9-]*)/g)].map((m) => m[1]!);
    expect(read.filter((name) => !declared.has(name) && !own.has(name))).toEqual([]);
    expect(read.length).toBeGreaterThan(0);
  });

  it("declares the gutter and strip widths the layout does its arithmetic with", () => {
    // The grid is written from these two properties while the clamp, the collapse
    // threshold and the squeeze are computed in pixels: if the sheet and the module
    // disagree, a drag lands a few pixels away from the gutter under the cursor.
    expect(sheet).toMatch(new RegExp(`--alkSplitGutter:\\s*${SPLIT_GUTTER_PX}px`));
    expect(sheet).toMatch(new RegExp(`--alkSplitStrip:\\s*${SPLIT_STRIP_PX}px`));
  });

  it("freezes the column animation for a reader who asked for less motion", () => {
    const reduced = /@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{([\s\S]*?)\n\}/.exec(sheet);
    expect(reduced).not.toBeNull();
    expect(reduced![1]).toMatch(/transition:\s*none/);
    // And the animation it is switching off is really declared.
    expect(sheet).toMatch(/transition:[^;]*grid-template-columns/);
  });

  it("stops the page fighting the drag while a gutter is being moved", () => {
    const rules = [...sheet.matchAll(/\[data-dragging\][^{]*\{([^}]*)\}/g)].map((m) => m[1]!);
    const body = rules.join("\n");
    expect(body).toMatch(/user-select:\s*none/);
    expect(body).toMatch(/cursor:\s*col-resize/);
    // An embedded preview swallows the pointer mid-drag unless it is switched off.
    expect(sheet).toMatch(/\[data-dragging\][\s\S]{0,200}?iframe[^{]*\{[^}]*pointer-events:\s*none/);
  });
});
