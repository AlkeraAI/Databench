// The shared draft on the live lane, and the field it leaves the reader with
// when the lane cannot run.
//
// While the live draft loads the field is the tab's own and its typing is
// carried onto the live draft when it arrives. A browser that cannot load Loro
// keeps the field to the tab and says so; a server that cannot serve the lane
// says so too and is asked again after a doubling wait; a chat the reader may
// not see has no shared draft and says nothing. Driven through the real hook
// over a source whose live draft the test moves from state to state, and once
// through the real cloud source over a fake document to pin that no state of
// the field writes the draft onto the document's op log.

import { act, cleanup, fireEvent, render, renderHook, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  Composer,
  type BindingNotice,
  type RemoteCaret,
  type TextBinding,
  type TextSelection,
  type TextView,
} from "@alkera/ui";

import type { LiveDraftState } from "@/api/realtime/crdt/liveDraft";
import type { DocHandle, OpInput } from "@/api/realtime/docSync";
import { CloudDataSource } from "@/pages/workspace/chat/data/CloudDataSource";

interface FakeSource {
  openLiveDraft?: () => { draft: unknown; release: () => void };
}

const sources = { current: null as FakeSource | CloudDataSource | null };
vi.mock("@/pages/workspace/chat/data", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/pages/workspace/chat/data")>()),
  chatData: () => sources.current as never,
}));

const { useSharedDraft, REJOIN_FIRST_MS, REJOIN_MAX_MS, LIVE_SYNC_UNSUPPORTED, LIVE_SYNC_UNAVAILABLE } = await import(
  "@/pages/workspace/chat/useSharedDraft"
);

class FakeLive {
  private listener: ((s: LiveDraftState) => void) | null = null;
  released = 0;
  subscribe(listener: (s: LiveDraftState) => void): () => void {
    this.listener = listener;
    listener({ kind: "pending" });
    return () => {
      this.listener = null;
    };
  }
  move(state: LiveDraftState): void {
    act(() => this.listener?.(state));
  }
  fallBack(reason: string, localText = ""): void {
    this.move({ kind: "fallback", fallback: { reason, localText, ackedText: localText, unacknowledged: "" } });
  }
}

class FakeBinding implements TextBinding {
  readonly maxBytes = 0;
  edits: { before: string; after: string }[] = [];
  constructor(public current: string) {}
  text(): string {
    return this.current;
  }
  attach(view: TextView): () => void {
    void view;
    return () => undefined;
  }
  edit(before: string, after: string, selection: TextSelection | null): void {
    void selection;
    this.edits.push({ before, after });
    this.current = after;
  }
  select(): void {}
  compositionEnded(): void {}
  undo(): boolean {
    return false;
  }
  redo(): boolean {
    return false;
  }
  subscribeCarets(listener: (carets: RemoteCaret[]) => void): () => void {
    listener([]);
    return () => undefined;
  }
  subscribeNotice(listener: (notice: BindingNotice | null) => void): () => void {
    listener(null);
    return () => undefined;
  }
}

const goLive = (live: FakeLive, binding: FakeBinding): void => live.move({ kind: "live", binding: binding as never });

/** A source whose live drafts are handed out in order, one per open. */
function source(...lives: FakeLive[]): { opened: () => number } {
  let opened = 0;
  sources.current = {
    openLiveDraft: () => {
      const live = lives[Math.min(opened, lives.length - 1)]!;
      opened += 1;
      return { draft: live, release: () => void (live.released += 1) };
    },
  };
  return { opened: () => opened };
}

/** Run the clock forward under React. */
const advance = (ms: number): Promise<void> =>
  act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  sources.current = null;
});

describe("useSharedDraft while the live draft loads", () => {
  it("keeps the field to the tab and says nothing", () => {
    source(new FakeLive());
    const { result } = renderHook(() => useSharedDraft("c1"));
    expect(result.current.binding).toBeUndefined();
    expect(result.current.onDraftChange).toBeTypeOf("function");
    expect(result.current.draftNotice).toBeUndefined();
  });

  it("carries what was typed onto the live draft, moved onto what others wrote meanwhile", () => {
    const live = new FakeLive();
    source(live);
    const { result } = renderHook(() => useSharedDraft("c1"));
    act(() => result.current.onDraftChange?.("hi"));
    act(() => result.current.onDraftChange?.("hi "));
    const binding = new FakeBinding("there");
    goLive(live, binding);
    expect(result.current.binding).toBe(binding);
    expect(result.current.onDraftChange).toBeUndefined();
    expect(binding.current).toBe("hi there");
  });

  it("carries keystrokes that land after the live draft arrives but before the field is bound to it", () => {
    // The field's handler is the one from the last render until React draws
    // the bound composer; a key pressed in between must reach the live draft.
    const live = new FakeLive();
    source(live);
    const { result } = renderHook(() => useSharedDraft("c1"));
    const local = result.current.onDraftChange!;
    act(() => local("ea"));
    const binding = new FakeBinding("seed ");
    goLive(live, binding);
    expect(binding.current).toBe("easeed ");
    act(() => local("ear"));
    act(() => local("early "));
    expect(binding.current).toBe("early seed ");
  });

  it("carries a keystroke in that window even when nothing was typed before it", () => {
    const live = new FakeLive();
    source(live);
    const { result } = renderHook(() => useSharedDraft("c1"));
    const local = result.current.onDraftChange!;
    const binding = new FakeBinding("seed ");
    goLive(live, binding);
    act(() => local("x"));
    expect(binding.current).toBe("xseed ");
  });

  it("carries nothing when nothing was typed", () => {
    const live = new FakeLive();
    source(live);
    renderHook(() => useSharedDraft("c1"));
    const binding = new FakeBinding("theirs");
    goLive(live, binding);
    expect(binding.edits).toEqual([]);
  });

  it("forgets one chat's typing when the composer moves to another", () => {
    const first = new FakeLive();
    const second = new FakeLive();
    source(first, second);
    const { result, rerender } = renderHook(({ id }) => useSharedDraft(id), { initialProps: { id: "c1" } });
    act(() => result.current.onDraftChange?.("for c1"));
    rerender({ id: "c2" });
    const binding = new FakeBinding("");
    goLive(second, binding);
    expect(binding.current).toBe("");
  });
});

describe("useSharedDraft when this browser cannot load the live lane", () => {
  it("keeps the field to the tab for good and says this browser does not support live sync", async () => {
    vi.useFakeTimers();
    // Only a browser with no WebAssembly cannot run Loro.
    vi.stubGlobal("WebAssembly", undefined);
    const live = new FakeLive();
    const { opened } = source(live);
    const { result } = renderHook(() => useSharedDraft("c1"));
    live.fallBack("load_failed");
    expect(result.current.draftNotice).toBe("This browser does not support our live sync.");
    expect(LIVE_SYNC_UNSUPPORTED).toBe("This browser does not support our live sync.");
    expect(result.current.binding).toBeUndefined();
    expect(result.current.onDraftChange).toBeTypeOf("function");
    await advance(REJOIN_MAX_MS * 3);
    expect(opened()).toBe(1);
  });
});

describe("useSharedDraft when Loro's code did not download", () => {
  // Offline, or on a flaky network, the lazy chunk that carries Loro fails to
  // load and the lane falls back with `load_failed`. That is a connection
  // problem in a browser that can run Loro, not a browser that cannot, and it
  // must clear once the network is back.
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  const goOffline = (): void => {
    vi.stubGlobal("navigator", { ...navigator, onLine: false });
    act(() => void window.dispatchEvent(new Event("offline")));
  };
  const goOnline = (): void => {
    vi.stubGlobal("navigator", { ...navigator, onLine: true });
    act(() => void window.dispatchEvent(new Event("online")));
  };

  it("never blames the browser, and asks again after the doubling wait", async () => {
    vi.useFakeTimers();
    const live = new FakeLive();
    const { opened } = source(live);
    const { result } = renderHook(() => useSharedDraft("c1"));
    live.fallBack("load_failed");
    expect(result.current.draftNotice).not.toBe(LIVE_SYNC_UNSUPPORTED);
    expect(result.current.draftNotice).toBe(LIVE_SYNC_UNAVAILABLE);
    await advance(REJOIN_FIRST_MS);
    expect(opened()).toBe(2);
  });

  it("says nothing under the field while offline, and asks again the moment the network is back", async () => {
    vi.useFakeTimers();
    const off = new FakeLive();
    const back = new FakeLive();
    const { opened } = source(off, back);
    const { result } = renderHook(() => useSharedDraft("c1"));
    goOffline();
    off.fallBack("load_failed");
    // The dock's own "Reconnecting…" covers it; the field is still the tab's.
    expect(result.current.draftNotice).toBeUndefined();
    expect(result.current.onDraftChange).toBeTypeOf("function");
    act(() => result.current.onDraftChange?.("typed offline"));
    expect(opened()).toBe(1);
    goOnline();
    // Not at the end of a thirty-second wait: now.
    expect(opened()).toBe(2);
    const binding = new FakeBinding("");
    goLive(back, binding);
    expect(result.current.draftNotice).toBeUndefined();
    expect(binding.current).toBe("typed offline");
  });

  it("does not reopen a lane that is live when the network flaps", () => {
    const live = new FakeLive();
    const { opened } = source(live);
    renderHook(() => useSharedDraft("c1"));
    goLive(live, new FakeBinding(""));
    goOffline();
    goOnline();
    expect(opened()).toBe(1);
  });
});

describe("useSharedDraft when the server cannot serve the live lane", () => {
  it.each([["crdt_unsupported"], ["a_reason_this_build_does_not_know"]])(
    "on %s says live sync is unavailable and asks again after a doubling wait, up to the cap",
    async (reason) => {
      vi.useFakeTimers();
      const live = new FakeLive();
      const { opened } = source(live);
      const { result } = renderHook(() => useSharedDraft("c1"));
      live.fallBack(reason);
      expect(result.current.draftNotice).toBe(LIVE_SYNC_UNAVAILABLE);
      expect(LIVE_SYNC_UNAVAILABLE).toBe("Live sync is unavailable right now.");
      // 30 s, 60 s, 120 s, 240 s, then 300 s for good.
      const waits = [30_000, 60_000, 120_000, 240_000, 300_000, 300_000];
      expect(waits[0]).toBe(REJOIN_FIRST_MS);
      for (const [i, wait] of waits.entries()) {
        await advance(wait - 1);
        expect(opened()).toBe(i + 1);
        await advance(1);
        expect(opened()).toBe(i + 2);
        // Still pending on the new ask, the reader keeps being told.
        expect(result.current.draftNotice).toBe(LIVE_SYNC_UNAVAILABLE);
        live.fallBack(reason);
      }
    },
  );

  it("rejoins when the server answers, carrying what was typed meanwhile, and drops the notice", async () => {
    vi.useFakeTimers();
    const off = new FakeLive();
    const back = new FakeLive();
    source(off, back);
    const { result } = renderHook(() => useSharedDraft("c1"));
    off.fallBack("crdt_unsupported");
    act(() => result.current.onDraftChange?.("typed while off"));
    await advance(REJOIN_FIRST_MS);
    expect(off.released).toBe(1);
    const binding = new FakeBinding("");
    goLive(back, binding);
    expect(result.current.binding).toBe(binding);
    expect(result.current.draftNotice).toBeUndefined();
    expect(binding.current).toBe("typed while off");
  });

  it("doubles its wait from the first to the longest and holds there", async () => {
    vi.useFakeTimers();
    const opens: number[] = [];
    sources.current = {
      openLiveDraft: () => {
        opens.push(Date.now());
        const live = new FakeLive();
        // Every ask is refused the moment it is made.
        queueMicrotask(() => live.fallBack("crdt_unsupported"));
        return { draft: live, release: () => undefined };
      },
    };
    renderHook(() => useSharedDraft("c1"));
    for (let step = 0; step < 50; step += 1) await advance(30_000);
    const waits = opens.slice(1, 8).map((at, i) => at - opens[i]!);
    expect(waits).toEqual([30_000, 60_000, 120_000, 240_000, 300_000, 300_000, 300_000]);
  });

  it("goes back to the first wait once it has been live again", async () => {
    vi.useFakeTimers();
    const lives = [new FakeLive(), new FakeLive(), new FakeLive(), new FakeLive()];
    const { opened } = source(...lives);
    renderHook(() => useSharedDraft("c1"));
    lives[0]!.fallBack("crdt_unsupported");
    await advance(REJOIN_FIRST_MS);
    lives[1]!.fallBack("crdt_unsupported");
    await advance(REJOIN_FIRST_MS * 2);
    goLive(lives[2]!, new FakeBinding(""));
    lives[2]!.fallBack("crdt_unsupported");
    await advance(REJOIN_FIRST_MS);
    expect(opened()).toBe(4);
  });

  it("keeps what the live draft showed when it is let go mid-session, and carries only the edit since", async () => {
    vi.useFakeTimers();
    const first = new FakeLive();
    const second = new FakeLive();
    source(first, second);
    const { result } = renderHook(() => useSharedDraft("c1"));
    goLive(first, new FakeBinding("shared"));
    first.fallBack("crdt_unsupported", "shared");
    expect(result.current.binding).toBeUndefined();
    expect(result.current.draftNotice).toBe(LIVE_SYNC_UNAVAILABLE);
    // The composer reports the text it kept, then the reader types on.
    act(() => result.current.onDraftChange?.("shared"));
    act(() => result.current.onDraftChange?.("shared and mine"));
    await advance(REJOIN_FIRST_MS);
    const binding = new FakeBinding("theirs: shared");
    goLive(second, binding);
    expect(binding.current).toBe("theirs: shared and mine");
  });

  it("stops asking for a chat the composer has left", async () => {
    vi.useFakeTimers();
    const live = new FakeLive();
    const { opened } = source(live);
    const { rerender } = renderHook(({ id }) => useSharedDraft(id), { initialProps: { id: "c1" } });
    live.fallBack("crdt_unsupported");
    rerender({ id: "c2" });
    expect(opened()).toBe(2);
    await advance(REJOIN_MAX_MS * 2);
    expect(opened()).toBe(2);
  });
});

describe("useSharedDraft when the reader may not see the chat", () => {
  it.each([["not_found"], ["forbidden"]])("on %s has no shared draft, says nothing and does not ask again", async (reason) => {
    vi.useFakeTimers();
    const live = new FakeLive();
    const { opened } = source(live);
    const { result } = renderHook(() => useSharedDraft("c1"));
    live.fallBack(reason);
    expect(result.current).toEqual({});
    await advance(REJOIN_MAX_MS * 2);
    expect(opened()).toBe(1);
  });
});

describe("useSharedDraft on a source with no shared draft", () => {
  it("returns nothing", () => {
    sources.current = {};
    const { result } = renderHook(() => useSharedDraft("c1"));
    expect(result.current).toEqual({});
  });

  it("lets go of the live draft when the chat changes or the composer goes", () => {
    const live = new FakeLive();
    source(live);
    const { rerender, unmount } = renderHook(({ id }) => useSharedDraft(id), { initialProps: { id: "c1" } });
    rerender({ id: "c2" });
    expect(live.released).toBe(1);
    unmount();
    expect(live.released).toBe(2);
  });
});

// --- the real cloud source -------------------------------------------------

function fakeDoc() {
  const sent: OpInput[] = [];
  const handle: DocHandle<unknown> = {
    onMessage: () => () => undefined,
    onPhase: () => () => undefined,
    getPhase: () => ({ phase: "live", epoch: 1, seq: 0, peerId: "p:mine", canWrite: true, pending: 0, error: null }),
    sendOp: (op: OpInput) => {
      sent.push(op);
      return Promise.resolve({ op_id: "op-1", seq: 1, changed: true });
    },
    dispose: () => undefined,
  };
  return { handle, sent };
}

function cloudSource(lives: FakeLive[]) {
  const doc = fakeDoc();
  const real = new CloudDataSource({
    rest: { listMessages: async () => ({ items: [], next_after_seq: 0, resync_from: null }) } as never,
    openDoc: () => doc.handle as DocHandle<never>,
    acquire: () => () => undefined,
    clientId: () => "client-1",
  });
  let opened = 0;
  real.openLiveDraft = () => {
    const live = lives[Math.min(opened, lives.length - 1)]!;
    opened += 1;
    return { draft: live as never, release: () => void (live.released += 1) };
  };
  sources.current = real;
  return { real, doc };
}

function composerProps() {
  return {
    modes: [{ value: "ask", label: "Ask" }],
    mode: "ask",
    models: [{ value: "m1", label: "M1" }],
    model: "m1",
    onModelChange: vi.fn(),
    efforts: [{ value: "low", label: "Low", bars: 1 as const }],
    effort: "low",
    onEffortChange: vi.fn(),
  };
}

function Dock({ chatId, onSend }: { chatId: string; onSend: (text: string) => void }) {
  const shared = useSharedDraft(chatId);
  return <Composer {...composerProps()} onSend={onSend} {...shared} />;
}

const field = (): HTMLTextAreaElement => screen.getByLabelText("Message Databench");

/** Every draft the document's op log was sent, in any shape. */
const opLogDrafts = (sent: OpInput[]): unknown[] =>
  sent.filter((op) => op.intent === "set_meta" && (op.meta as { draft?: unknown } | undefined)?.draft !== undefined);

describe("the composer over the real cloud source", () => {
  it("writes no draft to the document's op log while loading, local-only, live or let go", async () => {
    vi.useFakeTimers();
    const lives = [new FakeLive(), new FakeLive(), new FakeLive()];
    const { real, doc } = cloudSource(lives);
    real.subscribeChat("c1", () => undefined);
    const onSend = vi.fn();
    const view = render(<Dock chatId="c1" onSend={onSend} />);
    const type = (text: string): void => void fireEvent.change(field(), { target: { value: text } });

    type("loading");
    await advance(1_000);
    expect(opLogDrafts(doc.sent)).toEqual([]);
    lives[0]!.fallBack("crdt_unsupported");
    type("loading, then off");
    await advance(REJOIN_FIRST_MS);
    expect(opLogDrafts(doc.sent)).toEqual([]);
    const binding = new FakeBinding("");
    goLive(lives[1]!, binding);
    expect(binding.current).toBe("loading, then off");
    type("loading, then off, then live");
    lives[1]!.fallBack("crdt_unsupported", binding.current);
    type("loading, then off, then live, then let go");
    await advance(1_000);
    view.unmount();
    await advance(1_000);

    expect(opLogDrafts(doc.sent)).toEqual([]);
  });

  it("shows the browser notice under a field that still types and sends", async () => {
    vi.stubGlobal("WebAssembly", undefined);
    const live = new FakeLive();
    cloudSource([live]);
    const onSend = vi.fn();
    render(<Dock chatId="c1" onSend={onSend} />);
    live.fallBack("load_failed");
    expect(screen.getByText("This browser does not support our live sync.")).toBeTruthy();
    fireEvent.change(field(), { target: { value: "still mine" } });
    expect(field().value).toBe("still mine");
    fireEvent.keyDown(field(), { key: "Enter" });
    expect(onSend).toHaveBeenCalledWith("still mine");
  });
});
