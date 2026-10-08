import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { PreviewProps } from "../types";
import { FRAME_LOAD_TIMEOUT_MS, FramePreview } from "./FramePreview";

afterEach(cleanup);

function props(over: Partial<PreviewProps> = {}): PreviewProps {
  return {
    facts: { mime: "text/html", name: "report.html", size: 4096 },
    content: {
      kind: "frame",
      url: "https://files.example/c/p/tok-1/report.html",
      sandboxed: true,
      title: "Preview of report.html (sandboxed)",
    },
    version: "etag-1",
    status: "ready",
    onDownload: () => {},
    ...over,
  };
}

function frame(): HTMLIFrameElement {
  const el = document.querySelector("iframe");
  if (!el) throw new Error("no frame rendered");
  return el;
}

describe("a framed document", () => {
  it("locks an HTML document into a sandbox with no origin, no referrer and no capabilities", () => {
    render(<FramePreview {...props()} />);

    const el = frame();
    // An empty sandbox is the whole point: same-origin, scripts, forms, top
    // navigation and popups are all withheld. An ABSENT attribute would grant
    // every one of them, so assert the attribute is there AND empty.
    expect(el.getAttribute("sandbox")).toBe("");
    expect(el.getAttribute("referrerpolicy")).toBe("no-referrer");
    expect(el.getAttribute("allow")).toBe("");
    expect(el.getAttribute("loading")).toBe("lazy");
    expect(el.getAttribute("title")).toBe("Preview of report.html (sandboxed)");
    expect(el.getAttribute("src")).toBe("https://files.example/c/p/tok-1/report.html");
  });

  it("lets an HTML report run its own scripts and grants nothing else", () => {
    render(
      <FramePreview
        {...props({
          content: {
            kind: "frame",
            url: "https://files.example/c/p/tok-1/report.html",
            sandboxed: true,
            scripts: true,
            title: "Preview of report.html (sandboxed)",
          },
        })}
      />,
    );
    // allow-scripts ALONE: no allow-same-origin, so the page stays in an opaque
    // origin; no forms, popups or top-level navigation.
    expect(frame().getAttribute("sandbox")).toBe("allow-scripts");
  });

  it("leaves a PDF unsandboxed so the browser's own viewer runs, keeping the rest", () => {
    render(
      <FramePreview
        {...props({
          facts: { mime: "application/pdf", name: "q3.pdf", size: 9000 },
          content: {
            kind: "frame",
            url: "https://files.example/c/p/tok-2/q3.pdf",
            sandboxed: false,
            title: "Preview of q3.pdf",
          },
        })}
      />,
    );

    const el = frame();
    expect(el.hasAttribute("sandbox")).toBe(false);
    expect(el.getAttribute("referrerpolicy")).toBe("no-referrer");
    expect(el.getAttribute("allow")).toBe("");
    expect(el.getAttribute("title")).toBe("Preview of q3.pdf");
  });

  it("says so when there is nothing to frame yet", () => {
    render(<FramePreview {...props({ content: { kind: "none" } })} />);

    expect(document.querySelector("iframe")).toBeNull();
    expect(screen.getByRole("status")).toBeInTheDocument();
  });

  describe("when the file changes underneath it", () => {
    beforeEach(() => vi.useFakeTimers());
    afterEach(() => vi.useRealTimers());

    async function tick(ms: number): Promise<void> {
      await act(async () => {
        vi.advanceTimersByTime(ms);
      });
    }

    it("waits out a burst of writes before reloading, then swaps the source", async () => {
      const { rerender } = render(<FramePreview {...props()} />);

      rerender(
        <FramePreview
          {...props({
            version: "etag-2",
            content: {
              kind: "frame",
              url: "https://files.example/c/p/tok-1/report.html?v=2",
              sandboxed: true,
              title: "Preview of report.html (sandboxed)",
            },
          })}
        />,
      );
      // An agent rewriting a report writes it many times a second; reloading on
      // every write would leave a strobing pane, so nothing moves yet.
      expect(frame().getAttribute("src")).toBe("https://files.example/c/p/tok-1/report.html");
      await tick(299);
      expect(frame().getAttribute("src")).toBe("https://files.example/c/p/tok-1/report.html");

      await tick(1);
      expect(frame().getAttribute("src")).toBe("https://files.example/c/p/tok-1/report.html?v=2");
      expect(screen.getByText("Updated")).toBeInTheDocument();
    });

    it("restarts the wait on every further write", async () => {
      const { rerender } = render(<FramePreview {...props()} />);

      rerender(<FramePreview {...props({ version: "etag-2" })} />);
      await tick(200);
      rerender(<FramePreview {...props({ version: "etag-3" })} />);
      await tick(200);

      // 400 ms have passed, but only 200 since the last write.
      expect(screen.queryByText("Updated")).toBeNull();
      await tick(100);
      expect(screen.getByText("Updated")).toBeInTheDocument();
    });

    it("reloads even when the URL is unchanged", async () => {
      // A page grant is reused until it nears expiry, so new bytes usually arrive
      // behind the SAME url. Only replacing the frame re-fetches them.
      const { rerender } = render(<FramePreview {...props()} />);
      const before = frame();

      rerender(<FramePreview {...props({ version: "etag-2" })} />);
      await tick(300);

      expect(frame()).not.toBe(before);
      expect(frame().getAttribute("src")).toBe(before.getAttribute("src"));
    });

    it("stops announcing the update after a moment", async () => {
      const { rerender } = render(<FramePreview {...props()} />);

      rerender(<FramePreview {...props({ version: "etag-2" })} />);
      await tick(300);
      expect(screen.getByText("Updated")).toBeInTheDocument();

      await tick(2000);
      expect(screen.queryByText("Updated")).toBeNull();
    });

    it("never reloads while the version holds", async () => {
      const { rerender } = render(<FramePreview {...props()} />);
      const before = frame();

      rerender(<FramePreview {...props({ status: "ready" })} />);
      await tick(1000);

      expect(frame()).toBe(before);
      expect(screen.queryByText("Updated")).toBeNull();
    });
  });

  // The content origin is a second server, on a second hostname, reached by the
  // browser and not by us. When it does not answer, an iframe shows the
  // browser's own blank pane or a "refused to connect" chrome page — neither of
  // which tells the reader whose problem it is or what is left to do about it.
  describe("when the content origin does not answer", () => {
    beforeEach(() => vi.useFakeTimers());
    afterEach(() => vi.useRealTimers());

    async function tick(ms: number): Promise<void> {
      await act(async () => {
        vi.advanceTimersByTime(ms);
      });
    }

    async function load(): Promise<void> {
      const el = frame();
      await act(async () => {
        fireEvent.load(el);
      });
    }

    it("says the preview could not be loaded once the wait runs out", async () => {
      render(<FramePreview {...props()} />);
      // Still trying: a slow document is not a broken one, and a notice thrown
      // up after two seconds would replace a preview that was about to arrive.
      await tick(FRAME_LOAD_TIMEOUT_MS - 1);
      expect(document.querySelector("iframe")).not.toBeNull();
      expect(screen.queryByRole("status")).toBeNull();

      await tick(1);
      expect(screen.getByRole("status")).toHaveTextContent(/didn.t answer/i);
      // The frame is gone: a dead pane beside the sentence reads as the preview.
      expect(document.querySelector("iframe")).toBeNull();
    });

    it("leaves a document that loads in time alone", async () => {
      render(<FramePreview {...props()} />);
      await load();
      await tick(FRAME_LOAD_TIMEOUT_MS * 3);

      expect(screen.queryByRole("status")).toBeNull();
      expect(document.querySelector("iframe")).not.toBeNull();
    });

    it("offers the two things left to do, and tries again on request", async () => {
      const saved: string[] = [];
      render(<FramePreview {...props({ onDownload: () => saved.push("saved") })} />);
      await tick(FRAME_LOAD_TIMEOUT_MS);

      screen.getByRole("button", { name: "Download" }).click();
      expect(saved).toEqual(["saved"]);

      await act(async () => {
        screen.getByRole("button", { name: "Try again" }).click();
      });
      // A fresh attempt, not the sentence with a dead frame behind it.
      expect(document.querySelector("iframe")).not.toBeNull();
      expect(screen.queryByRole("status")).toBeNull();
      await load();
      await tick(FRAME_LOAD_TIMEOUT_MS);
      expect(screen.queryByRole("status")).toBeNull();
    });

    it("tries again by itself when the file is written again", async () => {
      const { rerender } = render(<FramePreview {...props()} />);
      await tick(FRAME_LOAD_TIMEOUT_MS);
      expect(screen.getByRole("status")).toBeInTheDocument();

      // New bytes are a new attempt: a server that was down a minute ago is not
      // a reason to refuse to draw what the machine has just written.
      rerender(<FramePreview {...props({ version: "etag-2" })} />);
      await tick(300);
      expect(document.querySelector("iframe")).not.toBeNull();
      expect(screen.queryByRole("status")).toBeNull();
    });
  });
});
