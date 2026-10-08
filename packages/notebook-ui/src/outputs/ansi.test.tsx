import { render } from "@testing-library/react";

import { PLAIN_STYLE, collapseCarriageReturns, parseAnsi, stripAnsi, xtermColor, type AnsiStyle } from "./ansi";
import { AnsiText } from "./AnsiText";

const ESC = "\x1b";

function style(overrides: Partial<AnsiStyle>): AnsiStyle {
  return { ...PLAIN_STYLE, ...overrides };
}

describe("parseAnsi", () => {
  it.each([
    ["no escapes", "plain", [{ text: "plain", style: PLAIN_STYLE }]],
    ["basic foreground", `${ESC}[31mred`, [{ text: "red", style: style({ fg: { kind: "palette", index: 1 } }) }]],
    ["bright foreground", `${ESC}[92mgreen`, [{ text: "green", style: style({ fg: { kind: "palette", index: 10 } }) }]],
    ["basic background", `${ESC}[44mbg`, [{ text: "bg", style: style({ bg: { kind: "palette", index: 4 } }) }]],
    ["bright background", `${ESC}[103mbg`, [{ text: "bg", style: style({ bg: { kind: "palette", index: 11 } }) }]],
    ["bold and underline", `${ESC}[1;4mx`, [{ text: "x", style: style({ bold: true, underline: true }) }]],
    ["256-color foreground", `${ESC}[38;5;208mo`, [{ text: "o", style: style({ fg: { kind: "palette", index: 208 } }) }]],
    ["256-color background", `${ESC}[48;5;22mo`, [{ text: "o", style: style({ bg: { kind: "palette", index: 22 } }) }]],
    ["truecolor foreground", `${ESC}[38;2;10;20;30mt`, [{ text: "t", style: style({ fg: { kind: "rgb", r: 10, g: 20, b: 30 } }) }]],
    ["truecolor background", `${ESC}[48;2;1;2;3mt`, [{ text: "t", style: style({ bg: { kind: "rgb", r: 1, g: 2, b: 3 } }) }]],
    ["colon 256-color", `${ESC}[38:5:99mc`, [{ text: "c", style: style({ fg: { kind: "palette", index: 99 } }) }]],
    ["colon truecolor with colorspace", `${ESC}[38:2::7:8:9mc`, [{ text: "c", style: style({ fg: { kind: "rgb", r: 7, g: 8, b: 9 } }) }]],
    ["truecolor then more codes", `${ESC}[38;2;1;2;3;1mz`, [{ text: "z", style: style({ fg: { kind: "rgb", r: 1, g: 2, b: 3 }, bold: true }) }]],
    ["out-of-range truecolor is ignored", `${ESC}[38;2;300;0;0mz`, [{ text: "z", style: PLAIN_STYLE }]],
  ])("%s", (_name, input, expected) => {
    expect(parseAnsi(input)).toEqual(expected);
  });

  it("resets with 0 and with an empty parameter list", () => {
    expect(parseAnsi(`${ESC}[1;31ma${ESC}[0mb${ESC}[32mc${ESC}[md`)).toEqual([
      { text: "a", style: style({ bold: true, fg: { kind: "palette", index: 1 } }) },
      { text: "b", style: PLAIN_STYLE },
      { text: "c", style: style({ fg: { kind: "palette", index: 2 } }) },
      { text: "d", style: PLAIN_STYLE },
    ]);
  });

  it.each([
    ["22 ends bold and dim", `${ESC}[1;2ma${ESC}[22mb`],
    ["24 ends underline", `${ESC}[4ma${ESC}[24mb`],
    ["39 ends the foreground", `${ESC}[31ma${ESC}[39mb`],
    ["49 ends the background", `${ESC}[41ma${ESC}[49mb`],
    ["23 ends italic", `${ESC}[3ma${ESC}[23mb`],
    ["27 ends inverse", `${ESC}[7ma${ESC}[27mb`],
    ["29 ends strikethrough", `${ESC}[9ma${ESC}[29mb`],
  ])("%s", (_name, input) => {
    const segments = parseAnsi(input);
    expect(segments).toHaveLength(2);
    expect(segments[0].style).not.toEqual(PLAIN_STYLE);
    expect(segments[1]).toEqual({ text: "b", style: PLAIN_STYLE });
  });

  it("drops non-SGR control sequences without changing style", () => {
    const input = `${ESC}[2K${ESC}[1Aa${ESC}]0;title\x07b${ESC}]8;;https://x.example${ESC}\\c${ESC}[?25ld`;
    expect(parseAnsi(input)).toEqual([{ text: "abcd", style: PLAIN_STYLE }]);
  });

  it("merges adjacent runs of the same style", () => {
    expect(parseAnsi(`${ESC}[31ma${ESC}[31mb`)).toEqual([{ text: "ab", style: style({ fg: { kind: "palette", index: 1 } }) }]);
  });

  it("strips escapes for copying", () => {
    expect(stripAnsi(`${ESC}[1;31mError${ESC}[0m: bad`)).toBe("Error: bad");
  });
});

describe("collapseCarriageReturns", () => {
  it.each([
    ["overwrites within a line", "10%\r50%\r100%", "100%"],
    ["keeps CRLF as a line end", "a\r\nb", "a\nb"],
    ["a trailing CR keeps the line", "done\r", "done"],
    ["each line on its own", "1\r2\nx\ry", "2\ny"],
    ["cursor up rewrites an earlier line (two bars)", "a 10%\nb 10%\n\x1b[2Aa 50%\nb 60%\n", "a 50%\nb 60%\n"],
    ["cursor up keeps the column", "x\ny\x1b[Az", "xz\ny"],
    ["cursor up stops at the first line", "x\x1b[5A\ry", "y"],
    ["cursor down moves on again", "a\nb\x1b[Ac\x1b[Bd", "ac\nb d"],
    ["erase to the end of the line", "abcdef\rab\x1b[K", "ab"],
    ["erase line clears it", "old\x1b[2Knew", "new"],
    ["colours stay in the text", "\x1b[31m1%\r\x1b[31m9%", "\x1b[31m9%"],
    ["a rewrite split across chunks joined as one", ["50%", "\r", "100%"].join(""), "100%"],
  ])("%s", (_name, input, expected) => {
    expect(collapseCarriageReturns(input)).toBe(expected);
  });
});

describe("xtermColor", () => {
  it.each([
    [16, "rgb(0, 0, 0)"],
    [196, "rgb(255, 0, 0)"],
    [21, "rgb(0, 0, 255)"],
    [231, "rgb(255, 255, 255)"],
    [232, "rgb(8, 8, 8)"],
    [255, "rgb(238, 238, 238)"],
  ])("maps %i", (index, expected) => {
    expect(xtermColor(index)).toBe(expected);
  });
});

describe("AnsiText", () => {
  it("draws styled runs as spans with classes and colors", () => {
    const { container } = render(<AnsiText text={`${ESC}[1;31mbold red${ESC}[0m ${ESC}[38;2;1;2;3mtrue${ESC}[0m ${ESC}[38;5;196mcube`} />);
    const spans = [...container.querySelectorAll("span")];
    const boldRed = spans.find((s) => s.textContent === "bold red");
    expect(boldRed).toHaveClass("nb-ansi-bold");
    expect(boldRed?.style.color).toBe("var(--nb-ansi-1)");
    expect(spans.find((s) => s.textContent === "true")?.style.color).toBe("rgb(1, 2, 3)");
    expect(spans.find((s) => s.textContent === "cube")?.style.color).toBe("rgb(255, 0, 0)");
  });

  it("swaps colors for inverse", () => {
    const { container } = render(<AnsiText text={`${ESC}[7;31;42mx`} />);
    const span = container.querySelector("span");
    expect(span?.style.color).toBe("var(--nb-ansi-2)");
    expect(span?.style.backgroundColor).toBe("var(--nb-ansi-1)");
  });

  it("never parses markup in the text", () => {
    const { container } = render(<AnsiText text={`${ESC}[31m<img src=x onerror=alert(1)><script>alert(1)</script>`} />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(container.textContent).toBe("<img src=x onerror=alert(1)><script>alert(1)</script>");
  });
});
