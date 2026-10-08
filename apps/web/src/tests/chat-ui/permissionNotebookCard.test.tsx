import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { PermissionConversationPart } from "@alkera/chat-model";
import { PermissionCard } from "@alkera/ui";

import {
  createConversationFoldState,
  foldHarnessEvent,
} from "@/pages/workspace/chat/data/harnessEventFold";
import { permissionCardProps } from "@/pages/workspace/chat/options";

// A notebook run's ask, from the event the box raises to the card a person
// reads: one row per cell by the name a person knows it by, the cells that run
// with them behind one quiet line, why it needs approval once, and the
// notebook as a link. No headings per line, no internal cell ids, and never
// the path the notebook has on the box's disk.

const BOX_FILE = "/opt/alkera-work/.alkera/workspaces/115d3feb/files/analysis.alknb.py";
const IDS = ["a7yg9x7evz", "007ferqvhc", "twtt7g67kt"];

function runEvent(options: { option_id: string; name: string }[]) {
  return {
    event_type: "permission.request",
    request_id: "ask-nb",
    permission_kind: "notebook",
    canonical_kind: "other",
    prompting: true,
    patterns: ["Run 2 cells in analysis.alknb.py"],
    subject: {
      capability: "notebook",
      effect: "write",
      confidence: "unknown",
      operation: "notebook_run",
      scope: "command",
      targets: [{ kind: "notebook", name: BOX_FILE }],
      reasons: IDS.map((id) => `_ (${id}): write, not certain`),
    },
    preview: {
      kind: "notebook",
      title: "Run 2 cells in analysis.alknb.py",
      notebook: {
        lead: "Run 2 cells in",
        file_name: "analysis.alknb.py",
        file_path: "analysis.alknb.py",
        tail: "",
        cells: [
          { name: "setup", code: "import alkera", role: "dependency" },
          { name: "Cell 2", code: "import alkera\nimport polars as pl\ndf = pl.read_csv('a.csv')", role: "target" },
          { name: "Cell 3", code: "df.head()", role: "target" },
        ],
        packages: [],
      },
    },
    options,
  };
}

const ONCE = [
  { option_id: "allow_once", name: "Allow once" },
  { option_id: "reject_once", name: "Reject once" },
];

function foldedAsk(options = ONCE): PermissionConversationPart {
  const state = createConversationFoldState();
  foldHarnessEvent(state, runEvent(options));
  const part = state.turns.flatMap((turn) => turn.parts).find((p) => p.kind === "permission");
  if (part?.kind !== "permission") throw new Error("no permission part folded");
  return part;
}

function renderCard(
  part: PermissionConversationPart,
  { onDecide = () => {}, onOpenFile }: { onDecide?: (id: string) => void; onOpenFile?: (path: string) => void } = {},
): HTMLElement {
  const props = permissionCardProps(part, undefined, onDecide, () => {}, undefined, undefined, true, onOpenFile);
  return render(
    <div className="chat-root">
      <PermissionCard {...props} />
    </div>,
  ).container;
}

describe("a notebook run's permission card", () => {
  it("lists the cells asked for by name with the start of their code", () => {
    const container = renderCard(foldedAsk());
    expect(screen.getByRole("heading", { level: 2 }).textContent).toBe("Run 2 cells in analysis.alknb.py");
    const rows = within(screen.getByRole("list", { name: "Cells to run" })).getAllByRole("listitem");
    expect(rows.map((row) => row.querySelector(".chat-notebook-ask__name")?.textContent)).toEqual([
      "Cell 2",
      "Cell 3",
    ]);
    expect(
      Array.from(rows[0].querySelectorAll(".chat-notebook-ask__line"), (line) => line.textContent),
    ).toEqual(["import alkera", "import polars as pl …"]);
    expect(container).toHaveTextContent("Can't tell if it changes anything");
  });

  it("has one heading, and no cell id, box path or classifier jargon", () => {
    const container = renderCard(foldedAsk());
    expect(container.querySelectorAll("h1, h2, h3, h4, h5, h6")).toHaveLength(1);
    const text = container.textContent ?? "";
    for (const leak of [...IDS, BOX_FILE, "/opt/", "not certain", "_ ("]) {
      expect(text).not.toContain(leak);
    }
    // "Always allow" would record nothing for code no classifier can read, so
    // the card neither offers it nor says what it covers.
    expect(text).not.toContain("Always allow");
  });

  it("keeps the cells that run with them behind one line until asked", async () => {
    renderCard(foldedAsk());
    const more = screen.getByRole("button", { name: "Also runs 1 cell they depend on" });
    expect(more).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("setup")).toBeNull();
    await userEvent.click(more);
    expect(more).toHaveAttribute("aria-expanded", "true");
    const related = screen.getByRole("list", { name: "Also runs 1 cell they depend on" });
    expect(within(related).getByText("setup")).toBeTruthy();
  });

  it("opens the notebook from its name where the host can open files", async () => {
    const onOpenFile = vi.fn();
    renderCard(foldedAsk(), { onOpenFile });
    await userEvent.click(screen.getByRole("button", { name: "analysis.alknb.py" }));
    expect(onOpenFile).toHaveBeenCalledWith("analysis.alknb.py");
  });

  it("names the notebook as plain text where the host cannot open files", () => {
    renderCard(foldedAsk());
    expect(screen.queryByRole("button", { name: "analysis.alknb.py" })).toBeNull();
    expect(screen.getByRole("heading", { level: 2 })).toHaveTextContent("analysis.alknb.py");
  });

  it("is answered from the keyboard: Enter allows, Escape rejects", async () => {
    const decided: string[] = [];
    const { unmount } = render(
      <div className="chat-root">
        <PermissionCard
          {...permissionCardProps(foldedAsk(), undefined, (id) => decided.push(id), () => {})}
        />
      </div>,
    );
    await userEvent.keyboard("{Enter}");
    unmount();
    renderCard(foldedAsk(), { onDecide: (id) => decided.push(id) });
    await userEvent.keyboard("{Escape}");
    expect(decided).toEqual(["allow_once", "reject_once"]);
  });

  it("says what always allow remembers, in plain words, where it is offered", () => {
    const part = foldedAsk([
      { option_id: "allow_once", name: "Allow once" },
      { option_id: "allow_always", name: "Always allow" },
      { option_id: "reject_once", name: "Reject once" },
    ]);
    const container = renderCard({ ...part, subject: { ...part.subject!, confidence: "exact" } });
    expect(container).toHaveTextContent(
      "Always allow skips this question whenever the agent asks to run 2 cells in analysis.alknb.py.",
    );
    expect(container.textContent).not.toContain("exact command");
  });
});
