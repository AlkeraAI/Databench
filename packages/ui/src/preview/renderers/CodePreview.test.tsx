import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { PreviewFacts, PreviewProps } from "../types";
import { CodePreview, codeRenderer } from "./CodePreview";
import { manualGate, numberedWindows, placeScroller, WindowedHost } from "./windowedHost.testkit";

afterEach(cleanup);

const KIB = 1024;

function facts(over: Partial<PreviewFacts> = {}): PreviewFacts {
  return { mime: "application/json", name: "config.json", size: 40, ...over };
}

function props(text: string, over: Partial<PreviewFacts> = {}): PreviewProps {
  return {
    facts: facts(over),
    content: { kind: "text", text },
    version: "etag-1",
    status: "ready",
    onDownload: () => {},
  };
}

/** The plate's lines, as the reader sees them. */
function lines(container: HTMLElement): string[] {
  return [...container.querySelectorAll(".alk-codeblock__t")].map((line) =>
    (line.textContent ?? "").replace(/\n$/, ""),
  );
}

describe("the code renderer's claim", () => {
  it.each([
    ["a json document", "application/json", "config.json", true],
    ["a python file the server sniffed as text", "text/plain", "run.py", true],
    ["a sql file", "text/plain", "model.sql", true],
    ["a shell script", "text/plain", "deploy.sh", true],
    ["a note with no grammar", "text/plain", "notes.txt", false],
    ["a file with no extension", "text/plain", "LICENSE", false],
    ["a spreadsheet", "text/csv", "rows.csv", false],
    ["a picture", "image/png", "chart.png", false],
  ])("%s", (_name, mime, name, claimed) => {
    expect(codeRenderer.match(facts({ mime, name }))).toBe(claimed);
  });

  it("asks the host for the decoded body", () => {
    expect(codeRenderer.needs(facts())).toBe("text");
  });
});

describe("rendering source", () => {
  it("highlights by the grammar the file's name names", () => {
    const { container } = render(<CodePreview {...props("def go():\n    pass\n", { mime: "text/plain", name: "run.py" })} />);

    const keyword = container.querySelector(".alk-tok--keyword");
    expect(keyword?.textContent).toBe("def");
  });

  it("leaves a file with no grammar as plain ink", () => {
    const { container } = render(
      <CodePreview {...props("def go():\n", { mime: "text/plain", name: "run.unknownext" })} />,
    );

    expect(container.querySelector(".alk-tok--keyword")).toBeNull();
    expect(container.textContent).toContain("def go():");
  });

  it("numbers the lines", () => {
    const { container } = render(<CodePreview {...props("a\nb\nc\n", { mime: "text/plain", name: "run.py" })} />);

    expect(container.querySelectorAll(".alk-codeblock__line").length).toBeGreaterThanOrEqual(3);
  });

  it("pretty-prints json up to half a mebibyte", () => {
    const { container } = render(<CodePreview {...props('{"a":1,"b":[2,3]}', { size: 512 * KIB })} />);

    expect(lines(container)).toEqual(["{", '  "a": 1,', '  "b": [', "    2,", "    3", "  ]", "}"]);
  });

  it("leaves json past half a mebibyte exactly as it arrived", () => {
    const { container } = render(<CodePreview {...props('{"a":1,"b":[2,3]}', { size: 512 * KIB + 1 })} />);

    expect(container.textContent).toContain('{"a":1,"b":[2,3]}');
    expect(container.textContent).not.toContain('"a": 1');
  });

  it("shows broken json as it is rather than failing on the parse", () => {
    const { container } = render(<CodePreview {...props('{"a":1,', { size: 8 })} />);

    expect(container.textContent).toContain('{"a":1,');
  });

  it("draws nothing when the host has no bytes yet", () => {
    const { container } = render(
      <CodePreview
        {...{ ...props(""), content: { kind: "none" } as const, status: "loading" as const }}
      />,
    );

    expect(container.textContent).toBe("");
  });
});

describe("source that has not all arrived", () => {
  const TOTAL = 3 * 1024 * 1024;
  const windows = numberedWindows(3, 5, "x =");

  function host(gate = manualGate()) {
    const view = render(
      <WindowedHost
        windows={windows}
        total={TOTAL}
        gate={gate}
        draw={(content) => (
          <CodePreview
            {...{ ...props("", { mime: "text/plain", name: "run.py", size: TOTAL }), content }}
          />
        )}
      />,
    );
    return { ...view, gate };
  }

  /** The gutter's numbers, beside the line each belongs to. */
  function numbered(container: HTMLElement): string[] {
    return [...container.querySelectorAll(".alk-codeblock__line")]
      .map((line) => {
        const n = line.querySelector(".alk-codeblock__n")?.textContent ?? "";
        const t = (line.querySelector(".alk-codeblock__t")?.textContent ?? "").replace(/\n$/, "");
        return `${n}:${t}`;
      })
      .filter((row) => !row.endsWith(":"));
  }

  it("says how much is here and brings the next window on the button", async () => {
    const { container, gate } = host();

    expect(screen.getByRole("status")).toHaveTextContent("Showing 1 MB of 3.1 MB");
    fireEvent.click(screen.getByRole("button", { name: "Show more" }));
    expect(screen.getByRole("status")).toHaveTextContent("Loading more…");
    await act(async () => gate.open());

    expect(lines(container).filter((line) => line.trim() !== "")).toEqual(
      Array.from({ length: 10 }, (_, index) => `x = ${index + 1}`),
    );
  });

  it("keeps the line numbers running on across windows", async () => {
    const { container, gate } = host();
    fireEvent.click(screen.getByRole("button", { name: "Show more" }));
    await act(async () => gate.open());

    // Line 6 is the first line of the second window, and it is numbered 6.
    expect(numbered(container).slice(4, 7)).toEqual(["5:x = 5", "6:x = 6", "7:x = 7"]);
  });

  it("asks once from a scroll near the end and keeps the reader's place", async () => {
    const { container, gate } = host();
    const scroller = container.querySelector(".alk-preview-code")!;
    placeScroller(scroller, { scrollHeight: 4000, clientHeight: 400, top: 3000 });

    fireEvent.scroll(scroller);
    fireEvent.scroll(scroller);
    await act(async () => gate.open());

    expect(lines(container).filter((line) => line.trim() !== "")).toEqual(
      Array.from({ length: 10 }, (_, index) => `x = ${index + 1}`),
    );
    expect(container.querySelector(".alk-preview-code")).toBe(scroller);
    expect((scroller as HTMLElement).scrollTop).toBe(3000);
  });

  it("offers the block's own copy only once the file is whole", async () => {
    const { container, gate } = host();
    expect(container.querySelector(".alk-codeblock-frame")).toBeNull();

    for (let window = 1; window < windows.length; window += 1) {
      fireEvent.click(screen.getByRole("button", { name: "Show more" }));
      await act(async () => gate.open());
    }

    expect(screen.queryByTestId("preview-more")).toBeNull();
    expect(container.querySelector(".alk-codeblock-frame")).not.toBeNull();
  });

  it("draws no line for source that arrived whole", () => {
    render(<CodePreview {...props("x = 1\n", { mime: "text/plain", name: "run.py" })} />);

    expect(screen.queryByTestId("preview-more")).toBeNull();
  });
});
