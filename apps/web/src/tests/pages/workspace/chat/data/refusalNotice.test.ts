// The refusal a cloud chat writes, rendered as the notice a reader sees.
//
// The wording lives in Python (`alkera_cli/cloud/refusal.py`) and the notice
// lives here (`harnessEventFold`'s system part), and the only thing that ever
// crossed between them was the live browser suite — a hard assertion on the
// real stack, against a real model, run on `main` and never on a pull request.
// So the three entries the mirror actually publishes for a refusal are recorded
// by `apps/cli/tests/cloud/test_cloud_round3_seams.py` and folded here through
// the real fold: rename the sentence, drop the `synthetic` flag, or stop
// authoring the note on a `system` message, and one of the two sides goes red.
//
// The contract: the reader is told, in the workspace's own words, that it will
// not run the statement — and the statement is quoted as inert TEXT, never as
// markup and never as a link.

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import {
  createConversationFoldState,
  foldHarnessEvent,
  type HarnessEvent,
} from "@/pages/workspace/chat/data/harnessEventFold";

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../../../../../../../..");
const read = (path: string): string => readFileSync(resolve(REPO_ROOT, path), "utf-8");

/** The published transcript entries, exactly as the machine wrote them. */
const ENTRIES = JSON.parse(
  read("packages/api-core/tests/fixtures/objects/seam/refusal_note_entries.json"),
) as { kind: string; role: string; payload: HarnessEvent }[];

/** The sentence, read off the module that owns it rather than copied here. */
function refusalCopy(reason: string): string {
  const source = read("apps/cli/alkera_cli/cloud/refusal.py");
  const match = new RegExp(`"${reason}":\\s*\\(?\\s*"((?:[^"\\\\]|\\\\.)*)"`).exec(source);
  if (!match) throw new Error(`no REFUSAL_COPY entry named ${reason}`);
  return match[1].replace(/\\"/g, '"').replace(/\\n/g, "\n");
}

function foldedNotice() {
  const state = createConversationFoldState();
  for (const entry of ENTRIES) foldHarnessEvent(state, entry.payload);
  const parts = state.turns.flatMap((turn) => turn.parts);
  return { state, parts };
}

describe("the refusal the machine wrote", () => {
  it("is three entries on a system message, the middle one synthetic", () => {
    expect(ENTRIES.map((entry) => entry.kind)).toEqual([
      "message.created",
      "part.created",
      "message.completed",
    ]);
    expect(ENTRIES.every((entry) => entry.role === "system")).toBe(true);
    const part = ENTRIES[1].payload.part as Record<string, unknown>;
    expect(part.synthetic).toBe(true);
  });

  it("renders as a notice, not as a suppressed synthetic part", () => {
    const { state, parts } = foldedNotice();
    expect(state.turns.map((turn) => turn.author)).toEqual(["system"]);
    expect(parts).toHaveLength(1);
    expect(parts[0].kind).toBe("system");
  });

  it("says the sentence refusal.py owns, with the statement quoted under it", () => {
    const { parts } = foldedNotice();
    const notice = parts[0] as { kind: string; text: string; detail?: string };
    expect(notice.text).toBe(refusalCopy("read_only_workspace"));
    expect(notice.detail).toContain("delete from prompts");
    // The quoted statement is data: it never becomes markup or a link.
    expect(notice.detail).toMatch(/^"[^"]*"$/);
  });
});
