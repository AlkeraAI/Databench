import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { PreviewSurface } from "./PreviewSurface";
import { registerPreviewRenderer, unregisterPreviewRenderer } from "./registry";
import type { PreviewProps } from "./types";

afterEach(cleanup);

function props(over: Partial<PreviewProps> = {}): PreviewProps {
  return {
    facts: { mime: "image/png", name: "chart.png", size: 40_000 },
    content: { kind: "blob", url: "blob:alkera/chart", mime: "image/png" },
    version: "etag-1",
    status: "ready",
    onDownload: () => {},
    ...over,
  };
}

describe("the surface that draws a file", () => {
  it("hands an image to the image renderer", () => {
    render(<PreviewSurface {...props()} />);

    expect(screen.getByRole("img")).toHaveAttribute("alt", "chart.png");
  });

  it("hands an HTML document to a sandboxed frame", () => {
    const { container } = render(
      <PreviewSurface
        {...props({
          facts: { mime: "text/html", name: "report.html", size: 900 },
          content: {
            kind: "frame",
            url: "https://files.example/c/p/tok/report.html",
            sandboxed: true,
            title: "Preview of report.html (sandboxed)",
          },
        })}
      />,
    );

    expect(container.querySelector("iframe")?.getAttribute("sandbox")).toBe("");
  });

  it("hands a type nobody claims to the card", () => {
    render(
      <PreviewSurface
        {...props({
          facts: { mime: "application/zip", name: "bundle.zip", size: 4096 },
          content: { kind: "none" },
        })}
      />,
    );

    expect(screen.getByText("Preview is not available for this type")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Download" })).toBeInTheDocument();
  });

  it("asks the registry rather than deciding for itself", () => {
    // Teaching every surface a type is the registry's whole job: a renderer a host
    // registers for a type the library already draws must win here too.
    registerPreviewRenderer({
      id: "host-png",
      priority: 99,
      match: (facts) => facts.mime === "image/png",
      needs: () => "blob",
      Component: () => <p>drawn by the host</p>,
    });
    try {
      render(<PreviewSurface {...props()} />);

      expect(screen.getByText("drawn by the host")).toBeInTheDocument();
      expect(screen.queryByRole("img")).toBeNull();
    } finally {
      unregisterPreviewRenderer("host-png");
    }
  });

  it("draws a picture of any size the host handed it", () => {
    render(
      <PreviewSurface
        {...props({
          facts: { mime: "image/png", name: "huge.png", size: 120 * 1024 * 1024 },
          content: { kind: "blob", url: "blob:huge", mime: "image/png" },
          status: "ready",
        })}
      />,
    );

    expect(screen.getByRole("img")).toHaveAttribute("src", "blob:huge");
    expect(screen.queryByTestId("preview-fallback")).toBeNull();
  });

  it.each([
    ["gone", "This file is no longer available"],
    ["pending", "Not synced yet"],
    ["error", "The preview could not be loaded"],
  ] as const)("explains a %s file on the card instead of an empty pane", (status, reason) => {
    render(<PreviewSurface {...props({ status, content: { kind: "none" } })} />);

    expect(screen.getByText(reason)).toBeInTheDocument();
    expect(screen.queryByRole("img")).toBeNull();
  });

  it("puts the host's notice above bytes it drew, and keeps the bytes", () => {
    const notice = "Showing the copy from 12:04; demo-box has a newer one";
    const { container } = render(<PreviewSurface {...props({ notice })} />);

    const line = screen.getByRole("note");
    expect(line).toHaveTextContent(notice);
    expect(screen.getByRole("img")).toHaveAttribute("alt", "chart.png");
    // Above the renderer, so it is read before the copy it qualifies.
    const pane = container.querySelector(".alk-preview");
    expect(pane?.firstElementChild).toBe(line);
  });

  it("says nothing over a current copy", () => {
    render(<PreviewSurface {...props()} />);

    expect(screen.queryByRole("note")).toBeNull();
  });

  it("does not qualify a card that drew no bytes", () => {
    render(
      <PreviewSurface
        {...props({
          status: "gone",
          content: { kind: "none" },
          notice: "Showing the copy",
        })}
      />,
    );

    expect(screen.queryByRole("note")).toBeNull();
    expect(screen.getByText("This file is no longer available")).toBeInTheDocument();
  });

  it("waits quietly while the bytes are on their way", () => {
    render(<PreviewSurface {...props({ status: "loading", content: { kind: "none" } })} />);

    expect(screen.getByRole("status")).toBeInTheDocument();
    expect(screen.queryByRole("img")).toBeNull();
    expect(screen.queryByRole("button", { name: "Download" })).toBeNull();
  });
});
