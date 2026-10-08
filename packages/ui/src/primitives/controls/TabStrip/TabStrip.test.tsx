import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { createEvent, fireEvent, render, screen, within } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { TabStrip } from "./TabStrip";
import type { TabStripItem } from "./types";

// The strip is the editor row of open files, so what matters are the things a
// reader does without thinking: arrow across it, close the one in front of you,
// and land somewhere sensible afterwards. Each case drives the real DOM — the
// roles assistive technology reads, the focus that moves, the callbacks a host
// acts on — rather than the component's internals.

const HERE = dirname(fileURLToPath(import.meta.url));

const TABS: TabStripItem[] = [
  { id: "files", label: "Files", pinned: true },
  { id: "a", label: "report.md" },
  { id: "b", label: "chart.png" },
  { id: "c", label: "notes.txt" },
];

/** A host that really removes a closed tab, so focus after a close is observed
 *  against the list the reader is left with rather than a frozen prop. */
function Harness({
  initial = TABS,
  initialActive = "a",
  onClose,
}: {
  initial?: TabStripItem[];
  initialActive?: string | null;
  onClose?: (id: string) => void;
}) {
  const [tabs, setTabs] = useState(initial);
  const [active, setActive] = useState<string | null>(initialActive);
  return (
    <TabStrip
      label="Open files"
      tabs={tabs}
      activeId={active}
      onActivate={setActive}
      onClose={(id) => {
        onClose?.(id);
        setTabs((prev) => prev.filter((tab) => tab.id !== id));
        if (id === active) setActive(null);
      }}
    />
  );
}

const tabFor = (label: string): HTMLElement => screen.getByRole("tab", { name: new RegExp(label) });

const closeIn = (label: string): HTMLElement =>
  within(tabFor(label)).getByRole("button", { name: `Close ${label}` });

/** A non-primary mouse click — Testing Library's shorthand map carries no `auxClick`. */
const auxClick = (el: HTMLElement, button: number): void => {
  fireEvent(el, new MouseEvent("auxclick", { bubbles: true, cancelable: true, button }));
};

describe("TabStrip", () => {
  it("wires each tab to the panel it controls", () => {
    render(<Harness />);
    const strip = screen.getByRole("tablist", { name: "Open files" });
    const tabs = within(strip).getAllByRole("tab");
    expect(tabs.map((tab) => tab.id)).toEqual(["tab-files", "tab-a", "tab-b", "tab-c"]);
    expect(tabs.map((tab) => tab.getAttribute("aria-controls"))).toEqual([
      "panel-files",
      "panel-a",
      "panel-b",
      "panel-c",
    ]);
    expect(tabs.map((tab) => tab.getAttribute("aria-selected"))).toEqual([
      "false",
      "true",
      "false",
      "false",
    ]);
  });

  it("keeps one tab stop — the active tab", () => {
    render(<Harness />);
    expect(screen.getAllByRole("tab").map((tab) => tab.tabIndex)).toEqual([-1, 0, -1, -1]);
  });

  it("gives the first tab the tab stop when nothing is active", () => {
    render(<Harness initialActive={null} />);
    expect(screen.getAllByRole("tab").map((tab) => tab.tabIndex)).toEqual([0, -1, -1, -1]);
  });

  it("activates the tab that was clicked", () => {
    const onActivate = vi.fn();
    render(
      <TabStrip
        label="Open files"
        tabs={TABS}
        activeId="a"
        onActivate={onActivate}
        onClose={vi.fn()}
      />,
    );
    fireEvent.click(tabFor("chart.png"));
    expect(onActivate).toHaveBeenCalledWith("b");
  });

  it("moves focus AND the selection with Left and Right", () => {
    render(<Harness />);
    tabFor("report.md").focus();
    fireEvent.keyDown(tabFor("report.md"), { key: "ArrowRight" });
    expect(tabFor("chart.png")).toHaveFocus();
    expect(tabFor("chart.png")).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(tabFor("chart.png"), { key: "ArrowLeft" });
    expect(tabFor("report.md")).toHaveFocus();
    expect(tabFor("report.md")).toHaveAttribute("aria-selected", "true");
  });

  it("wraps at both ends", () => {
    render(<Harness />);
    tabFor("Files").focus();
    fireEvent.keyDown(tabFor("Files"), { key: "ArrowLeft" });
    expect(tabFor("notes.txt")).toHaveFocus();
    fireEvent.keyDown(tabFor("notes.txt"), { key: "ArrowRight" });
    expect(tabFor("Files")).toHaveFocus();
  });

  it("jumps to the ends with Home and End", () => {
    render(<Harness />);
    tabFor("chart.png").focus();
    fireEvent.keyDown(tabFor("chart.png"), { key: "End" });
    expect(tabFor("notes.txt")).toHaveFocus();
    expect(tabFor("notes.txt")).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(tabFor("notes.txt"), { key: "Home" });
    expect(tabFor("Files")).toHaveFocus();
    expect(tabFor("Files")).toHaveAttribute("aria-selected", "true");
  });

  it("leaves other keys to the page", () => {
    const onActivate = vi.fn();
    render(
      <TabStrip
        label="Open files"
        tabs={TABS}
        activeId="a"
        onActivate={onActivate}
        onClose={vi.fn()}
      />,
    );
    fireEvent.keyDown(tabFor("report.md"), { key: "ArrowDown" });
    fireEvent.keyDown(tabFor("report.md"), { key: "b" });
    expect(onActivate).not.toHaveBeenCalled();
  });

  it("closes the focused tab with Delete and with Backspace", () => {
    const onClose = vi.fn();
    render(<Harness onClose={onClose} />);
    fireEvent.keyDown(tabFor("report.md"), { key: "Delete" });
    expect(onClose).toHaveBeenCalledWith("a");
    fireEvent.keyDown(tabFor("chart.png"), { key: "Backspace" });
    expect(onClose).toHaveBeenCalledWith("b");
  });

  it("will not close a pinned tab from the keyboard", () => {
    const onClose = vi.fn();
    render(<Harness onClose={onClose} />);
    fireEvent.keyDown(tabFor("Files"), { key: "Delete" });
    fireEvent.keyDown(tabFor("Files"), { key: "Backspace" });
    expect(onClose).not.toHaveBeenCalled();
    expect(tabFor("Files")).toBeInTheDocument();
  });

  it("closes on a middle click, and never on a pinned tab", () => {
    const onClose = vi.fn();
    render(<Harness onClose={onClose} />);
    auxClick(tabFor("chart.png"), 1);
    expect(onClose).toHaveBeenCalledWith("b");
    onClose.mockClear();
    auxClick(tabFor("Files"), 1);
    expect(onClose).not.toHaveBeenCalled();
  });

  it("ignores a right-button aux click", () => {
    const onClose = vi.fn();
    render(<Harness onClose={onClose} />);
    auxClick(tabFor("chart.png"), 2);
    expect(onClose).not.toHaveBeenCalled();
  });

  it("hands focus to the right neighbour when a tab in the middle closes", () => {
    render(<Harness />);
    fireEvent.click(closeIn("chart.png"));
    expect(screen.queryByRole("tab", { name: /chart\.png/ })).toBeNull();
    expect(tabFor("notes.txt")).toHaveFocus();
  });

  it("falls back to the left neighbour when the last tab closes", () => {
    render(<Harness />);
    fireEvent.click(closeIn("notes.txt"));
    expect(tabFor("chart.png")).toHaveFocus();
  });

  it("falls back to the pinned tab when the only other tab closes", () => {
    render(
      <Harness
        initial={[
          { id: "files", label: "Files", pinned: true },
          { id: "a", label: "report.md" },
        ]}
      />,
    );
    fireEvent.click(closeIn("report.md"));
    expect(tabFor("Files")).toHaveFocus();
  });

  it("closes from the button without activating the tab under it", () => {
    const onActivate = vi.fn();
    const onClose = vi.fn();
    render(
      <TabStrip
        label="Open files"
        tabs={TABS}
        activeId="a"
        onActivate={onActivate}
        onClose={onClose}
      />,
    );
    fireEvent.click(closeIn("chart.png"));
    expect(onClose).toHaveBeenCalledWith("b");
    expect(onActivate).not.toHaveBeenCalled();
  });

  it("gives a pinned tab no close button and keeps the close button out of the tab order", () => {
    render(<Harness />);
    expect(within(tabFor("Files")).queryByRole("button")).toBeNull();
    expect(closeIn("report.md").tabIndex).toBe(-1);
  });

  it("names each marker for a reader who cannot see the dot", () => {
    render(
      <Harness
        initial={[
          { id: "a", label: "report.md", marker: "updated" },
          { id: "b", label: "chart.png", marker: "gone" },
          { id: "c", label: "notes.txt" },
        ]}
      />,
    );
    expect(within(tabFor("report.md")).getByLabelText("updated")).toBeInTheDocument();
    expect(within(tabFor("chart.png")).getByLabelText("no longer available")).toBeInTheDocument();
    expect(within(tabFor("notes.txt")).queryByLabelText("updated")).toBeNull();
    expect(within(tabFor("notes.txt")).queryByLabelText("no longer available")).toBeNull();
  });

  it("carries the whole name in the hover text when the label is cut short", () => {
    render(
      <Harness initial={[{ id: "a", label: "a-very-long…", title: "a-very-long-report-name.md" }]} />,
    );
    expect(screen.getByText("a-very-long…")).toHaveAttribute("title", "a-very-long-report-name.md");
  });

  it("falls back to the label as the hover text", () => {
    render(<Harness initial={[{ id: "a", label: "report.md" }]} />);
    expect(screen.getByText("report.md")).toHaveAttribute("title", "report.md");
  });

  it("renders the trailing controls after the last tab, outside the tablist", () => {
    render(
      <TabStrip
        label="Open files"
        tabs={TABS}
        activeId="a"
        onActivate={vi.fn()}
        onClose={vi.fn()}
        trailing={<button type="button">More</button>}
      />,
    );
    const list = screen.getByRole("tablist", { name: "Open files" });
    // A tablist owns tabs and nothing else, so the controls sit beside it.
    expect(within(list).queryByRole("button", { name: "More" })).toBeNull();
    const order = Array.from(list.parentElement!.querySelectorAll('[role="tab"], button'));
    expect(order.at(-1)).toHaveTextContent("More");
  });

  it("scrolls a newly active tab into view", () => {
    const scrollIntoView = vi.fn();
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
      configurable: true,
      value: scrollIntoView,
    });
    const { rerender } = render(
      <TabStrip label="Open files" tabs={TABS} activeId="a" onActivate={vi.fn()} onClose={vi.fn()} />,
    );
    scrollIntoView.mockClear();
    rerender(
      <TabStrip label="Open files" tabs={TABS} activeId="c" onActivate={vi.fn()} onClose={vi.fn()} />,
    );
    expect(scrollIntoView).toHaveBeenCalledWith({ inline: "nearest", block: "nearest" });
  });

  it("renders the strip and nothing else when there are no tabs", () => {
    render(<Harness initial={[]} initialActive={null} />);
    expect(screen.getByRole("tablist", { name: "Open files" })).toBeInTheDocument();
    expect(screen.queryAllByRole("tab")).toEqual([]);
  });
});

describe("preview tabs", () => {
  it("draw a preview tab in italics and keep it on a double click", () => {
    const onPin = vi.fn();
    render(
      <TabStrip
        label="Open files"
        tabs={[{ id: "a", label: "report.md" }, { id: "b", label: "notes.txt", transient: true }]}
        activeId="a"
        onActivate={() => {}}
        onClose={() => {}}
        onPin={onPin}
      />,
    );
    const preview = screen.getByRole("tab", { name: /notes\.txt/ });
    expect(preview).toHaveClass("alk-tabstrip__tab--transient");
    expect(screen.getByRole("tab", { name: /report\.md/ })).not.toHaveClass("alk-tabstrip__tab--transient");
    fireEvent.doubleClick(preview);
    expect(onPin).toHaveBeenCalledWith("b");
  });
});

describe("dragging tabs", () => {
  const TYPE = "application/x-test-tab";

  function transfer(): DataTransfer {
    const data = new Map<string, string>();
    return {
      get types() {
        return [...data.keys()];
      },
      setData: (type: string, value: string) => void data.set(type, value),
      getData: (type: string) => data.get(type) ?? "",
      dropEffect: "none",
      effectAllowed: "all",
    } as unknown as DataTransfer;
  }

  function strip(onDrop = vi.fn()) {
    render(
      <TabStrip
        label="Open files"
        tabs={TABS}
        activeId="a"
        onActivate={() => {}}
        onClose={() => {}}
        drag={{ type: TYPE, payload: (id) => `payload:${id}`, onDrop }}
      />,
    );
    return onDrop;
  }

  /** A drag event at `x` over a tab laid out from 100 to 200. */
  function at(type: "dragOver" | "drop", target: HTMLElement, data: DataTransfer, x: number): void {
    target.getBoundingClientRect = () =>
      ({ left: 100, width: 100, top: 0, height: 20, right: 200, bottom: 20, x: 100, y: 0 }) as DOMRect;
    const event = createEvent[type](target, { dataTransfer: data });
    Object.defineProperty(event, "clientX", { value: x });
    fireEvent(target, event);
  }

  it("carries the host's payload under the host's type", () => {
    strip();
    const data = transfer();
    fireEvent.dragStart(screen.getByRole("tab", { name: /chart\.png/ }), { dataTransfer: data });
    expect(data.getData(TYPE)).toBe("payload:b");
  });

  it("drops before the tab under the left half of it and after it on the right half", () => {
    const onDrop = strip();
    const target = screen.getByRole("tab", { name: /chart\.png/ });
    const left = transfer();
    left.setData(TYPE, "payload:c");
    at("drop", target, left, 120);
    expect(onDrop).toHaveBeenLastCalledWith("payload:c", 2);
    const right = transfer();
    right.setData(TYPE, "payload:c");
    at("drop", target, right, 180);
    expect(onDrop).toHaveBeenLastCalledWith("payload:c", 3);
  });

  it("marks where a drag would land while it hovers", () => {
    strip();
    const target = screen.getByRole("tab", { name: /chart\.png/ });
    const data = transfer();
    data.setData(TYPE, "payload:c");
    at("dragOver", target, data, 120);
    expect(target).toHaveAttribute("data-drop", "before");
  });

  it("drops at the end past the last tab", () => {
    const onDrop = strip();
    const data = transfer();
    data.setData(TYPE, "payload:a");
    fireEvent.drop(screen.getByRole("tablist"), { dataTransfer: data });
    expect(onDrop).toHaveBeenCalledWith("payload:a", TABS.length);
  });

  it("takes no drag of another type", () => {
    const onDrop = strip();
    const data = transfer();
    data.setData("Files", "report.pdf");
    fireEvent.drop(screen.getByRole("tab", { name: /chart\.png/ }), { dataTransfer: data });
    expect(onDrop).not.toHaveBeenCalled();
  });

  it("is not draggable when the host does not allow it", () => {
    render(<Harness />);
    expect(screen.getByRole("tab", { name: /report\.md/ })).not.toHaveAttribute("draggable");
  });
});

describe("tabstrip.css", () => {
  const sheet = readFileSync(join(HERE, "tabstrip.css"), "utf8");
  const declared = new Set(
    [join(HERE, "../../../theme/tokens.css"), join(HERE, "../../../theme/motion.css")].flatMap(
      (path) =>
        Array.from(readFileSync(path, "utf8").matchAll(/(--alk[A-Za-z0-9-]*)\s*:/g)).map(
          (match) => match[1]!,
        ),
    ),
  );
  const spent = Array.from(
    sheet.replace(/\/\*[\s\S]*?\*\//g, "").matchAll(/var\(\s*(--alk[A-Za-z0-9-]*)/g),
  ).map((match) => match[1]!);

  it("paints only with names the design system declares", () => {
    // A misspelled custom property silently takes its literal fallback and stops
    // following the light/dark flip; nothing else in the toolchain sees that.
    expect(declared.size).toBeGreaterThan(80);
    expect(spent.filter((name) => !declared.has(name))).toEqual([]);
  });

  it("paints through the token layer rather than literals", () => {
    expect(spent.length).toBeGreaterThan(5);
  });

  it("ellipsizes a long tab label instead of widening the strip", () => {
    const rule = /\.alk-tabstrip__label\s*\{([^}]*)\}/.exec(sheet);
    expect(rule).not.toBeNull();
    expect(rule![1]).toContain("text-overflow: ellipsis");
    expect(rule![1]).toContain("overflow: hidden");
  });

  it("measures itself against its container, never the viewport", () => {
    expect(/\b\d+(\.\d+)?(vh|dvh|svh|lvh)\b/.test(sheet)).toBe(false);
  });
});
