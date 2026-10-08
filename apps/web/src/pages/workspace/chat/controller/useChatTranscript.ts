// The live transcript and its turn lifecycle: the chat store's folded turns
// with the interrupt overlay applied, the new-chat creation flow, the send
// path, and the stall watchdog.

import type { ComposerSendMeta, ConversationTurn, SuggestedPrompt } from "@alkera/chat-model";
import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import {
  startTransition,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type MutableRefObject,
} from "react";
import { useNavigate } from "react-router-dom";
import { ApiError } from "@/api/errors";
import { useRealtimeDown } from "@/api/events/status";
import { queryRetryDelay } from "@/api/retry";
import { onBrowserOnline } from "@/lib/online";
import { AUTH_REQUIRED_CODE } from "@/lib/rpcCodes";
import {
  HISTORY_RETRY,
  HISTORY_RETRY_ATTEMPTS,
  HISTORY_RETRY_FLOOR_MS,
  ladderDelay,
} from "@/lib/limits";
import {
  awaitingTurnStart,
  DRAFT_CHAT_KEY,
  isSendForbidden,
  optimisticUserTurn,
  SEND_FORBIDDEN,
  TRANSCRIPT_UNREADABLE,
  useChatStore,
  type QueuedMessage,
} from "../chatStore";
import { composerMetaToSendOptions } from "../adapters";
import { chatKeys } from "../chatKeys";
import type { ChatHandoff } from "../openSurfaces";
import { applyLocalInterruptState, type ChatInterrupts } from "./useChatInterrupts";
import { chatCaps, chatData, chatHost, errorText, transportFailure, type Chat, type ModelInfo, type SendOptions } from "../data";
import { servesModels } from "../data/ChatDataSource";

/** Drop thinking parts when the effective effort is none. A turn
 *  left with NO parts (a thinking-only assistant turn mid-stream) is dropped
 *  entirely — an empty bubble that later fills with text reads as the layout
 *  "shifting around". */
export function stripThinkingParts(turns: ConversationTurn[]): ConversationTurn[] {
  return turns
    .map((turn) => ({
      ...turn,
      parts: turn.parts.filter((part) => part.kind !== "thinking"),
    }))
    .filter((turn) => turn.parts.length > 0 || turn.author === "user");
}

/** Why the watchdog gave up on a turn. The two are different faults with
 *  different owners, so they are never told in the same words: `backend` is
 *  the shell's own agent going quiet (the reader can look at it); `workspace`
 *  is the machine the turn was running on becoming unreachable (they cannot). */
export type StallCause = "backend" | "workspace";

// Shown when the turn watchdog fires. Styled as a normal assistant error part,
// so it reads like any other surfaced error.
export const STALL_TURN: ConversationTurn = {
  id: "alk-stall",
  author: "assistant",
  status: "error",
  parts: [
    {
      id: "alk-stall-part",
      kind: "system",
      text: "The agent stopped responding without a result. Check that the backend and model gateway are running, then try again.",
      tone: "error",
    },
  ],
};

/** The same shape for the other fault: the box running the turn stopped
 *  answering. Telling this reader to check a backend and a gateway is the wrong
 *  diagnosis — they own neither, and the banner above the transcript is already
 *  saying the workspace is unreachable. */
export const WORKSPACE_LOST_TURN: ConversationTurn = {
  id: "alk-workspace-lost",
  author: "assistant",
  status: "error",
  parts: [
    {
      id: "alk-workspace-lost-part",
      kind: "system",
      text: "The machine stopped answering, so the turn was lost.",
      tone: "error",
    },
  ],
};

/** The same silence told to a browser reader. They have no shell process to
 *  look at, so the editor's diagnosis is the wrong one twice over: it names
 *  parts they do not own and asks them to check something they cannot reach.
 *  The line states what happened; the composer is right there, so it does not
 *  tell them to resend. */
export const WORKSPACE_LOST_TURN_QUIET: ConversationTurn = {
  id: "alk-workspace-quiet",
  author: "assistant",
  status: "error",
  parts: [
    {
      id: "alk-workspace-quiet-part",
      kind: "system",
      text: "The machine did not answer in time, so this turn was stopped.",
      tone: "error",
    },
  ],
};

/** The refused send, said where the withdrawn bubble was.
 *
 *  A 403 on `POST /chats/{id}/messages` withdraws the message and says so, so a
 *  reader whose rung does not carry a message never believes they sent one. */
export function sendRefusedTurn(reason: string): ConversationTurn {
  return {
    id: "alk-send-refused",
    author: "assistant",
    status: "error",
    parts: [{ id: "alk-send-refused-part", kind: "system", text: reason, tone: "error" }],
  };
}

const STALL_TURN_FOR: Record<StallCause, ConversationTurn> = {
  backend: STALL_TURN,
  workspace: WORKSPACE_LOST_TURN,
};

/** The browser hears both faults in the workspace's words: the cause is a
 *  distinction only the editor's reader can act on. */
const BROWSER_STALL_TURN_FOR: Record<StallCause, ConversationTurn> = {
  backend: WORKSPACE_LOST_TURN_QUIET,
  workspace: WORKSPACE_LOST_TURN,
};

function stallTurnFor(cause: StallCause): ConversationTurn {
  return chatHost().kind === "browser" ? BROWSER_STALL_TURN_FOR[cause] : STALL_TURN_FOR[cause];
}

const CREATE_ERROR_FALLBACK = "The chat could not be started. Try again in a moment.";

// Shown inline when creating a new chat fails (no chat exists yet to fold into).
// The host bridge serializes rejections as `{message, code?}` — surface the
// actual message (e.g. "Open a workspace folder to work in.") so the
// user gets the actionable reason, not a generic "backend down" guess. An
// AUTH_REQUIRED rejection flips the whole view to the login panel, but this
// turn outlives the re-login — so it must say "sign in", not "didn't respond".
/** What a refused create says, by the server's code. A server sentence is
 *  written for whoever runs it ("the folder is leased"), so a refusal this build
 *  knows reads as one of these, and one it does not know reads as the plain
 *  fallback rather than as the server's words. */
export const CREATE_REFUSALS: Readonly<Record<string, string>> = {
  "files.leased": "Another chat in this workspace has its files open. Try again once it finishes.",
  workspace_holds_one_chat: "That chat's workspace holds one chat. Start a new chat on its own.",
  workspaces_multi_chat_disabled: "Chats can't share a workspace yet. Start a new chat on its own.",
};

export function createErrorTurn(err: unknown): ConversationTurn {
  const e = typeof err === "object" && err !== null ? (err as { message?: unknown; code?: unknown }) : null;
  const code = typeof e?.code === "string" ? e.code : null;
  const known = code !== null ? CREATE_REFUSALS[code] : undefined;
  // A coded refusal from the API this build has no copy for: the server's own
  // words are an operator's, so the plain fallback stands in. A message with no
  // code (the editor's host, "Open a workspace folder…") is written for the
  // reader and is shown as it is.
  const coded = code !== null && code !== "error";
  // The words go back into the field on any failure, so a failure of the trip
  // says what happened and that nothing typed was lost.
  const transport = transportFailure(err);
  const detail =
    e?.code === AUTH_REQUIRED_CODE
      ? "you were signed out."
      : typeof e?.message === "string" && e.message
        ? e.message
        : null;
  const id = `alk-create-error-${Date.now()}`;
  return {
    id,
    author: "assistant",
    status: "error",
    parts: [
      {
        id: `${id}-part`,
        kind: "system",
        text: transport
          ? `${transport} Your message is still in the box.`
          : known
            ? known
            : detail && !coded
              ? `Couldn't start a new conversation: ${detail}`
              : CREATE_ERROR_FALLBACK,
        tone: "error",
      },
    ],
  };
}

/** The opening asks where the shell offers none of its own — the editor's, for
 *  a reader with a repository in front of them. A shell whose reader has
 *  something else to ask about says so through `ChatHost.emptyState`. */
export const DEFAULT_PROMPTS: SuggestedPrompt[] = [
  { id: "p1", label: "Explain this codebase", prefill: "Give me a high-level tour of this codebase: entry points, structure and key modules." },
  { id: "p2", label: "Find and fix a bug", prefill: "Find a bug in the code I have open and propose a fix." },
  { id: "p3", label: "Write tests", prefill: "Write tests for the file I have open." },
];

/** How long a turn may say NOTHING before a shell with no other liveness signal
 *  calls it dead.
 *
 *  The editor's floor only. A turn there runs against a daemon in the same
 *  machine as the reader, which publishes no turn state and has no status
 *  banner: if it dies, silence is the only thing that says so.
 *
 *  A CLOUD chat has both, so it has no silence floor at all — see the watchdog.
 *  A turn may legitimately run for hours or days, and every number this surface
 *  could pick is a number a real turn will one day exceed. */
const STALL_MS = 300_000;

// Seed the chat-list cache with the created chat BEFORE navigating, so the
// chat page renders the right pinned model/effort on first paint instead of a
// frame of the catalog default while the refetch lands (the model-desync: the
// composer locked onto the catalog's first model before the list caught up).
function seedChatListCache(queryClient: QueryClient, chat: Chat): void {
  const upsert = (old?: Chat[]): Chat[] => [chat, ...(old ?? []).filter((c) => c.id !== chat.id)];
  queryClient.setQueryData<Chat[]>(chatKeys.chats(), upsert);
}

/** The transcript above what is on screen, and how to ask for it. What the
 *  panel's top sentinel drives. */
export interface TranscriptHistory {
  /** The durable record holds turns above the window. */
  hasOlder: boolean;
  /** A page is on its way. */
  loading: boolean;
  /** Read one more page above the window. */
  loadOlder: () => void;
  /** The reader is back at the bottom: pages past the budget may go. */
  release: () => void;
  /** The last page failed and nothing is being asked for right now: the tape
   *  offers the retry rather than re-arming its sentinel. */
  failed: boolean;
}

export interface ChatTranscript {
  renderedTurns: ConversationTurn[];
  sending: boolean;
  /** The reader's newest message has not been taken up by anything yet: no
   *  turn is running for it (see `awaitsTurnStart`). A chat still being
   *  created is in the same wait. */
  awaitingTurnStart: boolean;
  loadingChat: boolean;
  /** The transcript could not be read and nothing of it is on screen. Never
   *  the same thing as a chat with no messages: the empty chat's suggestions
   *  over a conversation that failed to load read as one that was deleted. */
  loadFailed: boolean;
  /** Read the transcript again now. */
  retryLoad: () => void;
  /** Older turns the source can still page in; every field false/no-op on a
   *  source that serves whole transcripts. */
  history: TranscriptHistory;
  suggestedPrompts: SuggestedPrompt[];
  submitMessage: (text: string, meta?: ComposerSendMeta) => void;
  /** Messages typed while the turn was running, oldest first. Empty on a
   *  surface with no chat to hold them against yet. */
  queued: QueuedMessage[];
  /** A message this reader SENT was taken while a turn was already running, so
   *  it is waiting behind that turn. False again the moment that turn ends and
   *  theirs is the one being worked on. */
  queuedBehindTurn: boolean;
  /** Hold a message until the turn ends, instead of dropping the keypress.
   *  Absent where there is no chat to hold it against — a first message whose
   *  chat is still being created has nowhere to queue. */
  queueMessage?: (text: string, meta?: ComposerSendMeta) => void;
  editQueued?: (id: string, text: string) => void;
  removeQueued?: (id: string) => void;
  /** Send a message restored from a previous page session. Those never go on
   *  their own — opening a chat must not put words in it — so this is the
   *  reader's word that it should. */
  sendQueued?: (id: string) => void;
  /** Open the chat the next `submitMessage` on this new-chat surface will be
   *  the first message of — for a file that has to land in the chat before
   *  the message naming it can go. Opened once: a second call answers the
   *  same chat until the message is sent into it. Absent on a source that can
   *  only open a chat by sending into it. */
  startChat?: (title: string, meta?: ComposerSendMeta) => Promise<Chat>;
  cancelTurn: () => void;
  /** A Stop that has gone and has not been answered yet. The key it was
   *  pressed on says so and takes no further press until the source answers: a
   *  reader shown nothing for the round trip presses again, and every press
   *  would be another stop on the wire. */
  stopping: boolean;
  /** What Stop could not do, in the reader's words — null when it stopped the
   *  turn. The portal cannot abort a run on the machine, and saying so beats
   *  both silence and an uncaught rejection. */
  cancelNotice: string | null;
  dismissCancelNotice: () => void;
  /** Text the surface is handing BACK to the composer, under the stamp a
   *  composer adopts a draft by — a first message whose chat could not be
   *  started. Null while nothing has been handed back. */
  returnedDraft: { text: string; at: number } | null;
  /** A send the daemon refused because this chat's session is gone. Surfaces
   *  the reopen prompt. Reopening is always the user's click, never automatic. */
  staleSend: boolean;
  reopenStaleChat: () => void;
  dismissStaleSend: () => void;
}

export function useChatTranscript({
  chatId,
  handoff,
  models,
  reasoningVisible,
  interrupts,
  endCreating,
  machineUnavailable = false,
  newChatWorkspaceId,
}: {
  chatId: string | null;
  handoff: ChatHandoff | null;
  models: ModelInfo[];
  reasoningVisible: boolean;
  interrupts: ChatInterrupts;
  endCreating: () => void;
  /** The shell's word on the machine this chat's turns run on: true once it is
   *  known NOT to be serving (unreachable, stopped, refusing this chat). The
   *  editor has no such machine and leaves it false. */
  machineUnavailable?: boolean;
  /** The workspace a chat started from this empty composer is made in. */
  newChatWorkspaceId?: string;
}): ChatTranscript {
  // Live transcript + send lifecycle for the active chat — the single source of
  // truth (see chatStore). `open` loads history + streams daemon updates in.
  const entry = useChatStore((state) => (chatId ? state.byId[chatId] : undefined));
  // A chat handed its first message shows it in the very render the route
  // lands in — one frame BEFORE the effect below opens the chat and adopts the
  // hand-off, so that frame is never an empty screen.
  const held = useChatStore((state) =>
    chatId && !state.byId[chatId] ? state.pending[chatId] : undefined,
  );
  useEffect(() => {
    if (!chatId) return;
    return useChatStore.getState().open(chatId);
  }, [chatId]);
  const storeTurns = useMemo(
    () => (entry ? [...entry.base, ...entry.optimistic] : (held ?? [])),
    [entry, held],
  );
  const history = useTranscriptHistory(chatId);

  const [stalled, setStalled] = useState<StallCause | null>(null);
  // A turn WE initiated is awaiting a response — gates the watchdog so it never
  // stalls a passively-opened chat whose history ends on an unanswered user turn.
  const turnInitiatedRef = useRef(false);

  const creation = useChatCreation({ handoff, endCreating, clearStall: () => setStalled(null) });

  useTurnResets({ chatId, entry, clearOptimistic: creation.clearOptimistic, setStalled, turnInitiatedRef });

  // chatId null = a brand-new chat (handoff OR an empty-workspace first send):
  // both render from the local optimistic/creation state until a real id exists.
  // A hand-off not yet adopted is a turn in flight, the same as once it is.
  const sending = chatId ? (entry?.sending ?? Boolean(held?.length)) : creation.creatingBusy;
  const loadingChat = chatId ? entry?.loading ?? false : false;
  const loadFailed =
    chatId !== null && entry?.error === TRANSCRIPT_UNREADABLE && !loadingChat && storeTurns.length === 0;
  const retryLoad = useCallback(() => {
    if (chatId) useChatStore.getState().reload(chatId);
  }, [chatId]);
  useLoadRetry(chatId, loadFailed, retryLoad);

  const rendered = useRenderedTurns({
    chatId,
    storeTurns,
    optimisticTurns: creation.optimisticTurns,
    interrupts,
    reasoningVisible,
    stalled,
    sendRefusal: entry?.sendRefusal ?? null,
  });

  useTurnWatchdog({
    sending,
    hasPendingInterrupt: rendered.hasPendingInterrupt,
    entry,
    creatingBusy: creation.creatingBusy,
    turnInitiatedRef,
    chatId,
    machineUnavailable,
    cancelCreating: creation.cancelCreating,
    setStalled,
  });

  const [cancelNotice, setCancelNotice] = useState<string | null>(null);
  // A chat started from this composer is made in the workspace the shell
  // names, when it names one.
  const createInPlace = creation.createNewChat;
  const createNewChat = useCallback(
    (text: string, opts: SendOptions | undefined, into?: Chat): Promise<void> =>
      createInPlace(text, newChatWorkspaceId ? { ...opts, workspaceId: newChatWorkspaceId } : opts, into),
    [createInPlace, newChatWorkspaceId],
  );
  const { submitMessage, startChat, cancelTurn, stopping, queueMessage, editQueued, removeQueued, sendQueued } = useSendControls({
    chatId,
    models,
    createNewChat,
    cancelCreating: creation.cancelCreating,
    turnInitiatedRef,
    setCancelNotice,
  });
  const dismissCancelNotice = useCallback(() => setCancelNotice(null), []);

  const stale = useStaleSend(chatId, entry?.staleSend);

  return {
    renderedTurns: rendered.renderedTurns,
    sending,
    awaitingTurnStart: chatId ? awaitingTurnStart(storeTurns) : creation.creatingBusy,
    loadingChat,
    loadFailed,
    retryLoad,
    history,
    // The shell's own opening asks, where it has one: an editor reader is
    // offered its codebase, a browser reader their data (see ChatEmptyState).
    suggestedPrompts: chatHost().emptyState?.prompts ?? DEFAULT_PROMPTS,
    submitMessage,
    // A chat that does not exist yet has nowhere to hold a message against, so
    // it offers no queue and the composer keeps the words in the field.
    queued: chatId ? (entry?.queued ?? []) : [],
    // A chat that does not exist yet has no running turn to be behind.
    queuedBehindTurn: Boolean(chatId && entry?.queuedBehind),
    queueMessage: chatId ? queueMessage : undefined,
    editQueued: chatId ? editQueued : undefined,
    removeQueued: chatId ? removeQueued : undefined,
    sendQueued: chatId ? sendQueued : undefined,
    startChat,
    cancelTurn,
    stopping,
    cancelNotice,
    dismissCancelNotice,
    // Two things hand words back to the field: a create that never opened a
    // chat, and a send the limiter refused inside one. The newer stamp wins, so
    // whichever refusal happened last is the one the composer adopts.
    returnedDraft: newerDraft(creation.returnedDraft, entry?.returnedDraft ?? null),
    ...stale,
  };
}

/** How many times a transcript that failed to load is read again on its own. */
export const LOAD_RETRIES = 5;

/** Read a transcript that failed to load again: on a climbing wait, and at
 *  once when the browser comes back online or the event stream recovers. A
 *  chat nobody is typing in gets no frames, so without this a read that met a
 *  passing 503 stayed failed until the reader reloaded the page. */
function useLoadRetry(chatId: string | null, failed: boolean, retry: () => void): void {
  const attempts = useRef(0);
  const streamDown = useRealtimeDown();
  const wasDown = useRef(streamDown);
  useEffect(() => {
    attempts.current = 0;
  }, [chatId]);
  useEffect(() => {
    if (!failed) {
      attempts.current = 0;
      return;
    }
    const unfollow = onBrowserOnline({ online: () => retry() });
    let timer: ReturnType<typeof setTimeout> | null = null;
    if (attempts.current < LOAD_RETRIES) {
      timer = setTimeout(() => {
        attempts.current += 1;
        retry();
      }, queryRetryDelay(attempts.current, null));
    }
    return () => {
      unfollow();
      if (timer !== null) clearTimeout(timer);
    };
  }, [failed, retry]);
  useEffect(() => {
    const recovered = wasDown.current && !streamDown;
    wasDown.current = streamDown;
    if (recovered && failed) retry();
  }, [streamDown, failed, retry]);
}

/** The pages above the window, as the source reports them. The source's word
 *  is a map lookup, re-read on every render — and the store moves the surface
 *  whenever a page lands (a `history_loaded` reconcile), so the sentinel is
 *  never a render behind. `loading` is also held locally so the sentinel reads
 *  busy in the very render the request leaves in. A source without paging
 *  reads as nothing older. */
export function useTranscriptHistory(chatId: string | null): TranscriptHistory {
  const [busy, setBusy] = useState(false);
  // How many pages in a row have failed, and whether the tape should stop
  // asking. The source re-sets `hasOlder` on a failure, and the sentinel that
  // reads it re-arms the moment the request settles, so without a ladder here
  // a refusing server is asked once per round trip for as long as the tab is
  // open. `gone` is the settled answer: a chat that is not there has no pages.
  const attempts = useRef(0);
  const [failed, setFailed] = useState(false);
  const [gone, setGone] = useState(false);
  const retryTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const source = chatData();
  const reported = chatId && source.transcriptHistory ? source.transcriptHistory(chatId) : null;

  // A different chat starts from a clean ladder, and a pending retry from the
  // one before it must not fire against it.
  useEffect(() => {
    attempts.current = 0;
    setFailed(false);
    setGone(false);
  }, [chatId]);
  useEffect(
    () => () => {
      if (retryTimer.current !== null) clearTimeout(retryTimer.current);
      retryTimer.current = null;
    },
    [chatId],
  );

  const loadOlder = useCallback(() => {
    if (!chatId || !source.loadOlderTurns || busy || gone) return;
    if (retryTimer.current !== null) {
      clearTimeout(retryTimer.current);
      retryTimer.current = null;
    }
    // A call that arrives while the tape is still saying it failed is the
    // reader pressing Retry — the ladder's own wait clears that flag before it
    // asks again — and a person asking puts the ladder back at its first rung.
    if (failed) attempts.current = 0;
    setBusy(true);
    setFailed(false);
    const step = (): void => {
      const n = attempts.current;
      attempts.current = n + 1;
      setFailed(true);
      // Past the ladder nobody is watching a page that will not come: the
      // retry row stays and the reader's own press is what asks again.
      if (n + 1 < HISTORY_RETRY_ATTEMPTS) {
        retryTimer.current = setTimeout(() => {
          retryTimer.current = null;
          setFailed(false);
        }, historyRetryDelayMs(n));
      }
    };
    void source
      .loadOlderTurns(chatId)
      .then((moved) => {
        // A page that RESOLVES without bringing anything is the same standstill
        // as one that refused: left as a success it re-arms the sentinel on the
        // next render and the tape asks again, and again, with nothing to catch
        // it. It climbs the same ladder.
        if (moved === false) {
          step();
          return;
        }
        attempts.current = 0;
        setFailed(false);
      })
      .catch((err: unknown) => {
        // The chat itself is gone: there is no page above it to come back for,
        // so the tape stops asking rather than backing off towards a 404.
        const status = err instanceof ApiError ? err.status : 0;
        if (status === 404 || status === 410) {
          setGone(true);
          return;
        }
        step();
      })
      .finally(() => setBusy(false));
  }, [chatId, source, busy, gone, failed]);

  const release = useCallback(() => {
    if (chatId) source.releaseOlderTurns?.(chatId);
  }, [chatId, source]);

  return {
    hasOlder: !gone && (reported?.hasOlder ?? false),
    loading: busy || (reported?.loading ?? false),
    loadOlder,
    release,
    failed: failed && !busy,
  };
}

/** The wait before retry `attempt` (0-based): the floor doubled per attempt up
 *  to the cap, plus a jitter inside one floor so a fleet of tabs that lost the
 *  same server does not come back in step. */
export function historyRetryDelayMs(
  attempt: number,
  jitter01: number = Math.random(),
): number {
  const rung = Number.isFinite(attempt) ? Math.max(0, Math.floor(attempt)) : 0;
  const exponential = ladderDelay(rung, HISTORY_RETRY);
  const clamped = Number.isFinite(jitter01) ? Math.min(Math.max(jitter01, 0), 0.999_999) : 0;
  return exponential + clamped * HISTORY_RETRY_FLOOR_MS;
}

/** Per-chat lifecycle housekeeping around the watchdog's state. */
function useTurnResets({
  chatId,
  entry,
  clearOptimistic,
  setStalled,
  turnInitiatedRef,
}: {
  chatId: string | null;
  entry: { sending?: boolean; base?: ConversationTurn[] } | undefined;
  clearOptimistic: () => void;
  setStalled: (stalled: StallCause | null) => void;
  turnInitiatedRef: MutableRefObject<boolean>;
}): void {
  // Reset per-chat UI state only on an ACTUAL chat switch. Comparing the previous
  // chatId (not a mount-skip flag) keeps a handoff's seeded optimistic on mount
  // and is idempotent under StrictMode's dev double-invoke.
  const prevChatIdRef = useRef(chatId);
  useEffect(() => {
    if (prevChatIdRef.current === chatId) return;
    prevChatIdRef.current = chatId;
    setStalled(null);
    clearOptimistic();
    turnInitiatedRef.current = false;
  }, [chatId, clearOptimistic, setStalled, turnInitiatedRef]);

  // Re-arm the watchdog when remounting into a turn that is still in flight
  // (navigated away mid-stream and back). `entry.sending` can only be true for
  // a turn THIS webview initiated — cold opens always reconcile it to false —
  // so the passively-opened-chat protection above is intact, and a turn whose
  // daemon died while we were away still gets its 60s stall instead of
  // spinning forever.
  useEffect(() => {
    if (entry?.sending) turnInitiatedRef.current = true;
  }, [entry?.sending, turnInitiatedRef]);

  // The stall banner retires itself the moment REAL daemon activity folds in
  // (reconnect, late completion, next turn) — it must never outlive the outage
  // it reported. Keyed on `base`'s array identity: a streamed event reconciles
  // a fresh array, while the watchdog's own cancel() keeps the previous one,
  // so raising the stall can't immediately clear it.
  const foldedBase = entry?.base;
  useEffect(() => {
    setStalled(null);
  }, [foldedBase, setStalled]);
}

/** The send path and its cancel: an existing chat goes through the store; no
 *  chat yet means the creation flow starts (or stops). */
function useSendControls({
  chatId,
  models,
  createNewChat,
  cancelCreating,
  turnInitiatedRef,
  setCancelNotice,
}: {
  chatId: string | null;
  models: ModelInfo[];
  createNewChat: (text: string, opts: SendOptions | undefined, into?: Chat) => Promise<void>;
  cancelCreating: () => void;
  turnInitiatedRef: MutableRefObject<boolean>;
  setCancelNotice: (notice: string | null) => void;
}) {
  const queryClient = useQueryClient();
  // A chat opened ahead of its first message, for the files that message
  // carries. The send takes it: the message is the first thing written into
  // it, and the surface hops onto it the way it hops onto a created chat.
  const openedRef = useRef<Chat | null>(null);
  const canOpen = typeof chatData().startChat === "function";
  const startChat = useMemo(
    () =>
      canOpen
        ? async (title: string, meta?: ComposerSendMeta): Promise<Chat> => {
            if (openedRef.current) return openedRef.current;
            const source = chatData();
            if (!source.startChat) throw new Error("this workspace cannot open a chat before its first message");
            const engine = servesModels(chatCaps()) && meta ? composerMetaToSendOptions(meta, models) : undefined;
            const chat = await source.startChat(title, engine);
            openedRef.current = chat;
            return chat;
          }
        : undefined,
    [canOpen, models],
  );
  // The picks a message carries.
  // Gated on the model CATALOGUE, not on driving a harness: the browser has
  // a catalogue and no harness, and gating on the harness dropped the
  // reader's model pick before it ever reached the CREATE — which is how a
  // chat started on Haiku came back pinned to nothing and labelled with the
  // workspace default.
  // The attachments ride every source: they are node ids the host already
  // linked, not an engine choice, so the cloud source carries them even
  // though it drives no harness of its own.
  const sendOptionsFor = useCallback(
    (meta?: ComposerSendMeta): SendOptions | undefined => {
      const engine =
        servesModels(chatCaps()) && meta ? composerMetaToSendOptions(meta, models) : undefined;
      const attachments = meta?.attachments?.length ? meta.attachments : undefined;
      return attachments ? { ...engine, attachments } : engine;
    },
    [models],
  );
  // The same picks, minus the mode: the daemon owns an existing chat's
  // permission mode. It persists in the manifest and rehydrates on open, so a
  // prompt carries no mode at all — a pill still catching up would otherwise
  // overwrite the real one.
  const turnOptionsFor = useCallback(
    (meta?: ComposerSendMeta): SendOptions | undefined => {
      const opts = sendOptionsFor(meta);
      return opts
        ? { model: opts.model, effort: opts.effort, attachments: opts.attachments }
        : undefined;
    },
    [sendOptionsFor],
  );
  const submitMessage = useCallback((text: string, meta?: ComposerSendMeta): void => {
    // Slash commands never reach here — the composer routes them to their panel
    // (see the slash-command registry). This is only a prompt to the agent.
    if (!chatId) {
      const into = openedRef.current ?? undefined;
      openedRef.current = null;
      return void createNewChat(text, sendOptionsFor(meta), into);
    }
    turnInitiatedRef.current = true;
    useChatStore.getState().send(chatId, text, turnOptionsFor(meta));
    // The send bumps the chat's updatedAt on the daemon — refresh the list so
    // the freshness ("…h ago") every surface shows isn't stale on return.
    void queryClient.invalidateQueries({ queryKey: chatKeys.chats() });
  }, [chatId, sendOptionsFor, turnOptionsFor, createNewChat, queryClient, turnInitiatedRef]);

  // Holding a message is the same send, later: it is recorded against the chat
  // under the picks it was typed with, and the store fires it through the very
  // same path the moment the turn ends.
  const queueMessage = useCallback(
    (text: string, meta?: ComposerSendMeta): void => {
      if (!chatId) return;
      turnInitiatedRef.current = true;
      useChatStore.getState().queueMessage(chatId, text, turnOptionsFor(meta));
    },
    [chatId, turnOptionsFor, turnInitiatedRef],
  );
  const editQueued = useCallback(
    (id: string, text: string): void => {
      if (chatId) useChatStore.getState().editQueued(chatId, id, text);
    },
    [chatId],
  );
  const removeQueued = useCallback(
    (id: string): void => {
      if (chatId) useChatStore.getState().removeQueued(chatId, id);
    },
    [chatId],
  );
  const sendQueued = useCallback(
    (id: string): void => {
      if (!chatId) return;
      turnInitiatedRef.current = true;
      useChatStore.getState().sendQueued(chatId, id);
    },
    [chatId, turnInitiatedRef],
  );

  // Stopping is the SOURCE's verb, the way answering an ask is: the editor
  // cancels on the daemon's engine channel, the portal has to reach the machine
  // running the turn (the browser host has no engine channel). The source
  // reports what it managed, and what it could not do is said out loud.
  //
  // A refusal is read the way a refused send is: the stop route is gated on
  // send, so a reader watching a colleague's live turn is told the rung they
  // are on rather than the transport's own sentence about a status code.
  //
  // One press is one stop. The round trip takes long enough to read as nothing
  // having happened, so a reader who is shown nothing presses again — an owner
  // stopped the same turn three times in nine seconds that way. The key states
  // the stop it is waiting on, and the press that lands before that state is
  // painted is refused here too: the ref is read by the click, the state by the
  // render. Either end of the round trip frees it, a refused stop included —
  // a key stuck at stopping would leave the turn unstoppable.
  const [stopping, setStopping] = useState(false);
  const stoppingRef = useRef(false);
  const settleStop = useCallback((): void => {
    stoppingRef.current = false;
    setStopping(false);
  }, []);
  // Another chat's stop is not this one's: moving between them starts clean.
  useEffect(() => settleStop, [chatId, settleStop]);
  const cancelTurn = useCallback((): void => {
    if (stoppingRef.current) return;
    if (chatId) {
      stoppingRef.current = true;
      setStopping(true);
      useChatStore.getState().cancel(chatId);
      void chatData()
        .cancelTurn(chatId)
        .then(
          (outcome) => setCancelNotice(outcome.stopped ? null : outcome.reason ?? null),
          (err: unknown) => setCancelNotice(isSendForbidden(err) ? SEND_FORBIDDEN : errorText(err)),
        )
        .finally(settleStop);
    } else {
      cancelCreating();
    }
    turnInitiatedRef.current = false;
  }, [chatId, cancelCreating, setCancelNotice, settleStop, turnInitiatedRef]);

  return { submitMessage, startChat, cancelTurn, stopping, queueMessage, editQueued, removeQueued, sendQueued };
}

type ReturnedDraft = { text: string; at: number } | null;

/** The more recent of two drafts handed back to the composer. */
function newerDraft(a: ReturnedDraft, b: ReturnedDraft): ReturnedDraft {
  if (a === null) return b;
  if (b === null) return a;
  return b.at > a.at ? b : a;
}

/** The new-chat creation flow (the handoff path only — the store owns
 *  everything once a real chat id exists). The optimistic user turn and the
 *  working indicator are seeded synchronously so the FIRST paint shows the
 *  message with no flash of the empty screen. */
function useChatCreation({
  handoff,
  endCreating,
  clearStall,
}: {
  handoff: ChatHandoff | null;
  endCreating: () => void;
  clearStall: () => void;
}) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const claimDraftPrefs = useChatStore((state) => state.claimDraftPrefs);
  const holdPendingTurns = useChatStore((state) => state.holdPendingTurns);
  const setComposerPref = useChatStore((state) => state.setComposerPref);
  const [creatingBusy, setCreatingBusy] = useState(() => Boolean(handoff?.pendingMessage));
  const [optimisticTurns, setOptimisticTurns] = useState<ConversationTurn[]>(
    () => (handoff?.pendingMessage ? [optimisticUserTurn(handoff.pendingMessage)] : []),
  );
  // What a refused create hands back to the composer, under the stamp the
  // composer adopts a draft by.
  const [returnedDraft, setReturnedDraft] = useState<{ text: string; at: number } | null>(null);

  // Start a NEW chat: the optimistic user turn + indicator go up immediately
  // while createChat opens the harness (~2s) in the background; once it
  // resolves we navigate to the real chat, where the store takes over.
  const createNewChat = useCallback(async (text: string, opts: SendOptions | undefined, into?: Chat): Promise<void> => {
    setCreatingBusy(true);
    clearStall();
    // Minted once and kept: the SAME turn is what this surface shows while the
    // chat is being created and what the created chat opens holding, so the
    // reader watches one bubble rather than one that blinks out on the hop.
    const bubble = optimisticUserTurn(text);
    setOptimisticTurns([bubble]);
    try {
      // A chat already opened for this message's files takes the message as
      // its first; any other first message opens its chat by being sent.
      let chat: Chat;
      if (into) {
        await chatData().sendUserMessage(into.id, text, opts);
        chat = into;
      } else {
        chat = await chatData().createChat(text, opts);
      }
      seedChatListCache(queryClient, chat);
      // Hand the bubble to the chat BEFORE the route changes: the new route
      // mounts a fresh surface whose store entry is otherwise empty until the
      // box publishes the message back, which is the wait this closes.
      holdPendingTurns(chat.id, [bubble]);
      endCreating();
      claimDraftPrefs(chat.id);
      navigate(`/chat/${chat.id}`, { replace: true });
      // The surface is the same instance on both sides of that hop (the
      // portal keeps it mounted — see `useSurfaceKey`), so the creation flag
      // has to end here rather than die with a remount: the store owns
      // `sending` from now on. In the SAME transition as the navigation: the
      // router commits the new route as a transition, and a flag cleared
      // synchronously landed a frame earlier — one frame of the old route
      // with no working line under the bubble.
      startTransition(() => setCreatingBusy(false));
      // Deliberately NOT awaited before the navigation: the list was just
      // seeded with the created chat, so a refetch buys nothing the reader can
      // see and costs them a whole round trip of staring at the composer they
      // already sent from.
      void queryClient.invalidateQueries({ queryKey: chatKeys.chats() });
    } catch (err) {
      console.debug("[chat] new chat failed:", err);
      setCreatingBusy(false);
      setOptimisticTurns((prev) => [...prev, createErrorTurn(err)]);
      // The chat was never started, so the words are handed back rather than
      // left only inside a bubble the reader cannot edit or re-send. The stamp
      // is forced forward so two refusals in the same millisecond still each
      // reach the composer.
      setReturnedDraft((prev) => ({ text, at: Math.max(Date.now(), (prev?.at ?? 0) + 1) }));
    }
  }, [navigate, queryClient, claimDraftPrefs, endCreating, clearStall, holdPendingTurns]);

  // The handoff carries the first message: kick off createChat ONCE (the ref
  // is the single guard — StrictMode's dev double-invoke must not create two
  // chats). The composer pill follows the handed-off MODE immediately — the
  // daemon applies it on create, and without the seed the pill read "default"
  // and the next send would have flipped the session back.
  const handoffSentRef = useRef(false);
  useEffect(() => {
    if (handoffSentRef.current || !handoff?.pendingMessage) return;
    handoffSentRef.current = true;
    if (handoff.pendingOptions?.mode) setComposerPref(DRAFT_CHAT_KEY, { mode: handoff.pendingOptions.mode });
    void createNewChat(handoff.pendingMessage, handoff.pendingOptions ?? undefined, handoff.into ?? undefined);
  }, [handoff, createNewChat, setComposerPref]);

  const cancelCreating = useCallback(() => setCreatingBusy(false), []);
  const clearOptimistic = useCallback(() => setOptimisticTurns([]), []);
  return { creatingBusy, optimisticTurns, returnedDraft, createNewChat, cancelCreating, clearOptimistic };
}

/** What the transcript shows: the folded turns under the local interrupt
 *  overlay, reasoning hidden at effort none, with the stall banner appended
 *  while the watchdog's error stands. */
function useRenderedTurns({
  chatId,
  storeTurns,
  optimisticTurns,
  interrupts,
  reasoningVisible,
  stalled,
  sendRefusal,
}: {
  chatId: string | null;
  storeTurns: ConversationTurn[];
  optimisticTurns: ConversationTurn[];
  interrupts: ChatInterrupts;
  reasoningVisible: boolean;
  stalled: StallCause | null;
  sendRefusal: string | null;
}) {
  const { resolvedPermissions, expiredPermissions, answeredQuestions, answeredNotes, rejectedQuestions } = interrupts;
  const liveTurns = useMemo(() => {
    const overlay = { resolvedPermissions, expiredPermissions, answeredQuestions, answeredNotes, rejectedQuestions };
    const turns = chatId ? applyLocalInterruptState(storeTurns, overlay) : optimisticTurns;
    return reasoningVisible ? turns : stripThinkingParts(turns);
  }, [chatId, optimisticTurns, storeTurns, resolvedPermissions, expiredPermissions, answeredQuestions, answeredNotes, rejectedQuestions, reasoningVisible]);

  const renderedTurns = useMemo(() => {
    if (sendRefusal) return [...liveTurns, sendRefusedTurn(sendRefusal)];
    return stalled ? [...liveTurns, stallTurnFor(stalled)] : liveTurns;
  }, [liveTurns, stalled, sendRefusal]);

  // A pending permission/question prompt means the daemon is deliberately
  // waiting on the HUMAN (timeout_seconds=None on its side) — the turn isn't
  // stalled, however long the user deliberates.
  const hasPendingInterrupt = useMemo(() => renderedTurns.some(turnAwaitsHuman), [renderedTurns]);

  return { renderedTurns, hasPendingInterrupt };
}

function turnAwaitsHuman(turn: ConversationTurn): boolean {
  return turn.parts.some(
    (part) => (part.kind === "permission" || part.kind === "question") && part.status === "pending",
  );
}

/** Watchdog: a turn we initiated that will never finish should say so once,
 *  rather than spin for ever OR be declared dead while it is still being
 *  answered.
 *
 *  It asks about LIVENESS, not silence. In order, on each tick:
 *
 *  1. the machine says the turn is `working` → alive, however quiet. A model
 *     step on a small box routinely emits nothing for minutes, and a browser
 *     that calls that a crash both lies and frees the composer — so the next
 *     question is sent and the real answer lands underneath it;
 *  2. the cloud shell stops here. A turn there may run for hours or days —
 *     through a warehouse query, a compaction, a subagent — and neither the
 *     silence nor the machine's status is evidence that it ended. The status is
 *     told in the banner above the transcript, which the reader can act on;
 *     cancelling the turn under it is a claim the browser cannot support, and
 *     it frees the composer, so the next question goes out under a turn that is
 *     still running and the real answer lands underneath it;
 *  3. the editor's silence floor, the only signal that shell has. */
function useTurnWatchdog({
  sending,
  hasPendingInterrupt,
  entry,
  creatingBusy,
  turnInitiatedRef,
  chatId,
  machineUnavailable,
  cancelCreating,
  setStalled,
}: {
  sending: boolean;
  hasPendingInterrupt: boolean;
  entry: unknown;
  creatingBusy: boolean;
  turnInitiatedRef: MutableRefObject<boolean>;
  chatId: string | null;
  machineUnavailable: boolean;
  cancelCreating: () => void;
  setStalled: (stalled: StallCause | null) => void;
}): void {
  const onStall = useCallback(
    (cause: StallCause): void => {
      setStalled(cause);
      if (chatId) useChatStore.getState().cancel(chatId);
      else cancelCreating();
    },
    [chatId, cancelCreating, setStalled],
  );
  // `lastActivityRef` refreshes on every store update (a streamed daemon
  // event), on the creation flow's start, and when a pending human prompt
  // resolves (so the countdown restarts from the user's answer).
  const lastActivityRef = useRef(0);
  useEffect(() => {
    lastActivityRef.current = Date.now();
  }, [entry, creatingBusy, hasPendingInterrupt]);
  useEffect(() => {
    if (!sending || hasPendingInterrupt) return;
    const check = (): void => {
      if (!turnInitiatedRef.current) return;
      // Read the machine's word at CHECK time rather than subscribing: the turn
      // state rides the document's meta lane, which raises no transcript event,
      // and this timer is the only reader that needs it.
      if (chatId && chatData().turnState?.(chatId) === "working") return;
      if (chatHost().kind === "browser") return;
      if (Date.now() - lastActivityRef.current <= STALL_MS) return;
      turnInitiatedRef.current = false;
      onStall(machineUnavailable ? "workspace" : "backend");
    };
    const timer = setInterval(check, 5_000);
    return () => clearInterval(timer);
  }, [sending, hasPendingInterrupt, machineUnavailable, chatId, onStall, turnInitiatedRef]);
}

/** The reopen prompt for a send the daemon refused (SESSION_NOT_OPEN). The
 *  entry's slot holds the refusal record; only its presence matters here. */
function useStaleSend(chatId: string | null, staleSend: unknown) {
  const reopenStaleChat = useCallback((): void => {
    if (chatId) useChatStore.getState().reopenAndResend(chatId);
  }, [chatId]);
  const dismissStaleSend = useCallback((): void => {
    if (chatId) useChatStore.getState().dismissStaleSend(chatId);
  }, [chatId]);
  return { staleSend: Boolean(chatId && staleSend), reopenStaleChat, dismissStaleSend };
}
