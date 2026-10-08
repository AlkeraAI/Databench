import { useState } from "react";

import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Tree, treeTwistLabel, type TreeNode } from "./Tree";

// The selectable, collapsible index behind the teams register and the knowledge subject rail. These
// pin the observable contract: the ARIA tree shape, a twist that toggles WITHOUT selecting, the
// inert-when-closed subtree, a single roving tab stop, and the keyboard model. Every id and label a
// test searches for is a named field on N, so no test re-types a value the fixture owns.

afterEach(cleanup);

const TREE_LABEL = "Test tree";

const N = {
  all: { id: "all", label: "All entries", count: 9 },
  conv: { id: "conventions", label: "Conventions", count: 5 },
  finance: { id: "conventions/finance", label: "finance", count: 2 },
  legal: { id: "conventions/legal", label: "legal", count: 3 },
  schemas: { id: "schemas", label: "Schemas", count: 4 },
  core: { id: "schemas/core", label: "core", count: 4 },
} as const;

const NODES: TreeNode[] = [
  { id: N.all.id, label: N.all.label, emphasized: true, trailing: <span>{N.all.count}</span> },
  {
    id: N.conv.id,
    label: N.conv.label,
    trailing: <span>{N.conv.count}</span>,
    children: [
      { id: N.finance.id, label: N.finance.label, trailing: <span>{N.finance.count}</span> },
      { id: N.legal.id, label: N.legal.label, trailing: <span>{N.legal.count}</span> },
    ],
  },
  {
    id: N.schemas.id,
    label: N.schemas.label,
    trailing: <span>{N.schemas.count}</span>,
    children: [{ id: N.core.id, label: N.core.label, trailing: <span>{N.core.count}</span> }],
  },
];

/** A stateful host so selection + expansion actually update the DOM, with spies on the callbacks. */
function Harness({
  initialExpanded = [],
  initialSelected,
  onSelect: onSelectSpy,
  onToggle: onToggleSpy,
}: {
  initialExpanded?: string[];
  initialSelected?: string;
  onSelect?: (id: string) => void;
  onToggle?: (id: string) => void;
}) {
  const [expanded, setExpanded] = useState(() => new Set(initialExpanded));
  const [selected, setSelected] = useState<string | undefined>(initialSelected);
  return (
    <Tree
      nodes={NODES}
      ariaLabel={TREE_LABEL}
      selectedId={selected}
      expanded={expanded}
      onSelect={(id) => {
        onSelectSpy?.(id);
        setSelected(id);
      }}
      onToggle={(id) => {
        onToggleSpy?.(id);
        setExpanded((prev) => {
          const next = new Set(prev);
          if (next.has(id)) next.delete(id);
          else next.add(id);
          return next;
        });
      }}
    />
  );
}

/** The treeitem element that owns a given row label. */
function itemFor(label: string): HTMLElement {
  const el = screen.getByText(label).closest('[role="treeitem"]');
  if (!el) throw new Error(`no treeitem for ${label}`);
  return el as HTMLElement;
}

const expandBtn = (label: string) => screen.getByRole("button", { name: treeTwistLabel(false, label) });
const collapseBtn = (label: string) => screen.getByRole("button", { name: treeTwistLabel(true, label) });

describe("Tree — structure + ARIA", () => {
  it("is a named tree whose leaves carry no twist", () => {
    render(<Harness />);
    expect(screen.getByRole("tree", { name: TREE_LABEL })).toBeInTheDocument();
    expect(within(itemFor(N.all.label)).queryByRole("button")).toBeNull();
    expect(itemFor(N.all.label)).not.toHaveAttribute("aria-expanded");
    expect(itemFor(N.conv.label)).toHaveAttribute("aria-expanded", "false");
    expect(expandBtn(N.conv.label)).toBeInTheDocument();
  });

  it("keeps a collapsed subtree inert until it expands", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    expect(itemFor(N.conv.label).querySelector('[role="group"]')).toHaveAttribute("inert");

    await user.click(expandBtn(N.conv.label));
    expect(itemFor(N.conv.label)).toHaveAttribute("aria-expanded", "true");
    expect(itemFor(N.conv.label).querySelector('[role="group"]')).not.toHaveAttribute("inert");
  });
});

describe("Tree — selection", () => {
  it("clicking a row selects only it", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    render(<Harness initialExpanded={[N.conv.id]} onSelect={onSelect} />);
    await user.click(screen.getByText(N.finance.label));
    expect(onSelect).toHaveBeenCalledWith(N.finance.id);
    expect(itemFor(N.finance.label)).toHaveAttribute("aria-selected", "true");
    expect(itemFor(N.conv.label)).toHaveAttribute("aria-selected", "false");
  });

  it("clicking the twist expands without selecting", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    const onToggle = vi.fn();
    render(<Harness onSelect={onSelect} onToggle={onToggle} />);
    await user.click(expandBtn(N.conv.label));
    expect(onToggle).toHaveBeenCalledWith(N.conv.id);
    // The whole point of stopPropagation.
    expect(onSelect).not.toHaveBeenCalled();
  });
});

describe("Tree — roving tabindex + keyboard", () => {
  it.each([
    { label: "a selection", selected: N.conv.id, stop: N.conv.label },
    { label: "the first row when nothing is selected", selected: undefined, stop: N.all.label },
  ])("puts the single tab stop on $label", ({ selected, stop }) => {
    render(<Harness initialSelected={selected} />);
    const stops = screen.getAllByRole("treeitem").filter((el) => el.getAttribute("tabindex") === "0");
    expect(stops).toEqual([itemFor(stop)]);
  });

  it("keeps a reachable tab stop after a collapse hides the focused row", async () => {
    // Move the roving stop onto a child by keyboard (a raw .focus() would not update focusId), then
    // collapse its parent. The stop must re-derive against the visible set instead of stranding on an
    // inert row.
    const user = userEvent.setup();
    render(<Harness initialExpanded={[N.conv.id]} />);
    itemFor(N.conv.label).focus();
    await user.keyboard("{ArrowDown}");
    expect(itemFor(N.finance.label)).toHaveAttribute("tabindex", "0");

    await user.click(collapseBtn(N.conv.label));
    expect(itemFor(N.finance.label)).toHaveAttribute("tabindex", "-1");
    const stops = screen.getAllByRole("treeitem").filter((el) => el.getAttribute("tabindex") === "0");
    expect(stops).toHaveLength(1);
    expect(stops[0].closest('[role="group"][inert]')).toBeNull();
  });

  it.each(["{Enter}", " "])("%s selects the focused item", async (key) => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    render(<Harness onSelect={onSelect} />);
    itemFor(N.schemas.label).focus();
    await user.keyboard(key);
    expect(onSelect).toHaveBeenCalledWith(N.schemas.id);
  });

  it.each([
    { key: "{ArrowDown}", from: N.schemas.label, label: "ArrowDown at the last item" },
    { key: "{ArrowUp}", from: N.all.label, label: "ArrowUp at the first item" },
  ])("$label does not wrap", async ({ key, from }) => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    render(<Harness onSelect={onSelect} />);
    itemFor(from).focus();
    await user.keyboard(key);
    expect(itemFor(from)).toHaveFocus();
    expect(onSelect).not.toHaveBeenCalled();
  });

  // The horizontal keys carry the tree's whole open/descend/ascend model, including the branches that
  // must NOT toggle: an open parent descends, a leaf is inert, a child ascends, a closed root stalls.
  it.each([
    {
      label: "ArrowRight opens a closed parent in place",
      key: "{ArrowRight}",
      expanded: [],
      from: N.conv.label,
      focus: N.conv.label,
      toggled: N.conv.id,
    },
    {
      label: "ArrowRight descends into an open parent",
      key: "{ArrowRight}",
      expanded: [N.conv.id],
      from: N.conv.label,
      focus: N.finance.label,
      toggled: undefined,
    },
    {
      label: "ArrowRight on a leaf does nothing",
      key: "{ArrowRight}",
      expanded: [],
      from: N.all.label,
      focus: N.all.label,
      toggled: undefined,
    },
    {
      label: "ArrowLeft collapses an open parent in place",
      key: "{ArrowLeft}",
      expanded: [N.conv.id],
      from: N.conv.label,
      focus: N.conv.label,
      toggled: N.conv.id,
    },
    {
      label: "ArrowLeft on a child jumps to its parent",
      key: "{ArrowLeft}",
      expanded: [N.conv.id],
      from: N.finance.label,
      focus: N.conv.label,
      toggled: undefined,
    },
    {
      label: "ArrowLeft on a closed root does nothing",
      key: "{ArrowLeft}",
      expanded: [],
      from: N.conv.label,
      focus: N.conv.label,
      toggled: undefined,
    },
  ])("$label", async ({ key, expanded, from, focus, toggled }) => {
    const user = userEvent.setup();
    const onToggle = vi.fn();
    render(<Harness initialExpanded={expanded} onToggle={onToggle} />);
    itemFor(from).focus();
    await user.keyboard(key);
    expect(itemFor(focus)).toHaveFocus();
    if (toggled) expect(onToggle).toHaveBeenCalledWith(toggled);
    else expect(onToggle).not.toHaveBeenCalled();
  });

  it("Home jumps to the first visible item; End to the last", async () => {
    const user = userEvent.setup();
    render(<Harness initialExpanded={[N.conv.id]} />);
    itemFor(N.legal.label).focus();
    await user.keyboard("{Home}");
    expect(itemFor(N.all.label)).toHaveFocus();
    await user.keyboard("{End}");
    // Schemas keeps its own child collapsed, so it is the last visible row.
    expect(itemFor(N.schemas.label)).toHaveFocus();
  });

  it("ArrowDown skips a collapsed subtree", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    itemFor(N.conv.label).focus();
    await user.keyboard("{ArrowDown}");
    expect(itemFor(N.schemas.label)).toHaveFocus();
  });

  it("ArrowDown walks an expanded subtree before the next sibling", async () => {
    const user = userEvent.setup();
    render(<Harness initialExpanded={[N.conv.id]} />);
    itemFor(N.conv.label).focus();
    await user.keyboard("{ArrowDown}");
    expect(itemFor(N.finance.label)).toHaveFocus();
    await user.keyboard("{ArrowDown}");
    expect(itemFor(N.legal.label)).toHaveFocus();
    await user.keyboard("{ArrowDown}");
    expect(itemFor(N.schemas.label)).toHaveFocus();
  });
});

describe("Tree — a row the caller refuses", () => {
  const REASON = "You can't pick this one";

  /** The same fixture with one branch and one leaf marked unselectable — the
   *  shape a picker uses when the rows it cannot offer still have to be seen. */
  function RefusingHarness({ onSelect }: { onSelect: (id: string) => void }) {
    const refuse = (node: TreeNode): TreeNode =>
      node.id === N.conv.id || node.id === N.all.id
        ? { ...node, disabled: true, disabledReason: REASON }
        : node;
    return (
      <Tree
        nodes={NODES.map(refuse)}
        ariaLabel={TREE_LABEL}
        expanded={new Set([N.conv.id])}
        onSelect={onSelect}
        onToggle={() => {}}
      />
    );
  }

  it("draws a refused row, marks it, and says why", () => {
    render(<RefusingHarness onSelect={vi.fn()} />);
    // Still on screen: a tree with its unselectable rows removed is no longer
    // the shape of the thing being picked inside.
    expect(itemFor(N.all.label)).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByText(N.all.label).closest(".alk-tree__row")).toHaveAttribute(
      "title",
      REASON,
    );
    expect(itemFor(N.schemas.label)).not.toHaveAttribute("aria-disabled");
  });

  it("answers for neither a click nor a keypress on a refused row", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    render(<RefusingHarness onSelect={onSelect} />);
    await user.click(screen.getByText(N.all.label));
    itemFor(N.all.label).focus();
    await user.keyboard("{Enter}");
    await user.keyboard(" ");
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("still answers for the rows beside it", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    render(<RefusingHarness onSelect={onSelect} />);
    await user.click(screen.getByText(N.schemas.label));
    expect(onSelect).toHaveBeenCalledWith(N.schemas.id);
  });

  it("keeps a refused row on the keyboard walk and lets its children answer", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    render(<RefusingHarness onSelect={onSelect} />);
    // Conventions is refused; arrowing through it must still reach finance
    // below it, which is not — a refused branch is not a wall.
    itemFor(N.conv.label).focus();
    await user.keyboard("{ArrowDown}");
    expect(itemFor(N.finance.label)).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(onSelect).toHaveBeenCalledWith(N.finance.id);
  });
});
