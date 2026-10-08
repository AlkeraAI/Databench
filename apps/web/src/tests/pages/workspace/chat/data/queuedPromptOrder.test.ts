// A message sent while the agent is still working a turn waits behind it. Live,
// the relay put the new message on screen at the moment it was sent, and the
// rest of the running turn (its closing reply, or all of it when the send came
// early) landed BELOW it, so the second message read as coming before the work
// it did not start; a reload of the same log put it in the right place. The log
// is the chat's own (137fcff5), in which the second message has no prompt row:
// the box took it when the first turn ended, and only its echo is recorded.
import { describe, expect, it } from "vitest";

import type { ConversationTurn } from "@alkera/chat-model";
import log from "@/tests/fixtures/chat-convergence/137fcff5-d931-4a85-9004-d906c829069e/log.json";
import { openAt, referenceView, type LogRow } from "@/pages/workspace/chat/data/convergence";

const rows = log.rows as unknown as LogRow[];
/** The first turn's echo of the second message is at 39; 72 is its turn's end. */
const THROUGH = 72;
const SECOND = "[explore-stances] Run exactly this shell command with your bash tool: sleep 40 && echo done";

/** The tape's order: who speaks, and which parts, in turn order. */
const order = (turns: ConversationTurn[]): string[] =>
  turns.map((turn) => `${turn.author}: ${turn.parts.map((part) => `${part.kind} ${part.id}`).join(", ")}`);

/** A tab open from the start that hears the second send relayed after row `at`. */
async function liveWithSendAfter(at: number): Promise<string[]> {
  const opened = await openAt(rows, 0);
  try {
    await opened.push(rows.filter((row) => row.seq > 0 && row.seq <= at));
    const source = opened.source as unknown as { applyRelays(chatId: string, events: unknown[]): void };
    source.applyRelays("chat", [{ kind: "prompt", text: SECOND, client_id: "web-second-send" }]);
    await opened.push(rows.filter((row) => row.seq > at && row.seq <= THROUGH));
    return order((await opened.view()).turns);
  } finally {
    opened.close();
  }
}

describe("a message sent while a turn runs", () => {
  // 8: before the first reply opens; 15: after its thinking opens; 20: after
  // its command finished; 30: after its closing reply opened.
  it.each([8, 15, 20, 30])("stands below the running turn live, as a reload shows it (sent after row %i)", async (at) => {
    const reference = order((await referenceView(rows, THROUGH)).turns);
    expect(reference.map((line) => line.split(":")[0])).toEqual([
      "user",
      "assistant",
      "assistant",
      "user",
      "assistant",
      "assistant",
    ]);
    expect(await liveWithSendAfter(at)).toEqual(reference);
  });
});
