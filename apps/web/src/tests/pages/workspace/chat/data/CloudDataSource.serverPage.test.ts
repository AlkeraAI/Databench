// A cold open of a chat the machine already answered, fed the page the SERVER
// actually serves.
//
// Every other test of this source builds its REST rows by hand, with the
// harness event flat in `payload`. The server does not store it that way: the
// machine publishes an envelope `{event_id, role, kind, payload}` and docsync
// persists that envelope as the row, so `GET /chats/{id}/messages` hands back
// `payload.payload.event_type`, not `payload.event_type`. The fixture read here
// is that page, recorded from a real run (browser → REST → gateway → real
// mirror → docsync → REST) by
// `apps/cli/tests/cloud/test_cloud_round3_seams.py`, which also pins its shape.
//
// The contract: a reader who reloads the chat sees the machine's answer.

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it, vi } from "vitest";

import type { DocHandle } from "@/api/realtime/docSync";
import { CloudDataSource } from "@/pages/workspace/chat/data/CloudDataSource";

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../../../../../../../..");
const PAGE = JSON.parse(
  readFileSync(
    resolve(REPO_ROOT, "packages/api-core/tests/fixtures/objects/seam/chat_messages_page_from_server.json"),
    "utf-8",
  ),
) as { items: { chat_id: string; role: string; kind: string; payload: Record<string, unknown> }[]; next_after_seq: number; resync_from: number | null };

const CHAT_ID = PAGE.items[0]?.chat_id ?? "";

function idleDoc(): DocHandle<never> {
  return {
    onMessage: () => () => undefined,
    onPhase: () => () => undefined,
    getPhase: () => ({ phase: "live", epoch: 1, seq: 0, peerId: "p:1", canWrite: false, pending: 0, error: null }),
    sendOp: () => Promise.reject(new Error("a reader never writes to a chat document")),
    dispose: () => undefined,
  } as unknown as DocHandle<never>;
}

function sourceOverTheServerPage(): CloudDataSource {
  const listMessages = vi
    .fn()
    .mockResolvedValueOnce(PAGE)
    .mockResolvedValue({ items: [], next_after_seq: PAGE.next_after_seq, resync_from: null });
  return new CloudDataSource({
    rest: { listMessages } as never,
    openDoc: () => idleDoc(),
    acquire: () => () => undefined,
    clientId: () => "client-1",
  });
}

describe("reopening a chat over the page the server serves", () => {
  it("the recording is a real answered chat: a user prompt and the machine's rows", () => {
    expect(CHAT_ID).not.toBe("");
    expect(PAGE.items[0]?.role).toBe("user");
    expect(PAGE.items.map((item) => item.kind)).toContain("message.completed");
    expect(JSON.stringify(PAGE)).toContain("Answer: 42");
  });

  it("shows the machine's answer after a reload", async () => {
    const turns = await sourceOverTheServerPage().getChatTurns(CHAT_ID);
    const text = JSON.stringify(turns);
    expect(turns.length, "no turn was folded from the served page").toBeGreaterThan(0);
    expect(text).toContain("Answer: 42");
  });
});
