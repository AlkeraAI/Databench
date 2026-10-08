// The transcript panel: ONE surface over {id, item, status} entries, whether
// the chat is history being reread or a turn arriving live. The panel owns the
// tape (blocks, tear-lines between turns, the gutter-dot wrapper), the follow
// scroll, the turn's tail working display, the dock the host fills with its
// composer or interrupt — and the window's edges: the sentinel at the top that
// asks the host for the page above, the anchoring that keeps the viewport
// still while that page lands, and the way back to the live edge.
//
// Follow behavior: the transcript sticks to the bottom only while the reader
// is already there; anywhere else their place holds — a page landing above
// and a turn arriving below both leave what is on screen where it is.

import {
  Fragment,
  memo,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ReactElement,
  type ReactNode,
  type RefObject,
} from "react";

import { Activity } from "../activity";
import { useFollowScroll } from "../hooks";
import { Spinner } from "../indicators/Indicators";
import type { CardStep } from "../tools";
import {
  Notice,
  OrderTicket,
  SlashEffect,
  Tear,
  Thinking,
  TranscriptBlock,
  type NoticeAction,
  type NoticeLevel,
  type TicketCancellation,
  type TranscriptBlockKind,
} from "../transcript";
import "./panel.css";

/** What animates: "streaming" while thinking or prose arrives, "running"
 *  while a tool run or slash effect executes, "settled" once done. */
export type EntryStatus = "streaming" | "running" | "settled";

/** One transcript block's view. The closed kinds are the panel's own organs; a
 *  `slot` carries a block the host composed itself (the plan document, the
 *  question mirror, the subagent report), with `block` feeding the dot
 *  contract's per-kind metric. */
export type TranscriptItemView =
  | {
      kind: "user";
      text: string;
      at: string;
      pending?: boolean;
      cancelled?: TicketCancellation;
      fromTemplate?: string;
      fromTemplateAuthor?: string;
    }
  | { kind: "thinking"; duration: string; body: string[] }
  | { kind: "activity"; summary: string; steps: CardStep[] }
  | { kind: "slash"; command: string; outcome: string; data: string; pending?: string }
  | { kind: "notice"; level: NoticeLevel; title: string; body: string; action?: NoticeAction | null }
  | { kind: "slot"; block: TranscriptBlockKind; node: ReactNode };

export interface TranscriptEntry {
  id: string;
  item: TranscriptItemView;
  status: EntryStatus;
  /** The agent no longer holds this block — a /clear, or a turn a compaction
   *  summarised. It keeps its place in the tape and stays scrollable; only its
   *  weight drops. */
  dimmed?: boolean;
  /** Why it left the agent's context, carried by the FIRST block of the run so
   *  the reason is said once rather than on every line under it. */
  dimNote?: string;
}

/** The transcript above the first entry, and how to ask for it. */
export interface TranscriptHistoryControls {
  /** There are turns above what the tape holds. */
  hasOlder: boolean;
  /** A page is on its way; the sentinel waits rather than asking twice. */
  loading: boolean;
  /** Ask for the page above. */
  loadOlder: () => void;
  /** The reader is back at the live edge; pages past the budget may go. */
  release?: () => void;
  /** The last page failed and the tape is not asking again on its own right
   *  now. The top of the tape says so and offers the retry, which is also what
   *  puts the host's own retry ladder back at its first rung. */
  failed?: boolean;
}

/** What the top of the tape says when a page of earlier messages did not come. */
export const HISTORY_FAILED_NOTE = "Could not load earlier messages.";

/** How close to the top of the tape the reader must be for the next page to
 *  be asked for — before they reach the edge, so the page is there when they do. */
export const HISTORY_THRESHOLD_PX = 600;

export interface ChatPanelProps {
  entries: TranscriptEntry[];
  /** True while a turn is active: the tape's tail carries the looping working
   *  display for that whole span. */
  working?: boolean;
  /** What the working display says; `Working…` unless the host knows better
   *  (a message sent while the workspace is not up reads as waiting for it). */
  workingLabel?: string;
  /** The panel's header; the host composes `Chrome` or nothing. */
  header?: ReactNode;
  /** The dock's content: the composer, or the interrupt that replaces it. */
  dock?: ReactNode;
  /** Shown in the tape while the transcript has no entries. */
  empty?: ReactNode;
  /** True while the transcript itself is still arriving: a centered spinner
   *  holds the tape, so an opening chat never reads as an empty one. */
  loading?: boolean;
  /** Older transcript the host can page in above the first entry. Absent on a
   *  host whose entries are the whole transcript. */
  history?: TranscriptHistoryControls;
}

/** One entry, behind a memo boundary so a streamed token elsewhere skips the
 *  rows whose {id, item, status} did not change. */
const EntryRow = memo(function EntryRow({ entry, live }: { entry: TranscriptEntry; live: boolean }): ReactNode {
  return (
    <TranscriptBlock
      kind={entry.item.kind === "slot" ? entry.item.block : entry.item.kind}
      live={live}
      dimmed={entry.dimmed}
      dimNote={entry.dimNote}
      entryId={entry.id}
    >
      <EntryBody entry={entry} />
    </TranscriptBlock>
  );
});

function EntryBody({ entry }: { entry: TranscriptEntry }): ReactNode {
  const { item, status } = entry;
  switch (item.kind) {
    case "user":
      return (
        <OrderTicket
          text={item.text}
          at={item.at}
          pending={item.pending}
          cancelled={item.cancelled}
          fromTemplate={item.fromTemplate}
          fromTemplateAuthor={item.fromTemplateAuthor}
        />
      );
    case "thinking":
      // Open while the thought streams so the arrival is visible; the remount
      // on settle folds it back to the quiet label, reasoning being supporting
      // material once the answer starts.
      return (
        <Thinking
          key={status}
          duration={item.duration}
          body={item.body}
          defaultOpen={status === "streaming"}
          streaming={status === "streaming"}
        />
      );
    case "activity":
      // A run folds only on the transition into "settled": a group mounted
      // already settled reads as history and stays open.
      return <Activity summary={item.summary} steps={item.steps} folded={status === "settled"} />;
    case "slash":
      return (
        <SlashEffect
          command={item.command}
          outcome={item.outcome}
          data={item.data}
          pending={item.pending}
          running={status === "running"}
        />
      );
    case "notice":
      return <Notice level={item.level} title={item.title} body={item.body} action={item.action} />;
    case "slot":
      return item.node;
  }
}

function Tape({
  entries,
  working,
  workingLabel,
  empty,
  seeded,
  history,
}: {
  entries: TranscriptEntry[];
  working?: boolean;
  workingLabel?: string;
  empty?: ReactNode;
  seeded: Set<string> | null;
  history?: TranscriptHistoryControls;
}): ReactElement {
  // Pressing Retry unmounts the button it was pressed with, so a keyboard
  // reader would be dumped to the document. The sentinel that replaces it takes
  // the focus and, being a live region, says what is happening there now.
  const sentinelRef = useRef<HTMLDivElement>(null);
  const handedOver = useRef(false);
  const failed = Boolean(history?.hasOlder && history.failed);
  useEffect(() => {
    if (failed || !handedOver.current) return;
    handedOver.current = false;
    sentinelRef.current?.focus();
  }, [failed]);

  return (
    <div className="chat-tape" data-empty={entries.length === 0 && empty ? "" : undefined}>
      {failed && history ? (
        <p className="chat-history-failed" role="status">
          {HISTORY_FAILED_NOTE}
          <button
            type="button"
            className="chat-history-failed__retry"
            onClick={() => {
              handedOver.current = true;
              history.loadOlder();
            }}
          >
            Retry
          </button>
        </p>
      ) : null}
      {history?.hasOlder && !history.failed ? (
        <div
          className="chat-history"
          data-history-sentinel=""
          data-loading={history.loading ? "" : undefined}
          ref={sentinelRef}
          tabIndex={-1}
          role="status"
          aria-label={history.loading ? "Loading earlier messages" : "Earlier messages"}
        >
          {history.loading ? <Spinner size={14} /> : null}
        </div>
      ) : null}
      {entries.map((entry, i) => (
        <Fragment key={entry.id}>
          {i > 0 && entry.item.kind === "user" ? <Tear /> : null}
          <EntryRow entry={entry} live={seeded !== null && !seeded.has(entry.id)} />
        </Fragment>
      ))}
      {entries.length === 0 && empty ? empty : null}
      {working ? (
        <p className="chat-turnwork" aria-live="polite">
          <span className="chat-shimmer">{workingLabel ?? "Working…"}</span>
        </p>
      ) : null}
    </div>
  );
}

function Opening(): ReactElement {
  return (
    <div className="chat-loading" role="status" aria-label="Loading chat">
      <Spinner size={22} />
    </div>
  );
}

function entryElement(root: HTMLElement, id: string): HTMLElement | null {
  // Only the quote and the backslash end an attribute string; an id may carry
  // anything else (`part#1`, a path) as it is.
  const escaped = id.replace(/\\/g, "\\\\").replace(/"/g, '\\"');
  return root.querySelector<HTMLElement>(`[data-entry-id="${escaped}"]`);
}

/** Ask for the page above whenever the reader is within the threshold of the
 *  top and one is available. An `IntersectionObserver` on the sentinel does
 *  it without a scroll handler where the platform has one; the scroll
 *  position is checked as well, because a sentinel that stays in view (a
 *  transcript shorter than the viewport) fires the observer once and must
 *  still be asked again when the page that came was not enough. */
function useHistorySentinel(
  scrollRef: RefObject<HTMLDivElement | null>,
  history: TranscriptHistoryControls | undefined,
  entries: TranscriptEntry[],
): void {
  const hasOlder = history?.hasOlder ?? false;
  const loading = history?.loading ?? false;
  const loadOlder = history?.loadOlder;
  // A failed page is the host's to re-ask for, on its own ladder. Re-arming
  // here would mean one request per round trip for as long as the refusal
  // lasts, which is the reconnect storm with a different transport.
  const failed = history?.failed ?? false;

  useEffect(() => {
    const node = scrollRef.current;
    if (!node || !hasOlder || loading || failed || !loadOlder) return;
    const nearTop = () => node.scrollTop < HISTORY_THRESHOLD_PX;
    if (nearTop()) {
      loadOlder();
      return;
    }
    const onScroll = () => {
      if (nearTop()) loadOlder();
    };
    node.addEventListener("scroll", onScroll, { passive: true });
    let observer: IntersectionObserver | null = null;
    const sentinel = node.querySelector<HTMLElement>("[data-history-sentinel]");
    if (sentinel && typeof IntersectionObserver !== "undefined") {
      observer = new IntersectionObserver(
        (records) => {
          if (records.some((record) => record.isIntersecting)) loadOlder();
        },
        { root: node, rootMargin: `${HISTORY_THRESHOLD_PX}px 0px 0px 0px` },
      );
      observer.observe(sentinel);
    }
    return () => {
      node.removeEventListener("scroll", onScroll);
      observer?.disconnect();
    };
    // `entries` re-arms the check after a page lands: the sentinel may still be
    // within reach, and the observer does not fire again for a target that
    // never left the viewport.
  }, [scrollRef, hasOlder, loading, failed, loadOlder, entries]);
}

/** Keep what the reader is looking at where it is while a page lands above it.
 *
 *  After every commit the first entry's id and offset are recorded. When a
 *  commit changes the first entry and the previous first entry is still on the
 *  tape — a page was prepended — the previous first entry's new offset is
 *  measured and the scroll position moved by exactly its displacement, before
 *  paint. Measuring the entry's own movement, rather than the growth of the
 *  scroll height, holds the viewport still even when the live tail grew in
 *  the same commit. A pinned reader is left to the follow scroll. */
function useScrollAnchor(
  scrollRef: RefObject<HTMLDivElement | null>,
  entries: TranscriptEntry[],
  pinned: boolean,
): void {
  const anchor = useRef<{ id: string; top: number } | null>(null);
  useLayoutEffect(() => {
    const node = scrollRef.current;
    if (!node) return;
    const first = entries[0];
    const previous = anchor.current;
    if (
      previous &&
      first &&
      first.id !== previous.id &&
      !pinned &&
      entries.some((entry) => entry.id === previous.id)
    ) {
      const kept = entryElement(node, previous.id);
      if (kept) node.scrollTop += kept.offsetTop - previous.top;
    }
    const firstEl = first ? entryElement(node, first.id) : null;
    anchor.current = first && firstEl ? { id: first.id, top: firstEl.offsetTop } : null;
  }, [scrollRef, entries, pinned]);
}

export function ChatPanel({
  entries,
  working,
  workingLabel,
  header,
  dock,
  empty,
  loading,
  history,
}: ChatPanelProps): ReactElement {
  const { ref: scrollRef, pinned, pin } = useFollowScroll(entries);
  // The rise animation belongs to arrival. A host hydrates history over the
  // wire, so the seed waits for the first render that HAS entries: that batch
  // is history and mounts still; an id that appears later rises in.
  const seeded = useRef<Set<string> | null>(null);
  if (seeded.current === null && entries.length > 0) seeded.current = new Set(entries.map((entry) => entry.id));
  const opening = Boolean(loading) && entries.length === 0;

  useScrollAnchor(scrollRef, entries, pinned);
  useHistorySentinel(scrollRef, history, entries);

  // What arrived below while the reader was away from the live edge: the tail
  // entry changing is a turn moving. Cleared the moment they are back.
  const lastId = entries[entries.length - 1]?.id ?? null;
  const seenTail = useRef(lastId);
  const [unseen, setUnseen] = useState(false);
  useEffect(() => {
    if (pinned) {
      seenTail.current = lastId;
      setUnseen(false);
      return;
    }
    if (lastId !== seenTail.current) setUnseen(true);
  }, [lastId, pinned]);

  // At the live edge, whichever way it was reached: the rows far above may be
  // let go. The budget is evaluated on every commit that grew the tape, not
  // only on the transition into pinned: `release` is identity-stable, so keyed
  // on it alone a reader who never scrolls would evaluate it once for the life
  // of the tab, which is precisely the tab the budget exists for.
  const release = history?.release;
  const rowCount = entries.length;
  useEffect(() => {
    if (pinned) release?.();
  }, [pinned, release, rowCount]);

  return (
    <div className="chat-panel">
      {header}
      <div className="chat-live">
        <div className="chat-scroll" ref={scrollRef} data-follow={pinned ? "pinned" : "free"}>
          {opening ? (
            <Opening />
          ) : (
            <Tape
              entries={entries}
              working={working}
              workingLabel={workingLabel}
              empty={empty}
              seeded={seeded.current}
              history={history}
            />
          )}
        </div>
        {!pinned && entries.length > 0 ? (
          <button type="button" className="chat-jump" data-unseen={unseen ? "" : undefined} onClick={pin}>
            {unseen ? "New messages" : "Jump to latest"}
          </button>
        ) : null}
      </div>
      {dock ? (
        <div className="chat-dock">
          <div className="chat-dock__inner">{dock}</div>
        </div>
      ) : null}
    </div>
  );
}
