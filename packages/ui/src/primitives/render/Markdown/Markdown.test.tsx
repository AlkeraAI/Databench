import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ResourceReference } from "../../../types";
import { Markdown } from "./Markdown";
import { parseMarkdownBlocks } from "./markdownBlocks";

afterEach(cleanup);

const HANDLE = "a".repeat(64);

describe("parseMarkdownBlocks", () => {
  it("parses the full block subset in order", () => {
    const blocks = parseMarkdownBlocks(
      [
        "# Title",
        "",
        "para one",
        "continues",
        "",
        "> a quote",
        "> second line",
        "",
        "- one",
        "- two",
        "",
        "1. first",
        "2. second",
        "",
        "| a | b |",
        "| --- | --- |",
        "| 1 | 2 |",
        "",
        "```sql",
        "select 1;",
        "```",
        "",
        "$$",
        "x^2",
        "$$",
      ].join("\n"),
    );
    expect(blocks.map((b) => b.kind)).toEqual([
      "heading",
      "paragraph",
      "quote",
      "list",
      "list",
      "table",
      "code",
      "math",
    ]);
    expect(blocks[3]).toMatchObject({ ordered: false, items: ["one", "two"] });
    expect(blocks[4]).toMatchObject({ ordered: true, items: ["first", "second"] });
    expect(blocks[5]).toMatchObject({ headers: ["a", "b"], rows: [["1", "2"]] });
    expect(blocks[6]).toMatchObject({ language: "sql", text: "select 1;" });
    expect(blocks[7]).toMatchObject({ text: "x^2" });
  });

  it.each([
    { name: "canonical link line", line: `[Results](blob:${HANDLE})`, expected: { handle: HANDLE, name: "Results" } },
    { name: "blob:// form", line: `[R](blob://${HANDLE})`, expected: { handle: HANDLE, name: "R" } },
    { name: "tolerated bare line", line: "blob:res-1 My rows", expected: { handle: "res-1", name: "My rows" } },
    { name: "bare line without a name", line: "blob:res-2", expected: { handle: "res-2", name: "Result" } },
  ])("recognizes an own-line blob reference: $name", ({ line, expected }) => {
    expect(parseMarkdownBlocks(line)).toEqual([{ kind: "blob", ...expected }]);
  });

  it("does NOT lift a mid-sentence blob link into a card", () => {
    const blocks = parseMarkdownBlocks(`see [Results](blob:${HANDLE}) for more`);
    expect(blocks).toHaveLength(1);
    expect(blocks[0]?.kind).toBe("paragraph");
  });

  it("normalizes CRLF input", () => {
    expect(parseMarkdownBlocks("# A\r\n\r\ntext")).toHaveLength(2);
  });
});

describe("Markdown", () => {
  it("routes a [label](path) link through onResourceOpen with a matched resource", () => {
    const onResourceOpen = vi.fn();
    const resources: ResourceReference[] = [
      { kind: "file", label: "the model", target: "models/orders.sql", path: "models/orders.sql" },
    ];
    render(
      <Markdown content="see [the model](models/orders.sql)" resources={resources} onResourceOpen={onResourceOpen} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "the model" }));
    expect(onResourceOpen).toHaveBeenCalledWith(resources[0]);
  });

  it("falls back to onLinkClick with a path-derived resource when no opener is given", () => {
    const onLinkClick = vi.fn();
    render(<Markdown content="[readme](guide/README.md)" onLinkClick={onLinkClick} />);
    fireEvent.click(screen.getByRole("button", { name: "readme" }));
    expect(onLinkClick).toHaveBeenCalledWith("guide/README.md");
  });

  it("derives an external resource for http(s) targets", () => {
    const onResourceOpen = vi.fn();
    render(<Markdown content="[site](https://example.com)" onResourceOpen={onResourceOpen} />);
    fireEvent.click(screen.getByRole("button", { name: "site" }));
    expect(onResourceOpen).toHaveBeenCalledWith(
      expect.objectContaining({ kind: "external", target: "https://example.com", path: undefined }),
    );
  });

  it("renders a mid-sentence blob link as an inert titled span, never a button", () => {
    render(<Markdown content={`see [Results](blob:${HANDLE}) inline`} />);
    expect(screen.queryByRole("button", { name: "Results" })).toBeNull();
    expect(screen.getByTitle(`blob:${HANDLE}`)).toHaveTextContent("Results");
  });

  it("renders an own-line blob link as the rich reference card", () => {
    render(<Markdown content={`[Results](blob:${HANDLE})`} />);
    expect(document.querySelector(".alk-markdown-blob")).not.toBeNull();
    expect(screen.getByText("Results")).toBeInTheDocument();
  });

  it("renders inline and display math through KaTeX", () => {
    render(<Markdown content={"inline $x^2$ and\n\n$$\ny = mx\n$$"} />);
    expect(document.querySelector(".alk-markdown-inline-math .katex")).not.toBeNull();
    expect(document.querySelector(".alk-markdown-math .katex-display")).not.toBeNull();
  });

  it("keeps bold and code marks and renders emphasis", () => {
    render(<Markdown content={"**bold** and `select *` and *emphasis* and _also_"} />);
    expect(screen.getByText("bold").tagName).toBe("STRONG");
    expect(screen.getByText("select *").tagName).toBe("CODE");
    expect(screen.getByText("emphasis").tagName).toBe("EM");
    expect(screen.getByText("also").tagName).toBe("EM");
  });

  it.each([
    ["select * from a join b on a.id = b.id where x * 2 > 3", "star-operator-and-projection"],
    ["count(*) and count(*) both counted", "count-star-twice"],
    ["pscale_extensions.hypopg_list_indexes and run_day", "snake-case-identifiers"],
    ["a * b", "spaced-star"],
  ])("leaves %s verbatim", (content) => {
    render(<Markdown content={content} />);
    expect(screen.getByText(content)).toBeInTheDocument();
    expect(document.querySelector("em")).toBeNull();
  });

  it("emphasizes each of several alternatives set side by side with slashes", () => {
    // Verbatim from an agent reply: the words either side were italic and these
    // three showed their asterisks.
    const { container } = render(
      <Markdown content={"*migration* is skewed; *comparison*/*integration*/*alternatives* are spread evenly"} />,
    );
    expect(Array.from(container.querySelectorAll("em")).map((em) => em.textContent)).toEqual([
      "migration",
      "comparison",
      "integration",
      "alternatives",
    ]);
    expect(container.textContent).toBe("migration is skewed; comparison/integration/alternatives are spread evenly");
  });

  it.each([
    ["globs src/*/lib/* stay globs", "star-glob-between-slashes"],
    ["see a/_private_/b for it", "underscore-path-segment"],
    ["a/*b */c", "slash-run-ending-on-a-space"],
    ["x/*.csv/*", "slash-run-starting-on-a-dot"],
  ])("leaves %s verbatim next to a slash", (content) => {
    render(<Markdown content={content} />);
    expect(screen.getByText(content)).toBeInTheDocument();
    expect(document.querySelector("em")).toBeNull();
  });

  it("captions fenced code and carets only while streaming", () => {
    const { rerender } = render(<Markdown content={"```python\nprint(1)\n```"} streaming />);
    expect(screen.getByText("python")).toBeInTheDocument();
    expect(document.querySelector(".alk-stream-caret")).not.toBeNull();
    rerender(<Markdown content={"```python\nprint(1)\n```"} />);
    expect(document.querySelector(".alk-stream-caret")).toBeNull();
  });
});

describe("nested lists", () => {
  /** Each `<li>`'s own words, without the text of the lists nested in it. */
  const ownText = (li: Element): string =>
    Array.from(li.childNodes)
      .filter((node) => !(node instanceof Element && (node.tagName === "UL" || node.tagName === "OL")))
      .map((node) => node.textContent ?? "")
      .join("")
      .trim();

  it("renders a two-level bullet list with the sub-list inside its item", () => {
    const { container } = render(
      <Markdown content={["- fruit", "  - apple", "  - pear", "- veg", "  - leek"].join("\n")} />,
    );
    const top = container.querySelector(".alk-markdown > ul");
    expect(top).not.toBeNull();
    const items = Array.from(top?.children ?? []);
    expect(items.map(ownText)).toEqual(["fruit", "veg"]);
    expect(Array.from(items[0].querySelectorAll(":scope > ul > li")).map(ownText)).toEqual(["apple", "pear"]);
    expect(Array.from(items[1].querySelectorAll(":scope > ul > li")).map(ownText)).toEqual(["leek"]);
    // One list at the top, not the four items flattened into it.
    expect(container.querySelectorAll(".alk-markdown > ul")).toHaveLength(1);
  });

  it("renders a numbered list with a bullet sub-list, the numbering unbroken", () => {
    const { container } = render(
      <Markdown
        content={["1. Load the table", "   - check the schema", "   - count the rows", "2. Join it"].join("\n")}
      />,
    );
    const top = container.querySelectorAll(".alk-markdown > ol");
    expect(top).toHaveLength(1);
    const items = Array.from(top[0].children);
    expect(items.map(ownText)).toEqual(["Load the table", "Join it"]);
    expect(Array.from(items[0].querySelectorAll(":scope > ul > li")).map(ownText)).toEqual([
      "check the schema",
      "count the rows",
    ]);
    expect(items[1].querySelector("ul, ol")).toBeNull();
  });

  it("keeps a sub-list across a blank line and an item's continuation text", () => {
    const blocks = parseMarkdownBlocks(["1. first", "   more of first", "", "   - under first", "2. second"].join("\n"));
    expect(blocks).toEqual([
      {
        kind: "list",
        ordered: true,
        items: ["first\nmore of first", "second"],
        nested: [[{ kind: "list", ordered: false, items: ["under first"] }], []],
      },
    ]);
  });

  it("leaves a flat list's block exactly as it was", () => {
    expect(parseMarkdownBlocks("- one\n- two\n\nafter")).toEqual([
      { kind: "list", ordered: false, items: ["one", "two"] },
      { kind: "paragraph", text: "after" },
    ]);
  });

  it("ends the list where a marker of the other kind starts at its own level", () => {
    expect(parseMarkdownBlocks("- one\n1. first").map((block) => block.kind)).toEqual(["list", "list"]);
  });
});

describe("code spans and emphasis", () => {
  it("renders a code span inside bold as code, not literal backticks", () => {
    const { container } = render(<Markdown content={"**`./scratch` doesn't exist.**"} />);
    const strong = container.querySelector("strong");
    expect(strong?.querySelector("code")?.textContent).toBe("./scratch");
    expect(strong?.textContent).toBe("./scratch doesn't exist.");
    expect(container.textContent).not.toContain("`");
  });

  it("renders a code span inside italic as code", () => {
    const { container } = render(<Markdown content={"see *the `orders` table* here"} />);
    const em = container.querySelector("em");
    expect(em?.querySelector("code")?.textContent).toBe("orders");
    expect(em?.textContent).toBe("the orders table");
  });

  it("keeps bold marks inside a code span literal", () => {
    const { container } = render(<Markdown content={"run `**not bold**` now"} />);
    expect(container.querySelector("strong")).toBeNull();
    expect(container.querySelector("code")?.textContent).toBe("**not bold**");
  });

  it("does not let bold marks inside a code span close a bold run around it", () => {
    const { container } = render(<Markdown content={"**see `a**b` now**"} />);
    const strong = container.querySelector("strong");
    expect(strong?.textContent).toBe("see a**b now");
    expect(strong?.querySelector("code")?.textContent).toBe("a**b");
  });

  it("the control: a plain backtick pair still renders as code", () => {
    const { container } = render(<Markdown content={"the `select *` query"} />);
    expect(container.querySelector("code")?.textContent).toBe("select *");
    expect(container.querySelector("strong, em")).toBeNull();
  });
});

describe("dollar signs and inline math", () => {
  const MATH = ".alk-markdown-inline-math";

  it.each([
    "claude costs 7.5× per run what perplexity does ($22.82 vs $3.04 per 1k)",
    "the cost total is an estimate — $2,273, about $52,162 the analytics side reports",
    "a $5 and a $6 item",
    "the range $5–$10",
  ])("renders %j as plain text with its dollar signs", (content) => {
    const { container } = render(<Markdown content={content} />);
    expect(container.querySelector(MATH)).toBeNull();
    expect(container.querySelector("em")).toBeNull();
    expect(container.textContent).toBe(content);
  });

  it.each(["$x^2 + y^2$", "$\\alpha$"])("still renders %j as inline math", (content) => {
    const { container } = render(<Markdown content={`where ${content} holds`} />);
    expect(container.querySelector(`${MATH} .katex`)).not.toBeNull();
  });

  it("still renders $$ display math", () => {
    const { container } = render(<Markdown content={"$$\ny = mx + b\n$$"} />);
    expect(container.querySelector(".alk-markdown-math .katex-display")).not.toBeNull();
  });

  it("renders an escaped dollar sign as a literal dollar", () => {
    const { container } = render(<Markdown content={"it costs \\$5 and \\$6"} />);
    expect(container.querySelector(MATH)).toBeNull();
    expect(container.textContent).toBe("it costs $5 and $6");
  });
});

// An agent's answer listed fifteen tables as a one-column table, and the
// transcript printed it as "| Table | |---| | geo.alert_events | …" on one line:
// the divider rule wanted two columns. The text is the reply exactly as the
// chat's log recorded it.
describe("a one-column table", () => {
  const PERSISTED =
    "Read from planetscale (postgres): the configuration/dimension side lives there, so its own catalog is the answer.\n\n15 tables, all in schema `geo` of database `postgres`:\n\n| Table |\n|---|\n| `geo.alert_events` |\n| `geo.alert_rules` |\n| `geo.brands` |\n| `geo.competitors` |\n| `geo.config_audit` |\n| `geo.customers` |\n| `geo.engines` |\n| `geo.models` |\n| `geo.prompt_runs` |\n| `geo.prompt_schedules` |\n| `geo.prompt_versions` |\n| `geo.responses` |\n| `geo.share_of_voice_daily` |\n| `geo.target_prompts` |\n| `geo.users` |\n\nTwo things worth knowing:\n- `prompt_runs` and `share_of_voice_daily` exist on both sides; for counting/ranking over full history read them from `analytics` (tinybird), which is the side designated for those entities.\n- `mentions` is listed for both sides but is **not** present here — it exists only on `analytics`.\n\nWant me to describe columns for any of these?";

  it("parses as a table of fifteen rows", () => {
    const table = parseMarkdownBlocks(PERSISTED).find((block) => block.kind === "table");
    expect(table).toEqual({
      kind: "table",
      headers: ["Table"],
      rows: expect.arrayContaining([["`geo.alert_events`"], ["`geo.users`"]]),
    });
    if (table?.kind !== "table") throw new Error("no table");
    expect(table.rows).toHaveLength(15);
  });

  it("renders as a table, with no pipes left in the text", () => {
    const { container } = render(<Markdown content={PERSISTED} />);
    expect(container.querySelectorAll("table tr")).toHaveLength(16);
    expect(container.textContent).not.toContain("|");
  });

  it("still reads a bare rule under a line as no table", () => {
    const blocks = parseMarkdownBlocks("a | b\n---\nafter");
    expect(blocks.some((block) => block.kind === "table")).toBe(false);
  });
});
