import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { PreviewProps } from "../types";
import { ImagePreview, SvgImagePreview } from "./ImagePreview";

afterEach(cleanup);

function props(over: Partial<PreviewProps> = {}): PreviewProps {
  return {
    facts: { mime: "image/png", name: "chart.png", size: 40_000 },
    content: { kind: "blob", url: "blob:alkera/one", mime: "image/png" },
    version: "etag-1",
    status: "ready",
    onDownload: () => {},
    ...over,
  };
}

function image(): HTMLImageElement {
  return screen.getByRole("img") as HTMLImageElement;
}

function scaleOf(): number {
  const drawn = image().style.transform;
  const scale = /scale\(([\d.]+)\)/.exec(drawn);
  if (!scale) throw new Error(`no scale in "${drawn}"`);
  return Number(scale[1]);
}

describe("an image preview", () => {
  it("draws the bytes the host fetched, named for the file", () => {
    render(<ImagePreview {...props()} />);

    expect(image()).toHaveAttribute("src", "blob:alkera/one");
    expect(image()).toHaveAttribute("alt", "chart.png");
  });

  it("switches between fitting the pane and actual size", () => {
    const onViewState = vi.fn();
    render(<ImagePreview {...props({ onViewState })} />);

    const toggle = screen.getByRole("button", { name: /actual size/i });
    expect(toggle).toHaveAttribute("aria-pressed", "false");

    fireEvent.click(toggle);

    expect(screen.getByRole("button", { name: /actual size/i })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(image().dataset.mode).toBe("actual");
    expect(onViewState).toHaveBeenCalledWith({ mode: "actual", scale: 1 });
  });

  it("zooms under the wheel, and stops at the ends", () => {
    render(<ImagePreview {...props()} />);
    const stage = screen.getByTestId("preview-image-stage");

    fireEvent.wheel(stage, { deltaY: -100 });
    const zoomedIn = scaleOf();
    expect(zoomedIn).toBeGreaterThan(1);

    fireEvent.wheel(stage, { deltaY: 100 });
    expect(scaleOf()).toBeLessThan(zoomedIn);

    for (let i = 0; i < 80; i += 1) fireEvent.wheel(stage, { deltaY: -100 });
    expect(scaleOf()).toBe(8);

    for (let i = 0; i < 160; i += 1) fireEvent.wheel(stage, { deltaY: 100 });
    expect(scaleOf()).toBe(0.1);
  });

  it("keeps the zoom when the file is written again", () => {
    // The pane is how you watch a chart being redrawn. Snapping back to fit on
    // every write would throw away the part you had zoomed into.
    const { rerender } = render(<ImagePreview {...props()} />);
    fireEvent.wheel(screen.getByTestId("preview-image-stage"), { deltaY: -100 });
    const zoomed = scaleOf();

    rerender(
      <ImagePreview
        {...props({
          version: "etag-2",
          content: { kind: "blob", url: "blob:alkera/two", mime: "image/png" },
        })}
      />,
    );

    expect(scaleOf()).toBe(zoomed);
    expect(image()).toHaveAttribute("src", "blob:alkera/two");
  });

  it("opens where the host last left it", () => {
    render(<ImagePreview {...props({ viewState: { mode: "actual", scale: 2 } })} />);

    expect(scaleOf()).toBe(2);
    expect(image().dataset.mode).toBe("actual");
  });

  it("ignores view state it did not write", () => {
    render(<ImagePreview {...props({ viewState: { sort: "name" } })} />);

    expect(image().dataset.mode).toBe("fit");
  });

  it("says so when there are no bytes", () => {
    render(<ImagePreview {...props({ content: { kind: "none" } })} />);

    expect(screen.queryByRole("img")).toBeNull();
    expect(screen.getByRole("status")).toBeInTheDocument();
  });
});

describe("an SVG preview", () => {
  const svgProps = props({
    facts: { mime: "image/svg+xml", name: "flow.svg", size: 2000 },
    content: { kind: "blob", url: "blob:alkera/svg", mime: "image/svg+xml" },
  });

  it("draws the drawing as an image and nothing else", () => {
    // An `<img>` is the neutering: script, foreignObject and external fetches in
    // the document are all inert. Inlining it, or framing it, would run them.
    const { container } = render(<SvgImagePreview {...svgProps} />);

    expect(image()).toHaveAttribute("src", "blob:alkera/svg");
    expect(image()).toHaveAttribute("alt", "flow.svg");
    expect(container.querySelector("svg")).toBeNull();
    expect(container.querySelector("object")).toBeNull();
    expect(container.querySelector("embed")).toBeNull();
    expect(container.querySelector("iframe")).toBeNull();
  });

  it("carries no zoom controls", () => {
    render(<SvgImagePreview {...svgProps} />);

    expect(screen.queryByRole("button")).toBeNull();
  });

  it("says so when there are no bytes", () => {
    render(<SvgImagePreview {...props({ content: { kind: "none" } })} />);

    expect(screen.queryByRole("img")).toBeNull();
    expect(screen.getByRole("status")).toBeInTheDocument();
  });
});

// A name is not a format. A reader who renames an ELF binary `qa-elf.png` gets a
// PNG card, an `<img>` that cannot decode, and — before this — the browser's own
// broken-image glyph with the alt text beside it in an otherwise blank pane, plus
// an "Actual size" control that did nothing. The same modal has a good refusal
// for a file it cannot draw, so a failed decode is routed to it.
describe("a file whose bytes are not the picture its name promises", () => {
  function brokenImage(): void {
    fireEvent.error(screen.getByRole("img", { hidden: true }));
  }

  it("falls back to the refusal the modal already has for a type it cannot draw", () => {
    render(<ImagePreview {...props({ facts: { mime: "image/png", name: "qa-elf.png", size: 2_100 } })} />);
    brokenImage();

    expect(screen.queryByRole("img", { hidden: true })).toBeNull();
    expect(screen.getByText("Preview is not available for this type")).toBeInTheDocument();
    expect(screen.getByText("qa-elf.png")).toBeInTheDocument();
  });

  it("drops the framing control, which had nothing left to frame", () => {
    render(<ImagePreview {...props()} />);
    expect(screen.getByRole("button", { name: /actual size/i })).toBeInTheDocument();

    brokenImage();

    expect(screen.queryByRole("button", { name: /actual size/i })).toBeNull();
  });

  it("offers the way out: the bytes themselves", () => {
    const onDownload = vi.fn();
    render(<ImagePreview {...props({ onDownload })} />);
    brokenImage();

    screen.getByRole("button", { name: "Download" }).click();
    expect(onDownload).toHaveBeenCalled();
  });

  it("tries again when the bytes are replaced, so a new version is not judged by the old one", () => {
    const { rerender } = render(<ImagePreview {...props()} />);
    brokenImage();
    expect(screen.queryByRole("img", { hidden: true })).toBeNull();

    rerender(
      <ImagePreview
        {...props({ content: { kind: "blob", url: "blob:alkera/two", mime: "image/png" } })}
      />,
    );

    expect(screen.getByRole("img", { hidden: true })).toHaveAttribute("src", "blob:alkera/two");
    expect(screen.queryByText("Preview is not available for this type")).toBeNull();
  });

  it("does the same for a drawing that will not render", () => {
    render(<SvgImagePreview {...props({ facts: { mime: "image/svg+xml", name: "flow.svg", size: 2_000 } })} />);
    brokenImage();

    expect(screen.queryByRole("img", { hidden: true })).toBeNull();
    expect(screen.getByText("Preview is not available for this type")).toBeInTheDocument();
  });
});
