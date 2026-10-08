import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";

import { cleanup, render, screen } from "@testing-library/react";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";

import { LoadingIndicator, type LoadingForm, type LoadingSize } from "./LoadingIndicator";

// Tests for the @alkera/ui LoadingIndicator — one wait, said one way. What a caller can observe is
// the announcement (a live region carrying what is being waited on), the form ladder (which mark
// stands in for the wait), and the size vocabulary each form reads that word in.
//
// The forms COMPOSE the marks the estate already ships. A form that drew its own glyph would look
// right and drift the moment the real one changed, so each case asserts the composed component's
// own seam rather than a shape.

afterEach(cleanup);

const LABEL = "Reading the depot";
const FORMS: LoadingForm[] = ["spinner", "skeleton", "meter"];

const plate = (): HTMLElement => screen.getByRole("status");
const spinners = (): Element[] => [...document.querySelectorAll(".alk-spinner")];
const caption = (): Element | null => document.querySelector(".alk-loading__label");

describe("LoadingIndicator announces the wait", () => {
  it("defaults to a spinner in a live region the label names", () => {
    render(<LoadingIndicator label={LABEL} className="mine" />);

    expect(plate()).toHaveAttribute("data-form", "spinner");
    expect(plate().className).toContain("mine");
    expect(spinners()).toHaveLength(1);
    // A spinner over silence says nothing about what is slow, so the label reads beside it too.
    expect(caption()).toHaveTextContent(LABEL);
  });

  it.each<LoadingForm>(FORMS)("%s carries the label as its announced name", (form) => {
    render(<LoadingIndicator form={form} label={LABEL} value={0.5} />);
    expect(plate()).toHaveAccessibleName(LABEL);
  });

  it("names itself when the caller has nothing to say", () => {
    render(<LoadingIndicator />);
    expect(plate()).toHaveAccessibleName("Loading"); // pins-source: user-visible
    expect(caption()).toBeNull();
  });

  it("leaves a skeleton uncaptioned, since it already occupies the shape of what is coming", () => {
    render(<LoadingIndicator form="skeleton" label={LABEL} />);
    expect(caption()).toBeNull();
  });
});

describe("LoadingIndicator composes the estate's own marks", () => {
  it("draws the spinner the buttons and fields already use", () => {
    render(<LoadingIndicator form="spinner" />);
    expect(spinners()).toHaveLength(1);
    expect(document.querySelector(".alk-skeleton")).toBeNull();
    expect(screen.queryByRole("progressbar")).toBeNull();
  });

  it("stands rows of the Skeleton placeholder in for the content to come", () => {
    render(<LoadingIndicator form="skeleton" />);
    expect(document.querySelectorAll(".alk-skeleton").length).toBeGreaterThan(0);
    expect(spinners()).toHaveLength(0);
  });

  it("hands a measured wait to the Meter, the one form with a fraction to draw", () => {
    render(<LoadingIndicator form="meter" value={0.4} />);
    const bar = screen.getByRole("progressbar");
    expect(bar).toHaveClass("alk-meter");
    expect(bar).toHaveAttribute("aria-valuenow", "40");
    expect(spinners()).toHaveLength(0);
  });
});

describe("LoadingIndicator sizes every form off one word", () => {
  it.each([
    { size: "sm" as const, px: "16", rows: 1 },
    { size: "md" as const, px: "24", rows: 3 },
    { size: "lg" as const, px: "32", rows: 5 },
  ])("$size draws a $px spinner and $rows skeleton rows", ({ size, px, rows }) => {
    render(<LoadingIndicator size={size} />);
    expect(spinners()[0]).toHaveAttribute("width", px);
    expect(spinners()[0]).toHaveAttribute("height", px);

    cleanup();
    render(<LoadingIndicator form="skeleton" size={size} />);
    expect(document.querySelectorAll(".alk-skeleton")).toHaveLength(rows);
  });

  // The Meter's own ladder stops at md, so both larger words land on the same bar rather than
  // asking it for a step it does not have.
  it.each<[LoadingSize, string | null]>([
    ["sm", null],
    ["md", "md"],
    ["lg", "md"],
  ])("%s reads the meter at %s", (size, attr) => {
    render(<LoadingIndicator form="meter" value={0.5} size={size} />);
    const bar = screen.getByRole("progressbar");
    if (attr) expect(bar).toHaveAttribute("data-size", attr);
    else expect(bar).not.toHaveAttribute("data-size");
  });
});

describe("loading-indicator.css — the wait sits where the content will", () => {
  /** Read the stylesheet next to the test. vitest's CSS pipeline empties `?raw` imports, so the
   *  test's own path is the anchor (see TextInput.collapsible.test.tsx). */
  function readCss(name: string): string {
    const testPath = expect.getState().testPath;
    if (!testPath) throw new Error("vitest testPath unavailable, cannot locate the stylesheet");
    return readFileSync(join(dirname(testPath), name), "utf8");
  }

  let sheet: HTMLStyleElement;
  beforeAll(() => {
    sheet = document.createElement("style");
    sheet.textContent = readCss("loading-indicator.css");
    document.head.append(sheet);
  });
  afterAll(() => sheet.remove());

  it("centres a spinner in the space it was given", () => {
    render(<LoadingIndicator label={LABEL} />);
    const box = getComputedStyle(plate());
    expect(box.display).toBe("flex");
    expect(box.alignItems).toBe("center");
    expect(box.justifyContent).toBe("center");
  });

  it("lets a skeleton take the content's width instead of centring", () => {
    render(<LoadingIndicator form="skeleton" />);
    expect(getComputedStyle(plate()).alignItems).toBe("stretch");
  });
});
