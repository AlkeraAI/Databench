// The opencode code-cluster adapters are pure readers over wire payloads:
// todowrite's flat todo array, apply_patch's patch envelope, the LSP result
// union, and the two `Key: value` repo blobs. Each case drives resolveStep
// with a realistic payload and asserts the head facts and rendered interior a
// transcript reader sees.

import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { renderBody, stepOf, toolPart } from "./_steps";

// -- todowrite -------------------------------------------------------------

const TODOS = [
  { content: "Rig the fixtures", status: "completed", priority: "high" },
  { content: "Author the cases", status: "in_progress", priority: "medium" },
  { content: "Ship the port", status: "pending", priority: "low" },
];

describe("todowrite step", () => {
  it("tones progress pass only when all are done", () => {
    const step = stepOf(toolPart("todowrite", { output: JSON.stringify(TODOS) }));
    expect(step.verb).toBe("Planned");
    expect(step.object).toBe("3 todos");
    expect(step.data).toEqual({ kind: "count", text: "1 of 3 done" });
    expect(step.expanded).toBe(true);

    const done = TODOS.map((todo) => ({ ...todo, status: "completed" }));
    const all = stepOf(toolPart("todowrite", { output: JSON.stringify(done) }));
    expect(all.data).toEqual({ kind: "count", text: "3 of 3 done", tone: "pass" });
  });

  it("falls back to the call's input when the output cannot serve", () => {
    const flight = stepOf(toolPart("todowrite", { state: "running", input: { todos: TODOS } }));
    expect(flight.object).toBe("3 todos");
    // A call still out carries no result figure: the count is the answer's.
    expect(flight.data).toBeUndefined();

    const garbled = stepOf(toolPart("todowrite", { output: "{ not json", input: { todos: TODOS } }));
    expect(garbled.object).toBe("3 todos");
  });

  it("keeps the agent's order, status, and rank", () => {
    const body = renderBody(stepOf(toolPart("todowrite", { output: JSON.stringify(TODOS) })));
    const rows = [...body.querySelectorAll("li")];
    expect(rows.map((row) => row.textContent)).toEqual(["Rig the fixtures", "Author the cases", "Ship the port"]);
    expect(rows.map((row) => row.getAttribute("data-status"))).toEqual(["completed", "in_progress", "pending"]);
    expect(rows.map((row) => row.getAttribute("data-rank"))).toEqual(["high", "medium", "low"]);
    const shares = [...body.querySelectorAll("span[data-rank]")].map((share) => share.textContent);
    expect(shares).toEqual(["1 high", "1 medium", "1 low"]);
  });

  it("normalizes an unknown status instead of dropping it", () => {
    const rows = [{ content: "Odd one", status: "someday", priority: "urgent" }];
    const body = renderBody(stepOf(toolPart("todowrite", { output: JSON.stringify(rows) })));
    const row = body.querySelector("li");
    expect(row?.getAttribute("data-status")).toBe("pending");
    expect(row?.getAttribute("data-rank")).toBe("medium");
  });

  it("renders the no-todos band on an empty list", () => {
    const step = stepOf(toolPart("todowrite", { output: "[]" }));
    expect(step.object).toBe("0 todos");
    expect(step.data).toBeUndefined();
    expect(renderBody(step).textContent).toContain("The call carried no todos.");
  });

  it("routes the todo alias to the same adapter", () => {
    const step = stepOf(toolPart("todo", { output: JSON.stringify(TODOS) }));
    expect(step.verb).toBe("Planned");
    expect(step.loneSummary).toBe("Wrote the todo list");
  });
});

// -- apply_patch -----------------------------------------------------------

const PATCH = [
  "*** Begin Patch", // pins-source: opencode's apply_patch wire envelope, a frozen wire shape the adapter must keep parsing
  "*** Add File: src/new.ts",
  "+export const a = 1;",
  "+export const b = 2;",
  "*** Update File: src/app.ts",
  "*** Move to: src/main.ts",
  "@@ function main",
  " kept line",
  "-old line",
  "+new line",
  "bare context line",
  "*** Delete File: notes/old.md",
  "*** End Patch",
].join("\n");

const LONE_PATCH = ["*** Update File: src/app.ts", "*** Move to: src/main.ts", "@@ f", "+x"].join("\n");

/** The ledger's file rows, in the order the envelope named them. */
function fileRows(body: HTMLElement): HTMLElement[] {
  return [...body.querySelectorAll<HTMLElement>("[data-cap] > [data-change]")];
}

/** A row's summary button, the door to that file's diff. It leads the row, so
 *  the open-in-editor arrow behind it is never the one returned. */
function headOf(row: Element): HTMLButtonElement {
  const head = row.querySelector("button");
  if (!head) throw new Error("a patch row rendered no summary button");
  return head;
}

/** What a row says, its parts spaced the way a reader hears them. */
function rowSays(row: Element): string {
  return [...headOf(row).children]
    .map((part) => part.textContent?.trim() ?? "")
    .filter((text) => text.length > 0)
    .join(" ");
}

describe("apply_patch step", () => {
  it("parses the envelope into per-file counts and a diff figure", () => {
    const step = stepOf(toolPart("apply_patch", { input: { patchText: PATCH } }));
    expect(step.verb).toBe("Patched");
    expect(step.object).toBe("3 files");
    expect(step.objectKind).toBe("pattern");
    expect(step.data).toEqual({ kind: "diff", added: 3, removed: 1 });
    expect(step.expanded).toBe(true);
  });

  it("names a lone file by its landing path", () => {
    const step = stepOf(toolPart("apply_patch", { input: { patchText: LONE_PATCH } }));
    expect(step.object).toBe("src/main.ts");
    expect(step.objectKind).toBe("path");
  });

  it("renders one row per file in envelope order", () => {
    const body = renderBody(stepOf(toolPart("apply_patch", { input: { patchText: PATCH } })));
    const rows = fileRows(body);
    expect(rows.map((row) => row.getAttribute("data-change"))).toEqual(["added", "renamed", "deleted"]);
    // The removal figure sets in a true minus sign (U+2212), never a hyphen.
    expect(rows.map(rowSays)).toEqual([
      "Added src/new.ts +2 Show diff",
      "Renamed src/main.ts +1 −1 Show diff",
      "Deleted notes/old.md",
    ]);
    // A delete section carries no content, so its row has no diff to open.
    expect(headOf(rows[2]).disabled).toBe(true);
    // A patch of one file IS its diff, so that one opens itself.
    const lone = renderBody(stepOf(toolPart("apply_patch", { input: { patchText: LONE_PATCH } })));
    expect(rowSays(fileRows(lone)[0])).toContain("Hide diff");
  });

  it("keeps hunk context and an unmarked line whole", async () => {
    const body = renderBody(stepOf(toolPart("apply_patch", { input: { patchText: PATCH } })));
    // A batch of files opens no diff of its own, so the reading starts with a click.
    expect(body.querySelector('[role="group"]')).toBeNull();
    const rows = fileRows(body);
    await userEvent.setup().click(headOf(rows[1]));
    // An `@@` line names the enclosing block, and that reading labels the hunk.
    const hunk = body.querySelector('[role="group"][aria-label="function main"]');
    const lines = [...(hunk?.children ?? [])];
    expect(lines.map((line) => line.getAttribute("data-tone"))).toEqual(["ctx", "del", "add", "ctx"]);
    // A writer that left a context line bare keeps its whole text.
    expect(lines[3].textContent?.trim()).toBe("bare context line");
    // A rename still shows where it came from.
    expect(rows[1].textContent).toContain("from src/app.ts");
  });

  it("attaches a complaint to the file it names", async () => {
    const output = [
      "Patch applied.",
      "LSP errors detected in src/main.ts, please fix:",
      "TS2304: Cannot find name 'x'.",
    ].join("\n");
    const body = renderBody(stepOf(toolPart("apply_patch", { input: { patchText: PATCH }, output })));
    const rows = fileRows(body);
    // Closed, the flag is the whole signal, and it sits on the file the result named.
    const flagged = rows.filter((row) => row.querySelector('[aria-label="Flagged by the language server"]'));
    expect(flagged).toHaveLength(1);
    expect(flagged[0]).toBe(rows[1]);
    expect(rowSays(rows[1])).toContain("src/main.ts");

    // With one row open the attachment would be trivially true, so open them all.
    const user = userEvent.setup();
    for (const row of rows) {
      if (!headOf(row).disabled) await user.click(headOf(row));
    }
    const carrying = rows.filter((row) => row.textContent?.includes("TS2304: Cannot find name 'x'."));
    expect(carrying).toHaveLength(1);
    expect(carrying[0]).toBe(rows[1]);
  });

  it("states an empty envelope instead of throwing", () => {
    const step = stepOf(toolPart("apply_patch", { input: { patchText: "" } }));
    expect(step.object).toBe("0 files");
    expect(step.data).toBeUndefined();
    expect(renderBody(step).textContent).toContain("The patch carried no file sections.");
  });
});

// -- lsp -------------------------------------------------------------------

function lspPart(operation: string, result: unknown, input: Record<string, unknown> = {}): Parameters<typeof stepOf>[0] {
  return toolPart("lsp", { input: { operation, ...input }, output: JSON.stringify(result) });
}

describe("lsp step", () => {
  it("reads a plain Location and prints the server's line plus one", () => {
    const part = lspPart(
      "findReferences",
      [{ uri: "file:///ws/a.ts", range: { start: { line: 4, character: 2 }, end: { line: 4, character: 9 } } }],
      { filePath: "src/a.ts", line: 4, character: 2 },
    );
    const step = stepOf(part);
    expect(step.verb).toBe("Looked up references at");
    expect(step.object).toBe("src/a.ts:4:2");
    expect(step.objectKind).toBe("path");
    expect(step.data).toEqual({ kind: "count", text: "1 reference" });
    expect(step.expanded).toBe(false);
    // 4 on the wire is zero-based; the editor's line is 5.
    expect(renderBody(step).textContent).toContain("line 5");
  });

  it("reads SymbolInformation and decodes its kind", () => {
    const part = lspPart(
      "workspaceSymbol",
      [{ name: "OrderService", kind: 5, location: { uri: "file:///ws/src/svc.ts", range: { start: { line: 10 } } } }],
      { query: "Order" },
    );
    const step = stepOf(part);
    expect(step.verb).toBe("Searched symbols for");
    expect(step.object).toBe("Order");
    expect(step.objectKind).toBe("pattern");
    // SymbolKind 5 decodes to class, and the server's zero-based 10 prints as 11.
    expect(renderBody(step).textContent).toContain("classOrderService:11");
  });

  it("follows an outline's children as depth", () => {
    const part = lspPart(
      "documentSymbol",
      [
        {
          name: "Billing",
          kind: 5,
          range: { start: { line: 1 } },
          selectionRange: { start: { line: 2 } },
          children: [
            { name: "invoice", kind: 6, range: { start: { line: 5 } }, selectionRange: { start: { line: 5 } } },
          ],
        },
      ],
      { filePath: "src/billing.ts" },
    );
    const step = stepOf(part);
    expect(step.object).toBe("src/billing.ts");
    expect(step.data).toEqual({ kind: "count", text: "2 symbols" });
    const sites = [...renderBody(step).querySelectorAll<HTMLElement>("[data-cap] > div > div")];
    expect(sites.map((site) => site.style.getPropertyValue("--chat-lsp-depth"))).toEqual(["0", "1"]);
    // The outline row points at the name: selectionRange wins over range.
    expect(sites[0].textContent).toContain("Billing:3");
  });

  it("unwraps a call hierarchy from either direction's holder", () => {
    const item = {
      name: "main",
      kind: 12,
      uri: "file:///ws/c.ts",
      range: { start: { line: 7 } },
      selectionRange: { start: { line: 7 } },
    };
    const cases = [
      ["incomingCalls", "from", "Traced callers at", "1 caller"],
      ["outgoingCalls", "to", "Traced calls at", "1 call"],
    ] as const;
    for (const [operation, wrapper, verb, figure] of cases) {
      const part = lspPart(operation, [{ [wrapper]: item, fromRanges: [] }], {
        filePath: "src/c.ts",
        line: 7,
        character: 0,
      });
      const step = stepOf(part);
      expect(step.verb, operation).toBe(verb);
      expect(step.data, operation).toEqual({ kind: "count", text: figure });
      expect(renderBody(step).textContent, operation).toContain("functionmain:8");
    }
  });

  it("renders a hover through Prose, any shape", () => {
    const part = lspPart("hover", [{ contents: { kind: "markdown", value: "**signature** of the thing" } }], {
      filePath: "src/a.ts",
      line: 3,
      character: 1,
    });
    const step = stepOf(part);
    expect(step.verb).toBe("Inspected");
    expect(step.data).toEqual({ kind: "count", text: "1 note" });
    expect(renderBody(step).querySelector("strong")?.textContent).toBe("signature");

    for (const contents of ["plain docs", ["plain docs"]]) {
      const plain = stepOf(lspPart("hover", [{ contents }], { filePath: "a.ts" }));
      expect(renderBody(plain).textContent, String(contents)).toContain("plain docs");
    }
  });

  it("passes the server's no-results sentence through", () => {
    const said = toolPart("lsp", {
      input: { operation: "findReferences", filePath: "a.ts" },
      output: "No results found for symbol",
    });
    expect(renderBody(stepOf(said)).textContent).toContain("No results found for symbol");
    const silent = lspPart("findReferences", [], { filePath: "a.ts" });
    expect(renderBody(stepOf(silent)).textContent).toContain("The lookup came back empty.");
  });
});

// -- repo_clone ------------------------------------------------------------

const CLONE_OUTPUT = [
  "Repository ready: alkera/analytics",
  "Status: cached",
  "Local path: /home/x/.alkera/repos/alkera-analytics",
  "Branch: main",
  "Head: 0123456789abcdef",
].join("\n");

describe("repo_clone step", () => {
  it("speaks each status as its own verb and source line", () => {
    const cases = [
      ["cached", "Reused", "Reused the cached copy"],
      ["cloned", "Cloned", "Cloned from the remote"],
      ["refreshed", "Refreshed", "Refreshed from the remote"],
    ] as const;
    for (const [status, verb, source] of cases) {
      const output = CLONE_OUTPUT.replace("Status: cached", `Status: ${status}`);
      const step = stepOf(toolPart("repo_clone", { output, input: { repository: "alkera/analytics" } }));
      expect(step.verb, status).toBe(verb);
      expect(step.object, status).toBe("alkera/analytics");
      expect(renderBody(step).textContent, status).toContain(source);
    }

    // No status at all falls back; an unknown one is shown as the tool worded it.
    const silent = stepOf(toolPart("repo_clone", { output: "", input: { repository: "alkera/analytics" } }));
    expect(silent.verb).toBe("Cloned");
    const odd = CLONE_OUTPUT.replace("Status: cached", "Status: mirrored");
    expect(renderBody(stepOf(toolPart("repo_clone", { output: odd }))).textContent).toContain("mirrored");
  });

  it("pins the figure to the branch, then the seven-character head", () => {
    const branched = stepOf(toolPart("repo_clone", { output: CLONE_OUTPUT }));
    expect(branched.data).toEqual({ kind: "count", text: "main" });
    const headOnly = CLONE_OUTPUT.replace("Branch: main\n", "");
    const pinned = stepOf(toolPart("repo_clone", { output: headOnly }));
    expect(pinned.data).toEqual({ kind: "count", text: "0123456" });
  });

  it("reads the facts without case, shortening the commit", () => {
    const shouted = CLONE_OUTPUT.replace("Local path:", "LOCAL PATH:").replace("Head:", "HEAD:");
    const body = renderBody(stepOf(toolPart("repo_clone", { output: shouted })));
    expect(body.textContent).toContain("alkera-analytics");
    expect(body.textContent).toContain("0123456");
    expect(body.textContent).not.toContain("0123456789abcdef");
  });
});

// -- repo_overview ---------------------------------------------------------

const OVERVIEW_OUTPUT = [
  "Repository: alkera/analytics",
  "Branch: main",
  "Ecosystems: python, typescript",
  "Package manager: pnpm",
  "Dependency files: pyproject.toml, package.json",
  "Likely entrypoints:",
  "- main: src/index.ts",
  "- bin: cli.js",
  "Top-level structure:",
  "src/",
  "  index.ts",
  "  lib/",
  "    util.ts",
  "README.md",
  "(Structure truncated)",
].join("\n");

describe("repo_overview step", () => {
  it("counts only files in the profile", () => {
    const step = stepOf(
      toolPart("repo_overview", { output: OVERVIEW_OUTPUT, input: { repository: "alkera/analytics" } }),
    );
    expect(step.verb).toBe("Explored");
    expect(step.object).toBe("alkera/analytics");
    expect(step.data).toEqual({ kind: "count", text: "3 files" });
    expect(step.expanded).toBe(false);
    expect(step.footer).toBe("structure truncated");

    // A whole tree owes no extent line.
    const whole = OVERVIEW_OUTPUT.replace("\n(Structure truncated)", "");
    expect(stepOf(toolPart("repo_overview", { output: whole })).footer).toBeUndefined();
  });

  it("renders the chips, roles, and indented tree", () => {
    const body = renderBody(stepOf(toolPart("repo_overview", { output: OVERVIEW_OUTPUT })));
    const chips = [...body.querySelectorAll(".chat-repo-chip")].map((chip) => chip.textContent);
    expect(chips).toEqual(["main", "python", "typescript", "pnpm", "pyproject.toml", "package.json"]);
    const roles = [...body.querySelectorAll(".chat-repo-entry__role")].map((role) => role.textContent);
    expect(roles).toEqual(["main", "bin"]);
    const rows = [...body.querySelectorAll<HTMLElement>(".chat-repo-tree__row")];
    expect(rows.map((row) => row.querySelector(".chat-repo-tree__name")?.textContent)).toEqual([
      "src",
      "index.ts",
      "lib",
      "util.ts",
      "README.md",
    ]);
    expect(rows.map((row) => row.style.getPropertyValue("--chat-repo-level"))).toEqual(["0", "1", "1", "2", "0"]); // same-author-ok: mechanical custom-property rename (--ro-level -> --chat-repo-level)
    expect(rows.map((row) => row.hasAttribute("data-dir"))).toEqual([true, false, true, false, false]);
  });
});
