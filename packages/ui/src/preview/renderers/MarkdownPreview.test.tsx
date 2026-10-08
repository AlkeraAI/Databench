import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ChatFilesResolver } from "../../primitives/render";
import type { PreviewFacts, PreviewProps } from "../types";
import { MarkdownPreview, markdownRenderer } from "./MarkdownPreview";
import { manualGate, placeScroller, WindowedHost } from "./windowedHost.testkit";

afterEach(cleanup);

function facts(over: Partial<PreviewFacts> = {}): PreviewFacts {
  return { mime: "text/plain", name: "notes.md", size: 120, ...over };
}

function props(text: string, over: Partial<PreviewProps> = {}): PreviewProps {
  return {
    facts: facts(),
    content: { kind: "text", text },
    version: "etag-1",
    status: "ready",
    onDownload: () => {},
    ...over,
  };
}

describe("the markdown renderer's claim", () => {
  it.each([
    ["a markdown file the server sniffed as text", "text/plain", "report.md", true],
    ["a markdown file under the long extension", "text/plain", "report.markdown", true],
    ["a markdown mime whatever the name", "text/markdown", "report", true],
    ["a plain note", "text/plain", "notes.txt", false],
    ["a python file", "text/plain", "run.py", false],
    ["a spreadsheet", "text/csv", "rows.csv", false],
    // A `.md` the sniffer could not place is still the note the person wrote.
    // Without this it is a card reading "Preview is not available for this
    // type" over bytes that are plainly readable.
    ["markdown the server could not place", "application/octet-stream", "notes.md", true],
    // The name only ever picks BETWEEN text readers: a zip named `.md` is not
    // one, and a genuine binary keeps its card.
    ["an archive the server placed", "application/zip", "bundle.md", false],
    ["an unplaced file with no markdown name", "application/octet-stream", "blob.bin", false],
  ])("%s", (_name, mime, name, claimed) => {
    expect(markdownRenderer.match(facts({ mime, name }))).toBe(claimed);
  });

  it("asks the host for the decoded body", () => {
    expect(markdownRenderer.needs(facts())).toBe("text");
  });
});

describe("rendering a markdown file", () => {
  it("resolves a relative image through the resolver the host installed", async () => {
    const resolver: ChatFilesResolver = {
      resolveUrl: vi.fn(async (path: string) => (path === "charts/q3.png" ? "blob:q3" : null)),
    };

    const { container } = render(<MarkdownPreview {...props("![x](charts/q3.png)", { resolver })} />);

    await waitFor(() => expect(container.querySelector("img")).not.toBeNull());
    const image = container.querySelector("img");
    expect(image).toHaveAttribute("src", "blob:q3");
    expect(image).toHaveAttribute("alt", "x");
    expect(resolver.resolveUrl).toHaveBeenCalledWith("charts/q3.png");
  });

  it("shows the image's alt text when no resolver is installed rather than a broken picture", () => {
    render(<MarkdownPreview {...props("![x](charts/q3.png)")} />);

    expect(screen.getByRole("img", { name: "x" }).tagName).toBe("SPAN");
  });

  it("gives an external link a new tab, a severed opener and a visible marker", () => {
    render(<MarkdownPreview {...props("See [the docs](https://example.com/guide).")} />);

    const link = screen.getByRole("link", { name: /^the docs\b/ });
    expect(link).toHaveAttribute("href", "https://example.com/guide");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(link.querySelector(".alk-preview-markdown__external-marker")).not.toBeNull();
    expect(link.getAttribute("aria-label")).toMatch(/new tab/i);
  });

  it("renders a link inside a heading, a list and a table cell the same way", () => {
    render(
      <MarkdownPreview
        {...props(
          [
            "# [top](https://example.com/a)",
            "",
            "- [item](https://example.com/b)",
            "",
            "| col |",
            "| --- |",
            "| [cell](https://example.com/c) |",
          ].join("\n"),
        )}
      />,
    );

    const links = screen.getAllByRole("link");
    expect(links.map((link) => link.getAttribute("href"))).toEqual([
      "https://example.com/a",
      "https://example.com/b",
      "https://example.com/c",
    ]);
    for (const link of links) expect(link).toHaveAttribute("rel", "noopener noreferrer");
  });

  it.each([
    ["a script tag", "<script>alert(1)</script>", "script"],
    ["an image with an error handler", '<img src="x" onerror="alert(1)">', "img[src='x']"],
    ["an iframe", '<iframe src="https://evil.example"></iframe>', "iframe"],
    ["an anchor of its own", '<a href="javascript:alert(1)">go</a>', "a[href^='javascript']"],
  ])("never turns %s in the file into live markup", (_name, source, selector) => {
    const { container } = render(<MarkdownPreview {...props(`Before\n\n${source}\n\nAfter`)} />);

    expect(container.querySelector(selector)).toBeNull();
    expect(container.textContent).toContain(source);
  });

  it("keeps a math expression's markup out of the document", () => {
    const { container } = render(<MarkdownPreview {...props("$<script>alert(1)</script>$")} />);

    expect(container.querySelector("script")).toBeNull();
  });

  it("renders a fenced block as code rather than prose", () => {
    const { container } = render(<MarkdownPreview {...props("```python\ndef go():\n    pass\n```")} />);

    expect(container.querySelector("pre")).not.toBeNull();
    expect(container.textContent).toContain("def go():");
  });

  it("draws nothing when the host has no bytes yet", () => {
    const { container } = render(
      <MarkdownPreview {...props("", { content: { kind: "none" }, status: "loading" })} />,
    );

    expect(container.textContent).toBe("");
  });
});

describe("a note that has not all arrived", () => {
  const TOTAL = 2 * 1024 * 1024 + 512 * 1024;
  // Cut at blank lines, the way the host cuts a markdown window.
  const windows = [
    "# Chapter one\n\nIt starts [here](https://example.com/one).\n\n",
    "## Chapter two\n\nIt goes on at [the site](https://example.com/two).\n\n- a\n- b\n\n",
    "## Chapter three\n\nThe end.\n",
  ];

  function host(gate = manualGate()) {
    const view = render(
      <WindowedHost
        windows={windows}
        total={TOTAL}
        gate={gate}
        draw={(content) => <MarkdownPreview {...props("", { content })} />}
      />,
    );
    return { ...view, gate };
  }

  it("says how much is here and brings the next window on the button", async () => {
    const { gate } = host();
    expect(screen.getByRole("status")).toHaveTextContent("Showing 1 MB of 2.6 MB");
    expect(screen.queryByRole("heading", { name: "Chapter two" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Show more" }));
    expect(screen.getByRole("status")).toHaveTextContent("Loading more…");
    await act(async () => gate.open());

    expect(screen.getByRole("heading", { level: 1, name: "Chapter one" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "Chapter two" })).toBeInTheDocument();
    expect(screen.getAllByRole("listitem").map((item) => item.textContent)).toEqual(["a", "b"]);
  });

  it("keeps links and headings working in a window that landed later", async () => {
    const { gate } = host();
    fireEvent.click(screen.getByRole("button", { name: "Show more" }));
    await act(async () => gate.open());

    const later = screen.getByRole("link", { name: /the site/ });
    expect(later).toHaveAttribute("href", "https://example.com/two");
    expect(later).toHaveAttribute("target", "_blank");
    // The first window's link is still the link it was.
    expect(screen.getByRole("link", { name: /here/ })).toHaveAttribute(
      "href",
      "https://example.com/one",
    );
  });

  it("asks once from a scroll near the end and keeps the reader's place", async () => {
    const { container, gate } = host();
    const scroller = container.querySelector(".alk-preview-markdown")!;
    placeScroller(scroller, { scrollHeight: 3000, clientHeight: 600, top: 1300 });

    fireEvent.scroll(scroller);
    fireEvent.scroll(scroller);
    await act(async () => gate.open());

    expect(screen.getByRole("heading", { name: "Chapter two" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Chapter three" })).toBeNull();
    expect(container.querySelector(".alk-preview-markdown")).toBe(scroller);
    expect((scroller as HTMLElement).scrollTop).toBe(1300);
  });

  it("draws no line once the note is whole", async () => {
    const { gate } = host();
    for (let window = 1; window < windows.length; window += 1) {
      fireEvent.click(screen.getByRole("button", { name: "Show more" }));
      await act(async () => gate.open());
    }

    expect(screen.getByRole("heading", { name: "Chapter three" })).toBeInTheDocument();
    expect(screen.queryByTestId("preview-more")).toBeNull();
  });
});
