import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { PermissionConversationPart } from "@alkera/chat-model";
import { PermissionCard } from "@alkera/ui";

import { APPROVAL_WITHHELD } from "@/pages/workspace/chat/data/ChatDataSource";
import {
  EXEC_TITLE,
  alwaysScope,
  askedBy,
  permissionCardProps,
  permissionTitle,
} from "@/pages/workspace/chat/options";

function ask(overrides: Partial<PermissionConversationPart> = {}): PermissionConversationPart {
  return {
    id: "permission-1",
    kind: "permission",
    requestId: "permission-1",
    permissionKind: "edit",
    canonicalKind: "edit",
    patterns: ["src/app.ts"],
    options: [
      { optionId: "allow_once", name: "Allow once" },
      { optionId: "allow_always", name: "Always allow" },
      { optionId: "reject_once", name: "Deny" },
    ],
    status: "pending",
    ...overrides,
  };
}

function renderAsk(part: PermissionConversationPart): HTMLElement {
  const props = permissionCardProps(
    part,
    undefined,
    () => {},
    () => {},
  );
  return render(
    <div className="chat-root">
      <PermissionCard {...props} />
    </div>,
  ).container;
}

describe("permission card evidence", () => {
  it("shows the proposed diff before an edit is approved", () => {
    const container = renderAsk(
      ask({
        preview: {
          kind: "diff",
          title: "app.ts",
          content: "@@ -1,1 +1,1 @@\n-old\n+new\n",
        },
      }),
    );

    expect(container.querySelector(".chat-permission-preview")).not.toBeNull();
    expect(container.querySelector('[data-tone="del"]')?.textContent).toContain("old");
    expect(container.querySelector('[data-tone="add"]')?.textContent).toContain("new");
  });

  it("uses SQL highlighting and shows classified target and cost", () => {
    const container = renderAsk(
      ask({
        canonicalKind: "other",
        permissionKind: "warehouse",
        patterns: ["delete from analytics.orders where id = 7"],
        subject: {
          capability: "sql",
          targets: [{ kind: "table", name: "analytics.orders" }],
          cost: { usd: 0.42, bytesScanned: 1288490188 },
          reasons: [],
        },
      }),
    );

    expect(container.querySelectorAll(".chat-tok--keyword").length).toBeGreaterThan(0);
    expect(container.querySelector(".chat-permission-facts")?.textContent).toContain(
      "analytics.orders$0.421.2 GB scanned",
    );
  });

  it("marks project-wide approval with a check", async () => {
    const user = userEvent.setup();
    const container = renderAsk(ask());

    await user.click(screen.getByRole("button", { name: "Always allow" }));
    expect(container.querySelector(".chat-permission-scope__glyph")?.classList).toContain(
      "tabler-icon-check",
    );
  });
});

// "Always allow" records the classified `(capability, operation)`, not the
// command on the card: approving `git status` stops every `git status` being
// asked. The card is where that difference has to be said, because nothing else
// the reader sees carries it.
describe("what a standing grant would record", () => {
  const shellAsk = (subject: PermissionConversationPart["subject"]) =>
    ask({
      permissionKind: "bash",
      canonicalKind: "shell",
      patterns: ["git status"],
      subject,
    });

  it("names the rule under the command, not just the command", () => {
    const container = renderAsk(
      shellAsk({ capability: "shell", operation: "git_status", targets: [], reasons: [] }),
    );

    expect(container.querySelector(".chat-permission-note")?.textContent).toBe(
      "Always allow covers every git status command.",
    );
  });

  it("says an exact-command grant covers only that line, not the verb", () => {
    // A destructive command is never granted by its verb: the shell offers
    // "Always allow this exact command", and the note must not promise every rm.
    const container = renderAsk(
      ask({
        permissionKind: "bash",
        canonicalKind: "shell",
        patterns: ["rm -rf build/"],
        options: [
          { optionId: "allow_once", name: "Allow once" },
          { optionId: "allow_always", name: "Always allow this exact command" },
          { optionId: "reject_once", name: "Deny" },
        ],
        subject: { capability: "shell", operation: "rm", targets: [], reasons: [] },
      }),
    );
    expect(container.querySelector(".chat-permission-note")?.textContent).toBe(
      "Always allow covers this exact command.",
    );
    expect(container.textContent).not.toContain("covers every");
  });

  it.each([
    ["fs", "edit", "Always allow covers every edit file action."],
    ["network", "fetch", "Always allow covers every fetch request."],
    ["sql", "drop_table", "Always allow covers every drop table statement."],
  ])("reads the rule in %s's own register", (capability, operation, expected) => {
    const container = renderAsk(shellAsk({ capability, operation, targets: [], reasons: [] }));
    expect(container.querySelector(".chat-permission-note")?.textContent).toBe(expected);
  });

  it.each([
    ["no subject at all", undefined],
    [
      "an operation nothing could classify",
      { capability: "shell", operation: "unknown", targets: [], reasons: [] },
    ],
    ["a subject with no operation", { capability: "shell", targets: [], reasons: [] }],
  ])("says nothing when the classifier named nothing: %s", (_case, subject) => {
    const container = renderAsk(
      shellAsk(subject as PermissionConversationPart["subject"]),
    );
    expect(container.querySelector(".chat-permission-note")).toBeNull();
  });

  // The absence of the button is the whole message: nothing on the card is
  // allowed to explain another part of the card.
  it("says nothing at all on a card that offers no standing grant", () => {
    const container = renderAsk(
      ask({
        permissionKind: "bash",
        canonicalKind: "shell",
        patterns: ["git status"],
        subject: { capability: "shell", operation: "git_status", targets: [], reasons: [] },
        options: [
          { optionId: "allow_once", name: "Allow once" },
          { optionId: "reject_once", name: "Reject once" },
        ],
      }),
    );

    expect(screen.queryByRole("button", { name: /always/i })).toBeNull();
    expect(container.querySelector(".chat-permission-note")).toBeNull();
    expect(container.textContent).not.toMatch(/isn't offered/iu);
  });

  // A destructive line the shell expands at run time has no exact text to
  // grant, so the shell sends no `allow_always` for it — and the card must not
  // carry the exact-command note that a destructive ask otherwise gets.
  it.each([
    ["a variable", "rm -rf $BUILD_DIR"],
    ["a command substitution", "rm -rf `cat dirs.txt`"],
    ["a glob", "rm -rf build/*"],
  ])("offers no always and says nothing about it for a destructive line with %s", (_case, line) => {
    const container = renderAsk(
      ask({
        permissionKind: "bash",
        canonicalKind: "shell",
        patterns: [line],
        subject: { capability: "shell", operation: "rm", scope: "command", targets: [], reasons: [] },
        options: [
          { optionId: "allow_once", name: "Allow once" },
          { optionId: "reject_once", name: "Deny" },
        ],
      }),
    );

    expect(screen.queryByRole("button", { name: /always/i })).toBeNull();
    expect(container.textContent).not.toMatch(/always allow/iu);
    expect(container.querySelector(".chat-permission-note")).toBeNull();
  });
});

// The reader decides on what the card names. The tool, the verb and the concrete
// argument all have to reach it — a heading over a blank is the defect this
// covers, in every state the ask can arrive in.
describe("what the card names", () => {
  it.each([
    ["shell", "bash", "git status", "Run this command?", ".chat-permission-cmd__text"],
    ["edit", "edit", "src/app.ts", "Edit these files?", ".chat-permission-cmd__text"],
    [
      "network",
      "webfetch",
      "https://example.com/feed",
      "Fetch this address?",
      ".chat-permission-cmd__link",
    ],
  ])(
    "a %s ask reads as its verb over its own subject",
    (canonical, permissionKind, pattern, title, inset) => {
      const part = ask({
        permissionKind,
        canonicalKind: canonical as PermissionConversationPart["canonicalKind"],
        patterns: [pattern],
      });
      const container = renderAsk(part);

      expect(screen.getByRole("heading", { level: 2 }).textContent).toBe(title);
      expect(container.querySelector(inset)?.textContent).toBe(pattern);
      expect(container.querySelector(".chat-permission-unnamed")).toBeNull();
    },
  );

  // The three kinds whose own phrasing names no action: each renders its
  // subject plainly, under the most concrete heading the ask could supply.
  it.each([
    ["task", "task", "spawn a reviewer subagent", "Delegate this work?"],
    ["external", "external_directory", "/opt/alkera-work", "Allow access outside the project?"],
    ["other", "mcp__alkera__blob.query", "select 1", "Allow blob.query?"],
  ])(
    "a %s ask heads with the most concrete thing it carries",
    (canonical, permissionKind, pattern, title) => {
      const container = renderAsk(
        ask({
          permissionKind,
          canonicalKind: canonical as PermissionConversationPart["canonicalKind"],
          patterns: [pattern],
        }),
      );

      expect(screen.getByRole("heading", { level: 2 }).textContent).toBe(title);
      expect(container.querySelector(".chat-permission-cmd__text")?.textContent).toBe(pattern);
      expect(container.querySelector(".chat-permission-cmd__prompt")).toBeNull();
      expect(container.querySelector(".chat-permission-unnamed")).toBeNull();
    },
  );

  // An ask for a tool the harness hosts over MCP arrives under the tool's own
  // name with no category to speak of. "Allow this action?" names nothing the
  // reader can weigh; the tool's name does. The wire spelling is prefixed and
  // sanitized on the way through opencode, so the resolution has to go through
  // the tool surface's own door rather than printing what arrived.
  it.each(["mcp__alkera__blob.query", "alkera_blob_query", "blob.query"])(
    "names the tool the category could not: %s",
    (permissionKind) => {
      const part = ask({ permissionKind, canonicalKind: "other", patterns: ["select 1"] });

      expect(permissionTitle(part)).toBe("Allow blob.query?");
      expect(askedBy(part)).toBeUndefined();
    },
  );

  it.each([
    ["external_directory", "external", "Allow access outside the project?"],
    ["repo_clone", "external", "Clone this repository?"],
    ["repo_overview", "other", "Read this cached repository?"],
    ["doom_loop", "shell", "Repeat this call again?"],
    ["workflow_tool_approval", "shell", "Allow this workflow step?"],
  ])("asks %s's own question, not a wire key", (permissionKind, canonical, title) => {
    const part = ask({
      permissionKind,
      canonicalKind: canonical as PermissionConversationPart["canonicalKind"],
      patterns: ["/opt/alkera-work/notes.md"],
    });

    expect(permissionTitle(part)).toBe(title);
    expect(askedBy(part)).toBeUndefined();
  });

  // A key nothing has registered is the adapter's private vocabulary. It may
  // never become the reader's question — the generic one at least means
  // something to them.
  it.each([
    ["other", "Allow this action?"],
    ["external", "Allow this action?"],
    ["shell", "Run this command?"],
  ])("falls back to %s's own question for an unregistered key", (canonical, title) => {
    const part = ask({
      permissionKind: "some_future_guard",
      canonicalKind: canonical as PermissionConversationPart["canonicalKind"],
      patterns: ["whatever"],
    });

    expect(permissionTitle(part)).toBe(title);
    expect(permissionTitle(part)).not.toContain("some_future_guard");
    expect(askedBy(part)).toBeUndefined();
  });
});

// A harness can raise a permission for a call whose arguments never reach the
// ask — opencode does it for every tool it hosts over MCP, which is how the
// shell's card came to read "Run this command?" over a bare `*`. The card is
// the last reader of that ask, so it must not present a blank, or a glob, as
// the thing being approved.
describe("permission card with nothing named", () => {
  const unnamed = (): PermissionConversationPart =>
    ask({
      permissionKind: "bash",
      canonicalKind: "shell",
      patterns: [],
      options: [
        { optionId: "allow_once", name: "Allow once" },
        { optionId: "reject_once", name: "Reject once" },
      ],
    });

  // The reported defect: a heading over the sentence "No command was named." and
  // nothing else. The ask still carries a tool and a category; both belong on the
  // card, and the heading asks about the class of action rather than pointing at
  // a subject that is not there.
  it("names the tool and the class of action rather than what is missing", () => {
    const container = renderAsk(unnamed());

    expect(screen.getByRole("heading", { level: 2 }).textContent).toBe("Run a command?");
    expect(container.querySelector(".chat-permission-unnamed")?.textContent).toBe(
      "Requested by the bash tool.",
    );
    expect(container.textContent).not.toMatch(/was named|nothing was/iu);
    expect(container.querySelector(".chat-permission-cmd")).toBeNull();
    expect(container.querySelector(".chat-permission-cmd__prompt")).toBeNull();
  });

  it.each([
    ["edit", "edit", "Edit a file?"],
    ["network", "webfetch", "Fetch an address?"],
  ])("asks about the class a %s ask gates when it named no subject", (canonical, tool, title) => {
    const part = ask({
      permissionKind: tool,
      canonicalKind: canonical as PermissionConversationPart["canonicalKind"],
      patterns: [],
    });
    expect(permissionTitle(part)).toBe(title);
    expect(askedBy(part)).toBe(`Requested by the ${tool} tool.`);
  });

  // An ask key is the ADAPTER's vocabulary: opencode gates a subagent spawn
  // under `task`, which is no tool at all. The line is dropped rather than
  // handing the reader a key as the name of a tool.
  it.each([
    ["permission", "shell", "Run a command?"],
    ["task", "task", "Delegate this work?"],
  ])("shows no tool line for the %s key, which names no tool", (permissionKind, canonical, title) => {
    const container = renderAsk(
      ask({
        permissionKind,
        canonicalKind: canonical as PermissionConversationPart["canonicalKind"],
        patterns: [],
      }),
    );

    expect(screen.getByRole("heading", { level: 2 }).textContent).toBe(title);
    expect(container.querySelector(".chat-permission-unnamed")).toBeNull();
    expect(container.textContent).not.toContain(permissionKind);
    expect(container.querySelector(".chat-permission-cmd")).toBeNull();
  });

  it("offers no standing grant, and explains nothing about the one it withheld", () => {
    const container = renderAsk(unnamed());

    expect(screen.queryByRole("button", { name: /always/i })).toBeNull();
    expect(container.querySelector(".chat-permission-note")).toBeNull();
    // Deciding on this one is still reachable — withholding the standing grant
    // must not take the approval with it.
    expect(screen.getByRole("button", { name: /allow once/i })).not.toBeNull();
  });

  it("keeps the note off a card that does offer a standing grant", () => {
    const container = renderAsk(ask());
    expect(container.querySelector(".chat-permission-note")).toBeNull();
  });

  // The ask can reach the transcript before the tool call naming its subject;
  // the harness raises it again with the subject a moment later. Until then the
  // card says it is waiting and nothing on it can be pressed — a reader must
  // never approve, or be told nothing was named, on an ask still being named.
  it("waits with every action disabled while the subject is still on its way", async () => {
    const decided: string[] = [];
    const props = permissionCardProps(
      ask({
        permissionKind: "bash",
        canonicalKind: "shell",
        patterns: [],
        subjectPending: true,
        options: [
          { optionId: "allow_once", name: "Allow once" },
          { optionId: "reject_once", name: "Reject once" },
        ],
      }),
      undefined,
      (id) => decided.push(id),
      () => {},
    );
    const { container } = render(
      <div className="chat-root">
        <PermissionCard {...props} />
      </div>,
    );

    expect(container.querySelector(".chat-permission-waiting")?.textContent).toBe(
      "Waiting for the command…",
    );
    expect(container.querySelector(".chat-permission-unnamed")).toBeNull();
    expect(container.querySelector(".chat-permission-note")).toBeNull();
    const allow = screen.getByRole("button", { name: /allow once/i });
    const deny = screen.getByRole("button", { name: /reject once/i });
    expect(allow).toBeDisabled();
    expect(deny).toBeDisabled();
    await userEvent.click(allow);
    await userEvent.click(deny);
    expect(decided).toEqual([]);
  });

  it("says the subject was never named only once the ask settled without one", () => {
    const container = renderAsk(unnamed());
    expect(container.querySelector(".chat-permission-waiting")).toBeNull();
    expect(screen.getByRole("button", { name: /allow once/i })).not.toBeDisabled();
  });

  // An older mirror publishes the subject-less ask and never raises it again,
  // so the card sits on the tool line. The fold recovers the command from the
  // gated call's own part; the card must then read as the command it is, with
  // the decision back in the reader's hands.
  it("swaps the tool line for the command the gated call turned out to carry", async () => {
    const asked = ask({
      permissionKind: "bash",
      canonicalKind: "shell",
      patterns: [],
      options: [
        { optionId: "allow_once", name: "Allow once" },
        { optionId: "reject_once", name: "Reject once" },
      ],
    });
    const decided: string[] = [];
    const cardFor = (part: PermissionConversationPart) => (
      <div className="chat-root">
        <PermissionCard
          {...permissionCardProps(
            part,
            undefined,
            (id) => decided.push(id),
            () => {},
          )}
        />
      </div>
    );
    const { container, rerender } = render(cardFor(asked));
    expect(container.querySelector(".chat-permission-unnamed")?.textContent).toBe(
      "Requested by the bash tool.",
    );

    // What the fold writes onto the same part once the gated call's part lands.
    rerender(cardFor({ ...asked, patterns: ["cd /opt/alkera-work && ls"] }));

    expect(container.querySelector(".chat-permission-unnamed")).toBeNull();
    expect(container.querySelector(".chat-permission-cmd__text")?.textContent).toBe(
      "cd /opt/alkera-work && ls",
    );
    expect(screen.getByRole("heading", { level: 2 }).textContent).toBe("Run this command?");
    const allow = screen.getByRole("button", { name: /allow once/i });
    expect(allow).not.toBeDisabled();
    await userEvent.click(allow);
    expect(decided).toEqual(["allow_once"]);
  });

  // The whole point of waiting: the same ask, re-raised under its id with the
  // command the tool part carried, replaces the waiting line with the command
  // and hands the reader back their decision — in place, on the card they are
  // already looking at.
  it("swaps the waiting line for the command when the tool part lands", async () => {
    const pending = ask({
      permissionKind: "bash",
      canonicalKind: "shell",
      patterns: [],
      subjectPending: true,
      options: [
        { optionId: "allow_once", name: "Allow once" },
        { optionId: "reject_once", name: "Reject once" },
      ],
    });
    const decided: string[] = [];
    const cardFor = (part: PermissionConversationPart) => (
      <div className="chat-root">
        <PermissionCard
          {...permissionCardProps(
            part,
            undefined,
            (id) => decided.push(id),
            () => {},
          )}
        />
      </div>
    );
    const { container, rerender } = render(cardFor(pending));
    expect(container.querySelector(".chat-permission-waiting")).not.toBeNull();

    // What the fold writes onto the same part when the named copy arrives.
    rerender(
      cardFor({
        ...pending,
        subjectPending: undefined,
        patterns: ["cd /opt/alkera-work && ls"],
        subject: { capability: "shell", operation: "list", targets: [], reasons: [] },
        options: [
          { optionId: "allow_once", name: "Allow once" },
          { optionId: "allow_always", name: "Always allow" },
          { optionId: "reject_once", name: "Reject once" },
        ],
      }),
    );

    expect(container.querySelector(".chat-permission-waiting")).toBeNull();
    expect(container.querySelector(".chat-permission-unnamed")).toBeNull();
    expect(container.querySelector(".chat-permission-cmd__text")?.textContent).toBe(
      "cd /opt/alkera-work && ls",
    );
    expect(screen.getByRole("heading", { level: 2 }).textContent).toBe("Run this command?");
    const allow = screen.getByRole("button", { name: /allow once/i });
    expect(allow).not.toBeDisabled();
    await userEvent.click(allow);
    expect(decided).toEqual(["allow_once"]);
  });

  it("keeps the note off a card whose workspace refuses the ask outright", () => {
    const props = permissionCardProps(
      unnamed(),
      undefined,
      () => {},
      () => {},
      APPROVAL_WITHHELD,
    );
    const { container } = render(
      <div className="chat-root">
        <PermissionCard {...props} />
      </div>,
    );

    expect(container.querySelector(".chat-permission-refusal")).not.toBeNull();
    expect(container.querySelector(".chat-permission-note")).toBeNull();
  });
});

// The card's prose lines are the only place a reader learns what a decision
// covers, and they sit in a dock that narrows to 300px. Held to the same bar as
// every other line of product copy: sentence case, one sentence, and the fact
// with nothing wrapped around it.
describe("permission card copy", () => {
  // The budget a note has before it wraps to a third line in the narrow dock and
  // pushes the action bar off a short panel.
  const ONE_NOTE = 96;

  const scope = (capability: string, operation: string): string => {
    const line = alwaysScope("Always allow", {
      capability,
      operation,
      targets: [],
      reasons: [],
    });
    if (line === undefined) throw new Error(`no scope line for ${capability}/${operation}`);
    return line;
  };

  const requestedBy = (permissionKind: string): string => {
    const line = askedBy(ask({ permissionKind, canonicalKind: "shell", patterns: [] }));
    if (line === undefined) throw new Error(`no tool line for ${permissionKind}`);
    return line;
  };

  const lines = [
    scope("shell", "git_status"),
    scope("sql", "drop_table"),
    scope("fs", "edit"),
    scope("knowledge", "knowledge_share"),
    scope("knowledge", "knowledge_unshare"),
    scope("mongodb", "merge"),
    scope("file", "graph_create"),
    requestedBy("bash"),
    requestedBy("mcp__alkera__blob.query"),
  ];

  it.each(lines)("%s is one sentence in sentence case", (line) => {
    expect(line).toMatch(/^[A-Z].*\.$/u);
    // One sentence: nothing starts again after a full stop.
    expect(line).not.toMatch(/\.\s+\S/u);
    expect(line.length).toBeLessThanOrEqual(ONE_NOTE);
  });

  it.each(lines)("%s carries no filler", (line) => {
    expect(line).not.toMatch(/\b(please|simply|just|note that)\b/iu);
    expect(line).not.toContain("!");
    expect(line).not.toContain("…");
  });

  // Every heading the card can render, over every subject state, asks one
  // question about something concrete — never "this" pointing at a blank.
  const CANONICAL_KINDS = ["shell", "edit", "network", "task", "external", "other"] as const;

  it.each(CANONICAL_KINDS.flatMap((kind) => [`${kind} named`, `${kind} unnamed`]))(
    "%s reads as one concrete question",
    (label) => {
      const [kind, named] = label.split(" ");
      const title = permissionTitle(
        ask({
          permissionKind: "bash",
          canonicalKind: kind as PermissionConversationPart["canonicalKind"],
          patterns: named === "named" ? ["git status"] : [],
        }),
      );
      expect(title).toMatch(/^[A-Z].*\?$/u);
      expect(title.length).toBeLessThanOrEqual(ONE_NOTE);
      if (named === "unnamed") expect(title).not.toMatch(/\bthis (command|file|address)\b/u);
    },
  );
});

// A statement that runs a program or reaches files on the database server reads
// as data work in the inset; the heading is where the card says what it does.
describe("an exec ask", () => {
  function execAsk(effect: "exec" | "destroy"): PermissionConversationPart {
    return ask({
      canonicalKind: "other",
      permissionKind: "mcp__alkera__sql.query",
      patterns: ["COPY t TO '/tmp/out.csv'"],
      subject: { capability: "sql", effect, operation: "copy_file", targets: [], reasons: [] },
    });
  }

  it("heads the card with the exec sentence", () => {
    expect(permissionTitle(execAsk("exec"))).toBe(EXEC_TITLE);
    renderAsk(execAsk("exec"));
    expect(screen.getByText(EXEC_TITLE)).toBeInTheDocument();
  });

  it("asks one question and does not call a server-file write a read", () => {
    expect(EXEC_TITLE.split("?").length).toBe(2);
    expect(EXEC_TITLE.endsWith("?")).toBe(true);
    expect(EXEC_TITLE.toLowerCase()).not.toContain("read");
  });

  it("keeps the query's own question for a statement that is not exec", () => {
    expect(permissionTitle(execAsk("destroy"))).toBe("Run this destructive query?");
  });
});

// A knowledge share sends a note to the team; the reader is approving that note.
// The card shows it as a note — the title as a heading, the body as prose — and
// asks about the destination, instead of setting the policy statement in the
// command inset under "Allow this action?".
describe("permission card for a knowledge write", () => {
  const share = (
    overrides: Partial<PermissionConversationPart> = {},
  ): PermissionConversationPart =>
    ask({
      permissionKind: "knowledge",
      canonicalKind: "other",
      patterns: ["Share with the Team Knowledge Base:\nRevenue\nRefunds are netted out of revenue."],
      subject: {
        capability: "knowledge",
        effect: "egress",
        operation: "knowledge_share",
        targets: [{ kind: "knowledge", name: "Team Knowledge Base" }],
        reasons: [],
      },
      preview: {
        kind: "text",
        title: "Revenue",
        content: "Refunds are **netted** out of revenue.\n\nOn the day they settle.",
      },
      ...overrides,
    });

  it("asks about the destination and shows the note, not the statement", () => {
    const container = renderAsk(share());

    expect(
      screen.getByRole("heading", { name: "Share with the Team Knowledge Base?" }),
    ).not.toBeNull();
    expect(container.querySelector(".chat-permission-item__title")?.textContent).toBe("Revenue");
    const body = container.querySelector(".chat-permission-item__body");
    expect(body?.textContent).toContain("On the day they settle.");
    expect(body?.querySelector("strong")?.textContent).toBe("netted");
    expect(container.querySelector(".chat-permission-cmd")).toBeNull();
    expect(container.querySelector(".chat-permission-preview")).toBeNull();
    expect(container.textContent).not.toContain("Share with the Team Knowledge Base:");
    expect(container.querySelector(".chat-permission-facts")?.textContent).toBe(
      "Team Knowledge Base",
    );
    expect(container.querySelector(".chat-permission-note")?.textContent).toBe(
      "Always allow covers every share with the Team Knowledge Base.",
    );
  });

  it("shows a note that has no title as prose alone", () => {
    const container = renderAsk(
      share({ preview: { kind: "text", content: "Refunds are netted out of revenue." } }),
    );
    expect(container.querySelector(".chat-permission-item__title")).toBeNull();
    expect(container.querySelector(".chat-permission-item__body")?.textContent).toContain(
      "netted",
    );
  });

  it("folds a long note and opens it on request", async () => {
    const user = userEvent.setup();
    const long = Array.from({ length: 30 }, (_, i) => `Line ${i + 1} of the note.`).join("\n");
    const container = renderAsk(share({ preview: { kind: "text", content: long } }));
    const item = container.querySelector(".chat-permission-item");

    expect(item?.hasAttribute("data-folded")).toBe(true);
    await user.click(screen.getByRole("button", { name: "Show more" }));
    expect(item?.hasAttribute("data-folded")).toBe(false);
    await user.click(screen.getByRole("button", { name: "Show less" }));
    expect(item?.hasAttribute("data-folded")).toBe(true);
  });

  it("keeps a short note open with nothing to press", () => {
    const container = renderAsk(share());
    expect(container.querySelector(".chat-permission-item")?.hasAttribute("data-folded")).toBe(
      false,
    );
    expect(screen.queryByRole("button", { name: /show (more|less)/iu })).toBeNull();
  });

  it("says when the machine cut the note before sending it", () => {
    const cut = renderAsk(
      share({ preview: { kind: "text", title: "Revenue", content: "x", truncated: true } }),
    );
    expect(cut.querySelector(".chat-permission-item__cut")?.textContent).toBe(
      "The note continues past what is shown here.",
    );
    expect(renderAsk(share()).querySelector(".chat-permission-item__cut")).toBeNull();
  });

  it("reads a withdrawal as a sentence, not a command", () => {
    const container = renderAsk(
      share({
        patterns: ["Withdraw 'fact:revenue' from the Team Knowledge Base"],
        subject: {
          capability: "knowledge",
          effect: "egress",
          operation: "knowledge_unshare",
          targets: [{ kind: "knowledge", name: "Team Knowledge Base" }],
          reasons: [],
        },
        preview: undefined,
      }),
    );

    expect(
      screen.getByRole("heading", { name: "Withdraw from the Team Knowledge Base?" }),
    ).not.toBeNull();
    expect(container.querySelector(".chat-permission-statement")?.textContent).toBe(
      "Withdraw 'fact:revenue' from the Team Knowledge Base",
    );
    expect(container.querySelector(".chat-permission-cmd")).toBeNull();
    expect(container.querySelector(".chat-permission-note")?.textContent).toBe(
      "Always allow covers every withdrawal from the Team Knowledge Base.",
    );
  });

  it("falls back to the statement when an older machine sends no note", () => {
    const container = renderAsk(share({ preview: undefined }));
    expect(container.querySelector(".chat-permission-item")).toBeNull();
    expect(container.querySelector(".chat-permission-statement")?.textContent).toContain(
      "Refunds are netted out of revenue.",
    );
  });

  it("asks about the project base for a note a rule made it ask about", () => {
    expect(
      permissionTitle(
        share({
          subject: {
            capability: "knowledge",
            effect: "memory",
            operation: "knowledge_note",
            targets: [{ kind: "knowledge", name: "Project Knowledge Base" }],
            reasons: [],
          },
        }),
      ),
    ).toBe("Save to the Project Knowledge Base?");
  });

  it("keeps the line breaks the note was written with, and its markup", () => {
    const container = renderAsk(
      share({ preview: { kind: "text", content: "First line.\nSecond line with **weight**.\nThird line." } }),
    );
    const body = container.querySelector(".chat-permission-item__body");
    // The lines are still three lines in the paragraph (not one joined sentence),
    // and the emphasis still renders.
    expect(body?.textContent).toBe("First line.\nSecond line with weight.\nThird line.");
    expect(body?.querySelector("strong")?.textContent).toBe("weight");
  });
});

// The TUI already words every ask by the classifier's capability and effect;
// the card asked "Allow this action?" for everything but a shell command, an
// edit and a fetch. The same lanes, the same words.
describe("permission card question by capability", () => {
  type Subject = Partial<NonNullable<PermissionConversationPart["subject"]>>;
  const subjectAsk = (
    canonicalKind: PermissionConversationPart["canonicalKind"],
    subject: Subject,
    patterns: string[] = ["…"],
  ): PermissionConversationPart =>
    ask({
      permissionKind: "other",
      canonicalKind,
      patterns,
      subject: { targets: [], reasons: [], ...subject },
    });

  const questions: Array<[string, Subject, string]> = [
    ["a query", { capability: "sql", effect: "write", operation: "insert" }, "Run this query?"],
    [
      "a destructive query",
      { capability: "sql", effect: "destroy", operation: "drop_table" },
      "Run this destructive query?",
    ],
    [
      "a query that exports",
      { capability: "sql", effect: "egress", operation: "copy_into" },
      "Run this query? It exports data.",
    ],
    [
      "an aggregation",
      { capability: "mongodb", effect: "write", operation: "merge" },
      "Run this aggregation?",
    ],
    [
      "a destructive request",
      { capability: "elasticsearch", effect: "destroy", operation: "delete_index" },
      "Send this destructive request?",
    ],
    [
      "sdk code on a connection",
      {
        capability: "integration_sdk",
        effect: "destroy",
        operation: "call_integration_sdk",
        targets: [{ kind: "connection", name: "dbx-prod", connection: "dbx-prod" }],
      },
      "Run this code against dbx-prod?",
    ],
    [
      "a graph file",
      { capability: "file", effect: "write", operation: "graph_create" },
      "Write this graph file?",
    ],
    [
      "a control-plane action",
      {
        capability: "databricks",
        effect: "write",
        operation: "run_job",
        targets: [{ kind: "job", name: "42", connection: "dbx-prod" }],
      },
      "Allow this action on dbx-prod?",
    ],
  ];

  it.each(questions)("asks about %s", (_what, subject, expected) => {
    expect(permissionTitle(subjectAsk("other", subject))).toBe(expected);
  });

  it("keeps the harness's own question for a shell command", () => {
    expect(
      permissionTitle(subjectAsk("shell", { capability: "shell", operation: "git_status" })),
    ).toBe("Run this command?");
  });

  it("keeps the exec sentence above every lane", () => {
    expect(
      permissionTitle(subjectAsk("other", { capability: "sql", effect: "exec", operation: "copy" })),
    ).toBe(EXEC_TITLE);
  });

  it("sets a control-plane action as a sentence with its first word raised", () => {
    const container = renderAsk(
      subjectAsk(
        "other",
        {
          capability: "snowflake",
          effect: "write",
          operation: "suspend_warehouse",
          targets: [{ kind: "warehouse", name: "WH_1", connection: "snow-prod" }],
        },
        ["suspend warehouse WH_1"],
      ),
    );
    expect(container.querySelector(".chat-permission-statement")?.textContent).toBe(
      "Suspend warehouse WH_1",
    );
    expect(container.querySelector(".chat-permission-cmd")).toBeNull();
    expect(container.querySelector(".chat-permission-note")?.textContent).toBe(
      "Always allow covers every suspend warehouse action.",
    );
  });

  it("paints sdk code as python in the inset", () => {
    const container = renderAsk(
      subjectAsk(
        "other",
        {
          capability: "integration_sdk",
          effect: "destroy",
          operation: "call_integration_sdk",
          targets: [{ kind: "connection", name: "dbx-prod", connection: "dbx-prod" }],
        },
        ["import os\nfor job in client.jobs.list():\n    print(job.name)"],
      ),
    );
    expect(container.querySelector(".chat-permission-cmd")).not.toBeNull();
    expect(container.querySelectorAll(".chat-tok--keyword").length).toBeGreaterThan(0);
  });

  it("keeps a query in the mono inset", () => {
    const container = renderAsk(
      subjectAsk("other", { capability: "sql", effect: "write", operation: "insert" }, [
        "insert into t values (1)",
      ]),
    );
    expect(container.querySelector(".chat-permission-cmd")).not.toBeNull();
    expect(container.querySelector(".chat-permission-statement")).toBeNull();
  });
});
