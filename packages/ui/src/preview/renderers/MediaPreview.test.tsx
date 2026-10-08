import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { PreviewProps } from "../types";
import { MediaPreview } from "./MediaPreview";

afterEach(cleanup);

function props(over: Partial<PreviewProps> = {}): PreviewProps {
  return {
    facts: { mime: "video/mp4", name: "clip.mp4", size: 5_000_000 },
    content: { kind: "blob", url: "blob:alkera/clip", mime: "video/mp4" },
    version: "etag-1",
    status: "ready",
    onDownload: () => {},
    ...over,
  };
}

describe("a media preview", () => {
  it.each(["video/mp4", "video/webm"])("plays %s in a video element", (mime) => {
    const { container } = render(
      <MediaPreview
        {...props({
          facts: { mime, name: "clip", size: 10 },
          content: { kind: "blob", url: "blob:alkera/clip", mime },
        })}
      />,
    );

    const video = container.querySelector("video");
    expect(video).not.toBeNull();
    expect(video).toHaveAttribute("src", "blob:alkera/clip");
    expect(video).toHaveAttribute("controls");
    // A preview that starts playing on its own hijacks a room; the person presses play.
    expect(video?.hasAttribute("autoplay")).toBe(false);
    expect(container.querySelector("audio")).toBeNull();
  });

  it.each(["audio/mpeg", "audio/wav"])("plays %s in an audio element", (mime) => {
    const { container } = render(
      <MediaPreview
        {...props({
          facts: { mime, name: "note", size: 10 },
          content: { kind: "blob", url: "blob:alkera/note", mime },
        })}
      />,
    );

    const audio = container.querySelector("audio");
    expect(audio).not.toBeNull();
    expect(audio).toHaveAttribute("src", "blob:alkera/note");
    expect(audio).toHaveAttribute("controls");
    expect(audio?.hasAttribute("autoplay")).toBe(false);
    expect(container.querySelector("video")).toBeNull();
  });

  it("follows the file to its new bytes", () => {
    const { container, rerender } = render(<MediaPreview {...props()} />);

    rerender(
      <MediaPreview
        {...props({
          version: "etag-2",
          content: { kind: "blob", url: "blob:alkera/clip-2", mime: "video/mp4" },
        })}
      />,
    );

    expect(container.querySelector("video")).toHaveAttribute("src", "blob:alkera/clip-2");
  });

  it("says so when there are no bytes", () => {
    const { container } = render(<MediaPreview {...props({ content: { kind: "none" } })} />);

    expect(container.querySelector("video")).toBeNull();
    expect(screen.getByRole("status")).toBeInTheDocument();
  });
});
