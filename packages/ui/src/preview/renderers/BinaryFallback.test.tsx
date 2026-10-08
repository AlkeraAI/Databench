import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { PreviewProps, PreviewStatus } from "../types";
import { BinaryFallback } from "./BinaryFallback";

afterEach(cleanup);

const MIB = 1024 * 1024;

function props(over: Partial<PreviewProps> = {}): PreviewProps {
  return {
    facts: { mime: "application/zip", name: "bundle.zip", size: 2_621_440 },
    content: { kind: "none" },
    version: "etag-1",
    status: "ready",
    onDownload: () => {},
    ...over,
  };
}

describe("the card a file gets when nothing can draw it", () => {
  it("names the file, what the server says it is, and how big it is", () => {
    render(<BinaryFallback {...props()} />);

    expect(screen.getByText("bundle.zip")).toBeInTheDocument();
    expect(screen.getByText("application/zip · 2.6 MB")).toBeInTheDocument();
    expect(screen.getByText("Preview is not available for this type")).toBeInTheDocument();
  });

  it.each([
    [10, "10 B"],
    [2048, "2 KB"],
    [2_621_440, "2.6 MB"],
    [120 * MIB, "125.8 MB"],
  ])("reads %d bytes as %s", (size, reading) => {
    render(<BinaryFallback {...props({ facts: { mime: "application/zip", name: "a", size } })} />);

    expect(screen.getByText(`application/zip · ${reading}`)).toBeInTheDocument();
  });

  it.each<[PreviewStatus, string]>([
    ["gone", "This file is no longer available"],
    ["pending", "Not synced yet"],
    ["error", "The preview could not be loaded"],
  ])("explains %s", (status, reason) => {
    render(
      <BinaryFallback
        {...props({
          status,
          facts: { mime: "image/png", name: "big.png", size: 120 * MIB },
        })}
      />,
    );

    expect(screen.getByText(reason)).toBeInTheDocument();
  });

  describe("a file whose bytes have not arrived", () => {
    const unsynced = {
      mime: "application/octet-stream",
      name: "linked-q3.png",
      size: 0,
      synced: false,
    };

    it("names the machine it is fetching from instead of blaming the type", () => {
      render(<BinaryFallback {...props({ facts: { ...unsynced, machine: "the box" } })} />);

      expect(screen.getByText("Fetching from the box…")).toBeInTheDocument();
      expect(screen.queryByText("Preview is not available for this type")).toBeNull();
    });

    it("names the machine on a pending refusal too", () => {
      render(
        <BinaryFallback
          {...props({
            status: "pending",
            facts: {
              mime: "text/plain",
              name: "a.txt",
              size: 10,
              machine: "demo-box",
            },
          })}
        />,
      );

      expect(screen.getByText("Fetching from demo-box…")).toBeInTheDocument();
    });

    it("says what the host knows about the wait in place of its own sentence", () => {
      render(
        <BinaryFallback
          {...props({
            status: "pending",
            pendingReason: "Demo-box is offline · showing nothing yet",
            facts: {
              mime: "text/plain",
              name: "a.txt",
              size: 10,
              machine: "demo-box",
            },
          })}
        />,
      );

      expect(
        screen.getByText("Demo-box is offline · showing nothing yet"),
      ).toBeInTheDocument();
      expect(screen.queryByText("Fetching from demo-box…")).toBeNull();
    });

    it("keeps the host's wait sentence to a pending refusal", () => {
      // A file the facts call unsynced but the host did not refuse is not the
      // host's wait, so its sentence has no business on the card.
      render(
        <BinaryFallback
          {...props({
            pendingReason: "Demo-box is busy · trying again",
            facts: { ...unsynced, machine: "the box" },
          })}
        />,
      );

      expect(screen.getByText("Fetching from the box…")).toBeInTheDocument();
      expect(screen.queryByText("Demo-box is busy · trying again")).toBeNull();
    });

    it("says so plainly when the host has no machine to name", () => {
      render(<BinaryFallback {...props({ facts: unsynced })} />);

      expect(screen.getByText("Not synced yet")).toBeInTheDocument();
    });

    it("does not repeat the mime nobody set or the size of nothing", () => {
      render(<BinaryFallback {...props({ facts: unsynced })} />);

      expect(screen.queryByText("application/octet-stream · 0 B")).toBeNull();
      expect(screen.getByText("linked-q3.png")).toBeInTheDocument();
    });

    it("still offers the way out", () => {
      render(<BinaryFallback {...props({ facts: unsynced })} />);

      expect(screen.getByRole("button", { name: "Download" })).toBeInTheDocument();
    });

    it("is a picture waiting, not a generic file", () => {
      render(<BinaryFallback {...props({ facts: unsynced })} />);

      expect(screen.getByTestId("preview-fallback")).toHaveAttribute("data-kind", "image");
    });

    it("leaves an empty file whose bytes DID land alone", () => {
      // An upload that never stamped an mtime still committed a version. Keying
      // the wait on "no bytes and no stamp" told such a file it was still
      // arriving, forever.
      render(
        <BinaryFallback
          {...props({
            facts: {
              mime: "text/plain",
              name: "empty.txt",
              size: 0,
              synced: true,
            },
          })}
        />,
      );

      expect(screen.queryByText("Not synced yet")).toBeNull();
    });

    it("leaves an empty file that DID sync alone", () => {
      // Zero bytes with a stamp is a real, empty file — not one still on its way.
      render(
        <BinaryFallback
          {...props({
            facts: {
              mime: "application/zip",
              name: "empty.zip",
              size: 0,
              synced: true,
            },
          })}
        />,
      );

      expect(screen.getByText("Preview is not available for this type")).toBeInTheDocument();
      expect(screen.queryByText("Not synced yet")).toBeNull();
    });
  });

  it.each([
    ["book.xlsx", "No in-browser preview yet for an Excel workbook"],
    ["memo.docx", "No in-browser preview yet for a Word document"],
    ["deck.pptx", "No in-browser preview yet for a PowerPoint presentation"],
  ])("names the format for %s rather than reading out a mime", (name, reason) => {
    render(
      <BinaryFallback
        {...props({
          facts: {
            mime: "application/octet-stream",
            name,
            size: 4096,
            synced: true,
          },
        })}
      />,
    );

    expect(screen.getByText(reason)).toBeInTheDocument();
  });

  it("prefers the host's own account of a failure", () => {
    render(<BinaryFallback {...props({ status: "error", error: "The file changed — reload" })} />);

    expect(screen.getByText("The file changed — reload")).toBeInTheDocument();
  });

  it("always offers the way out", () => {
    const onDownload = vi.fn();
    render(<BinaryFallback {...props({ status: "gone", onDownload })} />);

    fireEvent.click(screen.getByRole("button", { name: "Download" }));

    expect(onDownload).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["image/png", "image"],
    ["video/mp4", "video"],
    ["audio/mpeg", "audio"],
    ["application/pdf", "pdf"],
    ["text/csv", "text"],
    ["application/zip", "file"],
  ])("marks %s with the %s glyph", (mime, kind) => {
    render(<BinaryFallback {...props({ facts: { mime, name: "a", size: 10 } })} />);

    expect(screen.getByTestId("preview-fallback").dataset.kind).toBe(kind);
  });
});
