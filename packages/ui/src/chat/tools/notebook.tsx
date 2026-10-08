// The agent's notebook calls, one quiet line each. The notebook itself shows
// every cell, output and change live, so the transcript only says what the
// agent did and to which notebook: no body, nothing to expand. A failed call
// still shows its reason through the shared failed-state path.
import type { ToolConversationPart } from "@alkera/chat-model";

import { count, readResult, records, str } from "./alkeraPayload";
import { goToCell } from "./notebookCell";
import type { CardHead, StepAction, StepEnvironment } from "./step";

/** The notebook's file name, without the folders above it. */
function fileName(part: ToolConversationPart): string {
  const result = readResult(part.output);
  const path = str(result?.path) || str(part.input?.path);
  const leaf = path.split("/").pop() ?? path;
  return leaf || "notebook";
}

function cellCount(value: unknown): number {
  return Array.isArray(value) ? value.length : 0;
}

type Line = { verb: string; text?: string };

const KERNEL_VERB: Record<string, string> = {
  interrupt: "Interrupted the kernel of",
  restart: "Restarted the kernel of",
  shutdown: "Shut down the kernel of",
};

/** What one call did, in a few words. */
function line(name: string, part: ToolConversationPart): Line {
  const input = part.input ?? {};
  const result = readResult(part.output) ?? {};
  switch (name) {
    case "notebook.create": {
      const cells = cellCount(input.cells);
      return { verb: "Created", text: cells > 0 ? count(cells, "cell", "cells") : undefined };
    }
    case "notebook.edit": {
      const ops = cellCount(input.ops);
      return { verb: "Edited", text: ops > 0 ? count(ops, "change", "changes") : undefined };
    }
    case "notebook.run": {
      const planned = cellCount(result.plan) || cellCount(result.cells);
      const status = str(result.status);
      const parts = [planned > 0 ? count(planned, "cell", "cells") : "", status].filter(Boolean);
      return { verb: "Ran", text: parts.join(" · ") || undefined };
    }
    case "notebook.read":
      return { verb: "Read", text: cellCount(result.cells) > 0 ? count(cellCount(result.cells), "cell", "cells") : undefined };
    case "notebook.output":
      return { verb: "Read output of", text: str(result.cell_id) || str(input.cell) || undefined };
    case "notebook.inspect":
      return { verb: "Inspected", text: str(input.name) || str(input.what) || undefined };
    case "notebook.graph":
      return { verb: "Traced cells in" };
    case "notebook.kernel":
      return { verb: KERNEL_VERB[str(input.action)] ?? "Checked the kernel of" };
    case "notebook.env": {
      const action = str(input.action);
      const packages = records(input.packages).length || cellCount(input.packages);
      return { verb: action === "install" ? "Installed packages in" : "Checked the environment of", text: packages > 0 ? count(packages, "package", "packages") : undefined };
    }
    case "notebook.settings":
      return { verb: "Changed settings of" };
    case "notebook.widget":
      return { verb: "Used a widget in" };
    default:
      return { verb: "Used" };
  }
}

/** The one cell a call read, when it read exactly one: an output, or a read
 *  that named a single cell. */
function oneCell(name: string, part: ToolConversationPart, env?: StepEnvironment): StepAction | undefined {
  if (part.state === "error") return undefined;
  const result = readResult(part.output) ?? {};
  const path = str(result.path);
  if (name === "notebook.output") return goToCell(env, path, str(result.cell_id), str(part.input?.cell));
  if (name !== "notebook.read") return undefined;
  const cells = records(result.cells);
  if (cells.length !== 1 || cellCount(part.input?.cells) !== 1) return undefined;
  return goToCell(env, path, str(cells[0].id), str(cells[0].name));
}

/** One head for every notebook tool: a verb, the notebook, a short count. */
export const head: CardHead = (part, env) => {
  const { verb, text } = line(part.name, part);
  return {
    verb,
    object: fileName(part),
    data: text ? { kind: "count", text } : undefined,
    expanded: false,
    body: undefined,
    action: oneCell(part.name, part, env),
  };
};
