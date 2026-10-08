// Reaching a compaction's summary from its own URL.
//
// `/chat/<id>/compaction/<eventId>` answered "Compaction not found." on the
// real box. That page is a COLD read: nothing mounts the chat surface, so no
// socket opens and the only source of the transcript is the durable REST page.
// The two things it depends on are pinned here — the REST replay folds the
// compaction under the id the event was published with, and the page resolves
// that id instead of reporting the summary missing.

import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";
import { CloudDataSource } from "@/pages/workspace/chat/data/CloudDataSource";

const NOW = "2026-09-19T20:32:04Z";

/** The durable event id the machine published the summary under — the id the
 *  transcript's card links to, and the one in the URL. */
const APPLIED_ID = "27fb2c9a355b535954bf";

const SUMMARY = "## Goal\nPELICAN-7741 holds 8312 pallets on lane ALPHA-9.";

/** A REST row as the server serves it: docsync persists the whole published
 *  ENVELOPE as the row's payload, so the harness event is one level in. */
function row(seq: number, payload: Record<string, unknown>) {
  const eventId = String(payload.event_id ?? `e-${seq}`);
  return {
    id: `m-${seq}`,
    chat_id: "c1",
    seq,
    role: "assistant" as const,
    kind: String(payload.event_type),
    event_id: eventId,
    payload: { event_id: eventId, role: "assistant", kind: String(payload.event_type), payload },
    created_at: NOW,
  };
}

const PAGE = {
  items: [
    row(1, { event_type: "message.created", event_id: "e-1", message_id: "a1", role: "assistant" }),
    row(2, {
      event_type: "part.created",
      event_id: "e-2",
      message_id: "a1",
      part: { part_id: "a1-t", message_id: "a1", type: "text", text: "Rows emitted." },
    }),
    row(3, {
      event_type: "compaction.applied",
      event_id: APPLIED_ID,
      summarised_message_ids: ["a1"],
      summary_text: SUMMARY,
    }),
  ],
  next_after_seq: 3,
  resync_from: null,
};

function coldSource() {
  const pages = [PAGE];
  const listMessages = vi.fn(async () => pages.shift() ?? { items: [], next_after_seq: 3, resync_from: null });
  const rest = {
    listMessages,
    getChat: vi.fn(async () => ({
      id: "c1",
      title: "Chat",
      machine_id: null,
      machine_status: "none" as const,
      created_at: NOW,
      updated_at: NOW,
      last_seq: 3,
      model: null,
      permission_mode: "read_only" as const,
    })),
  };
  const source = new CloudDataSource({
    rest: rest as never,
    // The detail page never opens the document — that is what makes it a cold
    // read, and what the "not found" depended on.
    openDoc: () => {
      throw new Error("the compaction detail page must not need the live document");
    },
    acquire: () => () => undefined,
    clientId: () => "client-1",
  });
  return { source, listMessages };
}

describe("a compaction read cold, from its own URL", () => {
  it("folds the summary under the id it was published with", async () => {
    const { source, listMessages } = coldSource();

    const turns = await source.getChatTurns("c1");

    expect(listMessages).toHaveBeenCalled();
    const parts = turns.flatMap((turn) => turn.parts);
    const compaction = parts.find((part) => part.kind === "compaction");
    expect(compaction?.id).toBe(APPLIED_ID);
    expect(compaction?.kind === "compaction" && compaction.text).toBe(SUMMARY);
  });

  it("shows the summary on the page the transcript links to", async () => {
    const { source } = coldSource();
    vi.doMock("@/pages/workspace/chat/data", async (importOriginal) => ({
      ...(await importOriginal<typeof import("@/pages/workspace/chat/data")>()),
      chatData: () => source,
    }));
    const { CompactionSurface } = await import("@/pages/workspace/chat/CompactionSurface");

    render(
      <QueryClientProvider client={createQueryClient()}>
        <MemoryRouter initialEntries={[`/chat/c1/compaction/${APPLIED_ID}`]}>
          <Routes>
            <Route path="/chat/:chatId/compaction/:partId" element={<CompactionSurface />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByText(/8312 pallets/)).toBeInTheDocument();
    expect(screen.queryByText("Compaction not found.")).not.toBeInTheDocument();
    vi.doUnmock("@/pages/workspace/chat/data");
  });
});
