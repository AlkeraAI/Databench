// The transcript's one contract for a file inside the chat: `![label](path)`
// for an image, `[label](path)` for a file, the path relative to the chat's
// effective root. These pin the rule (what IS an image, what stays text), the
// block the parser emits, and that rendering goes through the shell's resolver
// — a web variant answering a content URL, a webview variant answering a
// `vscode-webview-resource` URI — with the alt as the accessible name, a
// placeholder for a path that resolves to nothing, and never a spinner that
// stays.

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  CHAT_FILE_GONE_CONFIRM_MS,
  ChatFileNotReady,
  ChatFilesProvider,
  MISSING_RECHECK_MS,
  MISSING_RECHECKS,
  type ChatFileRef,
  type ChatFilesResolver,
} from "./ChatFiles";
import { Markdown } from "./Markdown";
import { chatImagePath, chatRelativePath, isChatImageName } from "./chatPaths";
import { parseMarkdownBlocks } from "./markdownBlocks";
import { Prose } from "../../../chat/prose";
import { OrderTicket } from "../../../chat/transcript/OrderTicket";

afterEach(cleanup);

describe("chatImagePath", () => {
  it.each([
    ["paste-1-ab12.png", "paste-1-ab12.png"],
    ["charts/revenue.PNG", "charts/revenue.PNG"],
    ["./a.jpg", "a.jpg"],
    ["b.jpeg", "b.jpeg"],
    ["c.gif", "c.gif"],
    ["d.webp", "d.webp"],
    ["e.svg", "e.svg"],
  ])("accepts the chat-relative image %s", (target, expected) => {
    expect(chatImagePath(target)).toBe(expected);
  });

  it.each([
    ["a web URL", "https://example.com/x.png"],
    ["a plain http URL", "http://example.com/x.png"],
    ["a data URI", "data:image/png;base64,AAAA"],
    ["an absolute path", "/tmp/x.png"],
    ["a parent escape", "../x.png"],
    ["a buried escape", "a/../../x.png"],
    ["a blob handle", "blob:abc"],
    ["a windows path", "a\\x.png"],
    ["a non-image type", "report.pdf"],
    ["a name with no extension", "x"],
    ["a query", "x.png?raw=1"],
    ["an empty target", ""],
  ])("refuses %s", (_label, target) => {
    expect(chatImagePath(target)).toBeNull();
  });

  it("chatRelativePath keeps a file of any type inside the root", () => {
    expect(chatRelativePath("q.csv")).toBe("q.csv");
    expect(chatRelativePath("sub/q.csv")).toBe("sub/q.csv");
    expect(chatRelativePath("../q.csv")).toBeNull();
    expect(chatRelativePath("https://x/q.csv")).toBeNull();
  });

  it("isChatImageName decides the composer's pill kind by name", () => {
    expect(isChatImageName("Screenshot 2026.png")).toBe(true);
    expect(isChatImageName("report.csv")).toBe(false);
  });
});

describe("parseMarkdownBlocks images", () => {
  it("emits an image block for an own-line chat image, alt and path", () => {
    expect(parseMarkdownBlocks("Here it is:\n\n![Revenue by month](revenue.png)\n\nDone.")).toEqual([
      { kind: "paragraph", text: "Here it is:" },
      { kind: "image", alt: "Revenue by month", path: "revenue.png" },
      { kind: "paragraph", text: "Done." },
    ]);
  });

  it("splits an image off a paragraph it follows without a blank line", () => {
    const blocks = parseMarkdownBlocks("See below\n![Chart](c.png)\nand above");
    expect(blocks.map((b) => b.kind)).toEqual(["paragraph", "image", "paragraph"]);
  });

  it("names an image with no alt after its file", () => {
    expect(parseMarkdownBlocks("![](charts/q3.png)")).toEqual([{ kind: "image", alt: "q3.png", path: "charts/q3.png" }]);
  });

  it.each([
    "![x](https://example.com/x.png)",
    "![x](data:image/png;base64,AAAA)",
    "![x](/abs/x.png)",
    "![x](../x.png)",
    "Inline ![x](x.png) mid-sentence",
  ])("keeps %s a paragraph", (line) => {
    expect(parseMarkdownBlocks(line)).toEqual([{ kind: "paragraph", text: line }]);
  });
});

const resolverOf = (url: string | null, open = vi.fn()): ChatFilesResolver & { openPath: ReturnType<typeof vi.fn> } => ({
  resolveUrl: vi.fn(async () => url),
  openPath: open,
});

describe("rendering chat images", () => {
  it("Prose renders an image through the injected web resolver, alt as the name, click opens", async () => {
    const resolver = resolverOf("/api/v1/files/drives/d/items/n/content?disposition=inline");
    render(
      <ChatFilesProvider resolver={resolver}>
        <Prose content="![Revenue by month](revenue.png)" />
      </ChatFilesProvider>,
    );
    await waitFor(() => expect(screen.getByRole("img", { name: "Revenue by month" }).tagName).toBe("IMG"));
    const img = screen.getByRole("img", { name: "Revenue by month" });
    expect(img.getAttribute("src")).toBe("/api/v1/files/drives/d/items/n/content?disposition=inline");
    expect(resolver.resolveUrl).toHaveBeenCalledWith("revenue.png");
    fireEvent.click(screen.getByRole("button", { name: "Open Revenue by month" }));
    expect(resolver.openPath).toHaveBeenCalledWith("revenue.png");
  });

  it("Markdown renders through a webview resolver answering a webview resource URI", async () => {
    const resolver = resolverOf("https://file+.vscode-resource.vscode-cdn.net/p/.alkera/chats/c/sandbox/x.svg");
    render(
      <ChatFilesProvider resolver={resolver}>
        <Markdown content="![Sketch](x.svg)" />
      </ChatFilesProvider>,
    );
    await waitFor(() => expect(screen.getByRole("img", { name: "Sketch" }).tagName).toBe("IMG"));
    const img = screen.getByRole("img", { name: "Sketch" });
    expect(img.getAttribute("src")).toContain("vscode-resource");
  });

  it("a path that resolves to nothing renders the alt in a placeholder, not a spinner", async () => {
    // The answer is held open, because the two states this pins look the same to
    // a reader who only asks "is it a SPAN?": while the path is being resolved
    // (a quiet loading block) AND once it is known to be nothing (the alt in a
    // placeholder), it is a span with role img named by the alt. `data-state`
    // tells them apart, so each is asserted at the moment it is the page's
    // answer rather than whenever the promise happened to land.
    let answer!: (url: string | null) => void;
    const resolver: ChatFilesResolver = {
      resolveUrl: () =>
        new Promise<string | null>((resolve) => {
          answer = resolve;
        }),
    };
    render(
      <ChatFilesProvider resolver={resolver}>
        <Prose content="![Missing chart](gone.png)" />
      </ChatFilesProvider>,
    );

    // Waiting for the answer is a quiet block named by the alt — no spinner,
    // no broken image.
    const waiting = screen.getByRole("img", { name: "Missing chart" });
    expect(waiting.tagName).toBe("SPAN");
    expect(document.querySelector('[data-state="resolving"]')).not.toBeNull();
    expect(document.querySelector('[data-state="missing"]')).toBeNull();

    answer(null);

    // …and nothing is what it stays: the placeholder settles, and the wait it
    // replaces is over rather than left running.
    await waitFor(() => expect(document.querySelector('[data-state="missing"]')).not.toBeNull());
    expect(screen.getByRole("img", { name: "Missing chart" }).tagName).toBe("SPAN");
    expect(document.querySelector('[data-state="resolving"]')).toBeNull();
  });

  it("a resolver that throws lands on the placeholder too", async () => {
    const resolver: ChatFilesResolver = { resolveUrl: async () => Promise.reject(new Error("no")) };
    render(
      <ChatFilesProvider resolver={resolver}>
        <Prose content="![Chart](c.png)" />
      </ChatFilesProvider>,
    );
    await waitFor(() => expect(document.querySelector('[data-state="missing"]')).not.toBeNull());
  });

  it("with no resolver installed an image is a placeholder immediately", () => {
    render(<Prose content="![Chart](c.png)" />);
    expect(document.querySelector('[data-state="missing"]')).not.toBeNull();
    expect(screen.getByRole("img", { name: "Chart" }).tagName).toBe("SPAN");
  });

  it("a web URL image renders as the text it is", async () => {
    const resolver = resolverOf("should-not-be-used");
    render(
      <ChatFilesProvider resolver={resolver}>
        <Prose content="![x](https://example.com/x.png)" />
      </ChatFilesProvider>,
    );
    await act(async () => {});
    expect(screen.queryByRole("img")).toBeNull();
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.getByText("![x](https://example.com/x.png)")).toBeTruthy();
    expect(resolver.resolveUrl).not.toHaveBeenCalled();
  });

  it("the reader's own bubble renders its pasted image and file link through the same resolver", async () => {
    const resolver = resolverOf("blob:preview");
    render(
      <ChatFilesProvider resolver={resolver}>
        <OrderTicket text={"look at this\n![Image 1](paste-1-ab12.png)\nand [File 1: q.csv](file-1-cd34.csv)"} at="now" />
      </ChatFilesProvider>,
    );
    await waitFor(() => expect(screen.getByRole("img", { name: "Image 1" }).tagName).toBe("IMG"));
    expect(screen.getByText(/look at this/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "File 1: q.csv" }));
    expect(resolver.openPath).toHaveBeenCalledWith("file-1-cd34.csv");
  });

  it("a chat file link falls back to the caller's routing when no resolver is installed", () => {
    const onResourceOpen = vi.fn();
    render(<Markdown content="[File 1: q.csv](q.csv)" onResourceOpen={onResourceOpen} />);
    fireEvent.click(screen.getByRole("button", { name: "File 1: q.csv" }));
    expect(onResourceOpen).toHaveBeenCalledWith(expect.objectContaining({ path: "q.csv" }));
  });
});

/** A shell that can also say WHERE a file is: the web's resolver, whose
 *  `locate` walks the chat folder and whose `reveal` puts the row in front of
 *  the reader in the Files panel. */
function locatingResolver(
  refs: Record<string, ChatFileRef>,
  over: Partial<ChatFilesResolver> = {},
): ChatFilesResolver {
  return {
    resolveUrl: vi.fn(async () => "blob:bytes"),
    locate: vi.fn(async (path: string) => refs[path] ?? null),
    reveal: vi.fn(),
    openPath: vi.fn(),
    ...over,
  };
}

const REPORT: ChatFileRef = {
  nodeId: "nd_report",
  parentId: "nd_work",
  name: "q3-report.html",
  path: "q3-report.html",
};
const NESTED: ChatFileRef = {
  nodeId: "nd_plot",
  parentId: "nd_charts",
  name: "q3.png",
  path: "charts/q3.png",
};

describe("a transcript reference to a file that is still there", () => {
  it("names the file and its folder on hover, and clicking reveals it instead of opening a door of its own", async () => {
    const resolver = locatingResolver({ "q3-report.html": REPORT });
    render(
      <ChatFilesProvider resolver={resolver}>
        <Markdown content="[Q3 report](q3-report.html)" />
      </ChatFilesProvider>,
    );

    const link = await screen.findByRole("button", { name: "Q3 report" });
    await waitFor(() =>
      expect(link.getAttribute("title")).toBe("q3-report.html (in the chat's folder)"),
    );
    fireEvent.click(link);
    expect(resolver.reveal).toHaveBeenCalledWith(REPORT);
    expect(resolver.openPath).not.toHaveBeenCalled();
  });

  it("names the subfolder a nested file sits in", async () => {
    const resolver = locatingResolver({ "charts/q3.png": NESTED });
    render(
      <ChatFilesProvider resolver={resolver}>
        <Markdown content="[The plot](charts/q3.png)" />
      </ChatFilesProvider>,
    );
    const link = await screen.findByRole("button", { name: "The plot" });
    await waitFor(() => expect(link.getAttribute("title")).toBe("q3.png (in charts)"));
  });

  it("falls back to the shell's own door when it can locate but cannot reveal", async () => {
    const resolver = locatingResolver({ "q3-report.html": REPORT }, { reveal: undefined });
    render(
      <ChatFilesProvider resolver={resolver}>
        <Markdown content="[Q3 report](q3-report.html)" />
      </ChatFilesProvider>,
    );
    const link = await screen.findByRole("button", { name: "Q3 report" });
    await waitFor(() => expect(link.getAttribute("title")).toContain("in the chat's folder"));
    fireEvent.click(link);
    expect(resolver.openPath).toHaveBeenCalledWith("q3-report.html");
  });

  it("reveals the image the same way, and titles it the same way", async () => {
    const resolver = locatingResolver({ "charts/q3.png": NESTED });
    render(
      <ChatFilesProvider resolver={resolver}>
        <Markdown content="![The plot](charts/q3.png)" />
      </ChatFilesProvider>,
    );
    await waitFor(() => expect(screen.getByRole("img", { name: "The plot" }).tagName).toBe("IMG"));
    expect(screen.getByRole("img", { name: "The plot" }).getAttribute("title")).toBe(
      "q3.png (in charts)",
    );
    fireEvent.click(screen.getByRole("button", { name: "Open The plot" }));
    expect(resolver.reveal).toHaveBeenCalledWith(NESTED);
    expect(resolver.openPath).not.toHaveBeenCalled();
  });

  it("makes EVERY mention of a file live, not only the first one in the transcript", async () => {
    // An agent names the file it produced in the message that produces it, and
    // again in the summary, and again when it edits it. A reader scrolling back
    // to the mention they remember must get the same door there as at the top:
    // a reference that is live only the first time is a transcript where the
    // way to a file depends on which sentence you happened to read.
    const resolver = locatingResolver({ "q3-report.html": REPORT, "charts/q3.png": NESTED });
    render(
      <ChatFilesProvider resolver={resolver}>
        <Markdown
          content={[
            "Wrote [the report](q3-report.html).",
            "",
            "Summary: see [the report](q3-report.html) and [the chart](charts/q3.png).",
            "",
            "Then [rewrote it](q3-report.html).",
          ].join("\n")}
        />
      </ChatFilesProvider>,
    );

    const links = await screen.findAllByRole("button");
    expect(links).toHaveLength(4);
    await waitFor(() =>
      expect(links.every((link) => link.className.includes("chat-file-ref--live"))).toBe(true),
    );
    // Each one titled with the file it actually points at, not with the path
    // the message wrote — which is what "live" buys the reader before a click.
    expect(links.map((link) => link.getAttribute("title"))).toEqual([
      "q3-report.html (in the chat's folder)",
      "q3-report.html (in the chat's folder)",
      "q3.png (in charts)",
      "q3-report.html (in the chat's folder)",
    ]);
    // And the last mention opens the file, the same as the first.
    fireEvent.click(links[3]);
    expect(resolver.reveal).toHaveBeenCalledWith(REPORT);
  });

  it("asks the shell once for a path named twice, and again once the folder is invalidated", async () => {
    const resolver = locatingResolver({ "q3-report.html": REPORT });
    const content = "[Q3 report](q3-report.html)\n\nand again [the report](q3-report.html)";
    const { rerender } = render(
      <ChatFilesProvider resolver={resolver} invalidate={1}>
        <Markdown content={content} />
      </ChatFilesProvider>,
    );
    await waitFor(() => expect(screen.getAllByRole("button")).toHaveLength(2));
    await waitFor(() => expect(resolver.locate).toHaveBeenCalledTimes(1));

    rerender(
      <ChatFilesProvider resolver={resolver} invalidate={2}>
        <Markdown content={content} />
      </ChatFilesProvider>,
    );
    await waitFor(() => expect(resolver.locate).toHaveBeenCalledTimes(2));
  });
});

describe("a reference, in a message still being written, to a file not found yet", () => {
  it("renders the link's label as plain text saying it has not arrived, and opens nothing", async () => {
    const resolver = locatingResolver({});
    render(
      <ChatFilesProvider resolver={resolver}>
        <Markdown content="[Q3 report](q3-report.html)" streaming />
      </ChatFilesProvider>,
    );
    await waitFor(() => expect(screen.getByText("has not arrived")).toBeTruthy());
    expect(screen.queryByText("not in the chat")).toBeNull();
    expect(screen.queryByText("not in the chat any more")).toBeNull();
    expect(screen.queryByRole("button", { name: "Q3 report" })).toBeNull();
    expect(screen.getByText("Q3 report").tagName).toBe("SPAN");
    fireEvent.click(screen.getByText("Q3 report"));
    expect(resolver.reveal).not.toHaveBeenCalled();
    expect(resolver.openPath).not.toHaveBeenCalled();
  });

  it("renders an image the agent named but that has not landed as not arrived, and buys no bytes for it", async () => {
    const resolver = locatingResolver({});
    render(
      <ChatFilesProvider resolver={resolver}>
        <Prose content="![200x200 red square](red-square.png)" streaming />
      </ChatFilesProvider>,
    );
    await waitFor(() => expect(screen.getByText("has not arrived")).toBeTruthy());
    expect(document.querySelector("figure.alk-chat-image")?.getAttribute("data-state")).toBe("pending");
    expect(screen.getByText("200x200 red square").tagName).toBe("SPAN");
    expect(screen.queryByRole("img")).toBeNull();
    expect(resolver.resolveUrl).not.toHaveBeenCalled();
  });

  it("asks again when the message finishes, and opens the file the drive has by then", async () => {
    const refs: Record<string, ChatFileRef> = {};
    const resolver = locatingResolver(refs);
    const content = "[Q3 report](q3-report.html)";
    const { rerender } = render(
      <ChatFilesProvider resolver={resolver}>
        <Prose content={content} streaming />
      </ChatFilesProvider>,
    );
    await waitFor(() => expect(screen.getByText("has not arrived")).toBeTruthy());
    // The box published the finished message only once the file was on the
    // drive, but the folder's own change signal has not reached this page.
    refs["q3-report.html"] = REPORT;
    rerender(
      <ChatFilesProvider resolver={resolver}>
        <Prose content={content} streaming={false} />
      </ChatFilesProvider>,
    );
    await waitFor(() => expect(screen.getByRole("button", { name: "Q3 report" })).toBeTruthy());
    expect(screen.queryByText("not in the chat")).toBeNull();
    expect(resolver.locate).toHaveBeenCalledTimes(2);
  });

  it("stops waiting when the message finishes without the file", async () => {
    const resolver = locatingResolver({});
    const content = "[Q3 report](q3-report.html)";
    const { rerender } = render(
      <ChatFilesProvider resolver={resolver}>
        <Prose content={content} streaming />
      </ChatFilesProvider>,
    );
    await waitFor(() => expect(screen.getByText("has not arrived")).toBeTruthy());
    rerender(
      <ChatFilesProvider resolver={resolver}>
        <Prose content={content} streaming={false} />
      </ChatFilesProvider>,
    );
    await waitFor(() => expect(screen.getByText("not in the chat")).toBeTruthy());
    expect(screen.queryByText("has not arrived")).toBeNull();
  });
});

describe("a finished message's reference to a file that lands just after it is read", () => {
  it("checks again on its own and becomes a link, with no folder signal and no reload", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const refs: Record<string, ChatFileRef> = {};
      const resolver = locatingResolver(refs);
      render(
        <ChatFilesProvider resolver={resolver}>
          <Prose content="[Q3 report](q3-report.html)" />
        </ChatFilesProvider>,
      );
      await waitFor(() => expect(screen.getByText("not in the chat")).toBeTruthy());
      // The box wrote the file a moment after the message was read, and the
      // folder's change signal never reached this page.
      refs["q3-report.html"] = REPORT;
      await vi.advanceTimersByTimeAsync(MISSING_RECHECK_MS * 4);
      await waitFor(() => expect(screen.getByRole("button", { name: "Q3 report" })).toBeTruthy());
      expect(screen.queryByText("not in the chat")).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it("settles on not in the chat after its rechecks, without asking for ever", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const resolver = locatingResolver({});
      render(
        <ChatFilesProvider resolver={resolver}>
          <Prose content="[Q3 report](q3-report.html)" />
        </ChatFilesProvider>,
      );
      await waitFor(() => expect(screen.getByText("not in the chat")).toBeTruthy());
      for (let i = 0; i <= MISSING_RECHECKS + 2; i += 1) {
        await vi.advanceTimersByTimeAsync(MISSING_RECHECK_MS * 2 ** i);
      }
      expect(screen.getByText("not in the chat")).toBeTruthy();
      expect(resolver.locate).toHaveBeenCalledTimes(MISSING_RECHECKS + 1);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("a reference, in a finished message, to a file the chat does not have", () => {
  // The box holds a finished message back until the files it names are on the
  // drive. One the drive still does not have is not on its way — the agent
  // never wrote it, or deleted it before this reader looked — so the reference
  // says so instead of waiting forever.
  it.each([
    ["Markdown", <Markdown key="m" content="[Q3 report](q3-report.html)" />],
    ["Prose", <Prose key="p" content="[Q3 report](q3-report.html)" />],
  ])("%s says the linked file is not in the chat, never that it has not arrived", async (_what, node) => {
    const resolver = locatingResolver({});
    render(<ChatFilesProvider resolver={resolver}>{node}</ChatFilesProvider>);
    await waitFor(() => expect(screen.getByText("not in the chat")).toBeTruthy());
    expect(screen.queryByText("has not arrived")).toBeNull();
    expect(screen.queryByText("not in the chat any more")).toBeNull();
    expect(screen.queryByRole("button", { name: "Q3 report" })).toBeNull();
    expect(screen.getByText("Q3 report").tagName).toBe("SPAN");
  });

  it("says a shown image is not in the chat, and buys no bytes for it", async () => {
    const resolver = locatingResolver({});
    render(
      <ChatFilesProvider resolver={resolver}>
        <Prose content="![200x200 red square](red-square.png)" />
      </ChatFilesProvider>,
    );
    await waitFor(() => expect(screen.getByText("not in the chat")).toBeTruthy());
    expect(document.querySelector("figure.alk-chat-image")?.getAttribute("data-state")).toBe("missing");
    expect(screen.queryByRole("img")).toBeNull();
    expect(resolver.resolveUrl).not.toHaveBeenCalled();
  });

  it("says the file a held result was written to is not in the chat once the agent deleted it", async () => {
    // The staging reply: the agent wrote the result out with blob.materialize,
    // charted it, removed the file as clean-up, and then linked the result.
    const handle = "9a21e98bc2c79b9d281d573492f1cb28875e2cfefebdf89fdd187ee4372f8bf8";
    const chatId = "5e76966f-9423-484b-8b7f-0ec781609d7e";
    const resolver = locatingResolver({});
    render(
      <ChatFilesProvider
        resolver={resolver}
        chatId={chatId}
        resultFiles={new Map([[handle, `/opt/alkera-work/.alkera/chats/${chatId}/scratch/result-9a21e98b.csv`]])}
      >
        <Prose content={`Full breakdown:\n\n[Active prompts by category/locale](blob:${handle})`} />
      </ChatFilesProvider>,
    );
    await waitFor(() => expect(screen.getByText("not in the chat")).toBeTruthy());
    expect(screen.queryByText("has not arrived")).toBeNull();
    expect(screen.getByText("Active prompts by category/locale").tagName).toBe("SPAN");
    expect(resolver.locate).toHaveBeenCalledWith("scratch/result-9a21e98b.csv");
  });

  it("a lookup that rejects is 'not found', not a lookup repeated forever", async () => {
    const locate = vi.fn(() => Promise.reject(new Error("offline")));
    const resolver = locatingResolver({}, { locate });
    render(
      <ChatFilesProvider resolver={resolver}>
        <Markdown content="[Q3 report](q3-report.html)" />
      </ChatFilesProvider>,
    );
    await waitFor(() => expect(screen.getByText("not in the chat")).toBeTruthy());
    expect(locate).toHaveBeenCalledTimes(1);
  });

  it("turns into the picture if the file lands later and the folder is invalidated", async () => {
    const refs: Record<string, ChatFileRef> = {};
    const resolver = locatingResolver(refs);
    const content = "![200x200 red square](red-square.png)";
    const { rerender } = render(
      <ChatFilesProvider resolver={resolver} invalidate={1}>
        <Markdown content={content} />
      </ChatFilesProvider>,
    );
    await waitFor(() => expect(screen.getByText("not in the chat")).toBeTruthy());
    refs["red-square.png"] = { nodeId: "nd_sq", parentId: null, name: "red-square.png", path: "red-square.png" };
    rerender(
      <ChatFilesProvider resolver={resolver} invalidate={2}>
        <Markdown content={content} />
      </ChatFilesProvider>,
    );
    await waitFor(() => expect(screen.getByRole("img", { name: "200x200 red square" }).tagName).toBe("IMG"));
    expect(screen.queryByText("not in the chat")).toBeNull();
  });
});

describe("a transcript reference to a file that was here and has been deleted", () => {
  it.each([
    ["an image", "![Revenue by month](revenue.png)", "gone"],
    ["a link", "[Revenue by month](revenue.png)", null],
  ] as const)("renders %s as not in the chat any more", async (_what, content, figureState) => {
    const refs: Record<string, ChatFileRef> = {
      "revenue.png": { nodeId: "nd_rev", parentId: null, name: "revenue.png", path: "revenue.png" },
    };
    const resolver = locatingResolver(refs);
    const { rerender } = render(
      <ChatFilesProvider resolver={resolver} invalidate={1}>
        <Markdown content={content} />
      </ChatFilesProvider>,
    );
    await waitFor(() =>
      expect(document.querySelector("img, button.chat-file-ref--live")).not.toBeNull(),
    );
    delete refs["revenue.png"];
    rerender(
      <ChatFilesProvider resolver={resolver} invalidate={2}>
        <Markdown content={content} />
      </ChatFilesProvider>,
    );
    // Looked for once more before it is called gone (CHAT_FILE_GONE_CONFIRM_MS).
    await waitFor(() => expect(screen.getByText("not in the chat any more")).toBeTruthy(), {
      timeout: 3_000,
    });
    expect(screen.queryByText("has not arrived")).toBeNull();
    expect(screen.getByText("Revenue by month").tagName).toBe("SPAN");
    if (figureState) {
      expect(document.querySelector("figure.alk-chat-image")?.getAttribute("data-state")).toBe(figureState);
    }
  });

  it("forgets what it saw when the resolver changes — another chat's file is not this one's", async () => {
    const first = locatingResolver({
      "revenue.png": { nodeId: "nd_rev", parentId: null, name: "revenue.png", path: "revenue.png" },
    });
    const content = "[Revenue by month](revenue.png)";
    const { rerender } = render(
      <ChatFilesProvider resolver={first}>
        <Markdown content={content} />
      </ChatFilesProvider>,
    );
    await waitFor(() => expect(document.querySelector("button.chat-file-ref--live")).not.toBeNull());
    rerender(
      <ChatFilesProvider resolver={locatingResolver({})}>
        <Markdown content={content} />
      </ChatFilesProvider>,
    );
    await waitFor(() => expect(screen.getByText("not in the chat")).toBeTruthy());
    expect(screen.queryByText("not in the chat any more")).toBeNull();
  });
});

describe("a shell that cannot look a path up", () => {
  it("keeps today's button, its path title and its own door", async () => {
    // The VS Code webview's resolver: it hands a path to its host and has no
    // lookup of its own, so nothing about the reference may change.
    const resolver = resolverOf("blob:x");
    render(
      <ChatFilesProvider resolver={resolver}>
        <Markdown content="[Q3 report](q3-report.html)" />
      </ChatFilesProvider>,
    );
    const link = screen.getByRole("button", { name: "Q3 report" });
    expect(link.getAttribute("title")).toBe("q3-report.html");
    expect(link.className).not.toContain("chat-file-ref--live");
    fireEvent.click(link);
    expect(resolver.openPath).toHaveBeenCalledWith("q3-report.html");
    await act(async () => {});
    expect(screen.queryByText("not in the chat any more")).toBeNull();
  });
});

/** A promise the test settles by hand, so what the page shows WHILE the shell is
 *  still answering is asserted at that moment rather than whenever it lands. */
function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void; reject: (error: unknown) => void } {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

type Deferred<T> = ReturnType<typeof deferred<T>>;

/** Let every settled promise and the renders it causes run, without a timer. */
async function settle(): Promise<void> {
  for (let i = 0; i < 5; i += 1) await act(async () => {});
}

/** Everything that reached the DOM under `root` between two assertions: a
 *  placeholder or loading block that flashed and went, and every `src` an image
 *  was pointed at. A flicker lives exactly in that gap. */
function watch(root: HTMLElement): { flashes: string[]; srcs: string[]; stop: () => void } {
  const flashes: string[] = [];
  const srcs: string[] = [];
  const note = (node: Node) => {
    if (!(node instanceof Element)) return;
    for (const hit of [node, ...Array.from(node.querySelectorAll("*"))]) {
      if (hit.matches(".alk-chat-image__missing, .alk-chat-image__loading")) flashes.push(hit.className);
      if (hit.tagName === "IMG") srcs.push(hit.getAttribute("src") ?? "");
    }
  };
  const take = (records: MutationRecord[]) => {
    for (const record of records) {
      if (record.type === "attributes" && record.target instanceof Element && record.target.tagName === "IMG") {
        srcs.push(record.target.getAttribute("src") ?? "");
      }
      record.addedNodes.forEach(note);
    }
  };
  const observer = new MutationObserver(take);
  observer.observe(root, { childList: true, subtree: true, attributes: true, attributeFilter: ["src"] });
  return {
    flashes,
    srcs,
    stop: () => {
      take(observer.takeRecords());
      observer.disconnect();
    },
  };
}

const CHART: ChatFileRef = { nodeId: "nd_chart", parentId: "nd_charts", name: "x.png", path: "charts/x.png" };
const CHART_MD = "![Active prompts by category and locale](charts/x.png)";
const CHART_ALT = "Active prompts by category and locale";

describe("a chat image while its answer is on the way", () => {
  it("first shows a quiet loading block with no visible alt text, then the picture", async () => {
    const answer = deferred<string | null>();
    const resolver: ChatFilesResolver = { resolveUrl: vi.fn(() => answer.promise) };
    render(
      <ChatFilesProvider resolver={resolver}>
        <Markdown content={CHART_MD} />
      </ChatFilesProvider>,
    );
    await settle();

    const waiting = screen.getByRole("img", { name: CHART_ALT });
    expect(waiting.className).toBe("alk-chat-image__loading");
    expect(waiting.getAttribute("aria-busy")).toBe("true");
    expect(waiting.textContent).toBe("");
    expect(screen.queryByText(CHART_ALT)).toBeNull();
    expect(document.querySelector(".alk-chat-image__missing")).toBeNull();

    answer.resolve("blob:v1");
    await settle();
    const img = screen.getByRole("img", { name: CHART_ALT });
    expect(img.tagName).toBe("IMG");
    expect(img.getAttribute("src")).toBe("blob:v1");
    expect(document.querySelector(".alk-chat-image__loading")).toBeNull();
  });
});

describe("a chat image already on screen while the folder keeps changing", () => {
  it("keeps the same picture through every re-ask, whatever the re-ask answers", async () => {
    // The live plane raises a frame for every lease and node change while the
    // agent writes; each one is an `invalidate` bump. None of them may take
    // the picture off the screen.
    const locates: Deferred<ChatFileRef | null>[] = [];
    const urls: Deferred<string | null>[] = [];
    let immediate = true;
    const resolver = locatingResolver(
      {},
      {
        locate: vi.fn(async () => {
          if (immediate) return CHART;
          const next = deferred<ChatFileRef | null>();
          locates.push(next);
          return next.promise;
        }),
        resolveUrl: vi.fn(async () => {
          if (immediate) return "blob:v1";
          const next = deferred<string | null>();
          urls.push(next);
          return next.promise;
        }),
      },
    );
    const tree = (invalidate: number) => (
      <ChatFilesProvider resolver={resolver} invalidate={invalidate}>
        <Markdown content={CHART_MD} />
      </ChatFilesProvider>
    );
    const { container, rerender } = render(tree(1));
    await settle();
    expect(screen.getByRole("img", { name: CHART_ALT }).getAttribute("src")).toBe("blob:v1");

    const seen = watch(container);
    immediate = false;
    for (let epoch = 2; epoch <= 6; epoch += 1) {
      rerender(tree(epoch));
      await settle();
      const img = screen.getByRole("img", { name: CHART_ALT });
      expect(img.tagName).toBe("IMG");
      expect(img.getAttribute("src")).toBe("blob:v1");
    }
    // The re-asks land late and every which way: found again, the same URL,
    // nothing, a failure. The picture already shown outlives all of them.
    expect(urls.length).toBeGreaterThan(0);
    locates.forEach((each) => each.resolve(CHART));
    await settle();
    urls.forEach((each, i) => {
      if (i % 3 === 0) each.resolve("blob:v1");
      else if (i % 3 === 1) each.resolve(null);
      else each.reject(new Error("transient"));
    });
    await settle();
    seen.stop();

    expect(seen.flashes).toEqual([]);
    expect(seen.srcs.filter((src) => src !== "blob:v1")).toEqual([]);
    expect(screen.getByRole("img", { name: CHART_ALT }).getAttribute("src")).toBe("blob:v1");
    expect(document.querySelector("figure.alk-chat-image")?.getAttribute("data-state")).toBe("ready");
  });

  it("swaps straight to a new version's picture when the agent rewrote the file", async () => {
    let next: Deferred<string | null> | null = null;
    const resolver = locatingResolver(
      { "charts/x.png": CHART },
      { resolveUrl: vi.fn(async () => (next ? next.promise : "blob:v1")) },
    );
    const tree = (invalidate: number) => (
      <ChatFilesProvider resolver={resolver} invalidate={invalidate}>
        <Markdown content={CHART_MD} />
      </ChatFilesProvider>
    );
    const { container, rerender } = render(tree(1));
    await settle();
    expect(screen.getByRole("img", { name: CHART_ALT }).getAttribute("src")).toBe("blob:v1");

    const seen = watch(container);
    const rebuy = deferred<string | null>();
    next = rebuy;
    rerender(tree(2));
    await settle();
    expect(screen.getByRole("img", { name: CHART_ALT }).getAttribute("src")).toBe("blob:v1");
    rebuy.resolve("blob:v2");
    await settle();
    seen.stop();

    expect(screen.getByRole("img", { name: CHART_ALT }).getAttribute("src")).toBe("blob:v2");
    expect(seen.flashes).toEqual([]);
    expect(seen.srcs).toEqual(["blob:v2"]);
  });

  it("keeps a live file link live while the folder is asked again", async () => {
    let hold: Deferred<ChatFileRef | null> | null = null;
    const resolver = locatingResolver({}, { locate: vi.fn(async () => (hold ? hold.promise : CHART)) });
    const tree = (invalidate: number) => (
      <ChatFilesProvider resolver={resolver} invalidate={invalidate}>
        <Markdown content="[The chart](charts/x.png)" />
      </ChatFilesProvider>
    );
    const { rerender } = render(tree(1));
    await settle();
    expect(screen.getByRole("button", { name: "The chart" }).className).toContain("chat-file-ref--live");

    hold = deferred<ChatFileRef | null>();
    rerender(tree(2));
    await settle();
    const link = screen.getByRole("button", { name: "The chart" });
    expect(link.className).toContain("chat-file-ref--live");
    expect(link.getAttribute("title")).toBe("x.png (in charts)");
  });

  it("a picture the browser has drawn is not replaced by the placeholder when a later load fails", async () => {
    render(
      <ChatFilesProvider resolver={resolverOf("blob:v1")}>
        <Markdown content={CHART_MD} />
      </ChatFilesProvider>,
    );
    await settle();
    const img = screen.getByRole("img", { name: CHART_ALT });
    fireEvent.load(img);
    fireEvent.error(img);
    await settle();
    expect(screen.getByRole("img", { name: CHART_ALT }).tagName).toBe("IMG");
    expect(document.querySelector(".alk-chat-image__missing")).toBeNull();
  });

  it("an image the browser could never draw is the placeholder", async () => {
    render(
      <ChatFilesProvider resolver={resolverOf("blob:broken")}>
        <Markdown content={CHART_MD} />
      </ChatFilesProvider>,
    );
    await settle();
    fireEvent.error(screen.getByRole("img", { name: CHART_ALT }));
    await settle();
    expect(document.querySelector('[data-state="missing"]')).not.toBeNull();
    expect(screen.getByText(CHART_ALT).className).toBe("alk-chat-image__missing");
  });
});

describe("a chat image whose bytes have not landed", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it.each([
    ["a shell that can look paths up", true],
    ["a shell that cannot", false],
  ])(
    "on %s: shows loading, asks nothing more on its own, and turns into the picture on the folder's signal",
    async (_label, locating) => {
      vi.useFakeTimers({ toFake: ["setTimeout", "setInterval"] });
      const resolveUrl = vi
        .fn<(path: string) => Promise<string | null>>()
        .mockRejectedValueOnce(new ChatFileNotReady())
        .mockResolvedValue("blob:landed");
      const resolver: ChatFilesResolver = locating
        ? locatingResolver({ "charts/x.png": CHART }, { resolveUrl })
        : { resolveUrl };
      const tree = (invalidate: number) => (
        <ChatFilesProvider resolver={resolver} invalidate={invalidate}>
          <Markdown content={CHART_MD} />
        </ChatFilesProvider>
      );
      const { rerender } = render(tree(1));
      await settle();

      expect(resolveUrl).toHaveBeenCalledTimes(1);
      expect(screen.getByRole("img", { name: CHART_ALT }).className).toBe("alk-chat-image__loading");
      expect(screen.queryByText(CHART_ALT)).toBeNull();
      expect(document.querySelector(".alk-chat-image__missing")).toBeNull();

      // No poll: a minute goes by and nothing is asked without the signal.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(60_000);
      });
      await settle();
      expect(resolveUrl).toHaveBeenCalledTimes(1);
      expect(screen.getByRole("img", { name: CHART_ALT }).className).toBe("alk-chat-image__loading");

      // The drive raises a change for the folder when the bytes land.
      rerender(tree(2));
      await settle();
      expect(resolveUrl).toHaveBeenCalledTimes(2);
      const img = screen.getByRole("img", { name: CHART_ALT });
      expect(img.tagName).toBe("IMG");
      expect(img.getAttribute("src")).toBe("blob:landed");
    },
  );

  it("a real 'nothing is there' is still the labelled placeholder, not loading", async () => {
    const resolver = locatingResolver({ "charts/x.png": CHART }, { resolveUrl: vi.fn(async () => null) });
    render(
      <ChatFilesProvider resolver={resolver}>
        <Markdown content={CHART_MD} />
      </ChatFilesProvider>,
    );
    await settle();
    expect(document.querySelector('[data-state="missing"]')).not.toBeNull();
    expect(screen.getByText(CHART_ALT).className).toBe("alk-chat-image__missing");
    expect(document.querySelector(".alk-chat-image__loading")).toBeNull();
  });
});

// "Not in the chat any more" says the file was deleted, so it is said only on
// evidence. A lookup made in the moment a save renames a new copy over the
// old one finds no file, and a lookup refused by a busy server is not
// "nowhere"; neither may mark a file that is still there as gone.
describe("a reference to a file that was here, when a lookup does not find it", () => {
  const PLAN: ChatFileRef = { nodeId: "nd_plan", parentId: null, name: "plan.md", path: "plan.md" };
  const content = "[the plan](plan.md)";

  it("asks a failing lookup again on a doubling wait, three times, then stops", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "setInterval", "Date"] });
    try {
      const asked: number[] = [];
      const locate = vi.fn(async () => {
        asked.push(Date.now());
        throw new Error("503 the database is busy");
      });
      render(
        <ChatFilesProvider resolver={locatingResolver({}, { locate })}>
          <Markdown content={content} />
        </ChatFilesProvider>,
      );
      for (let step = 0; step < 60; step += 1) {
        await act(async () => {
          await vi.advanceTimersByTimeAsync(1_000);
        });
      }
      const waits = asked.slice(1).map((at, i) => at - asked[i]!);
      expect(waits).toEqual([2_000, 4_000, 8_000]);
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps the link when a lookup fails, and asks again", async () => {
    let answer: () => Promise<ChatFileRef | null> = async () => PLAN;
    const resolver = locatingResolver({}, { locate: vi.fn(async () => answer()) });
    const { rerender } = render(
      <ChatFilesProvider resolver={resolver} invalidate={1}>
        <Markdown content={content} />
      </ChatFilesProvider>,
    );
    await waitFor(() => expect(document.querySelector("button.chat-file-ref--live")).not.toBeNull());

    answer = async () => {
      throw new Error("503 the database is busy");
    };
    rerender(
      <ChatFilesProvider resolver={resolver} invalidate={2}>
        <Markdown content={content} />
      </ChatFilesProvider>,
    );
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });
    expect(screen.queryByText("not in the chat any more")).toBeNull();
    expect(document.querySelector("button.chat-file-ref--live")).not.toBeNull();

    // The retry answers, and the reference is still the file.
    const asked = (resolver.locate as ReturnType<typeof vi.fn>).mock.calls.length;
    answer = async () => PLAN;
    await waitFor(
      () => expect((resolver.locate as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThan(asked),
      { timeout: 3_000 },
    );
    expect(document.querySelector("button.chat-file-ref--live")).not.toBeNull();
    expect(screen.queryByText("not in the chat any more")).toBeNull();
  });

  it("never says gone for a file that is missing for one look and back on the next", async () => {
    let present = true;
    const resolver = locatingResolver({}, { locate: vi.fn(async () => (present ? PLAN : null)) });
    const { rerender } = render(
      <ChatFilesProvider resolver={resolver} invalidate={1}>
        <Markdown content={content} />
      </ChatFilesProvider>,
    );
    await waitFor(() => expect(document.querySelector("button.chat-file-ref--live")).not.toBeNull());

    present = false;
    rerender(
      <ChatFilesProvider resolver={resolver} invalidate={2}>
        <Markdown content={content} />
      </ChatFilesProvider>,
    );
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });
    // The rename lands before the second look.
    present = true;
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, CHAT_FILE_GONE_CONFIRM_MS + 200));
    });
    expect(screen.queryByText("not in the chat any more")).toBeNull();
    expect(document.querySelector("button.chat-file-ref--live")).not.toBeNull();
  });
});
