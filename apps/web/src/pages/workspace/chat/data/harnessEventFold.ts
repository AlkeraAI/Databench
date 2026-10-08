import type {
  BlobReference,
  ConversationPart,
  ConversationTurn,
  PermissionAffectedView,
  PermissionConceptView,
  PermissionConversationPart,
  PermissionEffect,
  PermissionImpactView,
  PermissionNotebookView,
  PermissionSubjectView,
  QuestionConversationPart,
  CompactionConversationPart,
  ResourcePreview,
  ResourceReference,
  SubagentActivity,
  SubagentActivityTool,
  ToolConversationPart,
  ToolState,
} from "@alkera/chat-model";
import { modeChangePresentation, modelChangePresentation, readableToolError } from "@alkera/chat-model";
import { toolSettled } from "@alkera/chat-model";
import { FAILURE_SENTENCES, namedRefusal, type RefusalFacts } from "./refusals";
import type { CommandCardData } from "./commandCard";

export type HarnessEvent = Record<string, unknown>;
type TextishConversationPart =
  | Extract<ConversationPart, { kind: "text" }>
  | Extract<ConversationPart, { kind: "thinking" }>;

export interface ConversationFoldState {
  turns: ConversationTurn[];
  messageToTurn: Map<string, string>;
  partToTurn: Map<string, string>;
  toolToPart: Map<string, string>;
  requestToPart: Map<string, string>;
  currentCompactionPartId: string | null;
  /** Conversation tokens the model last reported carrying — input plus both
   *  cache halves, which is the figure a context window is spent against and
   *  the one the harness's own overflow check reads. Null until a turn reports
   *  usage. */
  contextTokens: number | null;
  /** A settled compaction whose "after" figure is still unknown: it is only
   *  knowable once the next reply reports what it carried. Holds that part's
   *  id until one does. */
  compactionAwaitingAfter: string | null;
  currentTurnId: string | null;
  currentAssistantTurnId: string | null;
  pendingAssistantStartedAt: string | null;
  touchedFiles: Map<string, ResourceReference>;
  /** Tool-call ids of the question tool ("question"), detected by name when
   *  the call is folded. `tool.call_update` events carry no name, so this lets
   *  the update path also skip the suppressed question tool card. */
  suppressedToolCallIds: Set<string>;
  /** Part ids of synthetic / ignored text parts — the harness's in-conversation
   *  steering (our plan-mode reminder, opencode's "plan approved" nudge). The
   *  flag only lands on the finalized part, but the live `part.started` +
   *  delta-chunk events stream the same part_id first; tracking the id here lets
   *  every text path (started / chunk / created) skip it so it never flashes. */
  suppressedSyntheticPartIds: Set<string>;
  /** Whether an error has already been surfaced for the current exchange. Lets
   *  a process-exit (rc=-15) crash that follows a real error (e.g. a refusal)
   *  be dropped as a downstream artifact, while a crash with no prior error
   *  still surfaces one polished message. Reset on each new user prompt. */
  sawTurnError: boolean;
  /** Early `SubagentStarted` pointers whose `spawn_agent` tool call hasn't been
   *  folded yet (the two events can race on the bus). Held here and bound to the
   *  matching spawn part when its `tool.call` arrives — so child-id binding is
   *  order-independent. Matched + drained by (agent, prompt). */
  pendingSubagentStarts: PendingSubagentStart[];
  /** Edit diffs the adapter captured AT ASK TIME and carried on
   *  `permission.request`, keyed by request id. The `file.edited` holding the
   *  same diff only lands AFTER the write, so this is the only copy that exists
   *  while the approver is deciding — see {@link captureAskTimeEditPreview}. */
  askTimeEditPreviews: Map<string, AskTimeEditPreview>;
  /** Asks still waiting to learn what they gate, by request id. A harness can
   *  publish the ask before the part carrying the call's arguments, and not
   *  every mirror raises the ask a second time once it knows — so the subject
   *  is recovered from the call's own part instead. Dropped once the subject
   *  lands or the ask resolves. */
  asksAwaitingCall: Map<string, AskAwaitingCall>;
  /** Prompts the SERVER took, folded from the transcript rather than from
   *  anything the machine said, keyed by turn id and holding the person's
   *  exact words. A prompt is durable the moment the server accepts it, so it
   *  stands in the transcript from then on; the box's later echo of the same
   *  words merges INTO that turn rather than adding a second one. Entries are
   *  never dropped — a late echo must still find its prompt. */
  promptTurns: Map<string, string>;
  /** A person's message the box has announced (`message.created`, role
   *  `user`) but not yet given a part: its start time, keyed by message id.
   *  The turn opens when the first part lands — a bare announcement is also
   *  what the box repeats for an old message every time the session moves on,
   *  and a page starting below the original must not read the repeat as a
   *  new, empty message. */
  announcedUserMessages: Map<string, string | undefined>;
  /** Text / reasoning parts the publisher has FINALIZED. The per-token deltas
   *  ride a lane of their own, so one that arrives after the finished part —
   *  a trailing chunk, a reconnect's replay, a page read mid-turn — would
   *  append the whole thought onto the end of itself. The final text is
   *  authoritative; a delta for a part already settled is stale. */
  settledTextPartIds: Set<string>;
  /** Asks whose resolution has been folded but whose request has not: the
   *  answer to a question, the option a permission was settled on. A request
   *  that lands later — a re-announce, a page read out of order — is created
   *  CLOSED from this, never as a card. An ask the transcript has already
   *  resolved cannot be answered again: the server refuses with 409, so a
   *  card for it would be one nobody could clear. Keyed by request id. */
  settledAsks: Map<string, SettledAsk>;
  /** Synthetic text parts whose message has not been announced yet, by message
   *  id. A daemon notice (a Stop note) is a synthetic part on a `system`
   *  message, and the model's steering injections are synthetic parts on an
   *  assistant one: which it is only the message's own row says, so a part that
   *  lands first waits here for it instead of being dropped as steering. */
  pendingSystemNotices: Map<string, Array<{ partId: string; text: string }>>;
  /** Prompts the box passed over: still waiting for an echo when a later
   *  message got one. No echo is ever theirs, and they never stand in for a
   *  later saying of the same words. */
  passedOverPrompts: Set<string>;
  /** Whether the box is working a turn: `running` seen, and no `idle`,
   *  `error` or `aborted` since. */
  sessionWorking: boolean;
  /** Messages said while the box was working a turn and not yet echoed. They
   *  wait behind that turn, so the rest of it (its next message, its closing
   *  reply) stands above them, as it does in the log the box writes. */
  queuedBehindTurn: Set<string>;
  /** The transcript sequence of the row being folded, when the source knows
   *  it (`setFoldingSeq`); null for a frame that carries none. */
  foldingSeq: number | null;
  /** Where in the transcript a person's turn stands, by turn id: the sequence
   *  of the prompt row for a message as the server recorded it, of the box's
   *  first row for an echo. It is what ties an echo to the saying it answers
   *  when two sayings share their words — the words alone cannot. */
  turnSeq: Map<string, number>;
  /** The transport attempt the session last reported running, as its status
   *  stamps it (`turn_id`); null until one has. */
  attemptId: string | null;
  /** The attempt each OPEN ask was raised under, by request id — so a
   *  terminal stamped with a different attempt leaves it alone, as the
   *  runtime does. An ask raised while no attempt was known has no entry and
   *  is closed by any idle. Dropped once the ask closes. */
  askAttempts: Map<string, string>;
  /** Where a text / reasoning part belongs in its turn, taken at the moment
   *  `part.started` named it. A reasoning part opens carrying no text at all,
   *  so nothing can be shown for it yet — it is built later, out of a token
   *  frame or out of the finalized `part.created`. Token frames ride a
   *  coalescing timer the durable lane does not, so on the cloud lane they
   *  land AFTER the tool call the model ran next, and appending the thought
   *  then files it below work that happened after it. The slot holds the part
   *  that was last in the turn when this one opened; the part is spliced in
   *  behind it instead. Dropped as soon as the part is placed. */
  reservedPartSlots: Map<string, { turnId: string; afterPartId: string | null }>;
  /** The order each textish part was opened in, so two thoughts whose words
   *  arrive out of order still read in the order they were thought. */
  textPartOrder: Map<string, number>;
  nextTextPartOrder: number;
  /** Textish parts opened and not yet closed by anything after them. */
  openTextPartIds: Set<string>;
  /** Textish parts a LATER part has closed. opencode opens its parts in order,
   *  so a part that is no longer the open one is finished whatever lane its
   *  closing frame is still riding — and a coalesced token frame arriving
   *  after that must not re-light it. */
  closedTextPartIds: Set<string>;
  /** Text and thought parts whose recorded row holds no words: opened by a
   *  `part.started` with empty text, or by a token frame with no row at all.
   *  Whatever they show came as token frames, which are never written down,
   *  until the `part.created` that records them lands — which clears this. */
  wordlessPartIds: Set<string>;
  /** Turn ids changed since the last {@link takeDirtyTurnIds}, or null for
   *  "all of them" — the default, and what any path that cannot name what it
   *  touched leaves behind. A snapshot of this state copies the named turns
   *  and carries the rest over by identity, so a streamed token costs one
   *  turn's copy rather than the loaded window's. */
  dirtyTurnIds: Set<string> | null;
  /** The notice standing for a model call the box is retrying, by the id of
   *  the system turn that carries it; null while no call is being retried. */
  retryNoticeTurnId: string | null;
  /** Where the agent whose events these are actually runs. The editor's daemon
   *  runs it on the reader's own machine; the portal's runs it on a workspace
   *  machine the reader does not own and cannot restart. The two need different
   *  words for the same crash — see {@link polishedErrorText}. */
  agentHost: AgentHost;
}

/** Whose machine the agent runs on, as the shell that folds these events knows
 *  it: `local` for the editor's daemon, `workspace` for the org's box. */
export type AgentHost = "local" | "workspace";

interface PendingSubagentStart {
  childSessionId: string;
  agent: string;
  prompt: string;
}

type SettledAsk =
  | {
      kind: "permission";
      optionId: string | undefined;
      decidedByName: string | undefined;
      decidedBy?: string;
      decidedVia?: string;
    }
  | { kind: "question"; status: "answered"; answers: string[][]; note: string | null | undefined }
  | { kind: "question"; status: "rejected"; reason: string | null | undefined };

interface AskTimeEditPreview {
  /** The file the ask would write. Carried ON the preview by the adapter
   *  (`_file_diff.diff_preview`) because a harness's tool-call id and the card's
   *  part id live in different id spaces — the path is the reliable key. */
  path: string | null;
  toolCallId: string | null;
  preview: ResourcePreview;
  diff?: { insertions: number; deletions: number };
  /** The tool part this diff was attached to, once one exists. */
  attachedToPartId: string | null;
}

/** A pending ask that named the call it gates but not what that call does. The
 *  call's own part carries the command or the path, so the card's subject is
 *  taken from there the moment the part lands. */
interface AskAwaitingCall {
  /** The permission part to write the recovered subject onto. */
  partId: string;
  /** Every id the ask gave for the gated call — a harness's own call id and the
   *  provider's can differ, and only one of them keys the transcript's part. */
  callIds: string[];
}

export function createConversationFoldState(
  opts: { agentHost?: AgentHost } = {},
): ConversationFoldState {
  return {
    agentHost: opts.agentHost ?? "local",
    turns: [],
    messageToTurn: new Map(),
    partToTurn: new Map(),
    toolToPart: new Map(),
    requestToPart: new Map(),
    currentCompactionPartId: null,
    contextTokens: null,
    compactionAwaitingAfter: null,
    currentTurnId: null,
    currentAssistantTurnId: null,
    pendingAssistantStartedAt: null,
    touchedFiles: new Map(),
    suppressedToolCallIds: new Set(),
    suppressedSyntheticPartIds: new Set(),
    sawTurnError: false,
    pendingSubagentStarts: [],
    askTimeEditPreviews: new Map(),
    asksAwaitingCall: new Map(),
    promptTurns: new Map(),
    sessionWorking: false,
    queuedBehindTurn: new Set(),
    announcedUserMessages: new Map(),
    settledTextPartIds: new Set(),
    settledAsks: new Map(),
    pendingSystemNotices: new Map(),
    passedOverPrompts: new Set(),
    foldingSeq: null,
    turnSeq: new Map(),
    attemptId: null,
    askAttempts: new Map(),
    reservedPartSlots: new Map(),
    textPartOrder: new Map(),
    nextTextPartOrder: 0,
    openTextPartIds: new Set(),
    closedTextPartIds: new Set(),
    wordlessPartIds: new Set(),
    dirtyTurnIds: null,
    retryNoticeTurnId: null,
  };
}

/** The order a textish part takes its place in — assigned the first time the
 *  fold hears of it, whichever lane brought the news. */
function textPartOrderOf(state: ConversationFoldState, partId: string): number {
  const known = state.textPartOrder.get(partId);
  if (known !== undefined) return known;
  const order = state.nextTextPartOrder;
  state.nextTextPartOrder += 1;
  state.textPartOrder.set(partId, order);
  return order;
}

/** Put a text / reasoning part where `part.started` said it goes. With no slot
 *  — a part whose open was never seen, or one that opened in another turn —
 *  the end of the turn is still the best guess. */
function placeTextishPart(
  state: ConversationFoldState,
  turn: ConversationTurn,
  part: TextishConversationPart,
): void {
  const slot = state.reservedPartSlots.get(part.id);
  state.reservedPartSlots.delete(part.id);
  if (!slot || slot.turnId !== turn.id) {
    turn.parts.push(part);
    return;
  }
  const anchor = slot.afterPartId;
  let at = 0;
  if (anchor !== null) {
    const found = turn.parts.findIndex((candidate) => candidate.id === anchor);
    // The anchor was removed under us (a suppressed synthetic, a deduped
    // prompt); its position is no longer knowable, so append.
    if (found < 0) {
      turn.parts.push(part);
      return;
    }
    at = found + 1;
  }
  // Parts that opened against the same anchor before this one keep their place
  // however late their own words arrived.
  const order = textPartOrderOf(state, part.id);
  while (at < turn.parts.length) {
    const sibling = state.textPartOrder.get(turn.parts[at].id);
    if (sibling === undefined || sibling > order) break;
    at += 1;
  }
  turn.parts.splice(at, 0, part);
}

/** Close every textish part the model has moved on from — it opened another
 *  thought, started the answer, or ran the tool it decided on. They are
 *  finished even while the frame that says so is still in flight on the
 *  durable lane, and a part not yet on screen is closed just as surely as one
 *  that is. Without this a thought keeps the in-progress "Thinking…" label
 *  under the work that came after it, for as long as the two lanes are apart. */
/** Close every open text or thought other than `openPartId`. `closerTurnId`
 *  is the turn of what closed them, or null when the turn itself is over.
 *
 *  A part whose recorded row holds no words — the box has not settled it, and
 *  what it shows reached this tab as token frames, which are never written
 *  down — is shown as settled prose only by its own `part.created`. While the
 *  box is still on the same message that row can still come, in any order
 *  (the coalescing lane hands a thought's words after the work that followed
 *  it), so a sibling closing it leaves it standing. Once the box has moved to
 *  a different message, or the turn is over, nothing will settle it: a fresh
 *  open shows nothing of it, and neither does this tab — what was never
 *  recorded is not shown as recorded. If the row does come later (a Stop raced
 *  it), it puts the words back in their place. A part whose row carried its
 *  words (the echo of a person's message, a re-announced part) is the
 *  record's, and stays. */
function closeOpenTextishParts(
  state: ConversationFoldState,
  openPartId: string | null,
  closerTurnId: string | null,
): void {
  for (const partId of state.openTextPartIds) {
    if (partId === openPartId) continue;
    state.openTextPartIds.delete(partId);
    state.closedTextPartIds.add(partId);
    const sameMessage = closerTurnId !== null && state.partToTurn.get(partId) === closerTurnId;
    if (state.wordlessPartIds.has(partId) && !sameMessage) {
      state.wordlessPartIds.delete(partId);
      dropUnsettledPart(state, partId);
    } else {
      const part = findPart(state, partId);
      if (part && (part.kind === "text" || part.kind === "thinking")) part.streaming = false;
    }
  }
}

/** Take a text or thought the box never settled off its turn. */
function dropUnsettledPart(state: ConversationFoldState, partId: string): void {
  const turnId = state.partToTurn.get(partId);
  const turn = turnId ? state.turns.find((candidate) => candidate.id === turnId) : undefined;
  if (!turn) return;
  const index = turn.parts.findIndex(
    (part) => part.id === partId && (part.kind === "text" || part.kind === "thinking"),
  );
  if (index < 0) return;
  turn.parts.splice(index, 1);
  state.partToTurn.delete(partId);
  markTurnDirty(state, turn.id);
}

export function cloneTurns(state: ConversationFoldState): ConversationTurn[] {
  return structuredClone(state.turns);
}

/** Every turn is changed as far as a reader of `dirtyTurnIds` is concerned.
 *  The default, and what any fold path that cannot name what it touched
 *  leaves behind: a snapshot then re-copies the lot, which is correct however
 *  the fold grows. */
export function markAllTurnsDirty(state: ConversationFoldState): void {
  state.dirtyTurnIds = null;
}

/** Exactly this turn changed — said only by a path that knows. */
function markTurnDirty(state: ConversationFoldState, turnId: string): void {
  if (state.dirtyTurnIds === null) {
    // Already standing at "all of them"; a narrower claim cannot shrink it.
    return;
  }
  state.dirtyTurnIds.add(turnId);
}

/** What has changed since the last read, and start counting again from
 *  nothing. Null means every turn. */
export function takeDirtyTurnIds(state: ConversationFoldState): ReadonlySet<string> | null {
  const dirty = state.dirtyTurnIds;
  state.dirtyTurnIds = new Set();
  return dirty;
}

/** Fold one daemon harness IR event into a UI-ready conversation state. */
export function foldHarnessEvent(state: ConversationFoldState, raw: HarnessEvent): void {
  const eventType = stringField(raw, "event_type");
  if (!eventType) return;

  // A token appended to a part already on screen changes one turn, and that
  // path names it. Everything else is read as changing all of them, so a fold
  // that grows a case is conservative until someone teaches it otherwise.
  if (eventType !== "agent.message_chunk" && eventType !== "agent.thought_chunk") {
    markAllTurnsDirty(state);
  }
  if (state.retryNoticeTurnId !== null && retryIsOver(eventType, raw)) clearRetryNotice(state);

  switch (eventType) {
    case "message.created":
      foldMessageCreated(state, raw);
      return;
    case "agent.message_chunk":
      appendTextChunk(state, raw, "text");
      return;
    case "agent.thought_chunk":
      appendTextChunk(state, raw, "thinking");
      return;
    case "part.started":
      foldPartStarted(state, raw);
      return;
    case "part.created":
      foldPartCreated(state, raw);
      return;
    case "tool.call":
      foldToolCall(state, raw);
      return;
    case "tool.call_update":
      foldToolUpdate(state, raw);
      return;
    case "permission.request":
      foldPermissionRequest(state, raw);
      return;
    case "permission.resolved":
      patchPermissionResolved(state, raw);
      return;
    case "question.request":
      foldQuestionRequest(state, raw);
      return;
    case "question.answered":
      patchQuestion(state, raw, "answered");
      return;
    case "question.rejected":
      patchQuestion(state, raw, "rejected");
      return;
    case "plan.updated":
      foldPlanUpdated(state, raw);
      return;
    case "file.edited":
      foldFileEdited(state, raw);
      return;
    case "subagent.started":
      // DATA-ONLY: don't render a card — bind the child id onto the originating
      // `spawn_agent` tool card so it can stream the running child + drill in.
      foldSubagentStarted(state, raw);
      return;
    // NB: subagent.completed is not emitted — the spawn's tool result already
    // carries the final {summary, stats, child_session_id}. A completed event
    // in an older chat's log falls through to the default below (no-op).
    case "session.next.compaction.started":
      foldCompactionStarted(state, raw);
      return;
    case "session.next.compaction.delta":
      patchCompactionDelta(state, raw);
      return;
    case "session.next.compaction.ended":
      patchCompactionEnded(state, raw);
      return;
    case "session.compacted":
    case "session.compaction":
    case "compaction.applied":
    case "compaction.created":
      foldCompaction(state, raw);
      return;
    case "prompt.cancelled":
      foldPromptCancelled(state, raw);
      return;
    case "turn.started":
      foldTurnStarted(state, raw);
      return;
    case "turn.finished":
      foldTurnFinished(state, raw);
      return;
    case "message.completed":
      foldMessageCompleted(state, raw);
      return;
    case "session.status_changed":
      foldStatusChanged(state, raw);
      return;
    case "retrying":
      foldRetrying(state, raw);
      return;
    case "session.created":
    case "session.updated":
    case "session.diff":
      return;
    case "conversation.cleared":
      // Keep the pre-clear turns VISIBLE but dimmed (settled, so no dead
      // interrupt or spinner survives) rather than deleting them — the same
      // treatment compaction gives elided turns (see dimCompactedTurns). A
      // remount replays this event from disk: deleting here showed an empty
      // "start a chat" screen on every reopen even though the live view had the
      // greyed scrollback. The live-tracking maps reset so post-clear turns
      // start a fresh conversation on top of the dimmed history.
      settleStaleInterrupts(state);
      for (const turn of state.turns) turn.cleared = true;
      state.messageToTurn.clear();
      state.partToTurn.clear();
      state.toolToPart.clear();
      state.requestToPart.clear();
      state.currentCompactionPartId = null;
      state.pendingAssistantStartedAt = null;
      state.touchedFiles.clear();
      state.suppressedToolCallIds.clear();
      state.suppressedSyntheticPartIds.clear();
      state.settledTextPartIds.clear();
      state.settledAsks.clear();
      state.pendingSystemNotices.clear();
      state.passedOverPrompts.clear();
      state.turnSeq.clear();
      state.askAttempts.clear();
      state.asksAwaitingCall.clear();
      state.sawTurnError = false;
      state.retryNoticeTurnId = null;
      // Drop any buffered SubagentStarts whose spawn never folded — else a stale
      // one could bind to a same-brief spawn in the post-clear conversation.
      state.pendingSubagentStarts = [];
      // The bright boundary card, persisted via the SAME event so it survives
      // reopen. A later /clear dims it too (it's just another turn above),
      // reproducing the multi-clear stacking. `/clear` therefore emits no
      // separate command.result — its card lives here.
      addCommandTurn(state, {
        command: "clear",
        label: "Conversation cleared",
        detail: "The next turn starts fresh.",
      });
      return;
    case "command.executed":
      addSystemTurn(state, `/${stringField(raw, "name") ?? "command"} executed.`);
      return;
    case "mode.changed": {
      // Written by the server when a person changes the mode, on the web or in
      // Slack: the same card both surfaces render, from the shared words.
      const mode = stringField(raw, "mode");
      if (!mode) return;
      const shown = modeChangePresentation(
        mode,
        stringField(raw, "previous_mode"),
        stringField(raw, "decided_by_name"),
        stringField(raw, "decided_via"),
      );
      addSystemTurn(state, shown.title, "info", `${shown.change} · ${shown.attribution}`);
      return;
    }
    case "model.changed": {
      // Written by the server when a person moves the chat to another model or
      // effort: the mode card's shape, so the two read alike in the transcript.
      const modelId = stringField(raw, "model_id");
      if (!modelId) return;
      const shown = modelChangePresentation({
        modelId,
        displayName: stringField(raw, "display_name"),
        effort: stringField(raw, "effort"),
        previousModelId: stringField(raw, "previous_model_id"),
        previousDisplayName: stringField(raw, "previous_display_name"),
        previousEffort: stringField(raw, "previous_effort"),
        changerName: stringField(raw, "decided_by_name"),
        surface: stringField(raw, "decided_via"),
      });
      addSystemTurn(state, shown.title, "info", `${shown.change} · ${shown.attribution}`);
      return;
    }
    case "revert.applied":
      // A revert cursor: everything AFTER `to_message_id` (and `to_part_id` if
      // set) is inactive. The daemon excludes it from the model's context, so
      // the transcript must drop it too — otherwise the webview shows turns the
      // model no longer sees. Non-anchor (unknown id) → no-op (forward-compat).
      applyRevert(state, stringField(raw, "to_message_id"), stringField(raw, "to_part_id"));
      return;
    case "tombstone.applied":
      // Specific events marked ignored (PII redaction, post-revert cleanup).
      // Drop the matching parts/turns so they leave the active transcript.
      applyTombstone(state, stringArrayField(raw, "event_ids"));
      return;
    // `command.result` is not emitted: slash results render in the
    // composer's transient panel, not the transcript. Any such event in an
    // older chat's log falls through to the default no-op.
    default:
      if (eventType.startsWith("session.next.")) return;
      return;
  }
}

/** A prompt the server has taken, folded from the transcript itself.
 *
 *  A person's message is durable the moment the server accepts it — it is a row
 *  of the transcript before any machine has seen it — so their own words belong
 *  on screen from that moment and stay there across a reload, whether or not a
 *  box ever picks the turn up. Until one does, the turn reads as still going
 *  out (`status: "running"`); the box's echo of the same words settles it in
 *  place rather than landing as a second copy of the message.
 *
 *  `id` is the prompt's own transcript id, so folding the same row twice — the
 *  send's own response and then the page a reload reads — is one turn. */
export function foldRelayedPrompt(
  state: ConversationFoldState,
  prompt: {
    id: string;
    text: string;
    at?: string;
    /** The sequence the server recorded the message at, when the teller knows
     *  it: what ties the message to ITS echo among sayings of the same words. */
    seq?: number | null;
    fromTemplate?: string;
    fromTemplateAuthor?: string;
  },
  opts: { taken?: boolean } = {},
): void {
  markAllTurnsDirty(state);
  const text = prompt.text;
  if (!prompt.id || !text.trim()) return;
  if (state.messageToTurn.has(prompt.id)) return;
  state.sawTurnError = false;
  const seq = prompt.seq ?? null;
  // The box's echo of these words may already be standing: live, the socket
  // can deliver the echo before the send's own response — or before the
  // durable page a reader is still fetching — has put the prompt on screen.
  // That echo IS this message, so the row names it rather than saying the
  // message a second time under the answer to it.
  const echo = echoStandingFor(state, text, seq);
  if (echo) {
    state.messageToTurn.set(prompt.id, echo.turn.id);
    // The transcript shows what the reader wrote, never the block the box
    // prepended to hand their files to the harness.
    echo.part.text = text;
    // The server's stamp on the person's row is when they spoke; the echo's
    // own time is when the box got round to it.
    if (prompt.at) echo.turn.startedAt = prompt.at;
    if (prompt.fromTemplate) echo.turn.fromTemplate = prompt.fromTemplate;
    if (prompt.fromTemplateAuthor) echo.turn.fromTemplateAuthor = prompt.fromTemplateAuthor;
    // Claimed: a second send of the same words gets its own turn.
    state.promptTurns.set(echo.turn.id, text);
    return;
  }
  const turn: ConversationTurn = {
    id: prompt.id,
    author: "user",
    // A machine has already written past this message, so it was taken —
    // whatever order the tellings of it reached this reader in.
    status: opts.taken ? "done" : "running",
    startedAt: prompt.at,
    ...(prompt.fromTemplate ? { fromTemplate: prompt.fromTemplate } : {}),
    ...(prompt.fromTemplateAuthor ? { fromTemplateAuthor: prompt.fromTemplateAuthor } : {}),
    parts: [{ id: `${prompt.id}-text`, kind: "text", text }],
  };
  state.turns.push(turn);
  state.messageToTurn.set(prompt.id, turn.id);
  state.promptTurns.set(turn.id, text);
  if (seq !== null) state.turnSeq.set(turn.id, seq);
  if (state.sessionWorking) state.queuedBehindTurn.add(turn.id);
}

/** Say which transcript row is about to be folded, so a turn it opens knows
 *  where it stands. A source with no sequences never calls this. */
export function setFoldingSeq(state: ConversationFoldState, seq: number | null): void {
  state.foldingSeq = seq;
}

/** A message the box will never hand to the agent.
 *
 *  The words are already on screen — the server makes a message durable the
 *  moment it takes it — so this names the turn already carrying them rather
 *  than adding a second telling of the same words. The state is the SERVER's:
 *  a turn is cancelled here only because a box said so, and the word for why
 *  is the box's word, carried through untouched for the reader to render.
 *
 *  A `message_id` no turn answers to is ignored: a cancellation for a message
 *  outside the window this reader holds is not something to invent a turn for.
 *  Folding the same event twice lands on the same turn with the same state. */
function foldPromptCancelled(state: ConversationFoldState, raw: HarnessEvent): void {
  const messageId = stringField(raw, "message_id");
  if (!messageId) return;
  const turnId = state.messageToTurn.get(messageId) ?? messageId;
  const turn = state.turns.find((candidate) => candidate.id === turnId);
  if (!turn || turn.author !== "user") return;
  turn.status = "cancelled";
  const reason = stringField(raw, "reason");
  if (reason) turn.cancelledReason = reason;
  // Nothing is waiting on a message that was never handed over, so it stops
  // standing for a prompt a later echo of the same words could merge into.
  state.promptTurns.delete(turn.id);
}

/** The turn a box already opened for these words, and the part carrying them —
 *  or null when nothing has echoed them yet. A turn that already stands for a
 *  prompt is never re-used, so sending the same message twice is two turns. */
function echoStandingFor(
  state: ConversationFoldState,
  text: string,
  promptSeq: number | null = null,
): { turn: ConversationTurn; part: { text: string } } | null {
  // The words alone do not say which echo is this message's: a person who says
  // the same thing twice has two echoes reading the same. Where the transcript
  // places both, a message's echo is the FIRST one after it — never one the
  // box wrote before the message existed. An echo nobody placed (a source
  // with no sequences) is taken in the order it stands, as before.
  let unplaced: { turn: ConversationTurn; part: { text: string } } | null = null;
  let nearest: { turn: ConversationTurn; part: { text: string }; seq: number } | null = null;
  for (const turn of state.turns) {
    if (turn.author !== "user" || state.promptTurns.has(turn.id)) continue;
    for (const part of turn.parts) {
      if (part.kind !== "text" || !echoesPrompt(part.text, text)) continue;
      const at = state.turnSeq.get(turn.id);
      if (promptSeq === null || at === undefined) {
        unplaced = unplaced ?? { turn, part };
      } else if (at > promptSeq && (nearest === null || at < nearest.seq)) {
        nearest = { turn, part, seq: at };
      }
      break;
    }
  }
  return nearest ?? unplaced;
}

/** Every prompt still reading as going out has been taken: the machine is
 *  speaking in this chat, which it only does on a turn it accepted. Called
 *  whenever a turn that is not the person's opens, so a box that answers
 *  without echoing the words back never leaves "Sending…" standing — and by
 *  the cloud window for every row a machine wrote after the prompt, so a
 *  tab that heard the send live reads the same `done` a fresh open reads off
 *  the durable page. */
export function promptsWereTaken(state: ConversationFoldState): void {
  if (state.promptTurns.size === 0) return;
  for (const turn of state.turns) {
    if (turn.author === "user" && turn.status === "running" && state.promptTurns.has(turn.id)) {
      turn.status = "done";
    }
  }
}

/** The agent has begun answering, right here.
 *
 *  A message reported as never run and then answered is one log making two
 *  claims, and only one can be on screen. The later word wins: the report was
 *  written by the server in the moment before its stop reached the box, and
 *  the box already had the message. It WAS sent — the stop ends the turn it
 *  started, which the stop's own terminal and the server's "Stopped by" line
 *  already say — so the bubble stops claiming otherwise.
 *
 *  Only the message this work opens directly on top of is withdrawn: a message
 *  dropped before a later one keeps its line. This is why it hangs off an
 *  ASSISTANT turn opening rather than off `promptsWereTaken`, which any later
 *  row at all calls.
 *
 *  Called where the machine shows itself: an assistant turn opening, and the
 *  terminal that ends a turn the box was stopped before it could say anything
 *  about.
 *
 *  "Directly on top of" steps over the notes, because in production there is
 *  always one: the server writes the cancellation and the "Stopped by <who>."
 *  line in a single transaction, so that system turn sits between the claim
 *  and the box's first row every time. A note is not work — on its own it
 *  withdraws nothing, since nothing calls this for one — but it is not
 *  something the message being answered can be hidden behind either.
 */
function machineIsAnswering(state: ConversationFoldState): void {
  let index = state.turns.length - 1;
  while (index >= 0 && state.turns[index].author === "system") index -= 1;
  const answered = index >= 0 ? state.turns[index] : undefined;
  if (answered === undefined || answered.author !== "user") return;
  if (answered.status !== "cancelled") return;
  answered.status = "done";
  answered.cancelledReason = undefined;
}

/** Whether `echo` is the machine's rendering of `said`. The box hands the
 *  harness the person's words with the files they attached listed above them,
 *  so the echo may carry a block the reader never typed — the words are the
 *  tail of it, and the words are what the transcript shows. */
function echoesPrompt(echo: string, said: string): boolean {
  const words = said.trim();
  if (!words) return false;
  const heard = echo.trim();
  return heard === words || heard.endsWith(`\n\n${words}`);
}

/** Settle the durable prompt this echo is a copy of and drop the turn the echo
 *  opened — one message, in the person's own words, kept in the prompt's place
 *  but under the ids the box gave the message and its part, which is what a
 *  reader who folds the echo first (the socket's window lands before the page)
 *  holds. Every later part of the echoed message routes onto this turn. */
function maybeMergeTakenPrompt(
  state: ConversationFoldState,
  canonicalTurn: ConversationTurn,
  text: string,
  messageId: string | null | undefined,
  partId?: string | null,
): boolean {
  if (canonicalTurn.author !== "user" || state.promptTurns.size === 0) return false;
  // An echo cannot answer a message the server recorded after it: where the
  // transcript places both, the words matching is not enough.
  const echoSeq = state.turnSeq.get(canonicalTurn.id) ?? state.foldingSeq;
  const waiting = state.turns.filter((turn) => {
    if (turn === canonicalTurn || state.passedOverPrompts.has(turn.id)) return false;
    // A message that already has its echo is not waiting for another: a second
    // saying of its words is a second message, with an echo of its own.
    if (!awaitingEcho(turn)) return false;
    const words = state.promptTurns.get(turn.id);
    if (words === undefined || !echoesPrompt(text, words)) return false;
    const saidAt = state.turnSeq.get(turn.id);
    return echoSeq === null || saidAt === undefined || saidAt < echoSeq;
  });
  // The box takes messages in order, so the echo is the EARLIEST waiting
  // saying's — earliest in the transcript, which is not the order the tellings
  // reached this reader in (pages arrive newest first). Where a saying's place
  // is unknown the order they stand in is all there is, as before.
  const placed = waiting.every((turn) => state.turnSeq.has(turn.id));
  const prompt = placed
    ? waiting.reduce<ConversationTurn | undefined>(
        (first, turn) =>
          first === undefined ||
          (state.turnSeq.get(turn.id) as number) < (state.turnSeq.get(first.id) as number)
            ? turn
            : first,
        undefined,
      )
    : waiting[0];
  if (!prompt) return false;
  // A message still waiting for its echo BEFORE the one being echoed now was
  // passed over — stopped before any box took it — and no later echo of the
  // same words is its. Left standing, the person saying the same thing again
  // would have the new turn dated by the old saying, where a page that never
  // held the old one dates it right.
  const promptSeq = state.turnSeq.get(prompt.id);
  const promptIndex = state.turns.indexOf(prompt);
  state.turns.forEach((earlier, index) => {
    if (earlier === prompt || earlier.author !== "user") return;
    if (!awaitingEcho(earlier) || !state.promptTurns.has(earlier.id)) return;
    const earlierSeq = state.turnSeq.get(earlier.id);
    const before =
      promptSeq !== undefined && earlierSeq !== undefined
        ? earlierSeq < promptSeq
        : index < promptIndex;
    if (before) state.passedOverPrompts.add(earlier.id);
  });
  const words = state.promptTurns.get(prompt.id) as string;
  prompt.status = "done";
  if (partId) {
    const synthetic = prompt.parts.find((part) => part.kind === "text" && part.text === words);
    if (synthetic) {
      synthetic.id = partId;
      state.partToTurn.set(partId, prompt.id);
    }
  }
  if (messageId) {
    // The echoed message now names THIS turn, so every part of it lands here
    // and the prompt's own transcript id keeps resolving to it.
    state.promptTurns.delete(prompt.id);
    for (const [rowId, turnId] of state.messageToTurn) {
      if (turnId === prompt.id) state.messageToTurn.set(rowId, messageId);
    }
    prompt.id = messageId;
    state.promptTurns.set(messageId, words);
    state.messageToTurn.set(messageId, messageId);
    if (partId) state.partToTurn.set(partId, messageId);
  }
  state.turns = state.turns.filter((turn) => turn !== canonicalTurn);
  return true;
}

export function addOptimisticUserTurn(
  state: ConversationFoldState,
  id: string,
  text: string,
): void {
  markAllTurnsDirty(state);
  state.sawTurnError = false;
  state.turns.push({
    id,
    author: "user",
    status: "done",
    parts: [{ id: `${id}-text`, kind: "text", text }],
  });
  if (state.sessionWorking) state.queuedBehindTurn.add(id);
}

/** True while the agent owes a response to the conversation — derived from the
 *  folded transcript (which carries the daemon's turn status), NOT a
 *  hand-toggled flag. The agent owes a response when the LAST turn is the
 *  user's message or an assistant turn still in flight (`completedAt` unset —
 *  `status` alone is unreliable: opencode emits no turn.started, so a
 *  streaming assistant turn reads "done" from creation). Shared here so both
 *  the chat store and the data source's activity view derive the working
 *  state from one rule. */
export function conversationAwaitsResponse(turns: ConversationTurn[]): boolean {
  const last = turns[turns.length - 1];
  if (!last) return false;
  // A message the box dropped is owed nothing: no machine was ever handed it,
  // and nothing will be. Left reading as awaited it kept the working display
  // and the "waiting behind the running turn" line up over a dead message.
  if (last.author === "user") return last.status !== "cancelled";
  if (last.author === "system") return false;
  if (last.status === "error" || last.status === "cancelled") return false;
  // A compaction the fold is still RUNNING is work in its own right: the card
  // above the composer says so, and it is owed on every source, including one
  // that publishes no turn state of its own.
  //
  // A compaction that has SETTLED is a synthetic, terminal marker — it lands on
  // its own assistant turn with no `completedAt`, and a manual /compact runs at
  // idle with no turn lifecycle around it, so reading that turn as live would
  // light the working indicator forever. Whether a turn is nonetheless still
  // going — an automatic compaction is followed by a re-send of the same prompt
  // — is the MACHINE's word to give, not the transcript's; the chat store asks
  // it (`machineWorking`) and holds the turn open on it.
  const tail = last.parts[last.parts.length - 1];
  if (tail?.kind === "compaction") return tail.streaming === true;
  return last.completedAt == null;
}

/** Whether the person's newest message is still waiting for a machine to take
 *  it up: the tape ends on it and it still reads as going out (`running`, see
 *  `foldRelayedPrompt`) — no echo of it, and nothing the box wrote after it.
 *  The box echoes the message the moment it hands it to the agent, so until
 *  then no turn is running, however long the message has waited. */
export function awaitsTurnStart(turns: ConversationTurn[]): boolean {
  const last = turns[turns.length - 1];
  return last?.author === "user" && last.status === "running";
}

/** The conversation tokens the newest turn reported carrying, or null while no
 *  turn has reported usage. The composer's context meter reads it, so the
 *  reader can watch the window fill instead of being surprised by a fold. */
export function conversationContextTokens(turns: ConversationTurn[]): number | null {
  for (let i = turns.length - 1; i >= 0; i -= 1) {
    const tokens = turns[i]?.contextTokens;
    if (typeof tokens === "number") return tokens;
  }
  return null;
}

/** What the chat has stopped to ask the human for, or null while it owes
 *  nothing. Only the live turn can hold an unanswered ask, so this reads the
 *  tail alone. A permission still with the policy judge (no `prompting`) is not
 *  an ask: nobody has been asked anything yet. A permission outranks a question
 *  because that is the order the dock offers them in. */
export function pendingAskKind(
  turns: ConversationTurn[],
): "permission" | "question" | "plan" | null {
  const last = turns[turns.length - 1];
  // A finished turn holds no live ask, whatever a part left behind says: an
  // abandoned request cannot be answered any more.
  if (!last || !conversationAwaitsResponse(turns)) return null;
  let question: "question" | "plan" | null = null;
  for (const part of last.parts) {
    if (part.kind === "permission" && part.status === "pending" && part.prompting)
      return "permission";
    if (part.kind === "question" && part.status === "pending") {
      question = part.questionKind === "plan_approval" ? "plan" : "question";
    }
  }
  return question;
}

/** What the publisher last said about a chat's turn, as the server holds it:
 *  `null` while no word has reached this reader yet. */
export type PublishedTurnState = "working" | "idle" | null;

/** Whether a replayed transcript's dangling parts (a tool still `running`, an
 *  ask never announced as a person's, a text still streaming) may be retired —
 *  decided from the SERVER's word alone, never from the shape of the read that
 *  delivered the transcript.
 *
 *  - `idle`: the turn is over, said by the box, or by the server when no box
 *    was left to say it; a part the transcript still holds open is one nothing
 *    will close, and reading it as ended is reading what the server said.
 *  - `working`: the turn is live; nothing is retired. A turn whose machine is
 *    gone is ended by the server, which then says `idle`.
 *  - `null`: no word has arrived — a REST read that landed before the socket's
 *    first frame, a snapshot whose meta carried other keys. Nothing is known,
 *    so nothing is invented: the parts stay as the transcript holds them until
 *    the word arrives. Retiring here would let a durable read that raced the
 *    socket's `working` mark a running write failed and close a turn whose
 *    real completion is still on its way. */
export function replayVerdict(turnState: PublishedTurnState): "keep" | "settle" {
  return turnState === "idle" ? "settle" : "keep";
}

/** Settle interrupts left dangling on an open-chat REPLAY (a fresh open after an
 *  extension reload): the daemon's in-flight wait is gone, so a pending
 *  permission/question can no longer be answered and a mid-flight tool call is
 *  dead. Mark them terminal so the reopened chat shows the tool as failed (✗)
 *  with no stale, unanswerable interrupt menu — instead of a frozen spinner and
 *  a permission prompt that does nothing. */
export function settleStaleInterrupts(
  state: ConversationFoldState,
  opts: { keepAnswerableAsks?: boolean } = {},
): void {
  markAllTurnsDirty(state);
  for (const turn of state.turns) {
    if (opts.keepAnswerableAsks && turnWaitsOnAPerson(turn)) {
      // A pending ask is state of the CHAT, not of the process that raised
      // it: the machine that opens the chat next re-offers the very same ask
      // and answers it, so the card stays live however long ago it was
      // raised — a cold open, a reload mid-ask, a resume after a sleep.
      continue;
    }
    let settled = false;
    for (const part of turn.parts) {
      part.streaming = false;
      if (part.kind === "permission" && part.status === "pending") {
        part.status = "resolved";
        settled = true;
      } else if (part.kind === "question" && part.status === "pending") {
        part.status = "rejected";
        settled = true;
      } else if (part.kind === "tool" && (part.state === "pending" || part.state === "running")) {
        part.state = "error";
        settled = true;
      }
    }
    // A turn left mid-interrupt/tool on reload is abandoned — mark it cancelled
    // so it reads as a finished (failed) turn, not an active one (otherwise it
    // would re-light the working indicator on the next live event).
    if (settled && turn.author !== "user" && turn.author !== "system") turn.status = "cancelled";
  }
  // The TRAILING assistant turn can be abandoned mid-TEXT-stream with no
  // pending interrupt/tool at all — nothing above settles it, so a reopened
  // dead chat would keep a blinking cursor and read as awaiting a response
  // forever. Mid-tail turns always carry `completedAt` (the persisted stream
  // is created → chunks → completed), so only the last turn needs this.
  const last = state.turns[state.turns.length - 1];
  if (
    last &&
    last.author === "assistant" &&
    last.completedAt == null &&
    !(opts.keepAnswerableAsks && turnWaitsOnAPerson(last))
  ) {
    last.status = "cancelled";
  }
  state.currentTurnId = null;
  state.currentAssistantTurnId = null;
}

/** Whether a turn holds an ask a person can still answer: a permission the
 *  machine announced as theirs (`prompting`) or a question, unresolved. An
 *  untagged permission is the policy's to answer and is not one. */
function turnWaitsOnAPerson(turn: ConversationTurn): boolean {
  return turn.parts.some(
    (part) =>
      (part.kind === "permission" && part.status === "pending" && part.prompting === true) ||
      (part.kind === "question" && part.status === "pending"),
  );
}

function foldMessageCreated(state: ConversationFoldState, raw: HarnessEvent): void {
  const messageId = stringField(raw, "message_id");
  if (!messageId || state.messageToTurn.has(messageId)) return;
  const role = stringField(raw, "role");
  const agent = stringField(raw, "agent");
  const author = role === "user" ? "user" : role === "system" ? "system" : "assistant";
  if (author === "user") {
    state.sawTurnError = false;
    // A synthetic part that led the person's own message is the adapter's
    // steering (the per-turn directive it prepends), never a notice.
    state.pendingSystemNotices.delete(messageId);
    // The turn opens with its first part (`ensureTurnForMessage`); until then
    // the announcement is only remembered, so a repeat of it is nothing.
    if (!state.announcedUserMessages.has(messageId)) {
      state.announcedUserMessages.set(messageId, stringField(raw, "time") ?? undefined);
      // The box took a person's message: it takes them in order, so nothing
      // said before this one is waiting behind a turn any more.
      state.queuedBehindTurn.clear();
    }
    return;
  }
  promptsWereTaken(state);
  if (author === "assistant") machineIsAnswering(state);
  const turn: ConversationTurn = {
    id: messageId,
    author,
    agentLabel: agent ?? undefined,
    status: "done",
    startedAt: stringField(raw, "time") ?? undefined,
    parts: [],
  };
  if (author === "assistant") placeAboveQueuedPrompts(state, turn);
  else state.turns.push(turn);
  state.messageToTurn.set(messageId, turn.id);
  const waiting = state.pendingSystemNotices.get(messageId);
  if (waiting) {
    state.pendingSystemNotices.delete(messageId);
    if (author === "system") {
      for (const held of waiting) {
        state.suppressedSyntheticPartIds.delete(held.partId);
        addSystemNoticePart(state, messageId, held.partId, held.text);
      }
    }
  }
  if (author === "assistant") {
    turn.status = state.currentTurnId ? "running" : "done";
    turn.startedAt = turn.startedAt ?? state.pendingAssistantStartedAt ?? undefined;
    state.currentAssistantTurnId = turn.id;
  }
}

function foldPartStarted(state: ConversationFoldState, raw: HarnessEvent): void {
  const messageId = stringField(raw, "message_id");
  const partId = stringField(raw, "part_id");
  const partType = stringField(raw, "part_type");
  if (!messageId || !partId || state.partToTurn.has(partId)) return;
  if (partType === "text" || partType === "reasoning") {
    const initial = objectField(raw, "initial");
    const initialType = stringField(initial, "type");
    if (initialType === "step-start" || initialType === "step-finish") return;
    // Synthetic / ignored steering parts (system reminder, plan-approved nudge)
    // are never shown — remember the id so the delta chunks skip it too.
    if (boolField(initial, "synthetic") || boolField(initial, "ignored")) {
      state.suppressedSyntheticPartIds.add(partId);
      return;
    }
    const text = stringField(initial, "text");
    // The runtime's "Backgrounded Tool Finished" wake is for the model only — the
    // finished job renders as its own tool card; never show the raw envelope.
    if (text && isBackgroundWake(text)) {
      state.suppressedSyntheticPartIds.add(partId);
      return;
    }
    const turn = ensureTurnForMessage(state, messageId, "assistant");
    // This part opening is what finishes the one above it.
    textPartOrderOf(state, partId);
    closeOpenTextishParts(state, partId, turn.id);
    if (!text) state.wordlessPartIds.add(partId);
    state.openTextPartIds.add(partId);
    if (!text) {
      // A reasoning part always opens empty, and a text part may. There is
      // nothing to show yet, but this IS the moment the part takes its place
      // in the turn — remember it, so whichever lane delivers the words first
      // does not file them below the tool call that came after.
      if (!state.reservedPartSlots.has(partId)) {
        const last = turn.parts[turn.parts.length - 1];
        state.reservedPartSlots.set(partId, { turnId: turn.id, afterPartId: last?.id ?? null });
      }
      return;
    }
    const kind = partType === "reasoning" ? "thinking" : "text";
    const deduped =
      kind === "text" &&
      (maybeDeduplicateOptimisticUser(turn, text, messageId) ||
        maybeMergeOptimisticUser(state, turn, text, messageId) ||
        maybeMergeTakenPrompt(state, turn, text, messageId, partId));
    if (deduped) {
      state.messageToTurn.set(messageId, turn.id);
      return;
    }
    placeTextishPart(state, turn, { id: partId, kind, text, streaming: false });
    state.partToTurn.set(partId, turn.id);
  }
}

function foldPartCreated(state: ConversationFoldState, raw: HarnessEvent): void {
  const part = objectField(raw, "part");
  if (!part) return;
  const type = stringField(part, "type");
  const partId = idField(part, "part_id") ?? idField(raw, "part_id") ?? newId("part");
  const messageId = stringField(part, "message_id") ?? stringField(raw, "message_id");
  if (type === "text" && systemAuthored(state, messageId)) {
    // The daemon speaks for itself on a `system` message — a refusal, a saved
    // result, a promote that failed. It is tooling-authored (so it carries the
    // `synthetic` flag the model's own steering injections use) but it is
    // written FOR the reader, so it renders as a notice rather than being
    // suppressed, and as plain text rather than prose: a quoted statement in
    // it is data, never markup.
    addSystemNoticePart(state, messageId, partId, stringField(part, "text") ?? "");
    return;
  }
  if (boolField(part, "synthetic") || boolField(part, "ignored")) {
    // The wire does not promise the message row before its part: a synthetic
    // part on a message nobody has announced is held for that row, which
    // decides whether it was a notice (shown) or steering (never shown).
    // Not so a part on the person's own message (announced, its turn not yet
    // open): that is the adapter's steering. A note may arrive as a started
    // and created pair just as steering does, so the pair says nothing — only
    // the message row does.
    if (
      type === "text" &&
      messageId &&
      !state.messageToTurn.has(messageId) &&
      !state.announcedUserMessages.has(messageId)
    ) {
      const held = state.pendingSystemNotices.get(messageId) ?? [];
      held.push({ partId, text: stringField(part, "text") ?? "" });
      state.pendingSystemNotices.set(messageId, held);
    }
    state.suppressedSyntheticPartIds.add(partId);
    removePart(state, partId);
    return;
  }
  if (type === "text" || type === "reasoning") {
    const text = stringField(part, "text") ?? "";
    // The model-only background wake is never shown (see foldPartStarted).
    if (text && isBackgroundWake(text)) {
      state.suppressedSyntheticPartIds.add(partId);
      removePart(state, partId);
      return;
    }
    const turn = ensureTurnForMessage(
      state,
      messageId,
      type === "text" ? "assistant" : "assistant",
    );
    const existing = findPart(state, partId);
    // The publisher has said what this part finally reads: it is not open any
    // more, whatever token frames are still on their way.
    state.openTextPartIds.delete(partId);
    state.wordlessPartIds.delete(partId);
    if (existing && (existing.kind === "text" || existing.kind === "thinking")) {
      if (!text) {
        removePart(state, partId);
        return;
      }
      existing.text = text;
      existing.streaming = false;
      state.settledTextPartIds.add(partId);
      return;
    }
    if (!text) return;
    const deduped =
      maybeDeduplicateOptimisticUser(turn, text, messageId) ||
      maybeMergeOptimisticUser(state, turn, text, messageId) ||
      maybeMergeTakenPrompt(state, turn, text, messageId, partId);
    if (deduped) {
      state.messageToTurn.set(messageId ?? turn.id, turn.id);
      return;
    }
    placeTextishPart(state, turn, {
      id: partId,
      kind: type === "reasoning" ? "thinking" : "text",
      text,
      streaming: false,
    });
    state.partToTurn.set(partId, turn.id);
    state.settledTextPartIds.add(partId);
    return;
  }
  if (type === "tool_call") {
    const callId = idField(part, "call_id") ?? partId;
    const name = stringField(part, "name") ?? stringField(part, "tool_name") ?? "tool";
    // The question tool renders via its dedicated question card.
    if (isQuestionTool(name)) {
      state.suppressedToolCallIds.add(callId);
      return;
    }
    const tool: ToolConversationPart = {
      id: partId,
      kind: "tool",
      callId,
      name,
      state: terminalToolState(
        stringField(part, "state"),
        stringField(part, "error_text"),
        objectField(part, "metadata"),
      ),
      input: objectField(part, "input") ?? undefined,
      output: objectOrStringField(part, "output"),
      errorText: readableToolError(stringField(part, "error_text")),
    };
    const references = toolReferences(part);
    if (references) tool.references = references;
    mergeOutputBlobReference(tool);
    upsertTool(state, messageId, tool);
    return;
  }
  if (type === "compaction") {
    const compaction: CompactionConversationPart = {
      id: partId,
      kind: "compaction",
      title: compactionTitle(part),
      text: compactionText(part),
    };
    upsertCompaction(state, messageId, compaction);
  }
}

function appendTextChunk(
  state: ConversationFoldState,
  raw: HarnessEvent,
  chunkKind: "text" | "thinking",
): void {
  const messageId = stringField(raw, "message_id");
  const partId = stringField(raw, "part_id");
  const text = stringField(raw, "text") ?? "";
  if (!partId) return;
  // A synthetic steering part streams deltas before its flag-bearing
  // `part.created` lands; once `part.started` marked it suppressed, drop them.
  if (state.suppressedSyntheticPartIds.has(partId)) return;
  // The publisher has already said what this part finally reads: a delta that
  // lands after it is a token of a text the part already carries in full, and
  // appending it writes the thought out twice.
  if (state.settledTextPartIds.has(partId)) return;
  const known = state.turns.length;
  const turn = ensureTurnForMessage(state, messageId, "assistant");
  // Opening a turn settles the prompts below it, so the narrow claim only
  // holds when the turn this token belongs to was already there.
  if (state.turns.length !== known) markAllTurnsDirty(state);
  const existing = findPart(state, partId);
  let textPart: TextishConversationPart;
  if (existing && (existing.kind === "text" || existing.kind === "thinking")) {
    textPart = existing;
  } else {
    if (!text) return;
    textPart = { id: partId, kind: chunkKind, text: "", streaming: true };
    // A part whose `part.started` was never seen (the editor's lane does not
    // always carry one) is open from its first token.
    if (!state.closedTextPartIds.has(partId)) state.openTextPartIds.add(partId);
    state.wordlessPartIds.add(partId);
    placeTextishPart(state, turn, textPart);
    state.partToTurn.set(partId, turn.id);
  }
  textPart.text += text;
  // A token frame that arrives after a later part closed this one is the
  // coalescing lane catching up, not the model thinking again.
  const closed = boolField(raw, "is_final") || state.closedTextPartIds.has(partId);
  if (closed) state.openTextPartIds.delete(partId);
  textPart.streaming = !closed;
  // The one turn a token moves. `ensureTurnForMessage` may have opened it, and
  // a new turn is copied on its first snapshot either way.
  markTurnDirty(state, turn.id);
}

/** Tools whose generic card carries no chat value, so it's suppressed everywhere
 *  a tool part could be created (live tool.call/update + persisted-replay part):
 *  opencode's `question` and `plan_present` drive the dedicated question /
 *  plan-approval card (kind:"plan_approval"); `plan` / `plan_exit` are opencode's
 *  plan-mode control tools that only announce the agent switching out of plan mode
 *  (the approval already rode through `plan_present`), so they render nothing. */
const QUESTION_SURFACE_TOOLS = new Set(["question", "plan_present", "plan", "plan_exit"]);

function isQuestionTool(name: string | null | undefined): boolean {
  return name ? QUESTION_SURFACE_TOOLS.has(name.toLowerCase()) : false;
}

function foldToolCall(state: ConversationFoldState, raw: HarnessEvent): void {
  const callId = idField(raw, "tool_call_id") ?? newId("tool");
  if (isQuestionTool(stringField(raw, "tool_name"))) {
    state.suppressedToolCallIds.add(callId);
    return;
  }
  const messageId = stringField(raw, "message_id");
  const tool: ToolConversationPart = {
    id: callId,
    kind: "tool",
    callId,
    name: stringField(raw, "tool_name") ?? "tool",
    toolKind: stringField(raw, "tool_kind") ?? undefined,
    state: toolState(stringField(raw, "status")),
    input: objectField(raw, "input") ?? undefined,
    streaming: stringField(raw, "status") !== "completed",
  };
  const references = toolReferences(raw);
  if (references) tool.references = references;
  // A backgrounded SQL/bash job's lifecycle card (running at submit → result at
  // finish), keyed `bgjob:<id>` — marked `background` (the card shows a "Background"
  // label) and folded into its OWN standalone turn so an out-of-band card never
  // captures the live turn's parts or its turn_summary (see backgroundCardTurn).
  if (isBackgroundCallId(callId)) {
    tool.background = true;
    upsertBackgroundTool(state, tool);
    return;
  }
  upsertTool(state, messageId, tool);
  // A spawn's tool.call usually precedes its SubagentStarted, but they race on the
  // bus — drain any start that arrived first onto this fresh spawn part.
  bindBufferedStart(state, tool);
}

/** DATA-ONLY `subagent.started`: bind `child_session_id` onto the originating
 *  `spawn_agent` tool card so the card can stream the running child + drill in.
 *  Correlates by (agent, prompt) — the MCP transport drops the chat tool-call id,
 *  so the spawn brief is the only shared key. If the spawn's tool.call hasn't been
 *  folded yet (a bus race), buffer the pointer and bind it on arrival. */
function foldSubagentStarted(state: ConversationFoldState, raw: HarnessEvent): void {
  const childSessionId = stringField(raw, "child_session_id");
  if (!childSessionId) return;
  // `||` not `??`: an empty agent_name defaults to "explore" too, matching how the
  // spawn part's own agent is read (spawnAgent), so the keys can't drift to "" vs "explore".
  const agent = stringField(raw, "agent_name") || "explore";
  const prompt = stringField(raw, "prompt") ?? "";
  const part = findUnboundSpawnPart(state, agent, prompt);
  if (part) {
    part.childSessionId = childSessionId;
    return;
  }
  state.pendingSubagentStarts.push({ childSessionId, agent, prompt });
}

/** When a spawn part is (re)folded, bind the first buffered start that matches its
 *  (agent, prompt) — the order-independent half of the correlation. */
function bindBufferedStart(state: ConversationFoldState, tool: ToolConversationPart): void {
  if (tool.childSessionId || !isSpawnTool(tool)) return;
  const agent = spawnAgent(tool);
  const prompt = spawnPrompt(tool);
  const idx = state.pendingSubagentStarts.findIndex(
    (p) => p.agent === agent && p.prompt === prompt,
  );
  if (idx === -1) return;
  tool.childSessionId = state.pendingSubagentStarts[idx].childSessionId;
  state.pendingSubagentStarts.splice(idx, 1);
}

/** Most-recent unbound `spawn_agent` part matching (agent, prompt). Searched
 *  newest→oldest so a re-spawn with the SAME brief in a later turn binds its own
 *  (current) card, not a stale earlier one; N parallel same-brief spawns in one turn
 *  bind first-unbound (identical briefs are visually identical anyway). */
function findUnboundSpawnPart(
  state: ConversationFoldState,
  agent: string,
  prompt: string,
): ToolConversationPart | null {
  for (let t = state.turns.length - 1; t >= 0; t--) {
    const parts = state.turns[t].parts;
    for (let p = parts.length - 1; p >= 0; p--) {
      const part = parts[p];
      if (part.kind !== "tool" || part.childSessionId || !isSpawnTool(part)) continue;
      if (spawnAgent(part) === agent && spawnPrompt(part) === prompt) return part;
    }
  }
  return null;
}

function isSpawnTool(part: ToolConversationPart): boolean {
  return /spawn_agent/i.test(part.name) || part.toolKind === "task";
}

function spawnAgent(part: ToolConversationPart): string {
  const agent = part.input?.agent;
  return typeof agent === "string" && agent ? agent : "explore";
}

function spawnPrompt(part: ToolConversationPart): string {
  const prompt = part.input?.prompt;
  return typeof prompt === "string" ? prompt : "";
}

/** The one-line brief the spawn was given, when it carried one. */
function spawnDescription(part: ToolConversationPart): string {
  const description = part.input?.description;
  return typeof description === "string" ? description.trim() : "";
}

/** How many of the child's most-recent tool calls the card's live activity shows. */
const RECENT_SUBAGENT_TOOLS = 6;

export interface SpawnChildLink {
  callId: string;
  childSessionId: string;
  /** The spawn tool call has reached a terminal state — its report is in, so the
   *  card collapses to {summary, stats} and the live observe can detach. */
  completed: boolean;
  /** What the parent calls this child. A spawned chat's own manifest has no
   *  title, so the brief its parent gave it is the only name it has. */
  label: string;
}

/** The parent's `spawn_agent` cards that have a bound child — what the data layer
 *  observes (running) or tears down (completed). */
export function collectSpawnChildren(state: ConversationFoldState): SpawnChildLink[] {
  return spawnChildrenOf(state.turns);
}

/** The same links off a plain transcript, for a reader holding a parent's turns
 *  rather than its live fold. */
export function spawnChildrenOf(turns: ConversationTurn[]): SpawnChildLink[] {
  const out: SpawnChildLink[] = [];
  for (const turn of turns) {
    for (const part of turn.parts) {
      if (part.kind !== "tool" || !isSpawnTool(part) || !part.childSessionId) continue;
      out.push({
        callId: part.callId,
        childSessionId: part.childSessionId,
        completed: toolSettled(part.state),
        label: spawnDescription(part) || `@${spawnAgent(part)}`,
      });
    }
  }
  return out;
}

/** A compact, live snapshot of an observed child's work, for mirroring onto its
 *  spawn card: the most-recent tool calls + a running total + whether it's active. */
export function summarizeSubagentActivity(child: ConversationFoldState): SubagentActivity {
  const tools: ToolConversationPart[] = [];
  for (const turn of child.turns) {
    for (const part of turn.parts) {
      if (part.kind === "tool") tools.push(part);
    }
  }
  const recentTools: SubagentActivityTool[] = tools.slice(-RECENT_SUBAGENT_TOOLS).map((part) => {
    const tool: SubagentActivityTool = { callId: part.callId, name: part.name, state: part.state };
    const detail = subagentToolDetail(part);
    if (detail) tool.detail = detail;
    return tool;
  });
  const byTool: Record<string, number> = {};
  for (const tool of tools) byTool[tool.name] = (byTool[tool.name] ?? 0) + 1;
  const activity: SubagentActivity = {
    recentTools,
    toolCount: tools.length,
    running: conversationAwaitsResponse(child.turns),
  };
  if (tools.length > 0) activity.byTool = byTool;
  const lastMessage = latestChildMessage(child.turns);
  if (lastMessage) activity.lastMessage = lastMessage;
  return activity;
}

/** The child's most-recent assistant text, mirrored onto the spawn card so its
 *  preview can stream the message as it lands. Walks newest→oldest, skipping the
 *  user echo, and returns the last non-empty `text` part. */
function latestChildMessage(turns: ConversationTurn[]): string | null {
  for (let turnIndex = turns.length - 1; turnIndex >= 0; turnIndex -= 1) {
    const turn = turns[turnIndex];
    if (turn.author === "user") continue;
    for (let partIndex = turn.parts.length - 1; partIndex >= 0; partIndex -= 1) {
      const part = turn.parts[partIndex];
      if (part.kind === "text" && part.text.trim()) return part.text;
    }
  }
  return null;
}

/** Write (or clear, when `activity` is undefined) the live activity onto the spawn
 *  card identified by `callId`. Returns whether the card was found. */
export function setSubagentActivity(
  state: ConversationFoldState,
  callId: string,
  activity: SubagentActivity | undefined,
): boolean {
  markAllTurnsDirty(state);
  for (const turn of state.turns) {
    for (const part of turn.parts) {
      if (part.kind !== "tool" || part.callId !== callId) continue;
      if (activity) part.childActivity = activity;
      else delete part.childActivity;
      return true;
    }
  }
  return false;
}

/** A short target hint for a child tool call (file path / command / query). */
function subagentToolDetail(part: ToolConversationPart): string | undefined {
  const input = part.input ?? {};
  const candidate =
    input.path ??
    input.file_path ??
    input.filePath ??
    input.command ??
    input.cmd ??
    input.query ??
    input.sql ??
    input.pattern;
  if (typeof candidate !== "string" || !candidate) return undefined;
  return candidate.length > 80 ? `${candidate.slice(0, 79)}…` : candidate;
}

function foldToolUpdate(state: ConversationFoldState, raw: HarnessEvent): void {
  const callId = stringField(raw, "tool_call_id");
  if (!callId) return;
  // Updates carry no tool name; skip if the matching call was suppressed.
  if (state.suppressedToolCallIds.has(callId) || isQuestionTool(stringField(raw, "tool_name")))
    return;
  let part = findPart(state, state.toolToPart.get(callId) ?? callId);
  if (!part || part.kind !== "tool") {
    // A `bgjob:` update with no preceding ToolCall (a dropped frame) still lands in
    // its OWN standalone turn — never currentAssistantTurn, which a background card
    // must never hijack.
    part = { id: callId, kind: "tool", callId, name: "tool", state: "pending" };
    const placed = isBackgroundCallId(callId) ? null : placeByHarnessOrder(state, part);
    const turn = placed
      ?? (isBackgroundCallId(callId) ? backgroundCardTurn(state, callId) : currentAssistantTurn(state));
    if (!placed) turn.parts.push(part);
    state.toolToPart.set(callId, part.id);
    state.partToTurn.set(part.id, turn.id);
  }
  const output = objectOrStringField(raw, "output");
  // A foreground bash/sql(background=true) STUB returns {job_id, note}: drop its card
  // — the bgjob:<id> lifecycle card represents the job (one card, not a redundant
  // "started in background" stub).
  if (!isBackgroundCallId(callId) && backgroundStubJobId(output)) {
    state.suppressedToolCallIds.add(callId);
    removePart(state, part.id);
    return;
  }
  const status = stringField(raw, "status");
  if (status) part.state = terminalToolState(status, stringField(raw, "error_text"), objectField(raw, "metadata"));
  const input = objectField(raw, "input");
  if (input) part.input = input;
  if (output !== undefined) part.output = output;
  // A failed alkera tool's text is its envelope; the reader gets the message.
  const errorText = readableToolError(stringField(raw, "error_text"));
  if (errorText) part.errorText = errorText;
  const references = toolReferences(raw);
  if (references) part.references = references;
  // The publisher shrinks a result that will not fit one transcript row and
  // records what it dropped. Carry the figure so the card can say how much of
  // the output is not here; a result that fit carries no field and no claim.
  const dropped = numberField(raw, "truncated_bytes");
  if (dropped !== null && dropped > 0) part.truncatedBytes = dropped;
  mergeOutputBlobReference(part);
  const deltas = arrayField(raw, "content_deltas");
  if (deltas.length > 0) {
    part.content = `${part.content ?? ""}${deltas.map(formatDelta).join("")}`;
  }
  mergeToolDiffMetadata(state, part, raw);
  part.streaming = part.state === "running" || part.state === "pending";
}

/** A harness part id: `prt_` and a time-ordered stem, so two of them sort in
 *  the order the harness opened the parts. */
const HARNESS_PART_ID = /^prt_[0-9a-f]{12}/;

/** Put a call whose own `tool.call` never arrived where the harness opened it:
 *  right after the last assistant part the harness opened before it. An update
 *  that lands after the reply that followed the call (a frame dropped over a
 *  reconnect) otherwise trails the reply, where a reload never puts it. Null
 *  when the ids say nothing (another harness's ids, or no part before it). */
function placeByHarnessOrder(
  state: ConversationFoldState,
  part: ToolConversationPart,
): ConversationTurn | null {
  if (!HARNESS_PART_ID.test(part.id)) return null;
  for (let t = state.turns.length - 1; t >= 0; t -= 1) {
    const turn = state.turns[t];
    if (turn.author !== "assistant") continue;
    for (let i = turn.parts.length - 1; i >= 0; i -= 1) {
      const id = turn.parts[i].id;
      if (HARNESS_PART_ID.test(id) && id < part.id) {
        turn.parts.splice(i + 1, 0, part);
        return turn;
      }
    }
  }
  return null;
}

/** Lift a write/edit patch carried by a tool update onto its transcript card. */
function mergeToolDiffMetadata(
  state: ConversationFoldState,
  part: ToolConversationPart,
  raw: HarnessEvent,
): void {
  const metadata = objectField(raw, "metadata");
  if (!metadata) return;
  const fileDiff = objectField(metadata, "filediff");
  const patch = stringField(fileDiff, "patch") ?? stringField(metadata, "diff");
  if (!patch) return;
  const path =
    stringField(fileDiff, "file") ?? stringField(metadata, "filepath") ?? writeToolPath(part);
  if (!path) return;
  const insertions = numberField(fileDiff, "additions");
  const deletions = numberField(fileDiff, "deletions");
  attachFileResource(
    state,
    part,
    fileResource(
      path,
      insertions !== null && deletions !== null ? { insertions, deletions } : undefined,
      { kind: "diff", title: path.split("/").pop() || path, content: patch },
    ),
  );
}

/** Tool-produced blob results → reference chips. Prefers the typed `references`
 *  a newer writer attaches ({handle, name, ref_type}); falls back to bare
 *  `attachments` (blob sha256s) with a derived name so existing results still
 *  surface. Returns undefined when there are none (so an update never clobbers
 *  references already folded onto the part). */
function toolReferences(source: unknown): BlobReference[] | undefined {
  const typed = arrayField(source, "references").flatMap((ref) => {
    const handle = stringField(ref, "handle");
    if (!handle) return [];
    const reference: BlobReference = {
      handle,
      name: stringField(ref, "name") ?? `Result ${handle.slice(0, 8)}`,
    };
    const refType = stringField(ref, "ref_type");
    if (refType) reference.refType = refType;
    const mime = stringField(ref, "mime");
    if (mime) reference.mime = mime;
    return [reference];
  });
  if (typed.length > 0) return typed;
  const handles = arrayField(source, "attachments").filter(isString);
  if (handles.length === 0) return undefined;
  return handles.map((handle, index) => ({ handle, name: `Result ${index + 1}` }));
}

/** Surface a spilled tool result as a reference chip. The daemon-hosted tools
 *  (sql.query, the generic executor) replace an oversized result with a
 *  `{preview, blob: {sha256}}` envelope — the real handle lives in the tool's
 *  OUTPUT, not in `attachments`. Pull it out, preferring the LLM-given name
 *  (`result_name`) the tool was asked to provide, else a name derived from the
 *  tool. A no-op when the tool already declared typed references. */
function mergeOutputBlobReference(part: ToolConversationPart): void {
  if (part.references && part.references.length > 0) return;
  // The output is frequently a JSON STRING (MCP-hosted tools — sql.query, the blob
  // tools — serialize their result to text), AND most alkera tools are invoked
  // through the generic `call_tool` wrapper, which nests the inner tool's result
  // under `result`. Parse + unwrap so the spilled `{…, blob:{sha256}}` envelope is
  // reachable; without this the SQL result rendered as a raw JSON dump.
  const output = blobBearingObject(part.output);
  const blob = objectField(output, "blob");
  const handle = stringField(blob, "sha256");
  if (!handle) return;
  // `result_name` / `ref_type` may sit at the top level OR inside the blob envelope
  // (sql.query nests them under `blob`).
  const name =
    stringField(output, "result_name") ??
    stringField(blob, "result_name") ??
    stringField(output, "name") ??
    friendlyResultName(part.name);
  const reference: BlobReference = { handle, name };
  // `ref_type` is the explicit shape signal; fall back to a result's `kind`
  // (blob.create) so its chip still renders as a table / text.
  const refType =
    stringField(output, "ref_type") ?? stringField(blob, "ref_type") ?? stringField(output, "kind");
  if (refType) reference.refType = refType;
  // Only an EXPLICIT mime (the JSON-encoded catch-all spill) — the blob's own
  // media_type is always "application/json" and would mislabel a rows result.
  const mime = stringField(output, "mime");
  if (mime) reference.mime = mime;
  part.references = [reference];
}

/** The object carrying the spilled blob envelope: parses a JSON-string output, and
 *  unwraps the generic `call_tool` `{result: …}` wrapper when the envelope lives one
 *  level down — so an alkera tool invoked through the wrapper still surfaces its blob. */
function blobBearingObject(output: unknown): Record<string, unknown> | null {
  const obj = outputAsObject(output);
  if (!obj) return null;
  if (objectField(obj, "blob")) return obj; // a direct `{…, blob}` envelope
  const inner = outputAsObject(obj.result); // `call_tool` nests it under `result`
  if (inner && objectField(inner, "blob")) return inner;
  return obj;
}

/** A tool's output as an object — parsing a JSON-string output (MCP-hosted tools
 *  serialize their result to text) so the spilled blob envelope is reachable. Null
 *  when the output is neither an object nor JSON-object text. */
function outputAsObject(output: unknown): Record<string, unknown> | null {
  if (output && typeof output === "object" && !Array.isArray(output)) {
    return output as Record<string, unknown>;
  }
  if (typeof output === "string") {
    try {
      const parsed: unknown = JSON.parse(output);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        return parsed as Record<string, unknown>;
      }
    } catch {
      return null;
    }
  }
  return null;
}

function friendlyResultName(toolName: string | undefined): string {
  const leaf = (toolName ?? "").split(/[.:/]/u).pop()?.trim();
  if (!leaf) return "Result";
  const titled = leaf.charAt(0).toUpperCase() + leaf.slice(1);
  return `${titled} result`;
}

function permissionOptions(raw: HarnessEvent): PermissionConversationPart["options"] {
  return arrayField(raw, "options").map((option) => ({
    optionId: stringField(option, "option_id") ?? "reject_once",
    name: stringField(option, "name") ?? "Reject",
  }));
}

function foldPermissionRequest(state: ConversationFoldState, raw: HarnessEvent): void {
  const requestId = stringField(raw, "request_id");
  if (!requestId) return;
  // The same request arrives up to twice: once from the event stream (published
  // BEFORE the policy/safety judge runs) and once from the broker's
  // human-prompt broadcast (tagged `prompting`). The second arrival upgrades
  // the pending part to interactive instead of creating a duplicate — and a
  // judge-resolved request never prompts, so the queue never flashes.
  const prompting = boolField(raw, "prompting");
  const subjectPending = boolField(raw, "subject_pending");
  captureAskTimeEditPreview(state, raw, requestId);
  const preview = resourcePreviewField(raw);
  if (state.requestToPart.has(requestId) || findPart(state, requestId)) {
    const existing = findPart(state, state.requestToPart.get(requestId) ?? requestId);
    if (prompting && existing?.kind === "permission" && existing.status === "pending") {
      existing.prompting = true;
    }
    if (preview && existing?.kind === "permission" && !existing.preview) {
      existing.preview = preview;
    }
    const notebook = permissionNotebook(raw);
    if (notebook && existing?.kind === "permission" && !existing.notebook) {
      existing.notebook = notebook;
    }
    // The ask raced ahead of the part naming its subject and is raised again,
    // same id, once that part lands: the subject lands on the card in place.
    if (
      existing?.kind === "permission" &&
      existing.status === "pending" &&
      existing.subjectPending &&
      !subjectPending
    ) {
      delete existing.subjectPending;
      existing.permissionKind = stringField(raw, "permission_kind") ?? existing.permissionKind;
      existing.canonicalKind = canonicalKind(stringField(raw, "canonical_kind"));
      existing.patterns = arrayField(raw, "patterns").filter(isString);
      existing.options = permissionOptions(raw);
      const subject = permissionSubject(raw);
      if (subject) existing.subject = subject;
      const impact = permissionImpact(raw);
      if (impact) existing.impact = impact;
    }
    if (existing?.kind === "permission" && existing.patterns.length > 0) {
      state.asksAwaitingCall.delete(requestId);
    }
    return;
  }
  const part: PermissionConversationPart = {
    id: requestId,
    kind: "permission",
    requestId,
    permissionKind: stringField(raw, "permission_kind") ?? "permission",
    canonicalKind: canonicalKind(stringField(raw, "canonical_kind")),
    patterns: arrayField(raw, "patterns").filter(isString),
    options: permissionOptions(raw),
    status: "pending",
  };
  // The resolution is already on record: this request is a re-announce, or a
  // page read out of order. It arrives closed, on the decision that was made,
  // and is never a person's to answer — the server would refuse the answer.
  const settled = state.settledAsks.get(requestId);
  if (settled?.kind === "permission") {
    state.settledAsks.delete(requestId);
    part.status = "resolved";
    if (settled.optionId !== undefined) part.selectedOptionId = settled.optionId;
    if (settled.decidedByName !== undefined) part.decidedByName = settled.decidedByName;
    if (settled.decidedBy !== undefined) part.decidedBy = settled.decidedBy;
    if (settled.decidedVia !== undefined) part.decidedVia = settled.decidedVia;
    if (prompting) part.asked = true;
  } else {
    if (prompting) {
      part.prompting = true;
      part.asked = true;
    }
    if (state.attemptId) state.askAttempts.set(requestId, state.attemptId);
  }
  if (subjectPending) part.subjectPending = true;
  const callIds = [stringField(raw, "tool_call_id"), stringField(raw, "provider_call_id")].filter(
    isString,
  );
  if (callIds.length > 0) part.callIds = callIds;
  if (preview) part.preview = preview;
  const notebook = permissionNotebook(raw);
  if (notebook) part.notebook = notebook;
  const subject = permissionSubject(raw);
  if (subject) part.subject = subject;
  const impact = permissionImpact(raw);
  if (impact) part.impact = impact;
  const turn = currentAssistantTurn(state);
  turn.parts.push(part);
  state.requestToPart.set(requestId, part.id);
  rememberAskAwaitingCall(state, raw, part);
}

/** Park an ask that named the call it gates but not what that call does, so the
 *  subject can be recovered from the call's own part.
 *
 *  A harness publishes the ask from the tool's `execute` while the part carrying
 *  the call's arguments travels a different lane, and a mirror that does not
 *  raise the ask a second time leaves the card with nothing but the tool's name
 *  for as long as the ask is up. The call's part is the only other place the
 *  command exists, and it is on its way. */
function rememberAskAwaitingCall(
  state: ConversationFoldState,
  raw: HarnessEvent,
  part: PermissionConversationPart,
): void {
  if (part.patterns.length > 0) return;
  const callIds = [stringField(raw, "tool_call_id"), stringField(raw, "provider_call_id")].filter(
    isString,
  );
  if (callIds.length === 0) return;
  state.asksAwaitingCall.set(part.requestId, { partId: part.id, callIds });
  drainAsksAwaitingCall(state);
}

/** What a gated call does, in the register its card's ask renders: the command
 *  line for a shell call, the path for a write. Null while the call has no
 *  arguments yet — a streaming `tool.call` opens before its input lands. */
function gatedCallSubject(tool: ToolConversationPart): string | null {
  const command = tool.input?.command;
  if (typeof command === "string" && command.trim()) return command;
  return writeToolPath(tool);
}

/** Put the recovered subject on every parked ask whose call now has a part. An
 *  ask keeps waiting while the call is still argument-less, and is dropped the
 *  moment it is named — the card reads the part, so this is what swaps the
 *  tool's name for the command in place. */
function drainAsksAwaitingCall(state: ConversationFoldState): void {
  for (const [requestId, record] of state.asksAwaitingCall) {
    const part = findPart(state, record.partId);
    if (part?.kind !== "permission" || part.status !== "pending") {
      state.asksAwaitingCall.delete(requestId);
      continue;
    }
    if (part.patterns.length > 0) {
      state.asksAwaitingCall.delete(requestId);
      continue;
    }
    for (const callId of record.callIds) {
      const tool = findPart(state, state.toolToPart.get(callId) ?? callId);
      if (tool?.kind !== "tool") continue;
      const subject = gatedCallSubject(tool);
      if (!subject) continue;
      part.patterns = [subject];
      delete part.subjectPending;
      state.asksAwaitingCall.delete(requestId);
      break;
    }
  }
}

/** Lift the edit diff a `permission.request` carries (schema 1.3.0:
 *  `insertions`/`deletions`/`preview`, captured at ASK time) onto the write tool
 *  card the ask gates, so WHAT would be written is reachable while the prompt is
 *  still up. The `file.edited` carrying the same diff only lands after the write,
 *  so without this there is no copy at all to show the approver.
 *
 *  This puts the diff ON the transcript's write card — its `+N −N` count shows
 *  unconditionally, the body one click away. The permission card itself still
 *  renders only title + patterns + effect hint; showing the diff inline there
 *  needs a slot on PermissionConversationPart and a PermissionSubject that
 *  renders it, neither of which lives in this file.
 *
 *  Idempotent by request id: the same request arrives twice (event stream, then
 *  the broker's prompting broadcast), and an ask that raced ahead of its
 *  `tool.call` attaches on the second arrival — or when the tool part shows up
 *  (see {@link drainAskTimeEditPreviews}). */
function captureAskTimeEditPreview(
  state: ConversationFoldState,
  raw: HarnessEvent,
  requestId: string,
): void {
  let record = state.askTimeEditPreviews.get(requestId);
  if (!record) {
    const preview = resourcePreviewField(raw);
    if (!preview) return; // a shell / network ask carries no diff
    const insertions = numberField(raw, "insertions");
    const deletions = numberField(raw, "deletions");
    record = {
      path: stringField(objectField(raw, "preview"), "path"),
      toolCallId: stringField(raw, "tool_call_id"),
      preview,
      diff: insertions !== null && deletions !== null ? { insertions, deletions } : undefined,
      attachedToPartId: null,
    };
    state.askTimeEditPreviews.set(requestId, record);
  }
  attachAskTimeEditPreview(state, record);
}

function attachAskTimeEditPreview(state: ConversationFoldState, record: AskTimeEditPreview): void {
  if (record.attachedToPartId) return;
  const tool = askedWriteTool(state, record);
  if (!tool) return;
  const path = record.path ?? writeToolPath(tool);
  if (!path) return;
  // At most ONE pre-approval diff per card+file. A second ask for the same file
  // belongs to a second write card; matching it onto the first would show the
  // approver two stacked diffs for one proposed write. Leave it unattached — the
  // drain retries once its own card arrives.
  if (askTimeEditPreviewOn(state, tool.id, path)) return;
  record.path = path;
  record.attachedToPartId = tool.id;
  tool.resources = [...(tool.resources ?? []), fileResource(path, record.diff, record.preview)];
}

/** The write card an ask gates: by PATH first, tool-call id second (a harness's
 *  tool-call id and the card's part id can live in different id spaces, so an
 *  id-only match misses). */
function askedWriteTool(
  state: ConversationFoldState,
  record: AskTimeEditPreview,
): ToolConversationPart | null {
  if (record.path) {
    const byPath = findWriteToolForPath(state, record.path);
    if (byPath) return byPath;
  }
  if (!record.toolCallId) return null;
  const part = findPart(state, state.toolToPart.get(record.toolCallId) ?? record.toolCallId);
  return part?.kind === "tool" ? part : null;
}

/** An ask can race ahead of the `tool.call` that renders its card — retry every
 *  still-unattached ask-time diff whenever a tool part appears or updates. */
function drainAskTimeEditPreviews(state: ConversationFoldState): void {
  for (const record of state.askTimeEditPreviews.values()) {
    attachAskTimeEditPreview(state, record);
  }
}

/** The request id of the ask-time diff sitting on `partId` for `path`, if any —
 *  the resource the real post-write `file.edited` supersedes. */
function askTimeEditPreviewOn(
  state: ConversationFoldState,
  partId: string,
  path: string,
): string | null {
  for (const [requestId, record] of state.askTimeEditPreviews) {
    if (record.attachedToPartId === partId && record.path === path) return requestId;
  }
  return null;
}

/** A rejected (or cancelled / timed-out) ask never wrote anything — pull its
 *  ask-time diff back off the card so the transcript can't be read as a record
 *  of a write that happened. */
function dropAskTimeEditPreview(state: ConversationFoldState, requestId: string): void {
  const record = state.askTimeEditPreviews.get(requestId);
  if (!record) return;
  state.askTimeEditPreviews.delete(requestId);
  const part = record.attachedToPartId ? findPart(state, record.attachedToPartId) : null;
  if (part?.kind !== "tool" || !record.path) return;
  const path = record.path;
  part.resources = (part.resources ?? []).filter((resource) => resource.target !== path);
}

/** Map the serialized ActionDescriptor (`subject`) carried on a 1.2.0+
 *  permission.request onto a UI view. Returns null when absent or hollow, so
 *  the card stays exactly as it was for adapters that don't classify. */
const NOTEBOOK_CELL_ROLES = new Set(["target", "dependency", "dependent"]);

/** A notebook tool's ask, read the way the server's presenter reads it
 *  (`alkera_core.permission_presentation.present._notebook_ask`). */
function permissionNotebook(raw: HarnessEvent): PermissionNotebookView | null {
  const preview = objectField(raw, "preview");
  if (!preview || stringField(preview, "kind") !== "notebook") return null;
  const notebook = objectField(preview, "notebook");
  const lead = stringField(notebook, "lead");
  const fileName = stringField(notebook, "file_name");
  if (!notebook || !lead || !fileName) return null;
  const cells = arrayField(notebook, "cells").flatMap((cell) => {
    const name = stringField(cell, "name");
    if (!name) return [];
    const role = stringField(cell, "role") ?? "target";
    return [
      {
        name,
        code: stringField(cell, "code") ?? "",
        role: (NOTEBOOK_CELL_ROLES.has(role) ? role : "target") as PermissionNotebookView["cells"][number]["role"],
      },
    ];
  });
  return {
    lead,
    fileName,
    filePath: stringField(notebook, "file_path") || fileName,
    tail: stringField(notebook, "tail") ?? "",
    cells,
    packages: arrayField(notebook, "packages").filter(isString),
  };
}

function permissionSubject(raw: HarnessEvent): PermissionSubjectView | null {
  const subject = objectField(raw, "subject");
  if (!subject) return null;
  const targets = arrayField(subject, "targets").flatMap((target) => {
    const name = stringField(target, "name");
    const kind = stringField(target, "kind");
    if (!name && !kind) return [];
    return [
      {
        kind: kind ?? "resource",
        name: name ?? "",
        connection: stringField(target, "connection") ?? undefined,
      },
    ];
  });
  const costRaw = objectField(subject, "cost_estimate");
  const cost = costRaw
    ? {
        usd: numberField(costRaw, "usd_amount") ?? undefined,
        bytesScanned: numberField(costRaw, "bytes_scanned") ?? undefined,
        rowsScanned: numberField(costRaw, "rows_scanned") ?? undefined,
        currency: stringField(costRaw, "wallet_currency") ?? undefined,
      }
    : undefined;
  const hasCost =
    cost &&
    (cost.usd !== undefined || cost.bytesScanned !== undefined || cost.rowsScanned !== undefined);
  const view: PermissionSubjectView = {
    capability: stringField(subject, "capability") ?? undefined,
    effect: permissionEffect(stringField(subject, "effect")),
    operation: stringField(subject, "operation") ?? undefined,
    targets,
    cost: hasCost ? cost : undefined,
    confidence: permissionConfidence(stringField(subject, "confidence")),
    reasons: arrayField(subject, "reasons").filter(isString),
    scope: stringField(subject, "scope") === "command" ? "command" : undefined,
  };
  if (
    !view.capability &&
    !view.effect &&
    !view.operation &&
    view.targets.length === 0 &&
    !view.cost &&
    view.reasons.length === 0
  ) {
    return null;
  }
  return view;
}

/** The tiers this build has a slot for. Ordered as the classifier grades them,
 *  read first. */
const PERMISSION_EFFECTS = new Set<string>([
  "read",
  "write",
  "destroy",
  "egress",
  "exec",
  "memory",
]);

/** The classifier's verdict, as the card and the approval gate read it.
 *
 *  A word with no slot here becomes `unknown` rather than nothing. The two are
 *  not the same fact: an ask whose verdict cannot be read is one the classifier
 *  DID grade, and the machine reads any grade that is not `read` as a write —
 *  so erasing the word turned a write into an unclassified ask, whose kind then
 *  spoke for it. For `network` and `other`, which no kind calls a write, that
 *  offered the reader an approval the box goes on to drop.
 *
 *  Missing and empty stay nothing, because that is what the machine reads them
 *  as too: no verdict, so the kind answers. */
function permissionEffect(raw: string | null): PermissionEffect | undefined {
  if (raw === null || raw === "") return undefined;
  return PERMISSION_EFFECTS.has(raw) ? (raw as PermissionEffect) : "unknown";
}

function permissionConfidence(raw: string | null): "exact" | "heuristic" | "unknown" | undefined {
  return raw === "exact" || raw === "heuristic" || raw === "unknown" ? raw : undefined;
}

/** What an edge whose transformation nobody could characterize carries. Mirrors
 *  `permissions/impact.py`. It stays visible, because such an edge under a
 *  destructive change is exactly what sends the decision to a human. */
const UNKNOWN_TRANSFORMATION = "unknown";

/** Map the serialized ImpactAssessment (`impact`) carried on a 1.4.0+
 *  permission.request onto a UI view: what the write reaches, what is filed
 *  against it, who owns it, and whether the read is whole.
 *
 *  Anything unreadable degrades the STATUS rather than being dropped, because an
 *  affected list the card cannot vouch for must not be shown as a clean one.
 *  Null only when the gate consulted no graph at all, or when the assessment
 *  carries nothing to say. The card then renders exactly as it did before. */
function permissionImpact(raw: HarnessEvent): PermissionImpactView | null {
  const impact = objectField(raw, "impact");
  if (!impact) return null;
  const affected: PermissionAffectedView[] = arrayField(impact, "affected").flatMap((row) => {
    const urn = stringField(row, "urn");
    if (!urn) return [];
    return [
      {
        urn,
        category: stringField(row, "category") ?? "",
        // A pre-taxonomy or untypeable edge reads "unknown" rather than blank.
        transformation: stringField(row, "transformation") || UNKNOWN_TRANSFORMATION,
        reason: stringField(row, "reason") ?? undefined,
      },
    ];
  });
  const concepts: PermissionConceptView[] = arrayField(impact, "concepts").flatMap((row) => {
    const meaning = stringField(row, "meaning")?.trim();
    if (!meaning) return [];
    return [
      {
        urn: stringField(row, "urn") ?? "",
        itemId: stringField(row, "item_id") ?? "",
        title: stringField(row, "title") ?? "",
        meaning,
        ownerTeams: stringArrayField(row, "owner_teams"),
      },
    ];
  });
  const view: PermissionImpactView = {
    status: stringField(impact, "status") === "resolved" ? "resolved" : "degraded",
    category: stringField(impact, "category") ?? "",
    targets: stringArrayField(impact, "targets"),
    affected,
    concepts,
    ownerTeams: stringArrayField(impact, "owner_teams").filter((team) => team.trim().length > 0),
    unsure: boolField(impact, "unsure"),
    reason: stringField(impact, "reason")?.trim() ?? "",
  };
  const empty =
    !view.affected.length && !view.concepts.length && !view.targets.length && !view.reason;
  return empty ? null : view;
}

function patchPermissionResolved(state: ConversationFoldState, raw: HarnessEvent): void {
  const requestId = stringField(raw, "request_id");
  const optionId = stringField(raw, "option_id");
  // Who decided, when the server recorded the answer and so knew the roster. A
  // decision the harness settled on the machine names nobody, and so does a
  // policy or a timeout: the notice then carries the option alone.
  const decidedByName = deciderNameOf(raw);
  // Anything that is not an allow — reject_once / reject_always / cancelled, by
  // the user, the policy, or a broker timeout — means no write happened.
  if (requestId && !optionId?.startsWith("allow")) dropAskTimeEditPreview(state, requestId);
  if (requestId) state.asksAwaitingCall.delete(requestId);
  if (!requestId) return;
  settlePermission(state, requestId, optionId ?? undefined, decidedByName, {
    by: stringField(raw, "decided_by") ?? undefined,
    via: stringField(raw, "decided_via") ?? undefined,
  });
}

/** The neutral name for a decision a member made whose name the event does
 *  not carry — the same words the server writes on a Stop it cannot name. */
export const A_MEMBER_OF_THIS_WORKSPACE = "a member of this workspace";

/** Who decided, as the event says it. The server stamps the member's name on
 *  the answer it records; the box's own resolution of the same ask, and a
 *  policy's or a timeout's, name nobody. A decision the event attributes to a
 *  person without naming them is still a person's, so the neutral name stands
 *  in until — if ever — the named event lands. */
function deciderNameOf(raw: HarnessEvent): string | undefined {
  const named = stringField(raw, "decided_by_name")?.trim();
  if (named) return named;
  return stringField(raw, "decided_by") === "user" ? A_MEMBER_OF_THIS_WORKSPACE : undefined;
}

/** Whether `name` says more than `standing` does: a real name beats the neutral
 *  one, and anything beats nothing. */
function namesMore(name: string | undefined, standing: string | undefined): boolean {
  if (name === undefined) return false;
  if (standing === undefined) return true;
  return standing === A_MEMBER_OF_THIS_WORKSPACE && name !== A_MEMBER_OF_THIS_WORKSPACE;
}

/** Settle an ask on a decision, whichever telling of it lands first.
 *
 *  The same ask is resolved more than once on the wire — the server's record
 *  of the member's answer, the box's own resolution once it hears it — and
 *  they arrive in either order. The FIRST decision stands: the option is
 *  never rewritten by a later telling, and a later telling that names nobody
 *  never erases the name an earlier one carried, while a later one that
 *  names the member fills the name in. An ask whose request has not landed
 *  yet keeps the decision under its id, merged the same way. */
function settlePermission(
  state: ConversationFoldState,
  requestId: string,
  optionId: string | undefined,
  decidedByName: string | undefined,
  decider: { by?: string; via?: string } = {},
): void {
  const part = findPart(state, state.requestToPart.get(requestId) ?? requestId);
  if (part?.kind !== "permission") {
    if (part) return;
    const standing = state.settledAsks.get(requestId);
    const known = standing?.kind === "permission" ? standing : null;
    state.settledAsks.set(requestId, {
      kind: "permission",
      optionId: known?.optionId ?? optionId,
      decidedByName: namesMore(decidedByName, known?.decidedByName)
        ? decidedByName
        : known?.decidedByName,
      // Who and where are the first telling's, like the option: a later copy
      // (the box's, naming nobody) fills only what is still unknown.
      decidedBy: known?.decidedBy ?? decider.by,
      decidedVia: known?.decidedVia ?? decider.via,
    });
    return;
  }
  part.status = "resolved";
  if (part.selectedOptionId === undefined) part.selectedOptionId = optionId;
  if (namesMore(decidedByName, part.decidedByName)) part.decidedByName = decidedByName;
  if (part.decidedBy === undefined) part.decidedBy = decider.by;
  if (part.decidedVia === undefined) part.decidedVia = decider.via;
  state.askAttempts.delete(part.requestId);
}

/** A member's answer to a permission ask, as the server relays it the moment
 *  it records the answer — before the box has resolved the ask, and before the
 *  recorded row (which carries the member's name) is read back. The card
 *  settles here, in place, for every reader: on the option, and on the
 *  neutral name until the named record lands. */
export function foldRelayedAnswer(
  state: ConversationFoldState,
  answer: { requestId: string; optionId?: string },
): void {
  if (!answer.requestId || answer.optionId === undefined) return;
  markAllTurnsDirty(state);
  state.asksAwaitingCall.delete(answer.requestId);
  settlePermission(state, answer.requestId, answer.optionId, A_MEMBER_OF_THIS_WORKSPACE, {
    by: "user",
  });
}

function foldQuestionRequest(state: ConversationFoldState, raw: HarnessEvent): void {
  const requestId = stringField(raw, "request_id");
  if (!requestId) return;
  const questions = arrayField(raw, "questions").map((question) => ({
    question: stringField(question, "question") ?? "",
    header: stringField(question, "header"),
    options: arrayField(question, "options")
      .map((option) => ({
        label: stringField(option, "label") ?? "",
        description: stringField(option, "description"),
      }))
      .filter((option) => option.label.length > 0),
    multiple: boolField(question, "multiple"),
    custom: !hasField(question, "custom") ? true : boolField(question, "custom"),
  }));
  // File-based plan mode surfaces the plan's Markdown on the event (read from the
  // model's plan file by the adapter), NOT in the tool-call input — so the webview
  // renders it WITHOUT the full plan being re-fed into the model's context.
  const planMarkdown = stringField(raw, "plan_markdown");
  const existing = findPart(state, state.requestToPart.get(requestId) ?? requestId);
  if (existing?.kind === "question") {
    existing.questionKind =
      stringField(raw, "kind") === "plan_approval" ? "plan_approval" : "question";
    existing.toolCallId = stringField(raw, "tool_call_id");
    existing.questions = questions;
    if (planMarkdown) existing.planMarkdown = planMarkdown;
    // The status is left as it is: a question re-offered while still open
    // stays open, and one already answered keeps its answer — a re-announce
    // does not re-open a settled ask.
    return;
  }
  const toolCallId = stringField(raw, "tool_call_id");
  const part: QuestionConversationPart = {
    id: requestId,
    kind: "question",
    requestId,
    questionKind: stringField(raw, "kind") === "plan_approval" ? "plan_approval" : "question",
    toolCallId,
    questions,
    status: "pending",
  };
  if (planMarkdown) part.planMarkdown = planMarkdown;
  // Its answer is already on record: the question arrives closed, on it.
  const settled = state.settledAsks.get(requestId);
  if (settled?.kind === "question") {
    state.settledAsks.delete(requestId);
    part.status = settled.status;
    if (settled.status === "answered") {
      part.answers = settled.answers;
      if (settled.note) part.note = settled.note;
    } else part.reason = settled.reason;
  } else if (state.attemptId) {
    state.askAttempts.set(requestId, state.attemptId);
  }
  const turn = currentAssistantTurn(state);
  turn.parts.push(part);
  state.requestToPart.set(requestId, part.id);
}

function patchQuestion(
  state: ConversationFoldState,
  raw: HarnessEvent,
  status: "answered" | "rejected",
): void {
  const requestId = stringField(raw, "request_id");
  const part = requestId ? findPart(state, state.requestToPart.get(requestId) ?? requestId) : null;
  const answers = arrayField(raw, "answers").map((inner) =>
    Array.isArray(inner) ? inner.filter(isString) : [],
  );
  const reason = stringField(raw, "reason");
  const note = stringField(raw, "note");
  if (part?.kind !== "question") {
    // No part to settle yet: the question is still to come, and must come settled.
    if (requestId && !part) {
      state.settledAsks.set(
        requestId,
        status === "answered"
          ? { kind: "question", status, answers, note }
          : { kind: "question", status, reason },
      );
    }
    return;
  }
  part.status = status;
  if (status === "answered") {
    part.answers = answers;
    // The machine's own echo names no note; it never unsays the recorded one.
    if (note) part.note = note;
  } else part.reason = reason;
  if (requestId) state.askAttempts.delete(requestId);
}

function foldPlanUpdated(state: ConversationFoldState, raw: HarnessEvent): void {
  const turn = currentAssistantTurn(state);
  const entries = arrayField(raw, "entries").map((entry, index) => ({
    id: stringField(entry, "id") ?? `plan-${index}`,
    text:
      stringField(entry, "text") ??
      stringField(entry, "content") ??
      stringField(entry, "title") ??
      `Step ${index + 1}`,
    status: planStatus(stringField(entry, "status")),
  }));
  const existing = turn.parts.find((part) => part.kind === "plan");
  if (existing?.kind === "plan") existing.entries = entries;
  else
    turn.parts.push({ id: stringField(raw, "event_id") ?? newId("plan"), kind: "plan", entries });
}

function foldFileEdited(state: ConversationFoldState, raw: HarnessEvent): void {
  const path = stringField(raw, "path");
  if (!path) return;
  const insertions = numberField(raw, "insertions");
  const deletions = numberField(raw, "deletions");
  const resource = fileResource(
    path,
    insertions !== null && deletions !== null ? { insertions, deletions } : undefined,
    resourcePreviewField(raw),
  );
  state.touchedFiles.set(path, resource);

  // A write/edit tool call already renders its own card. Attach this edit's
  // resource (path + diff preview) to that part instead of pushing a second,
  // redundant card. Only emit a standalone file_edited card when no matching
  // tool call produced the edit (e.g. an external/indirect change).
  const tool = findWriteToolForPath(state, path);
  if (tool) {
    attachFileResource(state, tool, resource);
    return;
  }
  currentAssistantTurn(state).parts.push({
    id: stringField(raw, "event_id") ?? `file-${path}`,
    kind: "file_edited",
    resource,
  });
}

/** Merge one file resource per path without losing fields from an earlier frame. */
function attachFileResource(
  state: ConversationFoldState,
  tool: ToolConversationPart,
  next: ResourceReference,
): void {
  const askRequestId = askTimeEditPreviewOn(state, tool.id, next.target);
  if (askRequestId) state.askTimeEditPreviews.delete(askRequestId);
  const existing = tool.resources ?? [];
  const prior = existing.find((item) => item.target === next.target);
  if (!prior) {
    tool.resources = [...existing, next];
    return;
  }
  const merged: ResourceReference = {
    ...prior,
    ...next,
    diff: next.diff ?? prior.diff,
    preview: next.preview ?? prior.preview,
  };
  tool.resources = existing.map((item) => (item === prior ? merged : item));
}

/** Find a write/edit tool part in the current turn whose input path matches
 *  the edited file, so file.edited can enrich it rather than duplicate it. */
function findWriteToolForPath(
  state: ConversationFoldState,
  path: string,
): ToolConversationPart | null {
  const turn = state.turns.find((t) => t.id === state.currentAssistantTurnId);
  if (!turn) return null;
  for (let i = turn.parts.length - 1; i >= 0; i -= 1) {
    const part = turn.parts[i];
    if (part.kind !== "tool") continue;
    if (!/write|edit/i.test(`${part.toolKind ?? ""} ${part.name}`)) continue;
    // Already carries this file's diff → a later edit of the same path belongs
    // to a different call. An ASK-time diff is the exception: it is a
    // placeholder for this very write, waiting to be superseded.
    if (
      part.resources?.some((resource) => resource.target === path) &&
      !askTimeEditPreviewOn(state, part.id, path)
    ) {
      continue;
    }
    if (writeToolPath(part) === path) return part;
  }
  return null;
}

/** The file a write/edit tool call targets, across the harnesses' input spellings. */
function writeToolPath(part: ToolConversationPart): string | null {
  const input = part.input ?? {};
  const path = input.path ?? input.filePath ?? input.file_path ?? input.file;
  return typeof path === "string" ? path : null;
}

// (foldSubagentStarted / patchSubagentCompleted removed — a spawn now folds as one
// `spawn_agent` tool card. The `SubagentConversationPart` type + SubagentCard stay
// dormant; the fold that produces them returns with the background-agent feature.)

function foldCompactionStarted(state: ConversationFoldState, raw: HarnessEvent): void {
  const partId = stringField(raw, "event_id") ?? stringField(raw, "id") ?? newId("compaction");
  const part: CompactionConversationPart = {
    id: partId,
    kind: "compaction",
    title: compactionTitle(raw),
    text: compactionText(raw),
    streaming: true,
  };
  upsertCompaction(state, stringField(raw, "message_id"), part);
  state.currentCompactionPartId = part.id;
}

function patchCompactionDelta(state: ConversationFoldState, raw: HarnessEvent): void {
  const text = compactionText(raw);
  if (!text) return;
  const part = currentCompactionPart(state) ?? createCurrentCompaction(state, raw);
  part.text += text;
  part.streaming = true;
}

function patchCompactionEnded(state: ConversationFoldState, raw: HarnessEvent): void {
  const part = currentCompactionPart(state) ?? createCurrentCompaction(state, raw);
  const text = compactionText(raw);
  if (text) part.text = text;
  part.title = compactionTitle(raw, part.title);
  part.streaming = false;
}

/** `SessionStatusChanged(phase="compacting")` — the fold started. Opens the
 *  card in its running state and holds it there; the part is only settled by
 *  the summary or by the turn ending, never by the next status. */
function beginCompaction(state: ConversationFoldState, raw: HarnessEvent): void {
  const open = currentCompactionPart(state);
  if (open?.streaming) return;
  const part: CompactionConversationPart = {
    id: stringField(raw, "event_id") ?? newId("compaction"),
    kind: "compaction",
    title: compactionTitle(raw),
    text: "",
    streaming: true,
    startedAt: stringField(raw, "time") ?? undefined,
  };
  upsertCompaction(state, stringField(raw, "message_id"), part);
  state.currentCompactionPartId = part.id;
}

function foldCompaction(state: ConversationFoldState, raw: HarnessEvent): void {
  const partId =
    stringField(raw, "part_id") ??
    stringField(raw, "event_id") ??
    stringField(raw, "id") ??
    newId("compaction");
  const elided = arrayField(raw, "summarised_message_ids").filter(isString);
  // The running card and the summary are ONE event to the reader: the card
  // opened by the `compacting` phase settles in place, keeping the durable id
  // the summary arrived under so a link to it still resolves.
  const open = currentCompactionPart(state);
  const part: CompactionConversationPart = open?.streaming
    ? open
    : { id: partId, kind: "compaction", text: "", streaming: false };
  if (open?.streaming) rekeyPart(state, open, partId);
  part.title = compactionTitle(raw, part.title);
  part.text = compactionText(raw);
  part.streaming = false;
  part.summarisedTurns = elided.length;
  if (state.contextTokens !== null) part.tokensBefore = state.contextTokens;
  if (!open?.streaming) upsertCompaction(state, stringField(raw, "message_id"), part);
  state.currentCompactionPartId = part.id;
  // The window AFTER the fold is not in this event — it is what the next reply
  // reports carrying, so the card fills that half when the figure exists.
  state.compactionAwaitingAfter = part.id;
  dimCompactedTurns(state, raw, part.id);
}

/** Move a part onto the id the durable event gave it, keeping the turn index
 *  in step so a later lookup (and the detail page's link) still finds it. */
function rekeyPart(
  state: ConversationFoldState,
  part: CompactionConversationPart,
  nextId: string,
): void {
  if (part.id === nextId) return;
  const turnId = state.partToTurn.get(part.id);
  state.partToTurn.delete(part.id);
  part.id = nextId;
  if (turnId) state.partToTurn.set(nextId, turnId);
}

/** The conversation tokens a usage report describes: input plus both cache
 *  halves — what the model actually carried, and what the harness's own
 *  overflow check measures. `total` includes the reply and is only a fallback. */
function contextTokensOf(tokens: Record<string, number> | null): number | null {
  if (!tokens) return null;
  const parts = ["input", "cache_read", "cache_write"];
  let sum = 0;
  let saw = false;
  for (const key of parts) {
    const value = tokens[key];
    if (typeof value === "number") {
      sum += value;
      saw = true;
    }
  }
  if (saw) return sum;
  return typeof tokens.total === "number" ? tokens.total : null;
}

/** Record what a turn reported carrying: on the turn (the composer's meter
 *  reads the newest one) and on the fold (the next compaction's "before"), and
 *  close out a compaction still waiting for its "after". */
function noteContextTokens(
  state: ConversationFoldState,
  turn: ConversationTurn | undefined,
  raw: HarnessEvent,
): void {
  const tokens = contextTokensOf(recordNumberField(raw, "tokens"));
  if (tokens === null) return;
  state.contextTokens = tokens;
  if (turn) turn.contextTokens = tokens;
  const awaiting = state.compactionAwaitingAfter;
  if (!awaiting) return;
  const part = findPart(state, awaiting);
  if (part?.kind === "compaction") part.tokensAfter = tokens;
  state.compactionAwaitingAfter = null;
}

/** Dim every turn a compaction elided from the agent's live context. The
 *  daemon's `summarised_message_ids` are exactly the folded turn ids, so each
 *  matching turn is marked `cleared` — kept readable but visibly out of
 *  context, the same treatment as a /clear. This is the seam that covers BOTH
 *  manual /compact and automatic (context-overflow) compaction: both arrive as
 *  one `compaction.applied` carrying the elided set, so neither needs a UI-side
 *  hook. The summary card's own turn is never dimmed — it's the bright boundary
 *  marker for the retained tail. */
function dimCompactedTurns(
  state: ConversationFoldState,
  raw: HarnessEvent,
  compactionPartId: string,
): void {
  const elided = new Set(arrayField(raw, "summarised_message_ids").filter(isString));
  if (elided.size === 0) return;
  const compactionTurnId = state.partToTurn.get(compactionPartId);
  for (const turn of state.turns) {
    if (turn.id === compactionTurnId || !elided.has(turn.id)) continue;
    turn.cleared = true;
    turn.clearedReason = "summarised";
  }
}

function foldTurnStarted(state: ConversationFoldState, raw: HarnessEvent): void {
  const turnId = stringField(raw, "turn_id") ?? newId("turn");
  state.currentTurnId = turnId;
  state.pendingAssistantStartedAt = stringField(raw, "time") ?? state.pendingAssistantStartedAt;
  const turn = state.currentAssistantTurnId
    ? state.turns.find((candidate) => candidate.id === state.currentAssistantTurnId)
    : undefined;
  if (!turn) return;
  turn.status = "running";
  turn.startedAt = stringField(raw, "time") ?? turn.startedAt;
}

function foldTurnFinished(state: ConversationFoldState, raw: HarnessEvent): void {
  const turn = currentAssistantTurn(state);
  const stopReason = stringField(raw, "stop_reason");
  const errorDetail = stringField(raw, "error_detail");
  // The raw "agent exited unexpectedly (rc=-15)" string is never shown in
  // the summary — the accompanying session.status_changed=error carries a
  // single polished message instead. Keep the turn's real stop reason.
  const isProcessExit = errorDetail !== null && isProcessExitText(errorDetail);
  // A genuine error shown in this summary marks the exchange as errored, so a
  // trailing rc=-15 teardown dedups against it instead of double-reporting.
  if (errorDetail && !isProcessExit) state.sawTurnError = true;
  turn.status =
    stopReason === "cancelled" ? "cancelled" : stopReason === "error" ? "error" : "done";
  turn.completedAt = stringField(raw, "time") ?? undefined;
  for (const part of turn.parts) part.streaming = false;
  turn.parts.push({
    id: stringField(raw, "event_id") ?? newId("summary"),
    kind: "turn_summary",
    summary: stringField(raw, "summary") ?? undefined,
    stopReason: stopReason ?? undefined,
    costUsd: numberField(raw, "cost_usd"),
    tokens: recordNumberField(raw, "tokens"),
    files: [...state.touchedFiles.values()],
    importantFiles: resourceArrayField(raw, "important_files"),
    artifacts: resourceArrayField(raw, "artifacts"),
    graphNodes: resourceArrayField(raw, "graph_nodes"),
    runs: resourceArrayField(raw, "runs"),
    errorDetail: isProcessExit ? null : errorDetail,
  });
  noteContextTokens(state, turn, raw);
  state.touchedFiles.clear();
  state.currentTurnId = null;
  state.currentAssistantTurnId = null;
  state.pendingAssistantStartedAt = null;
}

function foldMessageCompleted(state: ConversationFoldState, raw: HarnessEvent): void {
  const messageId = stringField(raw, "message_id");
  const turnId = messageId ? state.messageToTurn.get(messageId) : undefined;
  const turn = turnId ? state.turns.find((candidate) => candidate.id === turnId) : undefined;
  const errorText = completionErrorText(raw);
  if (turn) {
    turn.status = errorText ? "error" : "done";
    turn.completedAt = stringField(raw, "time") ?? turn.completedAt;
    // The message is over: nothing will settle a part of it the box never
    // wrote down, so it goes (see `closeOpenTextishParts`); the rest are
    // settled. Only this message's parts — another message's open part is
    // still the box's to finish.
    for (const partId of [...state.openTextPartIds]) {
      if (state.partToTurn.get(partId) !== turn.id) continue;
      state.openTextPartIds.delete(partId);
      state.closedTextPartIds.add(partId);
      if (state.wordlessPartIds.has(partId)) {
        state.wordlessPartIds.delete(partId);
        dropUnsettledPart(state, partId);
      }
    }
    for (const part of turn.parts) part.streaming = false;
  }
  noteContextTokens(state, turn, raw);
  if (errorText) recordError(state, errorText);
}

/** How many attempts at a model call the transcript reports as a retry in
 *  progress. Past it the notice stops promising and tells the reader what to
 *  do: a call that has failed this often is not going to answer soon. */
export const MODEL_RETRY_LIMIT = 3;

/** What the box says it is retrying, as a class the reader can act on. The
 *  box's own sentence is never shown: it is written for an operator, and it
 *  can name the host or address the call was going to — which is the
 *  platform's, never the tenant's to see. */
export type ModelRetryClass = "unreachable" | "rate_limited" | "overloaded" | "failed";

export function modelRetryClass(reason: string | null | undefined): ModelRetryClass {
  const said = reason ?? "";
  if (/rate.?limit|too many requests|\b429\b/i.test(said)) return "rate_limited";
  if (/overload|\b529\b|\b503\b|temporarily unavailable|capacity/i.test(said)) return "overloaded";
  if (/connect|unreachable|not reachable|resolve|dns|timed? ?out|timeout|network|socket|ECONN|ENOTFOUND|EAI_AGAIN|fetch failed/i.test(said)) {
    return "unreachable";
  }
  return "failed";
}

const RETRY_HEADLINE: Record<ModelRetryClass, string> = {
  unreachable: "Can't reach the model.",
  rate_limited: "The model is rate limited.",
  overloaded: "The model is overloaded.",
  failed: "The model call failed.",
};

/** The one line a retried model call reads as, at a given attempt. */
export function modelRetryNotice(reason: string | null | undefined, attempt: number): string {
  const headline = RETRY_HEADLINE[modelRetryClass(reason)];
  if (attempt >= MODEL_RETRY_LIMIT) {
    return `${headline} Still failing after ${attempt} attempts.`;
  }
  return attempt > 1 ? `${headline} Retrying (attempt ${attempt})…` : `${headline} Retrying…`;
}

/** The box is retrying a model call. One notice stands for the whole run of
 *  retries and is rewritten in place, so a call retried for an hour is one
 *  line that says how long it has been failing, not sixty. */
function foldRetrying(state: ConversationFoldState, raw: HarnessEvent): void {
  const attempt = numberField(raw, "attempt") ?? 1;
  const text = modelRetryNotice(stringField(raw, "reason"), attempt);
  const standing = state.turns.find((turn) => turn.id === state.retryNoticeTurnId);
  const part = standing?.parts[0];
  if (part && part.kind === "system") {
    part.text = text;
    part.tone = attempt >= MODEL_RETRY_LIMIT ? "error" : "warning";
    return;
  }
  const id = newId("retry");
  state.turns.push({
    id,
    author: "system",
    status: "done",
    parts: [{ id: `${id}-part`, kind: "system", text, tone: attempt >= MODEL_RETRY_LIMIT ? "error" : "warning" }],
  });
  state.retryNoticeTurnId = id;
}

/** What a retry is waiting for has come: the model answered (any part or tool
 *  the box writes), or the turn ended. The status and bookkeeping rows the box
 *  writes between attempts are not either. */
function retryIsOver(eventType: string, raw: HarnessEvent): boolean {
  if (eventType === "retrying") return false;
  if (eventType === "session.status_changed") return stringField(raw, "status") !== "running";
  if (eventType === "message.created" || eventType.startsWith("session.")) return false;
  return true;
}

function clearRetryNotice(state: ConversationFoldState): void {
  const id = state.retryNoticeTurnId;
  state.retryNoticeTurnId = null;
  const at = state.turns.findIndex((turn) => turn.id === id);
  if (at >= 0) state.turns.splice(at, 1);
  markAllTurnsDirty(state);
}

function foldStatusChanged(state: ConversationFoldState, raw: HarnessEvent): void {
  const status = stringField(raw, "status");
  const attempt = stringField(raw, "turn_id");
  if (status === "running" && attempt) state.attemptId = attempt;
  if (status === "running") state.sessionWorking = true;
  // The turn is over, so a message waiting behind it is next: whatever the box
  // writes from here answers it.
  if (status === "idle" || status === "error" || status === "aborted") {
    state.sessionWorking = false;
    state.queuedBehindTurn.clear();
  }
  // The harness announces the fold BEFORE the summary exists, and the phase it
  // announces it with is overwritten by the next one microseconds later. So the
  // phase opens a card that stays open on its own: only the summary landing (or
  // the turn ending) settles it. Without it a ten- to eighty-second fold is
  // indistinguishable from a stalled turn.
  if (stringField(raw, "phase") === "compacting") {
    beginCompaction(state, raw);
    return;
  }
  if (status === "error") {
    recordError(
      state,
      stringField(raw, "detail") ?? "The agent stopped on an error.",
      readRefusal(objectField(raw, "refusal")),
    );
    return;
  }
  // The box's own word that nothing is waiting on it. An ask still open on a
  // card at that point is one nobody is holding — its harness has moved past
  // it, or a re-announce outlived its resolution — and an answer to it has
  // nowhere to land: the server refuses it as already answered, and the
  // composer stays behind a card that cannot clear. Closed here, as no longer
  // needed: no option is claimed, since nobody chose one. As the runtime reads
  // it, an idle stamped with a DIFFERENT attempt is inert for an ask raised
  // under another; one stamped with the ask's own attempt, or with none,
  // closes it.
  if (status === "idle") {
    flushPendingSystemNotices(state);
    closeOpenAsks(state, attempt);
    // The turn is over: a part the box never wrote down goes with it, the
    // rest are settled (see `closeOpenTextishParts`).
    closeOpenTextishParts(state, null, null);
    return;
  }
  // A turn stopped from OUTSIDE the model — the mirror's wall-clock / tool /
  // token budget, or a workspace that came back to find the turn gone — ends
  // as `aborted` carrying the sentence that explains it. Without a reader the
  // turn simply stopped: no reason on screen, and the composer stayed in its
  // working state until the 60 s stall watchdog gave up on it. A plain stop
  // (someone pressed Stop) carries no detail and stays silent.
  if (status !== "aborted" && status !== "cancelled") return;
  flushPendingSystemNotices(state);
  // The turn is over either way. A detail is a SENTENCE to show, not the
  // condition for believing the terminal: reading it as one left a turn the
  // box had ended still streaming — the composer waiting, the tool card
  // running — on the one abort that carries no sentence, which is the plain
  // one a reader's Stop produces. Settling first is what the status means.
  // A turn ended, so a turn existed — but WHOSE. When there was a live turn to
  // settle, the terminal is about that one, and a message queued behind it is
  // a message the box was never handed: withdrawing its "never run" there told
  // the reader something was sent that never left the lane. Only when the
  // terminal settles NOTHING is it the sole row about a turn the box was
  // stopped before it could say a word of — and then it is that turn's, and
  // the message under it was sent after all.
  if (!settleLiveTurn(state, stringField(raw, "time"))) machineIsAnswering(state);
  const detail = stringField(raw, "detail");
  if (!detail) return;
  addSystemTurn(state, detail, "warning");
}

/** A synthetic part still waiting for its message row when the turn reaches
 *  its terminal status is shown as the notice it most likely was: the row it
 *  waited for is not coming with the turn over, and a note the server wrote
 *  is worth more on screen than a steering line is worth hiding. */
function flushPendingSystemNotices(state: ConversationFoldState): void {
  for (const [messageId, held] of state.pendingSystemNotices) {
    for (const part of held) {
      state.suppressedSyntheticPartIds.delete(part.partId);
      addSystemNoticePart(state, messageId, part.partId, part.text);
    }
  }
  state.pendingSystemNotices.clear();
}

/** Close every ask still open under `attempt` (or under any, for an idle
 *  that names none), without a decision: the machine is not waiting on them.
 *  An ask raised under a different attempt is left as it is. */
function closeOpenAsks(state: ConversationFoldState, attempt: string | null): void {
  const closes = (requestId: string): boolean => {
    const raisedUnder = state.askAttempts.get(requestId);
    return attempt === null || raisedUnder === undefined || raisedUnder === attempt;
  };
  for (const turn of state.turns) {
    for (const part of turn.parts) {
      if (part.kind === "permission" && part.status === "pending" && closes(part.requestId)) {
        part.status = "resolved";
        state.askAttempts.delete(part.requestId);
      } else if (part.kind === "question" && part.status === "pending" && closes(part.requestId)) {
        part.status = "rejected";
        state.askAttempts.delete(part.requestId);
      }
    }
  }
}

/** End the assistant turn that was in flight when something outside it said
 *  stop: nothing can stream into it and nobody can answer its asks any more. */
/** Returns whether there WAS a live turn to settle — which is the same
 *  question as "is this terminal about a turn the reader can see". */
function settleLiveTurn(state: ConversationFoldState, time: string | null | undefined): boolean {
  const turn =
    state.turns.find((candidate) => candidate.id === state.currentAssistantTurnId) ??
    state.turns[state.turns.length - 1];
  if (!turn || turn.author !== "assistant" || turn.completedAt != null) return false;
  // The turn is over: a text or thought the box never settled goes with it
  // (see `closeOpenTextishParts`).
  closeOpenTextishParts(state, null, null);
  for (const part of turn.parts) {
    part.streaming = false;
    if (part.kind === "permission" && part.status === "pending") part.status = "resolved";
    else if (part.kind === "question" && part.status === "pending") part.status = "rejected";
    else if (part.kind === "tool" && (part.state === "pending" || part.state === "running"))
      part.state = "error";
  }
  turn.status = "cancelled";
  turn.completedAt = time ?? new Date().toISOString();
  state.currentTurnId = null;
  state.currentAssistantTurnId = null;
  state.pendingAssistantStartedAt = null;
  return true;
}

function ensureTurnForMessage(
  state: ConversationFoldState,
  messageId: string | null | undefined,
  fallbackAuthor: "assistant" | "user" | "system",
): ConversationTurn {
  if (messageId) {
    const turnId = state.messageToTurn.get(messageId);
    const existing = turnId ? state.turns.find((turn) => turn.id === turnId) : undefined;
    if (existing) return existing;
  }
  const announced = messageId ? state.announcedUserMessages.has(messageId) : false;
  const author = announced ? "user" : fallbackAuthor;
  if (author !== "user") promptsWereTaken(state);
  if (author === "assistant") machineIsAnswering(state);
  const turn: ConversationTurn = {
    id: messageId ?? newId("message"),
    author,
    status: "done",
    parts: [],
  };
  if (announced && messageId) {
    turn.startedAt = state.announcedUserMessages.get(messageId);
    state.announcedUserMessages.delete(messageId);
  }
  if (author === "assistant") placeAboveQueuedPrompts(state, turn);
  else state.turns.push(turn);
  if (messageId) state.messageToTurn.set(messageId, turn.id);
  // Where the box's echo of a person's message stands in the transcript.
  if (author === "user" && state.foldingSeq !== null) state.turnSeq.set(turn.id, state.foldingSeq);
  if (author === "assistant") state.currentAssistantTurnId = turn.id;
  return turn;
}

function workTurnForMessage(
  state: ConversationFoldState,
  messageId: string | null | undefined,
): ConversationTurn {
  const turn = ensureTurnForMessage(state, messageId, "assistant");
  return turn.author === "user" ? currentAssistantTurn(state) : turn;
}

function currentAssistantTurn(state: ConversationFoldState): ConversationTurn {
  const existing = state.currentAssistantTurnId
    ? state.turns.find((turn) => turn.id === state.currentAssistantTurnId)
    : undefined;
  if (existing) return existing;
  machineIsAnswering(state);
  const turn: ConversationTurn = {
    id: newId("assistant"),
    author: "assistant",
    status: "running",
    parts: [],
  };
  placeAboveQueuedPrompts(state, turn);
  state.currentAssistantTurnId = turn.id;
  return turn;
}

/** Add an assistant turn to the transcript. A message still waiting behind the
 *  turn the box is working stays below the rest of that turn: the box has not
 *  taken it, and a new message of the running turn is that turn's, not the
 *  waiting message's answer. Anywhere else the turn goes at the end. */
function placeAboveQueuedPrompts(state: ConversationFoldState, turn: ConversationTurn): void {
  let at = state.turns.length;
  while (at > 0) {
    const below = state.turns[at - 1];
    if (below.author !== "user" || !awaitingEcho(below) || !state.queuedBehindTurn.has(below.id)) break;
    at -= 1;
  }
  state.turns.splice(at, 0, turn);
}

function upsertTool(
  state: ConversationFoldState,
  messageId: string | null | undefined,
  tool: ToolConversationPart,
): void {
  const partId = state.toolToPart.get(tool.callId);
  const existing = partId ? findPart(state, partId) : null;
  if (existing?.kind === "tool") {
    Object.assign(existing, tool);
    drainAskTimeEditPreviews(state);
    drainAsksAwaitingCall(state);
    return;
  }
  const turn = ensureTurnForMessage(state, messageId, "assistant");
  // The model ran a tool, so whatever it was saying or thinking above is said
  // and thought. Only on the call's FIRST sight: a later update lands under a
  // part that may legitimately be streaming by then.
  closeOpenTextishParts(state, null, turn.id);
  turn.parts.push(tool);
  state.toolToPart.set(tool.callId, tool.id);
  state.partToTurn.set(tool.id, turn.id);
  drainAskTimeEditPreviews(state);
  drainAsksAwaitingCall(state);
}

/** A backgrounded SQL/bash job shows as two cards, each its own standalone turn: the
 *  START breadcrumb `bgjob:<id>` (emitted at submit, resolved on completion) where the
 *  agent launched it, and the FINISH card `bgdone:<id>` (emitted on completion) carrying
 *  the result — a new id, so its turn lands at the conversation tail when the job ends. */
function isBackgroundCallId(callId: string): boolean {
  return callId.startsWith("bgjob:") || callId.startsWith("bgdone:");
}

/** A prompt the runtime issued on its own behalf: the synthetic "Backgrounded Tool
 *  Finished" wake, or a `<harness_turn>` such as the analyst verification pass. The
 *  MODEL acts on it; the user never sees the envelope (a finished job renders as its
 *  own tool card). Suppress any user text part that is one. */
function isBackgroundWake(text: string): boolean {
  const body = text.trimStart();
  return body.startsWith("<backgrounded_tool") || body.startsWith("<harness_turn");
}

/** The `job_id` a foreground `bash`/`sql.query(background=true)` STUB result carries
 *  (it returns `{job_id, note}` when it detaches the work). Non-empty ONLY for such a
 *  stub, so its redundant card is dropped in favor of the `bgjob:<id>` lifecycle one.
 *  A payload carrying actual rows is a RESULT whatever else it says, so it never
 *  reads as a stub: dropping it would eat a completed query's card. */
function backgroundStubJobId(output: unknown): string {
  let obj = output;
  if (typeof obj === "string") {
    try {
      obj = JSON.parse(obj);
    } catch {
      return "";
    }
  }
  if (obj && typeof obj === "object" && !Array.isArray(obj)) {
    const record = obj as Record<string, unknown>;
    if (Array.isArray(record.preview_rows) && record.preview_rows.length > 0) return "";
    const jobId = record.job_id;
    return typeof jobId === "string" ? jobId : "";
  }
  return "";
}

/** The standalone turn a background card lives in. Deterministically keyed by the
 *  callId (so a replay folds identically), and DELIBERATELY never set as
 *  currentAssistantTurnId — an out-of-band card must not capture the live turn's
 *  streaming parts or absorb its turn_summary (foldTurnFinished). */
function backgroundCardTurn(state: ConversationFoldState, callId: string): ConversationTurn {
  const known = state.messageToTurn.get(callId);
  const existing = known ? state.turns.find((turn) => turn.id === known) : undefined;
  if (existing) return existing;
  const turn: ConversationTurn = {
    id: `bgcard:${callId}`,
    author: "assistant",
    status: "done",
    parts: [],
  };
  state.turns.push(turn);
  state.messageToTurn.set(callId, turn.id);
  return turn;
}

function upsertBackgroundTool(state: ConversationFoldState, tool: ToolConversationPart): void {
  const partId = state.toolToPart.get(tool.callId);
  const existing = partId ? findPart(state, partId) : null;
  if (existing?.kind === "tool") {
    // A late RUNNING ToolCall (it lost a race to the terminal update) must never
    // downgrade an already-finished card back to running or drop its result — keep
    // the terminal state/streaming.
    const settled = toolSettled(existing.state);
    const next =
      settled && tool.state === "running"
        ? { ...tool, state: existing.state, streaming: false }
        : tool;
    Object.assign(existing, next);
    return;
  }
  const turn = backgroundCardTurn(state, tool.callId);
  turn.parts.push(tool);
  state.toolToPart.set(tool.callId, tool.id);
  state.partToTurn.set(tool.id, turn.id);
}

function upsertCompaction(
  state: ConversationFoldState,
  messageId: string | null | undefined,
  compaction: CompactionConversationPart,
): void {
  const existing = findPart(state, compaction.id);
  if (existing?.kind === "compaction") {
    Object.assign(existing, compaction);
    return;
  }
  const turn = workTurnForMessage(state, messageId);
  turn.parts.push(compaction);
  state.partToTurn.set(compaction.id, turn.id);
}

function currentCompactionPart(state: ConversationFoldState): CompactionConversationPart | null {
  const part = findPart(state, state.currentCompactionPartId ?? undefined);
  return part?.kind === "compaction" ? part : null;
}

function createCurrentCompaction(
  state: ConversationFoldState,
  raw: HarnessEvent,
): CompactionConversationPart {
  const part: CompactionConversationPart = {
    id: stringField(raw, "event_id") ?? stringField(raw, "id") ?? newId("compaction"),
    kind: "compaction",
    title: compactionTitle(raw),
    text: "",
    streaming: true,
  };
  upsertCompaction(state, stringField(raw, "message_id"), part);
  state.currentCompactionPartId = part.id;
  return part;
}

function findPart(
  state: ConversationFoldState,
  partId: string | undefined,
): ConversationPart | null {
  if (!partId) return null;
  for (const turn of state.turns) {
    const part = turn.parts.find((p) => p.id === partId);
    if (part) return part;
  }
  return null;
}

function removePart(state: ConversationFoldState, partId: string): void {
  for (const turn of state.turns) {
    turn.parts = turn.parts.filter((part) => part.id !== partId);
  }
  state.partToTurn.delete(partId);
}

/** A person's message no box has echoed yet: it still stands under the id the
 *  send or the row gave it. Once echoed it is renamed to the box's message. */
function awaitingEcho(turn: ConversationTurn): boolean {
  return turn.id.startsWith("usr:") || turn.id.startsWith("local-");
}

function maybeDeduplicateOptimisticUser(
  turn: ConversationTurn,
  text: string,
  messageId: string | null | undefined,
): boolean {
  if (turn.author !== "user" || !turn.id.startsWith("local-")) return false;
  const existing = turn.parts[0];
  if (existing?.kind !== "text" || existing.text !== text) return false;
  if (messageId) turn.id = messageId;
  return true;
}

function maybeMergeOptimisticUser(
  state: ConversationFoldState,
  canonicalTurn: ConversationTurn,
  text: string,
  messageId: string | null | undefined,
): boolean {
  if (!messageId || canonicalTurn.author !== "user") return false;
  const optimistic = state.turns.find((turn) => {
    if (turn === canonicalTurn || turn.author !== "user" || !turn.id.startsWith("local-"))
      return false;
    const existing = turn.parts[0];
    return existing?.kind === "text" && existing.text === text;
  });
  if (!optimistic) return false;
  optimistic.id = messageId;
  state.messageToTurn.set(messageId, optimistic.id);
  state.turns = state.turns.filter((turn) => turn !== canonicalTurn);
  return true;
}

/** Whether `messageId` names a turn the daemon authored as `system`. */
function systemAuthored(
  state: ConversationFoldState,
  messageId: string | null | undefined,
): boolean {
  if (!messageId) return false;
  const turnId = state.messageToTurn.get(messageId);
  return state.turns.some((turn) => turn.id === turnId && turn.author === "system");
}

/** A daemon notice on a `system` message: its first line is the sentence the
 *  reader sees, anything after it the detail underneath (the statement a
 *  refusal quotes). Both render as text nodes. */
function addSystemNoticePart(
  state: ConversationFoldState,
  messageId: string | null | undefined,
  partId: string,
  raw: string,
): void {
  const [headline, ...rest] = raw.split("\n");
  if (!headline.trim()) return;
  const turn = ensureTurnForMessage(state, messageId, "system");
  const detail = rest.join(" ").trim();
  const existing = findPart(state, partId);
  if (existing && existing.kind === "system") {
    existing.text = headline;
    existing.detail = detail || undefined;
    return;
  }
  turn.parts.push({
    id: partId,
    kind: "system",
    text: headline,
    detail: detail || undefined,
    tone: "info",
  });
  state.partToTurn.set(partId, turn.id);
}

function addSystemTurn(
  state: ConversationFoldState,
  text: string,
  tone: "info" | "warning" | "error" = "info",
  detail?: string,
): void {
  state.turns.push({
    id: newId("system"),
    author: "system",
    status: "done",
    parts: [
      { id: newId("system-part"), kind: "system", text, tone, ...(detail ? { detail } : {}) },
    ],
  });
}

/** Append a command-result card as its own (bright) system turn, persisted in
 *  the fold rather than held as UI state. */
function addCommandTurn(state: ConversationFoldState, card: CommandCardData): void {
  const id = newId("command");
  state.turns.push({
    id,
    author: "system",
    status: card.tone === "error" ? "error" : "done",
    parts: [{ id: `${id}-part`, kind: "command", ...card }],
  });
}

// Surface a daemon error as one polished assistant-error turn. A process-exit
// (rc=-15) crash that follows an already-surfaced error is dropped as a
// downstream artifact; a crash with no prior error still shows one message.
function recordError(
  state: ConversationFoldState,
  rawText: string,
  refusal: RefusalFacts | null = null,
): void {
  const text = rawText || "The agent stopped on an error.";
  if (isProcessExitText(text) && state.sawTurnError) return;
  const failure = classifyTurnFailure(text, state.agentHost, refusal);
  addAssistantErrorTurn(state, failure.text, failure.cause, refusal);
}

function addAssistantErrorTurn(
  state: ConversationFoldState,
  text: string,
  failureCause: TurnFailureCause = "unknown",
  refusal: RefusalFacts | null = null,
): void {
  state.sawTurnError = true;
  const named = refusal ? namedRefusal(refusal.code) : undefined;
  const amounts = refusal && named?.detail ? named.detail(refusal) : null;
  if (
    state.turns.some(
      (turn) =>
        turn.author === "assistant" &&
        turn.status === "error" &&
        turn.parts.some(
          (part) => part.kind === "system" && part.tone === "error" && part.text === text,
        ),
    )
  )
    return;
  state.turns.push({
    id: newId("assistant-error"),
    author: "assistant",
    status: "error",
    parts: [
      {
        id: newId("assistant-error-part"),
        kind: "system",
        text,
        tone: "error",
        failureCause,
        ...(amounts ? { detail: amounts } : {}),
        ...(refusal?.resetsAt ? { failureResetsAt: refusal.resetsAt } : {}),
        ...(refusal?.manageUrl ? { failureManageUrl: refusal.manageUrl } : {}),
      },
    ],
  });
}

function completionErrorText(raw: HarnessEvent): string | null {
  const direct = stringField(raw, "error") ?? stringField(raw, "error_text");
  if (direct) return direct;
  const error = objectField(raw, "error");
  return (
    stringField(error, "message") ?? stringField(error, "detail") ?? stringField(error, "error")
  );
}

/** What the reader is told when the agent process died under the turn.
 *
 *  The editor's reader owns the machine it died on, so naming the backend and
 *  the model gateway is something they can act on. The portal's reader owns
 *  neither — their agent runs on the org's workspace machine — and telling them
 *  to check a gateway is the wrong diagnosis for a box that stopped answering,
 *  which is exactly what the kill drill put on screen beside a banner already
 *  saying the workspace was unreachable. */
export const AGENT_STOPPED_LOCALLY =
  "The agent stopped unexpectedly. Check that the backend and model gateway are running, then try again.";
export const WORKSPACE_STOPPED_ANSWERING =
  "The machine stopped answering, so the turn was lost.";

/** Why a turn failed, in the vocabulary the reader's shell answers with.
 *
 *  The wire gives one raw sentence — an upstream provider's message, the
 *  daemon's crash detail — and a raw sentence is not something a reader can
 *  act on: it names an HTTP status or a process return code. Classifying it
 *  once, here, lets the transcript show a plain cause AND lets the surface
 *  offer the one next step that cause has (a Retry for a provider that may
 *  answer next time) without either side parsing English.
 *
 *  A new open cause is added by appending a rule to {@link FAILURE_RULES} and
 *  giving the surface an arm for its tag. An extension names its own causes
 *  through `./refusals` (NAMED_REFUSALS, FAILURE_SENTENCES). */
export type TurnFailureCause =
  | "provider_unavailable"
  | "model_unavailable"
  | "context_too_long"
  | "workspace_stopped"
  | "agent_stopped"
  | "unknown"
  // A cause an extension names.
  | (string & {});

interface FailureRule {
  cause: TurnFailureCause;
  /** Matched against the raw wire sentence. */
  match: RegExp;
  /** The one sentence the reader sees. A function where the host changes it. */
  title: string | ((host: AgentHost) => string);
}

/** The open sentence rules. The surface routes the next step itself, so each
 *  title carries only the cause. */
const FAILURE_RULES: readonly FailureRule[] = [
  {
    cause: "model_unavailable",
    match: /no provider credentials|model_not_found|model not found|unknown model/iu,
    title: "That model is not available on this workspace.",
  },
  {
    cause: "context_too_long",
    match: /context_length_exceeded|context length|too many tokens/iu,
    title: "This conversation is too long for the model to answer.",
  },
  {
    cause: "provider_unavailable",
    match:
      /overloaded|rate.?limit|too many requests|\b(429|500|502|503|504)\b|upstream|timed? ?out|temporarily unavailable|service unavailable|gateway_error|could not reach the gateway/iu,
    title: "The model provider is unavailable.",
  },
];

/** The failure the harness ends a turn with once its model call has failed too
 *  often or for too long (`apps/cli/alkera_cli/harness/model_retry.py`):
 *  "<lead> after N attempts[ in D]; the turn was stopped." */
const MODEL_RETRY_GIVE_UP = /^[^;]* after \d+ attempts?(?: in \d+ (?:seconds?|minutes?))?; the turn was stopped\.$/u;

/** The daemon's synthetic crash detail when the agent exits on a signal. The
 *  raw string is never shown; the classifier maps it to a friendly one. */
function isRawProcessExitText(text: string): boolean {
  return /agent exited unexpectedly\s*\(rc=-?\d+\)/iu.test(text);
}

export interface TurnFailure {
  cause: TurnFailureCause;
  /** The sentence shown as the notice's title. */
  text: string;
}

/** The refusal facts on a persisted status event, or null when it carries
 *  none (most failures, and a row written by an older machine). */
export function readRefusal(raw: unknown): RefusalFacts | null {
  const code = stringField(raw, "code");
  if (!code) return null;
  return {
    code,
    teamName: stringField(raw, "team_name"),
    resetsAt: stringField(raw, "resets_at"),
    manageUrl: stringField(raw, "manage_url"),
    fields: raw,
  };
}

/** Classify one failure: by the refusal's code when an extension names it,
 *  else by the raw sentence. An extension's sentences are matched before the
 *  open rules, because a sentence it owns may also carry words the generic
 *  transport rule would otherwise claim. */
export function classifyTurnFailure(
  text: string,
  host: AgentHost,
  refusal?: RefusalFacts | null,
): TurnFailure {
  const named = refusal ? namedRefusal(refusal.code) : undefined;
  if (named && refusal) return { cause: named.cause, text: named.title(refusal) };
  for (const sentence of FAILURE_SENTENCES.items()) {
    if (sentence.match.test(text)) return { cause: sentence.cause, text: sentence.title };
  }
  // The harness's own sentence for a turn it stopped after retrying the model
  // call: already in the reader's words, naming the kind and the count. A
  // generic rule would claim it ("overloaded", "timed out") and drop the count.
  if (MODEL_RETRY_GIVE_UP.test(text)) return { cause: "provider_unavailable", text };
  if (isRawProcessExitText(text)) {
    return host === "workspace"
      ? { cause: "workspace_stopped", text: WORKSPACE_STOPPED_ANSWERING }
      : { cause: "agent_stopped", text: AGENT_STOPPED_LOCALLY };
  }
  for (const rule of FAILURE_RULES) {
    if (rule.match.test(text)) {
      return {
        cause: rule.cause,
        text: typeof rule.title === "string" ? rule.title : rule.title(host),
      };
    }
  }
  return { cause: "unknown", text };
}

/** A turn lost because the agent process died under it — either the raw detail
 *  above, or the sentence a cloud mirror already put in the reader's words. Both
 *  are reported once, by the status line, so the turn summary stays quiet. */
function isProcessExitText(text: string): boolean {
  return (
    isRawProcessExitText(text) ||
    /^the workspace (restarted while answering|stopped answering)/iu.test(text.trim())
  );
}

function fileResource(
  path: string,
  diff?: { insertions: number; deletions: number },
  preview?: ResourcePreview,
): ResourceReference {
  return {
    kind: "file",
    label: path.split("/").pop() || path,
    target: path,
    path,
    diff,
    preview,
  };
}

function resourceArrayField(source: unknown, key: string): ResourceReference[] {
  return arrayField(source, key).flatMap((item) => {
    if (!item || typeof item !== "object") return [];
    const kind = stringField(item, "kind");
    const target = stringField(item, "target") ?? stringField(item, "path");
    if (!target) return [];
    const path = stringField(item, "path");
    const line = numberField(item, "line");
    const insertions = numberField(item, "insertions");
    const deletions = numberField(item, "deletions");
    const resource: ResourceReference = {
      kind: resourceKind(kind),
      label: stringField(item, "label") ?? target.split("/").pop() ?? target,
      target,
      path: path ?? undefined,
      line: line ?? undefined,
      diff: insertions !== null && deletions !== null ? { insertions, deletions } : undefined,
      preview: resourcePreviewField(item),
    };
    return [resource];
  });
}

function resourcePreviewField(source: unknown): ResourcePreview | undefined {
  const preview = objectField(source, "preview");
  if (!preview) return undefined;
  const kind = previewKind(stringField(preview, "kind"));
  const rows = arrayField(preview, "rows").flatMap((row) => {
    if (!row || typeof row !== "object" || Array.isArray(row)) return [];
    const out: Record<string, string | number | boolean | null> = {};
    for (const [key, value] of Object.entries(row)) {
      if (
        typeof value === "string" ||
        typeof value === "number" ||
        typeof value === "boolean" ||
        value === null
      ) {
        out[key] = value;
      }
    }
    return [out];
  });
  return {
    kind,
    title: stringField(preview, "title") ?? undefined,
    content: stringField(preview, "content") ?? undefined,
    columns: arrayField(preview, "columns").filter(isString),
    rows: rows.length > 0 ? rows : undefined,
    truncated: boolField(preview, "truncated"),
  };
}

function previewKind(raw: string | null): ResourcePreview["kind"] {
  if (raw === "diff" || raw === "table" || raw === "json" || raw === "terminal") return raw;
  return "text";
}

function resourceKind(raw: string | null): ResourceReference["kind"] {
  if (
    raw === "file" ||
    raw === "diff" ||
    raw === "data_source" ||
    raw === "sql" ||
    raw === "graph" ||
    raw === "run" ||
    raw === "node" ||
    raw === "artifact" ||
    raw === "external"
  )
    return raw;
  return "file";
}

function objectField(source: unknown, key: string): Record<string, unknown> | null {
  if (!source || typeof source !== "object") return null;
  const value = (source as Record<string, unknown>)[key];
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function objectOrStringField(
  source: unknown,
  key: string,
): Record<string, unknown> | string | undefined {
  if (!source || typeof source !== "object") return undefined;
  const value = (source as Record<string, unknown>)[key];
  if (typeof value === "string") return value;
  if (value && typeof value === "object" && !Array.isArray(value))
    return value as Record<string, unknown>;
  return undefined;
}

function stringField(source: unknown, key: string): string | null {
  if (!source || typeof source !== "object") return null;
  const value = (source as Record<string, unknown>)[key];
  return typeof value === "string" ? value : null;
}

/** An IDENTITY read: a field that is present but empty is no id at all.
 *
 *  These feed React keys, so the difference matters. `""` passes a `?? fallback`
 *  untouched, and two parts that both answered `""` then rendered under the same
 *  key — React's own answer to which is to duplicate or omit one of them, i.e. a
 *  row of the conversation silently going missing. */
function idField(source: unknown, key: string): string | null {
  return stringField(source, key) || null;
}

function numberField(source: unknown, key: string): number | null {
  if (!source || typeof source !== "object") return null;
  const value = (source as Record<string, unknown>)[key];
  return typeof value === "number" ? value : null;
}

function stringArrayField(source: unknown, key: string): string[] {
  if (!source || typeof source !== "object") return [];
  const value = (source as Record<string, unknown>)[key];
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
}

/** Drop the live-tracking map entries that pointed into removed turns/parts, so
 *  the next event folds onto the (now shorter) transcript cleanly. */
function pruneTrackingForTurns(state: ConversationFoldState, removedTurnIds: Set<string>): void {
  for (const [k, v] of state.messageToTurn)
    if (removedTurnIds.has(v)) state.messageToTurn.delete(k);
  const removedPartIds = new Set<string>();
  for (const [k, v] of state.partToTurn) {
    if (removedTurnIds.has(v)) {
      removedPartIds.add(k);
      state.partToTurn.delete(k);
    }
  }
  for (const [k, v] of state.toolToPart) if (removedPartIds.has(v)) state.toolToPart.delete(k);
  for (const [k, v] of state.requestToPart)
    if (removedPartIds.has(v)) state.requestToPart.delete(k);
  for (const [k, v] of state.askTimeEditPreviews) {
    if (v.attachedToPartId && removedPartIds.has(v.attachedToPartId)) {
      state.askTimeEditPreviews.delete(k);
    }
  }
  if (state.currentTurnId && removedTurnIds.has(state.currentTurnId)) state.currentTurnId = null;
  if (state.currentAssistantTurnId && removedTurnIds.has(state.currentAssistantTurnId)) {
    state.currentAssistantTurnId = null;
  }
}

/** Apply a revert cursor: drop every turn AFTER the one holding `toMessageId`
 *  (and, when `toPartId` is given, the parts after it within that turn). */
function applyRevert(
  state: ConversationFoldState,
  toMessageId: string | null,
  toPartId: string | null,
): void {
  if (!toMessageId) return;
  const cursorTurnId = state.messageToTurn.get(toMessageId);
  const cursorIdx = cursorTurnId ? state.turns.findIndex((t) => t.id === cursorTurnId) : -1;
  if (cursorIdx < 0) return; // unknown anchor — forward-compat no-op
  const removed = new Set(state.turns.slice(cursorIdx + 1).map((t) => t.id));
  state.turns.splice(cursorIdx + 1);
  if (toPartId) {
    const turn = state.turns[cursorIdx];
    const partIdx = turn.parts.findIndex((p) => p.id === toPartId);
    if (partIdx >= 0) {
      for (const part of turn.parts.slice(partIdx + 1)) pruneTrackingForPart(state, part.id);
      turn.parts.splice(partIdx + 1);
    }
  }
  pruneTrackingForTurns(state, removed);
}

/** Apply tombstones: each id can name a turn (drop it) or a part (drop it from
 *  its turn). Non-destructive at the source — only the active transcript shrinks. */
function applyTombstone(state: ConversationFoldState, eventIds: string[]): void {
  if (!eventIds.length) return;
  const ids = new Set(eventIds);
  // Drop whole turns whose id is tombstoned.
  const removedTurns = new Set(state.turns.filter((t) => ids.has(t.id)).map((t) => t.id));
  if (removedTurns.size) {
    state.turns = state.turns.filter((t) => !removedTurns.has(t.id));
    pruneTrackingForTurns(state, removedTurns);
  }
  // Drop individual tombstoned parts from their turns.
  for (const turn of state.turns) {
    const keep = turn.parts.filter((p) => !ids.has(p.id));
    if (keep.length !== turn.parts.length) {
      for (const part of turn.parts) if (ids.has(part.id)) pruneTrackingForPart(state, part.id);
      turn.parts = keep;
    }
  }
}

function pruneTrackingForPart(state: ConversationFoldState, partId: string): void {
  state.partToTurn.delete(partId);
  for (const [k, v] of state.toolToPart) if (v === partId) state.toolToPart.delete(k);
  for (const [k, v] of state.requestToPart) if (v === partId) state.requestToPart.delete(k);
  for (const [k, v] of state.askTimeEditPreviews) {
    if (v.attachedToPartId === partId) state.askTimeEditPreviews.delete(k);
  }
}

function recordNumberField(source: unknown, key: string): Record<string, number> | null {
  const value = objectField(source, key);
  if (!value) return null;
  const out: Record<string, number> = {};
  for (const [k, v] of Object.entries(value)) {
    if (typeof v === "number") out[k] = v;
  }
  return out;
}

function compactionText(source: unknown): string {
  const data = objectField(source, "data");
  // `summary_text` is the field the real `compaction.applied` event carries
  // (CompactionApplied). The `summary`/`text`/… aliases cover the speculative
  // streaming-compaction shape; without `summary_text` the summary the daemon
  // actually sends would render as an empty card.
  return (
    stringField(source, "summary_text") ??
    stringField(source, "summary") ??
    stringField(source, "text") ??
    stringField(source, "content") ??
    stringField(source, "compaction_text") ??
    stringField(data, "summary") ??
    stringField(data, "text") ??
    stringField(data, "content") ??
    ""
  );
}

function compactionTitle(source: unknown, fallback = "Compaction"): string {
  const data = objectField(source, "data");
  const title = stringField(source, "title") ?? stringField(data, "title");
  if (title) return title;
  const reason = stringField(source, "reason") ?? stringField(data, "reason");
  if (reason === "auto") return "Auto compaction";
  if (reason === "manual") return "Manual compaction";
  return fallback;
}

function boolField(source: unknown, key: string): boolean {
  if (!source || typeof source !== "object") return false;
  return (source as Record<string, unknown>)[key] === true;
}

function hasField(source: unknown, key: string): boolean {
  return Boolean(source && typeof source === "object" && key in source);
}

function arrayField(source: unknown, key: string): unknown[] {
  if (!source || typeof source !== "object") return [];
  const value = (source as Record<string, unknown>)[key];
  return Array.isArray(value) ? value : [];
}

function toolState(raw: string | null): "pending" | "running" | "completed" | "error" {
  if (raw === "running" || raw === "completed" || raw === "error") return raw;
  return "pending";
}

/** The words opencode's session processor stamps on every tool still running
 *  when the turn is aborted (vendor/opencode/packages/opencode/src/session/
 *  processor.ts, the abort cleanup: `status: "error"`, `error: "Tool execution
 *  aborted"`, `metadata.interrupted: true`). Matched whole, never as a substring,
 *  and only for a row that predates the flag reaching the wire: a tool whose own
 *  error mentions an abort is still that tool's failure. */
const HARNESS_ABORT_TEXT = "Tool execution aborted";

/** A terminal the harness wrote for a call its turn's Stop cut off, which
 *  therefore ended without a result rather than failing. The flag is the signal;
 *  the exact words are the fallback for a row written without it. */
function stoppedByTurn(status: ToolState, errorText: string | null, metadata: Record<string, unknown> | null): boolean {
  if (status !== "error") return false;
  return metadata?.interrupted === true || errorText === HARNESS_ABORT_TEXT;
}

/** A tool terminal's state, with the harness's abort told apart from a failure. */
function terminalToolState(
  raw: string | null,
  errorText: string | null,
  metadata: Record<string, unknown> | null,
): ToolState {
  const state = toolState(raw);
  return stoppedByTurn(state, errorText, metadata) ? "stopped" : state;
}

function canonicalKind(raw: string | null): PermissionConversationPart["canonicalKind"] {
  if (
    raw === "edit" ||
    raw === "shell" ||
    raw === "network" ||
    raw === "task" ||
    raw === "external"
  )
    return raw;
  return "other";
}

function planStatus(raw: string | null): "pending" | "in_progress" | "completed" {
  if (raw === "completed" || raw === "in_progress") return raw;
  if (raw === "done") return "completed";
  return "pending";
}

function formatDelta(delta: unknown): string {
  if (typeof delta === "string") return delta;
  if (!delta || typeof delta !== "object") return "";
  const text =
    stringField(delta, "text") ?? stringField(delta, "content") ?? stringField(delta, "data");
  return text ?? "";
}

function isString(value: unknown): value is string {
  return typeof value === "string";
}

function newId(prefix: string): string {
  return `${prefix}-${Math.random().toString(36).slice(2)}`;
}
