// No live document type discards what its reader typed and the server has not
// taken. Each is carried over, or offered back where the reader can see it.
//
// Every type in `LIVE_DOC_TYPES` is driven through the handle the app really
// opens (a live draft, a live file, a live notebook), against the stand-in
// server holding real Loro documents, with the tab's own session store and the
// window's own `pagehide`. Four ways the edits could be lost:
//
// * the page reloads with edits pending (a file and a notebook kept no stash);
// * the last view lets go and the linger runs out before the server is back;
// * the history restarts between two pages (the stash was dropped unseen);
// * an edit is refused, or the document stops being served, while no view
//   listens for the offer.
//
// A type added to `LIVE_DOC_TYPES` without a row here fails the first test.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { LiveNotice } from "@/api/realtime/crdt/channel";
import { LIVE_DOC_TYPES, type LiveDocType } from "@/api/realtime/crdt/docTypes";
import { LIVE_DRAFT_LINGER_MS, acquireLiveDraft, closeAllLiveDrafts } from "@/api/realtime/crdt/liveDraft";
import { LIVE_FILE_LINGER_MS, acquireLiveFile, closeAllLiveFiles } from "@/api/realtime/crdt/liveFile";
import { NotebookDocument } from "@/api/realtime/crdt/notebookDoc";
import {
  LIVE_NOTEBOOK_LINGER_MS,
  acquireLiveNotebook,
  closeAllLiveNotebooks,
} from "@/pages/workspace/chat/workspace/notebook/liveNotebook";

import { LiveServer, loroNode, type FakeSocket } from "./liveServer";

const ID = "7a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d";
const ACCOUNT = { userId: "usr_dana", orgId: "org_a" };
const loadLoro = () => Promise.resolve(loroNode);

/** One view's hold on a live document, in the words every type shares. */
interface Held {
  /** The handle itself: the same object for as long as the registry keeps it. */
  handle: object;
  isLive(): boolean;
  /** Type `words` at the end of the document, as its editor would. */
  type(words: string): void;
  /** What the reader sees. */
  shown(): string;
  /** Start showing offers of text; returns what has been offered so far. */
  watchOffers(): LiveNotice[];
  /** What a view that subscribes now is handed of a document that stopped
   *  being served: the text typed here the server never took. */
  fallbackOffer(): string | null;
  release(): void;
}

interface TypeUnderTest {
  lingerMs: number;
  /** Put the document the server starts with in place. */
  seed(server: LiveServer, channel: string): void;
  hold(socket: FakeSocket): Held;
  /** The page is gone: everything it held in memory with it. */
  closeAll(): void;
  /** What the server holds, in the words `shown` uses. */
  served(server: LiveServer, channel: string): string;
  /** What the next page does with a stash from a replaced epoch. */
  acrossEpochs: "carried" | "offered";
}

function notebookSources(doc: InstanceType<typeof loroNode.LoroDoc>): string {
  const reader = new NotebookDocument({
    loro: loroNode,
    source: { doc, canWrite: false, listen: () => () => {}, localCommitted: () => {} },
  });
  const sources = reader.snapshot().cells.map((cell) => cell.source);
  reader.dispose();
  return sources.join("\n");
}

const TYPES: Record<LiveDocType, TypeUnderTest> = {
  chat_draft: {
    lingerMs: LIVE_DRAFT_LINGER_MS,
    seed: (server, channel) => server.seed(channel, "base"),
    served: (server, channel) => server.text(channel),
    closeAll: closeAllLiveDrafts,
    acrossEpochs: "carried",
    hold(socket) {
      const { draft, release } = acquireLiveDraft(ID, { socket, loadLoro, hueOf: () => 0, account: ACCOUNT });
      const binding = () => {
        if (draft.state.kind !== "live") throw new Error("the draft is not live");
        return draft.state.binding;
      };
      return {
        handle: draft,
        isLive: () => draft.state.kind === "live",
        type: (words) => binding().edit(binding().text(), binding().text() + words, null),
        shown: () => binding().text(),
        watchOffers: () => {
          const offers: LiveNotice[] = [];
          binding().subscribeNotice((notice) => {
            if (notice !== null) offers.push(notice);
          });
          return offers;
        },
        fallbackOffer: () => {
          let offer: string | null = null;
          draft.subscribe((state) => {
            if (state.kind === "fallback") offer = state.fallback.unacknowledged;
          })();
          return offer;
        },
        release,
      };
    },
  },
  file: {
    lingerMs: LIVE_FILE_LINGER_MS,
    seed: (server, channel) => server.seed(channel, "base"),
    served: (server, channel) => server.text(channel),
    closeAll: closeAllLiveFiles,
    acrossEpochs: "carried",
    hold(socket) {
      const { file, release } = acquireLiveFile(ID, { socket, loadLoro, account: ACCOUNT });
      const text = () => {
        const doc = file.channel.doc;
        if (file.state.kind !== "live" || doc === null) throw new Error("the file is not live");
        return doc.getText("content");
      };
      return {
        handle: file,
        isLive: () => file.state.kind === "live",
        type: (words) => {
          text().insert(text().length, words);
          file.channel.doc!.commit({ origin: "local" });
          file.channel.localCommitted();
        },
        shown: () => text().toString(),
        watchOffers: () => {
          const offers: LiveNotice[] = [];
          file.channel.listen({
            notice: (notice) => {
              if (notice !== null) offers.push(notice);
            },
          });
          return offers;
        },
        fallbackOffer: () => {
          let offer: string | null = null;
          file.subscribe((state) => {
            if (state.kind === "fallback") offer = state.unacknowledged;
          })();
          return offer;
        },
        release,
      };
    },
  },
  notebook: {
    lingerMs: LIVE_NOTEBOOK_LINGER_MS,
    seed: (server, channel) => {
      const { doc } = server.docOf(channel);
      new NotebookDocument({
        loro: loroNode,
        source: { doc, canWrite: true, listen: () => () => {}, localCommitted: () => {} },
        newId: () => "basecell00",
      }).apply([{ op: "insert", source: "base" }]);
    },
    served: (server, channel) => notebookSources(server.docOf(channel).doc as InstanceType<typeof loroNode.LoroDoc>),
    closeAll: closeAllLiveNotebooks,
    acrossEpochs: "offered",
    hold(socket) {
      const { notebook, release } = acquireLiveNotebook(ID, { socket, loadLoro, account: ACCOUNT });
      const doc = () => {
        if (notebook.state.kind !== "live") throw new Error("the notebook is not live");
        return notebook.state.doc;
      };
      return {
        handle: notebook,
        isLive: () => notebook.state.kind === "live",
        type: (words) => void doc().apply([{ op: "insert", kind: "sql", source: words.trim() }]),
        shown: () =>
          doc()
            .snapshot()
            .cells.map((cell) => cell.source)
            .join("\n"),
        watchOffers: () => {
          const offers: LiveNotice[] = [];
          notebook.channel.listen({
            notice: (notice) => {
              if (notice !== null) offers.push(notice);
            },
          });
          return offers;
        },
        fallbackOffer: () => {
          let offer: string | null = null;
          notebook.subscribe((state) => {
            if (state.kind === "fallback") offer = state.unacknowledged;
          })();
          return offer;
        },
        release,
      };
    },
  },
};

const channelOf = (docType: LiveDocType): string => `doc:${docType}:${ID}`;

/** How many times `token` appears in `text`. */
const count = (text: string, token: string): number => text.split(token).length - 1;

/** One turn of the loop: a millisecond of the page's clock, and the promise
 *  chains (Loro, the handles' awaits) that were waiting on it. */
async function turn(): Promise<void> {
  await vi.advanceTimersByTimeAsync(1);
  await new Promise<void>((resolve) => setImmediate(resolve));
}

/** Deliver the socket's frames until `until` holds (or, without one, until
 *  nothing has moved for a few turns). */
async function pump(socket: FakeSocket, until?: () => boolean): Promise<void> {
  let quiet = 0;
  for (let i = 0; i < 2000; i += 1) {
    await turn();
    if (socket.inbox.length > 0) {
      quiet = 0;
      let delivered = false;
      void socket.deliver(1).then(() => (delivered = true));
      while (!delivered) await turn();
      continue;
    }
    if (until ? until() : (quiet += 1) > 5) return;
  }
  if (until) throw new Error("the document never got there");
}

/** Restart the history: a new epoch holding what the server had. */
function restartHistory(server: LiveServer, channel: string): void {
  const old = server.docOf(channel);
  const restarted = new loroNode.LoroDoc();
  restarted.setPeerId(2n);
  restarted.import(old.doc.export({ mode: "snapshot" }));
  server.docs.set(channel, { doc: restarted as typeof old.doc, epoch: old.epoch + 1 });
}

beforeEach(() => {
  // The page's clock (the linger, the channel's waits) is the test's to move;
  // `setImmediate` stays real, so promise chains still run.
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
  window.sessionStorage.clear();
});

afterEach(() => {
  for (const type of Object.values(TYPES)) type.closeAll();
  window.sessionStorage.clear();
  vi.useRealTimers();
});

describe("what a reader typed and the server has not taken", () => {
  it("is covered for every live document type the build holds", () => {
    expect(Object.keys(TYPES).sort()).toEqual(Object.keys(LIVE_DOC_TYPES).sort());
  });

  describe.each(Object.keys(LIVE_DOC_TYPES) as LiveDocType[])("in a %s", (docType) => {
    const type = TYPES[docType];
    const channel = channelOf(docType);

    /** A page that typed `words` while the server was away, then went away
     *  itself. Returns the server it left behind. */
    async function pageThatLeftWith(words: string): Promise<LiveServer> {
      const server = new LiveServer();
      type.seed(server, channel);
      const socket = server.socket("first");
      const held = type.hold(socket);
      await pump(socket, held.isLive);
      socket.disconnect();
      held.type(words);
      expect(count(type.served(server, channel), words.trim())).toBe(0);
      window.dispatchEvent(new Event("pagehide"));
      type.closeAll();
      return server;
    }

    it("survives a page reload: the next page lands it exactly once", async () => {
      const server = await pageThatLeftWith(" tok00 tok01 tok02");
      const socket = server.socket("second");
      const held = type.hold(socket);
      await pump(socket, () => held.isLive() && count(type.served(server, channel), "tok00") > 0);
      await pump(socket);
      for (const token of ["tok00", "tok01", "tok02"]) {
        expect(count(type.served(server, channel), token)).toBe(1);
        expect(count(held.shown(), token)).toBe(1);
      }
      expect(count(held.shown(), "base")).toBe(1);
      // Answered for: a third page has nothing left to land a second time.
      window.dispatchEvent(new Event("pagehide"));
      type.closeAll();
      const third = server.socket("third");
      const again = type.hold(third);
      await pump(third, again.isLive);
      await pump(third);
      expect(count(type.served(server, channel), "tok00")).toBe(1);
      expect(count(again.shown(), "tok00")).toBe(1);
    });

    it("outlives its last view and the linger while the server is away, and is let go once it has landed", async () => {
      const server = new LiveServer();
      type.seed(server, channel);
      const socket = server.socket("ana");
      const held = type.hold(socket);
      await pump(socket, held.isLive);
      socket.disconnect();
      held.type(" kept");
      held.release();
      await vi.advanceTimersByTimeAsync(type.lingerMs * 3);
      expect(count(type.served(server, channel), "kept")).toBe(0);
      socket.reconnect();
      await pump(socket, () => count(type.served(server, channel), "kept") === 1);
      // A view that comes back meanwhile is handed the same document.
      const back = type.hold(socket);
      expect(back.handle).toBe(held.handle);
      expect(count(back.shown(), "kept")).toBe(1);
      back.release();
      // Nothing left to lose: the linger now lets it go.
      await pump(socket);
      await vi.advanceTimersByTimeAsync(type.lingerMs + 1);
      const fresh = type.hold(socket);
      expect(fresh.handle).not.toBe(held.handle);
    });

    it(`is ${type.acrossEpochs} when the history restarted between two pages`, async () => {
      const server = await pageThatLeftWith(" tok00 tok01");
      restartHistory(server, channel);
      const socket = server.socket("second");
      const held = type.hold(socket);
      await pump(socket, held.isLive);
      const offers = held.watchOffers();
      await pump(socket);
      if (type.acrossEpochs === "carried") {
        for (const token of ["tok00", "tok01"]) {
          expect(count(type.served(server, channel), token)).toBe(1);
          expect(count(held.shown(), token)).toBe(1);
        }
        expect(offers).toEqual([]);
      } else {
        expect(offers).toEqual([{ message: "Your latest edits could not be kept.", restorable: "tok00 tok01" }]);
      }
      expect(count(held.shown(), "base")).toBe(1);
    });

    it("is offered back to the next view when an edit is refused while nobody listens", async () => {
      const server = new LiveServer();
      type.seed(server, channel);
      const socket = server.socket("ana");
      const held = type.hold(socket);
      await pump(socket, held.isLive);
      server.failNext = { code: "crdt_rejected" };
      held.type(" refused");
      await pump(socket);
      expect(count(type.served(server, channel), "refused")).toBe(0);
      // The view let go, and the linger ran out, before anybody looked.
      held.release();
      await vi.advanceTimersByTimeAsync(type.lingerMs * 2);
      const back = type.hold(socket);
      expect(back.handle).toBe(held.handle);
      await pump(socket, back.isLive);
      const offers = back.watchOffers();
      expect(offers).toHaveLength(1);
      expect(offers[0]?.restorable?.trim()).toBe("refused");
    });

    it("is handed to the next view when the document stops being served while nobody shows it", async () => {
      const server = new LiveServer();
      type.seed(server, channel);
      const socket = server.socket("ana");
      const held = type.hold(socket);
      await pump(socket, held.isLive);
      server.muteAcks = true;
      held.type(" pending");
      await pump(socket);
      held.release();
      server.unsupport(channel);
      await pump(socket);
      await vi.advanceTimersByTimeAsync(type.lingerMs * 2);
      // Spent and unheld, but not replaced: it still owes somebody the text.
      const back = type.hold(socket);
      expect(back.handle).toBe(held.handle);
      expect(back.fallbackOffer()?.trim()).toBe("pending");
      back.release();
      // Handed over: the next hold opens the document afresh.
      const fresh = type.hold(socket);
      expect(fresh.handle).not.toBe(held.handle);
    });
  });
});
