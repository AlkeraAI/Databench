// A link into a turn the window does not hold still opens it.
//
// A chat now opens on its newest page, so
// every surface that FINDS something in the transcript by id — the compaction a
// card links to, the plan a question card points at, the results page listing
// everything a chat produced — would answer "not found" for anything above the
// tail if it read only the loaded window. Each of them pages up until it holds
// what it is after.
//
// The counter-case is in the same file: the same link against a source that
// serves no older pages finds nothing, which is what makes the passing half
// evidence of the paging and not of the fixture.

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ConversationTurn } from "@alkera/chat-model";
import { createQueryClient } from "@/api/queryClient";
import { BlobsSurface } from "@/pages/workspace/chat/BlobsSurface";
import { CompactionSurface } from "@/pages/workspace/chat/CompactionSurface";
import type { ChatDataSource } from "@/pages/workspace/chat/data/ChatDataSource";
import {
  createBrowserChatHost,
  installChatRuntime,
  resetChatRuntime,
} from "@/pages/workspace/chat/data";

const CHAT = "c1";
const OLD_PART = "compaction-week-one";

/** Three pages of transcript, newest last. The compaction the link names is on
 *  the OLDEST one, two pages above where the chat opens. */
function pageOfTurns(index: number): ConversationTurn[] {
  const parts =
    index === 0
      ? [
          {
            kind: "compaction" as const,
            id: OLD_PART,
            title: "Week one",
            text: "summarised the first week",
          },
          {
            kind: "tool" as const,
            id: "tool-old",
            name: "sql.query",
            status: "completed",
            references: [{ handle: "blob-old", name: "week-one.csv", refType: "table" }],
          },
        ]
      : [{ kind: "text" as const, id: `t${index}`, text: `turn ${index}` }];
  return [
    {
      id: `turn-${index}`,
      author: index % 2 === 0 ? "user" : "assistant",
      parts,
    } as unknown as ConversationTurn,
  ];
}

const PAGES = [pageOfTurns(0), pageOfTurns(1), pageOfTurns(2)];

interface Fake {
  source: ChatDataSource;
  reads: () => number;
}

/** A source that opens on the newest page and serves the ones above it on
 *  demand, exactly as the two real sources do. `pages` of 1 is a source that
 *  serves whole transcripts: nothing is ever older. */
function pagingSource(pages: number): Fake {
  let loaded = 1;
  let reads = 0;
  const source = {
    caps: { opencodeActive: false, modelCatalog: false },
    getChatTurns: async () => PAGES.slice(PAGES.length - loaded).flat(),
    transcriptHistory: () => ({ hasOlder: loaded < pages, loading: false }),
    loadOlderTurns: async () => {
      reads += 1;
      if (loaded < pages) loaded += 1;
    },
    releaseOlderTurns: () => undefined,
    subscribeChat: () => () => undefined,
    listChats: async () => [{ id: CHAT, title: "Long chat", updatedAt: "2026-09-19T12:00:00Z" }],
    fetchBlob: async () => ({ kind: "text", text: "", columns: [], rows: [], total: 0 }),
    getChatActivity: () => ({}),
    getSubagentChats: () => [],
    getSubagentLabels: () => ({}),
  };
  return { source: source as unknown as ChatDataSource, reads: () => reads };
}

function mount(element: React.ReactElement, path: string, at: string) {
  const qc = createQueryClient({ retry: false });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[at]}>
        <Routes>
          <Route path={path} element={element} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  resetChatRuntime();
  vi.unstubAllGlobals();
});

beforeEach(() => {
  vi.stubGlobal("IntersectionObserver", undefined);
});

describe("a compaction linked from an old turn", () => {
  it("is found by paging up to it", async () => {
    const fake = pagingSource(PAGES.length);
    installChatRuntime({ source: fake.source, host: createBrowserChatHost() });
    mount(
      <CompactionSurface />,
      "/chat/:chatId/compaction/:partId",
      `/chat/${CHAT}/compaction/${OLD_PART}`,
    );
    expect(await screen.findByText("summarised the first week")).toBeTruthy();
    // Two pages above the one the chat opened on, and not one read more: the
    // walk stops the moment the card is loaded.
    expect(fake.reads()).toBe(2);
  });

  it("is not found where the source serves no older pages", async () => {
    const fake = pagingSource(1);
    installChatRuntime({ source: fake.source, host: createBrowserChatHost() });
    mount(
      <CompactionSurface />,
      "/chat/:chatId/compaction/:partId",
      `/chat/${CHAT}/compaction/${OLD_PART}`,
    );
    expect(await screen.findByText("Compaction not found.")).toBeTruthy();
    expect(fake.reads()).toBe(0);
  });

  it("says it is still loading rather than not found while it pages", async () => {
    const fake = pagingSource(PAGES.length);
    installChatRuntime({ source: fake.source, host: createBrowserChatHost() });
    mount(
      <CompactionSurface />,
      "/chat/:chatId/compaction/:partId",
      `/chat/${CHAT}/compaction/${OLD_PART}`,
    );
    // Never "not found" on the way: the reader would take that as the answer.
    await waitFor(() => expect(screen.queryByRole("status")).toBeTruthy());
    expect(screen.queryByText("Compaction not found.")).toBeNull();
    expect(await screen.findByText("summarised the first week")).toBeTruthy();
  });
});

describe("the results page over a windowed transcript", () => {
  it("lists a result from a turn above the window", async () => {
    const fake = pagingSource(PAGES.length);
    installChatRuntime({ source: fake.source, host: createBrowserChatHost() });
    mount(<BlobsSurface />, "/chat/:chatId/results", `/chat/${CHAT}/results`);
    expect(await screen.findByText("week-one.csv")).toBeTruthy();
    // Exhaustive: this page has no other way to reach an old result.
    expect(fake.reads()).toBe(2);
  });
});
