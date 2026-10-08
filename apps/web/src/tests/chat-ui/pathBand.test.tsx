// The path band shows a workspace file the way the editor names it: relative
// under the root, absolute anywhere else, with the full path always one hover
// away. Relativizing across a root boundary would name the wrong file, so
// every boundary case here is a regression net for that rule.

import { fireEvent, render, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { PathBand } from "@alkera/ui";

// The full path rides a truncation tooltip, measured off the live element, and jsdom
// reports every layout box as zero — so a band there is never cut off. Stamping the two
// widths is the only way to reach the branch that offers the tip.
function clip(el: Element): void {
  Object.defineProperty(el, "scrollWidth", { configurable: true, value: 420 });
  Object.defineProperty(el, "clientWidth", { configurable: true, value: 60 });
}

function bandText(container: HTMLElement): string {
  const text = container.querySelector(".chat-tool-band__text");
  if (!text) throw new Error("the band rendered no path text");
  return text.textContent ?? "";
}

describe("path band display", () => {
  it("relativizes only across a real root boundary", () => {
    const cases: [string, string | undefined, string][] = [
      ["/ws/repo/src/main.py", "/ws/repo", "src/main.py"],
      ["/ws/repo/src/main.py", "/ws/repo/", "src/main.py"],
      ["/etc/hosts", "/ws/repo", "/etc/hosts"],
      ["/ws/repo", "/ws/repo", "/ws/repo"],
      ["/ws/repo/", "/ws/repo", "/ws/repo/"],
      // /ws/repo2 shares the root's characters but not its boundary; slicing by
      // prefix alone would rename it to "2".
      ["/ws/repo2/f.py", "/ws/repo", "/ws/repo2/f.py"],
      ["/a/b.py", undefined, "/a/b.py"],
    ];
    for (const [path, relativeTo, shown] of cases) {
      const { container } = render(
        <div className="chat-root">
          <PathBand path={path} relativeTo={relativeTo} />
        </div>,
      );
      expect(bandText(container), `${path} under ${relativeTo}`).toBe(shown);
    }
  });

  it("hands back the full path when the band is clipped", async () => {
    const { container } = render(
      <div className="chat-root">
        <PathBand path="/ws/repo/src/main.py" relativeTo="/ws/repo" />
      </div>,
    );
    expect(bandText(container)).toBe("src/main.py");
    expect(container.querySelector(".chat-tool-band--path")?.tagName).not.toBe("BUTTON");

    // Clipped, the reading alone no longer names the file — the tip is where the
    // whole path lives, and it is the absolute one, not the shortened reading.
    const text = container.querySelector(".chat-tool-band__text") as HTMLElement;
    clip(text);
    fireEvent.pointerEnter(text);
    await waitFor(() => expect(text).toHaveAttribute("aria-describedby"));
    expect(document.getElementById(text.getAttribute("aria-describedby") ?? "")).toHaveTextContent(
      "/ws/repo/src/main.py",
    );
  });

  it("becomes a button only when onOpen is wired", async () => {
    const user = userEvent.setup();
    const onOpen = vi.fn();
    const { container, getByRole } = render(
      <div className="chat-root">
        <PathBand path="/ws/repo/src/main.py" relativeTo="/ws/repo" onOpen={onOpen} />
      </div>,
    );
    // The band names its whole target to assistive tech even though it shows the
    // relative reading — a press has to say which file it opens.
    const band = getByRole("button", { name: "Open /ws/repo/src/main.py" });
    expect(band).toBe(container.querySelector(".chat-tool-band--path"));
    await user.click(band);
    expect(onOpen).toHaveBeenCalledTimes(1);
  });

  it("splits the shown path into trail and leaf", () => {
    const { container } = render(
      <div className="chat-root">
        <PathBand path="/ws/repo/src/main.py" relativeTo="/ws/repo" />
      </div>,
    );
    expect(container.querySelector(".chat-tool-dir")?.textContent).toBe("src/");
    expect(container.querySelector(".chat-tool-leaf")?.textContent).toBe("main.py");
  });
});
