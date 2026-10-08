// The detector compares a live fold against a fold of the durable log. The
// late-stop chat is the case it got wrong: checked while a text part streamed
// (the log ends at 44, the part's `part.created` is 45), the live tab held the
// part from token frames and the durable fold could not — reported as
// `parts.length` live 2 vs snapshot 1 on the turn being written.

import { describe, expect, it } from "vitest";

import type { ConversationTurn } from "@alkera/chat-model";

import { referenceView, type LogRow } from "@/pages/workspace/chat/data/convergence";
import { checkConvergence } from "@/pages/workspace/chat/data/convergenceDetector";
import type { ChatDataSource } from "@/pages/workspace/chat/data/ChatDataSource";

import late from "@/tests/fixtures/chat-convergence/52f6f441-d4f3-4cf6-a3eb-8b58b6f21fc8/log.json";
import three from "@/tests/fixtures/chat-convergence/ada0e7f2-3ecf-4f98-911a-21caf94591fa/log.json";

const LOG = (late as { rows: LogRow[] }).rows;
const CHAT = "52f6f441-d4f3-4cf6-a3eb-8b58b6f21fc8";
const WRITING = "msg_0c4cace28001LHq4zcC49Rva5V";

/** The REST surface over the rows up to `n`, paged as the portal pages it. */
function restUpTo(n: number) {
  const rows = LOG.filter((row) => row.seq <= n);
  return {
    listMessages: async (_chatId: string, query: { afterSeq?: number } = {}) => {
      const items = rows.filter((row) => row.seq > (query.afterSeq ?? 0)).slice(0, 20);
      return {
        items,
        next_after_seq: items.length ? items[items.length - 1].seq : (query.afterSeq ?? 0),
        resync_from: null,
        prev_before: null,
        has_older: false,
        cut: false,
      };
    },
  };
}

const liveOver = (turns: ConversationTurn[]): ChatDataSource =>
  ({ getChatTurns: async () => turns, turnState: () => "working" }) as unknown as ChatDataSource;

/** What the tab holds mid-stream at 44: the durable fold, plus the text part
 *  the token frames have been filling since its `part.started` at 41. */
async function midStream(): Promise<ConversationTurn[]> {
  const durable = (await referenceView(LOG, 44)).turns;
  return durable.map((turn) =>
    turn.id === WRITING
      ? {
          ...turn,
          parts: [
            ...turn.parts,
            { id: "prt_streaming", kind: "text" as const, text: "Shield volcanoes are", streaming: true },
          ],
        }
      : turn,
  );
}

describe("the convergence check mid-stream", () => {
  it("does not report the part the token frames are still writing", async () => {
    const turns = await midStream();
    expect(turns.find((turn) => turn.id === WRITING)?.parts).toHaveLength(2);
    const report = await checkConvergence(CHAT, liveOver(turns), restUpTo(44) as never);
    expect(report?.n).toBe(44);
    expect(report?.divergences.map((d) => d.path)).toEqual([]);
  });

  it("still reports a settled part the live fold holds and the log does not", async () => {
    const turns = (await midStream()).map((turn) =>
      turn.id === WRITING
        ? { ...turn, parts: turn.parts.map((part) => ("streaming" in part ? { ...part, streaming: false } : part)) }
        : turn,
    );
    const report = await checkConvergence(CHAT, liveOver(turns), restUpTo(44) as never);
    expect(report?.divergences.map((d) => d.path)).toContain(`turn[4:${WRITING}].parts.length`);
  });

  it("agrees once the part has settled into the log", async () => {
    const turns = (await referenceView(LOG, 45)).turns;
    const report = await checkConvergence(CHAT, liveOver(turns), restUpTo(45) as never);
    expect(report?.divergences).toEqual([]);
  });

  it("hands back the rows it folded, so a stored window matches its own n", async () => {
    const report = await checkConvergence(CHAT, liveOver(await midStream()), restUpTo(44) as never);
    expect(report?.log.map((row) => row.seq)).toEqual(LOG.filter((row) => row.seq <= 44).map((row) => row.seq));
  });
});

describe("the convergence check on the three-Stops chat", () => {
  // The detector's other hit: `turn[7:…].parts.length` live 1 vs snapshot 0,
  // stored with 63 rows — the third turn's only part was the prose still
  // streaming (`part.started` 60, `part.created` 64), with the Stop's rows
  // already recorded under it.
  const ROWS = (three as { rows: LogRow[] }).rows;
  // The last message a box opened: the turn the third Stop cut.
  const WRITING_THIRD = ROWS.filter((row) => row.kind === "message.created" && row.role === "assistant")
    .map((row) => (row.payload as { payload: { message_id: string } }).payload.message_id)
    .pop() as string;
  const rest = (n: number) => ({
    listMessages: async (_chatId: string, query: { afterSeq?: number } = {}) => {
      const items = ROWS.filter((row) => row.seq <= n && row.seq > (query.afterSeq ?? 0)).slice(0, 25);
      return {
        items,
        next_after_seq: items.length ? items[items.length - 1].seq : (query.afterSeq ?? 0),
        resync_from: null,
        prev_before: null,
        has_older: false,
        cut: false,
      };
    },
  });

  it("does not report the third turn's prose while it is still being written", async () => {
    const durable = (await referenceView(ROWS, 63)).turns;
    const writing = durable.find((turn) => turn.id === WRITING_THIRD);
    expect(writing?.parts, "the durable fold holds no part for it yet").toEqual([]);
    const turns = durable.map((turn) =>
      turn.id === WRITING_THIRD
        ? { ...turn, parts: [{ id: "prt_prose", kind: "text" as const, text: "The Geology of", streaming: true }] }
        : turn,
    );
    const report = await checkConvergence("ada0e7f2", liveOver(turns), rest(63) as never);
    expect(report?.n).toBe(63);
    expect(report?.divergences.map((d) => d.path)).toEqual([]);
  });

  it("agrees on the whole log, all three notes included", async () => {
    const turns = (await referenceView(ROWS, 68)).turns;
    const report = await checkConvergence("ada0e7f2", liveOver(turns), rest(68) as never);
    expect(report?.divergences).toEqual([]);
    expect(turns.filter((turn) => turn.author === "system")).toHaveLength(3);
  });
});
