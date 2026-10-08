import { render } from "@testing-library/react";

import { extractMath, renderMarkdown } from "./markdown";
import { MarkdownView } from "./MarkdownOutput";

const PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==";

function dom(markdown: string): HTMLElement {
  const holder = document.createElement("div");
  holder.innerHTML = renderMarkdown(markdown);
  return holder;
}

function everyAttribute(root: HTMLElement): string[] {
  return [...root.querySelectorAll("*")].flatMap((el) => [...el.attributes].map((a) => `${el.tagName.toLowerCase()}[${a.name}=${a.value}]`));
}

describe("Markdown sanitization", () => {
  it.each([
    ["a script tag", "hi <script>alert(1)</script>", "script"],
    ["an iframe", '<iframe src="https://evil.example"></iframe>', "iframe"],
    ["an object", '<object data="x.swf"></object>', "object"],
    ["an embed", '<embed src="x">', "embed"],
    ["a form", '<form action="https://evil.example"><input name="p"></form>', "form, input"],
    ["a style element", "<style>body{display:none}</style>", "style"],
    ["svg", '<svg onload="alert(1)"><script>alert(1)</script><circle r="4"/></svg>', "svg, circle, script"],
    ["math", '<math><mi xlink:href="javascript:alert(1)">x</mi></math>', "math, mi"],
    ["a link element", '<link rel="stylesheet" href="https://evil.example/x.css">', "link"],
    ["a meta refresh", '<meta http-equiv="refresh" content="0;url=https://evil.example">', "meta"],
    ["a base element", '<base href="https://evil.example/">', "base"],
    ["a div", "<div>x</div>", "div"],
  ])("removes %s", (_name, markdown, selector) => {
    expect(dom(markdown).querySelector(selector)).toBeNull();
  });

  it.each([
    ["onclick", '<span class="x" onclick="alert(1)">t</span>'],
    ["onerror", `<img src="${PNG}" onerror="alert(1)">`],
    ["onmouseover on a link", '<a href="https://ok.example" onmouseover="alert(1)">t</a>'],
    ["ontoggle on details", '<details open ontoggle="alert(1)"><summary>s</summary>b</details>'],
  ])("strips the %s handler", (_name, markdown) => {
    const attributes = everyAttribute(dom(markdown));
    expect(attributes.some((a) => /\[on/i.test(a))).toBe(false);
  });

  it("strips style attributes", () => {
    const root = dom('<span class="c" style="position:fixed;inset:0">t</span><p style="color:red">p</p>');
    expect(root.querySelector("[style]")).toBeNull();
    expect(root.querySelector("span.c")).not.toBeNull();
  });

  it.each([
    ["javascript: in a Markdown link", "[x](javascript:alert(1))"],
    ["javascript: in an HTML link", '<a href="javascript:alert(1)">x</a>'],
    ["entity-encoded javascript:", '<a href="jav&#x61;script:alert(1)">x</a>'],
    ["javascript: with leading space and case", '<a href=" JaVaScRiPt:alert(1)">x</a>'],
    ["vbscript:", '<a href="vbscript:msgbox(1)">x</a>'],
    ["data:text/html", '<a href="data:text/html,<script>alert(1)</script>">x</a>'],
    ["data: in a Markdown link", "[x](data:text/html;base64,PHNjcmlwdD4=)"],
  ])("removes %s", (_name, markdown) => {
    const root = dom(markdown);
    for (const a of root.querySelectorAll("a")) {
      expect(a.getAttribute("href") ?? "").not.toMatch(/^\s*(javascript|vbscript|data):/i);
    }
    // The text may still read "javascript:"; no attribute may carry it.
    expect(everyAttribute(root).filter((a) => /javascript:|vbscript:|data:text/i.test(a))).toEqual([]);
  });

  it.each([
    ["an http image", "![x](https://tracker.example/p.png)"],
    ["an HTML image by URL", '<img src="https://tracker.example/p.png">'],
    ["a protocol-relative image", '<img src="//tracker.example/p.png">'],
    ["an SVG data image", '<img src="data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=">'],
    ["an image with no src", '<img alt="x">'],
  ])("removes %s", (_name, markdown) => {
    expect(dom(markdown).querySelector("img")).toBeNull();
  });

  it("keeps raster data images, from Markdown and from HTML", () => {
    const root = dom(`![dot](${PNG})\n\n<img src="${PNG}" alt="html dot">`);
    const images = [...root.querySelectorAll("img")];
    expect(images.map((img) => img.getAttribute("alt"))).toEqual(["dot", "html dot"]);
    expect(images.every((img) => img.getAttribute("src") === PNG)).toBe(true);
  });

  it("opens links in a new tab without an opener or referrer", () => {
    const root = dom("[docs](https://docs.example/x) and <a href=\"/rel\">rel</a> and https://auto.example");
    const links = [...root.querySelectorAll("a")];
    expect(links.map((a) => a.getAttribute("href"))).toEqual(["https://docs.example/x", "/rel", "https://auto.example"]);
    for (const a of links) {
      expect(a.getAttribute("target")).toBe("_blank");
      expect(a.getAttribute("rel")).toBe("noopener noreferrer");
    }
  });

  it("keeps the allowed tags", () => {
    const root = dom(
      [
        "# Title",
        "",
        "Some **bold**, *em*, ~~gone~~ and `code`; H<sub>2</sub>O, x<sup>2</sup>.",
        "",
        "- one",
        "1. first",
        "",
        "> quoted",
        "",
        "```python",
        "print(1)",
        "```",
        "",
        "| a | b |",
        "|:--|--:|",
        "| 1 | 2 |",
        "",
        "<details><summary>More</summary>hidden</details>",
        "",
        '<span class="badge">tag</span>',
        "",
        "---",
      ].join("\n"),
    );
    for (const selector of ["h1", "strong", "em", "s", "code", "sub", "sup", "ul li", "ol li", "blockquote", "pre code", "table thead th", "table tbody td", "details summary", "span.badge", "hr"]) {
      expect(root.querySelector(selector), selector).not.toBeNull();
    }
    expect(root.querySelector("pre code")).toHaveClass("language-python");
  });

  it("carries table alignment as a class, not a style", () => {
    const root = dom("| a | b | c |\n|:--|:-:|--:|\n| 1 | 2 | 3 |");
    const cells = [...root.querySelectorAll("tbody td")];
    expect(cells.map((td) => td.className)).toEqual(["nb-md-align-left", "nb-md-align-center", "nb-md-align-right"]);
    expect(root.querySelector("[style]")).toBeNull();
  });

  it("drops classes outside span, code languages and alignment", () => {
    const root = dom('<p class="nb-output-area">x</p><code class="language-sql evil">y</code><td class="evil">z</td>');
    expect(root.querySelector("p")?.hasAttribute("class")).toBe(false);
    expect(root.querySelector("code")?.className).toBe("language-sql");
  });

  it("drops ids and data attributes", () => {
    const root = dom('<span class="a" id="clobber" data-x="1" name="n">t</span>');
    expect(everyAttribute(root)).toEqual(["span[class=a]"]);
  });
});

describe("KaTeX math", () => {
  it("renders inline math inside a paragraph", () => {
    const root = dom("Euler: $e^{i\\pi} + 1 = 0$ done");
    const math = root.querySelector("p .nb-md-math .katex");
    expect(math).not.toBeNull();
    expect(root.querySelector(".katex-display")).toBeNull();
    expect(root.querySelector("annotation")?.textContent).toBe("e^{i\\pi} + 1 = 0");
    expect(root.textContent).toContain("Euler:");
    expect(root.textContent).toContain("done");
  });

  it("renders display math", () => {
    const root = dom("$$\n\\sum_{i=1}^n i = \\frac{n(n+1)}{2}\n$$");
    expect(root.querySelector(".nb-md-math--display .katex-display")).not.toBeNull();
    expect(root.querySelector("annotation")?.textContent).toBe("\\sum_{i=1}^n i = \\frac{n(n+1)}{2}");
  });

  it("protects math from Markdown emphasis", () => {
    const root = dom("$a_1 * b_2 * c_3$");
    expect(root.querySelector("em")).toBeNull();
    expect(root.querySelector("annotation")?.textContent).toBe("a_1 * b_2 * c_3");
  });

  it("keeps KaTeX's own inline styles", () => {
    const root = dom("$\\frac{a}{b}$");
    expect(root.querySelector(".katex [style]")).not.toBeNull();
  });

  it("leaves code spans and fenced code alone", () => {
    const root = dom("`$x$` and\n\n```\n$y$\n```");
    expect(root.querySelector(".katex")).toBeNull();
    expect(root.querySelector("p code")?.textContent).toBe("$x$");
    expect(root.querySelector("pre code")?.textContent).toBe("$y$\n");
  });

  it.each([
    ["prices", "It costs $5 and $10 today", 0],
    ["an escaped dollar", "\\$x$ is literal", 0],
    ["a space after the opener", "$ x$", 0],
  ])("does not treat %s as math", (_name, markdown, count) => {
    expect(dom(markdown).querySelectorAll(".katex")).toHaveLength(count);
  });

  it("renders bad TeX as an error, not a throw", () => {
    const root = dom("$\\frac{$");
    expect(root.querySelector(".katex-error")).not.toBeNull();
  });

  it("does not let TeX commands produce links or untrusted markup", () => {
    const root = dom("$\\href{javascript:alert(1)}{x}$ $\\url{https://evil.example}$ $\\htmlClass{x}{y}$");
    expect(root.querySelector("a")).toBeNull();
    expect(everyAttribute(root).filter((a) => /javascript:|https:/i.test(a))).toEqual([]);
  });

  it("puts math in an attribute back as source text", () => {
    const root = dom('[x](https://ok.example "$a<b$")');
    const title = root.querySelector("a")?.getAttribute("title");
    expect(title).toBe("$a<b$");
    expect(root.querySelector("a .katex")).toBeNull();
  });

  it("extracts math without touching the rest", () => {
    const { text, math } = extractMath("a $x$ b $$y$$ c");
    expect(math).toEqual([
      { tex: "x", display: false },
      { tex: "y", display: true },
    ]);
    expect(text.replace(/\uE000\d+\uE001/g, "_")).toBe("a _ b _ c");
  });
});

describe("MarkdownView", () => {
  it("draws the sanitized HTML", () => {
    const { container } = render(<MarkdownView markdown={"**hi** <script>window.__pwned = 1</script>"} />);
    expect(container.querySelector("strong")).toHaveTextContent("hi");
    expect(container.querySelector("script")).toBeNull();
  });
});
