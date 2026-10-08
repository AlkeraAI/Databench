// A call whose own `tool.call` frame never reached the tab (dropped over a
// reconnect) is recovered from its update. Live, an update that landed after
// the reply that followed the call put the recovered call at the END of the
// turn, so a "Ran 1 terminal command" digest trailed the final reply, and a
// reload regrouped it above. The rows are the chat's own (137fcff5, its first
// turn): the recovered call now stands where the harness opened it, live as on
// reload.
import { describe, expect, it } from "vitest";

import type { ConversationTurn } from "@alkera/chat-model";
import log from "@/tests/fixtures/chat-convergence/137fcff5-d931-4a85-9004-d906c829069e/log.json";
import { openAt, type LogRow } from "@/pages/workspace/chat/data/convergence";

const rows = (log.rows as unknown as LogRow[]).filter((row) => row.seq <= 35);
const CALL = 18;
const RUNNING = 19;
const COMPLETED = 20;
/** The closing reply's own message: opened at 24, its text at 30, done at 33. */
const REPLY_DONE = 33;

const order = (turns: ConversationTurn[]): string[] =>
  turns.map((turn) => `${turn.author}: ${turn.parts.map((part) => `${part.kind} ${part.id}`).join(", ")}`);

/** A tab open from the start that hears these rows, in this order. */
async function live(heard: LogRow[]): Promise<string[]> {
  const opened = await openAt(heard, 0);
  try {
    await opened.push(heard.filter((row) => row.seq > 0));
    return order((await opened.view()).turns);
  } finally {
    opened.close();
  }
}

const bySeq = (seq: number): LogRow => {
  const row = rows.find((candidate) => candidate.seq === seq);
  if (!row) throw new Error(`no row ${seq}`);
  return row;
};

describe("a call recovered from its update", () => {
  it("stands where the harness opened it when its update lands after the reply", async () => {
    const whole = await live(rows);
    const missed = rows.filter((row) => ![CALL, RUNNING, COMPLETED].includes(row.seq));
    const late = [
      ...missed.filter((row) => row.seq <= REPLY_DONE),
      bySeq(COMPLETED),
      ...missed.filter((row) => row.seq > REPLY_DONE),
    ];
    expect(await live(late)).toEqual(whole);
    // The reply is the last thing on the tape, with no call under it.
    expect(whole.at(-1)).toMatch(/^assistant: text prt_\w+$/);
  });

  it("stands where the harness opened it when its update lands before the reply", async () => {
    const whole = await live(rows);
    expect(await live(rows.filter((row) => row.seq !== CALL && row.seq !== RUNNING))).toEqual(whole);
  });
});
