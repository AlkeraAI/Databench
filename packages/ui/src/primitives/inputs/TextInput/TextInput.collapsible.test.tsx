import { useState, type CSSProperties, type ReactNode } from "react";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TextInput } from "./TextInput";
import { pushEsc } from "../../../hooks";

// The search flavour (`type="search" collapsible`) has two modes chosen by a media query: PLAIN
// (there's room, a bare field) and COLLAPSED (narrow, a magnifier toggle that expands on click).
// The tests drive both. `matchMedia` is the only seam that selects the mode, so each suite pins it:
// `matches: true` forces the collapsed path, `false` the plain path. We assert only what a user /
// AT observes — the field's presence and focus, the toggle's presence, the onChange payload, the
// fold triggers — never a class. (The folded field is hidden via CSS `visibility`, which jsdom
// doesn't apply, so tab-unreachability while folded is a live-browser concern, not assertable here.)

const LABEL = "Search the workspace"; // the accessible name the caller supplies; owned by the test
const PLACEHOLDER = "Search the catalog";
// The collapsed magnifier inherits the caller's aria-label (falling back to a generic "Search" only
// when the caller names nothing) — so the toggle announces WHAT it searches, same as the field.
const TOGGLE = LABEL;
const WIDTH_VAR = { "--alk-search-w": "22rem" } as CSSProperties;

/** Install a controllable collapse media query. `matches` seeds the collapsed state; the returned
 *  `set(next)` flips it and notifies subscribers (so a test can cross the breakpoint at runtime).
 *  The `prefers-reduced-motion` query always resolves false so motion paths render normally. */
function stubMediaQuery(matches: boolean) {
  let narrow = matches;
  const listeners = new Set<() => void>();
  window.matchMedia = ((query: string) => ({
    get matches() {
      return query.includes("prefers-reduced-motion") ? false : narrow;
    },
    media: query,
    onchange: null,
    addListener: (cb: () => void) => listeners.add(cb),
    removeListener: (cb: () => void) => listeners.delete(cb),
    addEventListener: (_: string, cb: () => void) => listeners.add(cb),
    removeEventListener: (_: string, cb: () => void) => listeners.delete(cb),
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
  return {
    set(next: boolean) {
      narrow = next;
      act(() => listeners.forEach((cb) => cb()));
    },
  };
}

/** A controlled collapsible search that owns its own value, so typing / clearing behaves like a
 *  real parent (every call site is controlled). */
function Harness({ initial = "", rightSection }: { initial?: string; rightSection?: ReactNode }) {
  const [value, setValue] = useState(initial);
  return (
    <TextInput
      type="search"
      collapsible
      value={value}
      onChange={(e) => setValue(e.target.value)}
      placeholder={PLACEHOLDER}
      aria-label={LABEL}
      rightSection={rightSection}
    />
  );
}

afterEach(cleanup);

describe("TextInput collapsible search — plain mode (there's room for the field)", () => {
  beforeEach(() => stubMediaQuery(false));

  it("renders the field directly with no expand toggle", () => {
    render(<TextInput type="search" collapsible value="" onChange={() => {}} aria-label={LABEL} placeholder={PLACEHOLDER} />);
    // The field is the accessible searchbox; there is no toggle to press (and no clear ✕ — empty).
    expect(screen.getByRole("searchbox", { name: LABEL })).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("types and clears through the controlled parent", async () => {
    const user = userEvent.setup();
    render(<Harness initial="" />);
    const field = screen.getByRole("searchbox", { name: LABEL });
    await user.type(field, "assay");
    expect(field).toHaveValue("assay");
    // The reset reaches the parent, not just the DOM node, and the clear button only exists while
    // there is a value to clear.
    await user.click(screen.getByRole("button", { name: "Clear search" }));
    expect(field).toHaveValue("");
    expect(field).toHaveFocus();
    expect(screen.queryByRole("button", { name: "Clear search" })).toBeNull();
  });

  it("does not reserve a clear gutter until the controlled search has a value", async () => {
    const user = userEvent.setup();
    render(<Harness rightSection={<span aria-hidden="true" />} />);
    const field = screen.getByRole("searchbox", { name: LABEL });

    expect(field).toHaveAttribute("data-trail", "1");
    await user.type(field, "a");
    expect(field).toHaveAttribute("data-trail", "2");
    await user.click(screen.getByRole("button"));
    expect(field).toHaveAttribute("data-trail", "1");
  });
});

describe("TextInput collapsible search — collapsed mode (narrow)", () => {
  beforeEach(() => stubMediaQuery(true));

  it("starts folded: the magnifier toggle stands in, reporting aria-expanded=false", () => {
    render(<TextInput type="search" collapsible value="" onChange={() => {}} aria-label={LABEL} placeholder={PLACEHOLDER} />);
    expect(screen.getByRole("button", { name: TOGGLE })).toHaveAttribute("aria-expanded", "false");
  });

  it("expands on click, focuses the field, and the toggle stands down while open", async () => {
    const user = userEvent.setup();
    render(<TextInput type="search" collapsible value="" onChange={() => {}} aria-label={LABEL} placeholder={PLACEHOLDER} />);
    await user.click(screen.getByRole("button", { name: TOGGLE }));
    const field = screen.getByRole("searchbox", { name: LABEL });
    await waitFor(() => expect(field).toHaveFocus());
    // While expanded the field covers the toggle's slot — the toggle is gone from the tree, so a
    // screen reader can't land on a control the pointer can no longer reach.
    expect(screen.queryByRole("button", { name: TOGGLE })).toBeNull();
  });

  it("blur-when-empty folds back, but blur-with-a-value stays open", async () => {
    const user = userEvent.setup();
    render(<Harness initial="" />);

    // Open, leave empty, blur → folds (the toggle returns).
    await user.click(screen.getByRole("button", { name: TOGGLE }));
    await waitFor(() => expect(screen.getByRole("searchbox", { name: LABEL })).toHaveFocus());
    await user.tab();
    expect(screen.getByRole("button", { name: TOGGLE })).toHaveAttribute("aria-expanded", "false");

    // Open again, type, blur → stays open AND keeps the query (the whole point of the asymmetric
    // case: a query in progress must not vanish on blur).
    const QUERY = "descent";
    await user.click(screen.getByRole("button", { name: TOGGLE }));
    const field = screen.getByRole("searchbox", { name: LABEL });
    await waitFor(() => expect(field).toHaveFocus());
    await user.type(field, QUERY);
    await user.tab();
    expect(screen.queryByRole("button", { name: TOGGLE })).toBeNull();
    expect(screen.getByRole("searchbox", { name: LABEL })).toHaveValue(QUERY);
  });
});

describe("TextInput collapsible search — Escape + toggle naming", () => {
  beforeEach(() => stubMediaQuery(true));

  it("Escape on an EMPTY expanded search folds it and returns focus to the toggle", async () => {
    const user = userEvent.setup();
    render(<Harness initial="" />);
    await user.click(screen.getByRole("button", { name: TOGGLE }));
    const field = screen.getByRole("searchbox", { name: LABEL });
    await waitFor(() => expect(field).toHaveFocus());
    await user.keyboard("{Escape}");
    // Folded again — and the keyboard user lands back on the toggle, not in a void.
    const toggle = screen.getByRole("button", { name: TOGGLE });
    await waitFor(() => expect(toggle).toHaveFocus());
    expect(toggle).toHaveAttribute("aria-expanded", "false");
  });

  it("Escape with a value does NOT fold (native search-clear gets the first Escape)", async () => {
    const user = userEvent.setup();
    render(<Harness initial="" />);
    await user.click(screen.getByRole("button", { name: TOGGLE }));
    const field = screen.getByRole("searchbox", { name: LABEL });
    await waitFor(() => expect(field).toHaveFocus());
    await user.type(field, "descent");
    await user.keyboard("{Escape}");
    // Still expanded: a query in progress is never thrown away by the fold shortcut. (jsdom doesn't
    // implement WebKit's native clear-on-Escape, so the value also survives here.)
    expect(screen.queryByRole("button", { name: TOGGLE })).toBeNull();
    expect(screen.getByRole("searchbox", { name: LABEL })).toBeInTheDocument();
  });

  it("the fold takes the Escape alone, leaving the next to the page", async () => {
    // The fold rides the shared overlay stack, so one press dismisses exactly one surface. A
    // field-local keydown fold leaked the same press to the page layer beneath it.
    const user = userEvent.setup();
    const pageLayer = vi.fn();
    const unregister = pushEsc(pageLayer);
    try {
      render(<Harness initial="" />);
      await user.click(screen.getByRole("button", { name: TOGGLE }));
      await waitFor(() => expect(screen.getByRole("searchbox", { name: LABEL })).toHaveFocus());
      await user.keyboard("{Escape}");
      // Folded — and the press was consumed whole; the page layer beneath never fired.
      expect(screen.getByRole("button", { name: TOGGLE })).toHaveAttribute("aria-expanded", "false");
      expect(pageLayer).not.toHaveBeenCalled();
      // Folded, the search holds no stack entry: the next press belongs to the page layer.
      await user.keyboard("{Escape}");
      expect(pageLayer).toHaveBeenCalledTimes(1);
    } finally {
      unregister();
    }
  });

});

describe("TextInput collapsible search — crossing the breakpoint at runtime", () => {
  it("widening past the collapse query reverts an expanded search to the plain field", async () => {
    const user = userEvent.setup();
    const mq = stubMediaQuery(true); // start narrow
    render(<Harness initial="" />);

    // Open the folded search.
    await user.click(screen.getByRole("button", { name: TOGGLE }));
    await waitFor(() => expect(screen.getByRole("searchbox", { name: LABEL })).toHaveFocus());

    // The viewport widens past the collapse query → the toggle disappears and the plain field takes
    // over; the expanded state does not linger. (This exercises the media `change` subscription +
    // the reset-on-leave effect that a fixed-matchMedia test never reaches.)
    mq.set(false);
    expect(screen.queryByRole("button", { name: TOGGLE })).toBeNull();
    expect(screen.getByRole("searchbox", { name: LABEL })).toBeInTheDocument();
  });
});

describe("TextInput rootStyle", () => {
  // The inline dial lands on the outermost wrapper each shape owns, never on the input itself, so a
  // width var inherits down into the field.
  it.each([
    {
      label: "a bare search",
      node: <TextInput type="search" value="" onChange={() => {}} aria-label={LABEL} rootStyle={WIDTH_VAR} />,
      target: (input: HTMLElement) => input.parentElement!,
    },
    {
      label: "a collapsible search",
      // input -> .alk-input-affix -> .alk-input-collapse
      node: (
        <TextInput type="search" collapsible value="" onChange={() => {}} aria-label={LABEL} rootStyle={WIDTH_VAR} />
      ),
      target: (input: HTMLElement) => input.parentElement!.parentElement!,
    },
  ])("reaches the wrapper of $label", ({ node, target }) => {
    stubMediaQuery(false);
    render(node);
    const input = screen.getByRole("searchbox", { name: LABEL });
    expect(target(input).style.getPropertyValue("--alk-search-w")).toBe("22rem");
    expect(input.style.getPropertyValue("--alk-search-w")).toBe("");
  });

  it("reaches the labeled field's scaffold, not the input", () => {
    stubMediaQuery(false);
    render(<TextInput label="Issuer" value="" onChange={() => {}} rootStyle={{ maxWidth: "20rem" }} />);
    const input = screen.getByRole("textbox", { name: "Issuer" });
    expect(input.closest("div")!.parentElement!.style.maxWidth).toBe("20rem");
    expect(input.style.maxWidth).toBe("");
  });
});
