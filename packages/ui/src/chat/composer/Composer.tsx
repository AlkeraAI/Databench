// The composer: field-first. The writing field sits on top of the unit, full
// width and borderless; beneath it a flat instrument rail -- the slash key and
// the model gauge lead, the effort gauge, the permission mode, and send close.
// The rail is one row at every width the panel can be: it never wraps, and
// composer.css derives the width at which each label is allowed back from
// responsive rungs. The active permission mode drives the unit's outline
// AND the send fill, so the mode is unmistakable. Typing "/" as the first
// character opens the slash-command menu anchored to the unit, and the rail's
// leading "/" key is the same door for a pointer; Shift+Tab cycles the
// permission mode from the field.
//
// The webview cannot paint outside itself, so every menu this file opens is
// anchored to the rail and side-aligned rather than hung off its trigger: a
// trigger-anchored menu wider than the room to its right would leave the panel.
import {
  Fragment,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
  type RefObject,
} from "react";
import { Text, Tooltip, isChatImageName, resolveModelMark, useDismiss } from "../sharedUi";
import { currentBrand } from "../../brand/brand";
import { useRovingFocus } from "../hooks";
import { ModeGlyph } from "../modes";
import { MESSAGE_COUNTER_AT } from "../../theme/limits";
import {
  UPLOAD_ATTEMPTS,
  dedupeUploads,
  defaultRetryDelayMs,
  describeUploadSize,
  numberedUploads,
  refuseUpload,
  removeUploadToken,
  renumberTokens,
  replaceUploadTokens,
  uploadToken,
  uploadTokenLabel,
  type ComposerUploader,
  type StagedUpload,
  type UploadKind,
} from "./uploads";
import { shortModelLabel } from "./modelLabels";
import { spliceFor, type BindingNotice, type TextBinding, type TextSelection } from "./textBinding";
import "./composer.css";
export interface ModeOption {
  value: string;
  label: string;
  description?: string;
  /** Compact trigger word for a mode whose full label can't fit the rail. */
  short?: string;
}
export interface ModelOption {
  value: string;
  label: string;
  /** Why the open chat cannot move to this model, when it cannot. The row is
   *  greyed and never picks the model; consecutive rows that share a reason
   *  are listed under it once. */
  unavailable?: string;
  /** The way to use this model anyway, when there is one: a new chat. The
   *  greyed row itself is then the control that takes it (its accessible name
   *  is ``label``), so a keyboard reaches it like any other row. */
  escape?: { label: string; onSelect: () => void };
}
export interface EffortOption {
  value: string;
  label: string;
  bars: 0 | 1 | 2 | 3;
}
export interface SlashCommand {
  command: string;
  summary: string;
  usage?: string;
}
/** One file the reader has put on the message. The host owns everything about
 *  it — where the bytes go, what identity it ends up with — so the chip states
 *  only what a reader can act on: the name they picked, how far the bytes have
 *  got, and why one did not make it. */
export interface ComposerAttachment {
  /** Stable for the life of the chip; the host's handle, not a node id, so a
   *  chip can be shown (and removed) before anything is linked. */
  id: string;
  name: string;
  /** 0-100 while the bytes are still going up; absent once the file is kept. */
  progress?: number;
  /** Why this file is not on the message. The chip says it and the host sends
   *  nothing for it. */
  error?: string;
}
/** Where somebody ELSE's caret is in the shared draft, as the composer draws
 *  it. The host resolves the identity — who this is, what colour they wear —
 *  and the composer draws it, so the primitive stays presentational and a shell
 *  where a chat has one keyboard passes nothing at all.
 *
 *  `before`/`after` are the text that surrounded the caret when the peer
 *  reported it. A shared draft is merged three ways, so by the time a frame
 *  lands the text may have shifted under the offset; that context is what lets
 *  the caret be re-found instead of left pointing at the wrong character. */
export interface RemoteCaret {
  /** The PEER, not the person: two tabs of one colleague are two carets. */
  id: string;
  /** What the flag beside the bar says. */
  name: string;
  /** The peer's colour, 0-359 — the same hue their face wears elsewhere. */
  hue: number;
  offset: number;
  /** Where their selection started. Equal to `offset` for a plain caret. */
  anchor: number;
  before?: string;
  after?: string;
}
/** Where the local caret is, as reported on every move of it. */
export interface ComposerSelection {
  /** The head of the selection — where the caret itself is. */
  offset: number;
  /** The other end. Equal to `offset` when nothing is selected. */
  anchor: number;
  /** The text either side of the caret, for whoever has to re-find it after a
   *  merge has shifted the draft under it. */
  before: string;
  after: string;
}
/** How much text either side of the caret rides with it. */
export const CARET_CONTEXT_CHARS = 32;
/**
 * Where a caret reported against some earlier text sits in `text` now.
 *
 * The offset alone is a lie the moment somebody types above it: the shared
 * draft is merged, every later character shifts, and a caret drawn at the raw
 * offset lands mid-word. So the context the peer sent with it is searched for
 * and the occurrence whose caret position is NEAREST the reported one wins —
 * in text that repeats ("ok. ok. ok.") that re-anchors to the copy the peer was
 * actually in rather than the first one. With no context, or none that still
 * occurs, the offset is clamped into the text: a caret at a plausible place
 * beats a caret that has silently gone.
 */
export function anchorCaret(
  text: string,
  caret: { offset: number; anchor: number; before?: string; after?: string },
): { offset: number; anchor: number } {
  const clamp = (n: number): number => Math.max(0, Math.min(text.length, n));
  const before = caret.before ?? "";
  const after = caret.after ?? "";
  // A selection travels with its head: both ends move by the same shift, so a
  // re-anchored range keeps the length the peer actually has selected.
  const shifted = (to: number): { offset: number; anchor: number } => ({
    offset: to,
    anchor: clamp(caret.anchor + (to - caret.offset)),
  });
  const at = clamp(caret.offset);
  if (
    text.slice(Math.max(0, at - before.length), at) === before &&
    text.startsWith(after, at)
  ) {
    return shifted(at);
  }
  const needle = `${before}${after}`;
  if (needle.length > 0) {
    let best: number | null = null;
    for (
      let i = text.indexOf(needle);
      i !== -1;
      i = text.indexOf(needle, i + 1)
    ) {
      const candidate = i + before.length;
      if (
        best === null ||
        Math.abs(candidate - caret.offset) < Math.abs(best - caret.offset)
      ) {
        best = candidate;
      }
    }
    if (best !== null) return shifted(best);
  }
  return shifted(at);
}
export interface ComposerProps {
  /** The host is running a turn: send flips to Stop and a send is a no-op. */
  busy?: boolean;
  /** Nothing can be sent from here right now — the workspace is starting, or is
   *  not answering. The unit stays where it is, visibly unavailable, and says
   *  why through `unavailableReason`; hiding it would take the affordance away
   *  at the moment the reader most needs to see it is coming back. */
  unavailable?: boolean;
  /** What to show in the field while `unavailable`. */
  unavailableReason?: string;
  placeholder?: string;
  /** The context readout — what the rail SHOWS ("5%", "72k"). Omit to hide it. */
  context?: string;
  /** How full the window is, 0–100, where the shell knows the window. The ring
   *  is drawn only from this: a readout that shows a token count with no window
   *  to spend it against would otherwise be read as a percentage of nothing. */
  contextPercent?: number;
  /** The whole fact, for the tooltip and the accessible name ("68,160 of
   *  200,000 tokens"). Falls back to the shown value. */
  contextTitle?: string;
  modes: ModeOption[];
  mode: string;
  /** Change the permission mode. Omit where the shell's session fixes the mode
   *  (a cloud chat runs read-only by construction): the chip then states the
   *  mode and cannot be operated, by menu or by Shift+Tab, rather than offering
   *  a switch nothing behind it would honour. */
  onModeChange?: (value: string) => void;
  /** Why the mode cannot be changed by this reader, when the reason is theirs
   *  rather than the shell's: the chip is locked and says so on hover. */
  modeLockedReason?: string;
  models: ModelOption[];
  model: string;
  /** Move the chat onto another model. Omit where the shell will not move an
   *  open chat: the chip then states the model the session runs under and opens
   *  nothing, rather than taking a pick that would land nowhere. */
  onModelChange?: (value: string) => void;
  /** A line under the model menu, when the shell has one to say (a switch that
   *  applies only once the chat restarts). */
  modelHint?: string;
  /** A model the session is fixed to, stated as a readout beside the mode. For
   *  a shell that serves no model catalogue of its own there is nothing to pick
   *  between, so the reader still gets to see what answers them. Omit where the
   *  picker above is live. */
  staticModel?: { id: string; label: string };
  efforts: EffortOption[];
  effort: string;
  /** Change the effort. Omitted for the same reason as the model, and usually
   *  together with it: the two ride one pin. */
  onEffortChange?: (value: string) => void;
  slashCommands?: SlashCommand[];
  /** Commit the message. Never called with an empty or whitespace-only text.
   *  A command picked from the slash menu commits as its own line, so the host
   *  routes it the same way it routes a typed one. */
  onSend: (text: string) => void;
  /** The longest message the server behind this shell accepts, in characters.
   *  The number is the SERVER's, read out of its published schema by the host
   *  and never spelled here. Omitted, the composer counts nothing and refuses
   *  nothing: a shell that cannot say what the ceiling is must not invent one. */
  maxMessageChars?: number;
  onStop?: () => void;
  /** A Stop the host has sent and not heard back about. The key states it and
   *  takes no further press until the host clears it: the round trip is long
   *  enough to read as nothing having happened, and a reader shown nothing
   *  presses again. A host that answers its own Stop synchronously leaves this
   *  false and the key never passes through the state. */
  stopping?: boolean;
  /** Files the reader picked. Omit to leave the composer with no attach door:
   *  a shell that has nowhere to put bytes offers no control that would answer
   *  a click with nothing. */
  onAttach?: (files: File[]) => void;
  /** What the host made of those picks. Rendered as chips above the rail. */
  attachments?: ComposerAttachment[];
  /** Take one back off the message. Omit where a chip cannot be withdrawn. */
  onRemoveAttachment?: (id: string) => void;
  /** Where an image or file the reader pastes, drops or picks goes. Present,
   *  the composer stages the file as an inline pill (`[Image 1]`, `[File 1]`),
   *  uploads it at once, blocks Send until every upload has landed, and on
   *  send writes each pill out as the transcript's markdown for that path.
   *  Omit where the shell has nowhere to put bytes: paste and drop then do
   *  nothing, and the attach door is `onAttach`'s (or absent). */
  uploader?: ComposerUploader;
  /** An upload that failed for good, after its retries: the pill is already
   *  withdrawn from the text; the shell shows the reader `reason` its own way
   *  (a toast). The composer states it inline too. */
  onUploadFailed?: (name: string, reason: string) => void;
  /** A draft the composer should be SHOWING, from somewhere other than this
   *  keyboard — a colleague typing in the same chat, or the chat's own saved
   *  draft after a reload.
   *
   *  Adopted when `at` changes, and only then: the host hands back a new `at`
   *  exactly when there is a newer draft this composer has not already got, so
   *  a reader's own keystrokes are never echoed into the field under their
   *  caret. A host with no shared draft passes nothing and the field is purely
   *  local, as it has always been. */
  draft?: { text: string; at: number };
  /** Every local edit, as it happens. The host decides what to do with it —
   *  the browser debounces it onto the chat's document so the other people in
   *  the chat see it; the editor ignores it. Called with the empty string when
   *  a send clears the field, which is what retires a shared draft. */
  onDraftChange?: (text: string) => void;
  /** The field lost focus: a good moment to persist a draft that a debounce is
   *  still holding, so walking away from the tab does not lose it. */
  onDraftBlur?: () => void;
  /** The other people typing in this same draft, already resolved to a name and
   *  a colour and already WITHOUT this reader: the composer draws every caret it
   *  is handed, so a host that left its own peer in would draw a second caret
   *  chasing the real one. Omit where a chat has one keyboard. */
  remoteCarets?: RemoteCaret[];
  /** Where this reader's caret went — on typing, clicking, arrowing, selecting.
   *  Fires as often as the caret moves; the host owns the throttle, because the
   *  host is what knows the socket's budget. */
  onSelectionChange?: (selection: ComposerSelection) => void;
  /** A live text binding: the field becomes one view of a shared document.
   *  Edits, caret moves and undo go to the binding; other people's edits and
   *  carets come from it, with this reader's selection kept where it was. With
   *  a binding, `draft`, `onDraftChange`, `onDraftBlur`, `remoteCarets` and
   *  `onSelectionChange` are not used. */
  binding?: TextBinding;
  /** Why this field is the reader's alone when the host would share it — a
   *  shared draft that cannot run here. Drawn as a quiet line under the field
   *  for as long as it is given. */
  draftNotice?: string;
  /** A message the reader already SENT was taken while a turn was running, so
   *  the agent will not reach it until that turn ends. Nothing is left in the
   *  field and nothing is held here — the line under the field is the only
   *  thing standing between a cleared composer and a reader who cannot tell
   *  whether their message arrived. The host clears it when the turn ends. */
  queuedBehindTurn?: boolean;
  /** Messages already held for the end of the turn, oldest first, drawn under
   *  the field. Omit where the host holds none. */
  queued?: QueuedComposerMessage[];
  /** Hold this message until the turn ends, instead of sending it now. Given,
   *  Enter during a turn moves the words out of the field and into `queued`.
   *  Omit where the host has nowhere to keep them: the words then stay in the
   *  field and go out on the turn's own end. */
  onQueue?: (text: string) => void;
  /** Rewrite a held message before it goes. */
  onEditQueued?: (id: string, text: string) => void;
  /** Take a held message back. */
  onRemoveQueued?: (id: string) => void;
  /** Send a `restored` message now. Those go only when the reader says so, so
   *  each one is drawn with a send key of its own. */
  onSendQueued?: (id: string) => void;
  /** A message offered from outside the field — a suggested first ask — sent
   *  through this composer so it goes under the same rules a typed one does.
   *
   *  Acted on once per `at`. On a composer holding no words of the reader's
   *  own, the offer becomes the message and is committed at once, carrying
   *  every file already staged here: a staged pill is never left behind by a
   *  press that sends past it. On a composer holding words, the offer is
   *  written in front of them and nothing is sent, because sending would put
   *  words in the reader's mouth and dropping theirs would lose them. On an
   *  `unavailable` composer the offer is ignored, as a typed Send would be. */
  offer?: { text: string; at: number };
}
/** A message the host is holding until the turn ends. */
export interface QueuedComposerMessage {
  id: string;
  text: string;
  /** This was held by a PREVIOUS page session and has not been sent. It will
   *  not send itself — the reader's click is what sends it — so it is drawn
   *  apart from the ones that go on their own, under its own line. */
  restored?: boolean;
  /** The reader stopped the turn this was waiting behind, so it was never
   *  sent and never will be on its own. Drawn under its own line saying so,
   *  with the key that sends it if they want it after all. */
  stopped?: boolean;
  /** The reader sent this and the server could not take it. Drawn first,
   *  under "Not sent", with a Retry of its own; the host may be retrying it
   *  on a timer meanwhile. */
  unsent?: boolean;
}
const SEND_LABEL = "Send";
const ATTACH_LABEL = "Attach files";
const STOP_LABEL = "Stop";
/** The stop is on the wire. One word, because it is the receipt for a press —
 *  the reader needs to know it landed, not what it is doing. */
const STOPPING_LABEL = "Stopping…";
const MODEL_LABEL = "Model";
const EFFORT_LABEL = "Effort";
const MODE_LABEL = "Permission mode";
const SLASH_LABEL = "Slash commands";
const MODE_CYCLE_HINT = "Shift+Tab cycles modes";
const QUEUED_LABEL = "Queued · sends when the turn finishes";
/** A message this tab found waiting when it opened. Nothing has sent it and
 *  nothing will until the reader says so, so the line must not promise that
 *  it goes on its own. */
const RESTORED_LABEL = "Queued · not sent";
/** A message the reader's Stop cancelled. It was never handed over, and the
 *  turn ending is what would have sent it — so it says what happened to it,
 *  in the same words the transcript uses for a message the box dropped. */
const STOPPED_LABEL = "Not sent (stopped)";
/** A message the reader sent that the server could not take. */
const UNSENT_LABEL = "Not sent";
const RETRY_LABEL = "Retry";
/** What a reader is told when Enter lands mid-turn and this composer has
 *  nowhere to put the message: it stays in the field and goes out on the end
 *  of the turn, which is what this says. */
const WORKING_HINT = "The agent is working. Your message will send when it finishes.";
/** What a reader is told about a message that HAS gone: the server took it
 *  while a turn was running, so it waits for that turn. Stated from the moment
 *  it was taken until it is the turn being worked on — the field clears on a
 *  send, and without this line that clearing is the only thing that happens. */
const QUEUED_BEHIND_TURN = "Queued behind the running turn";

/** Why a message this long cannot go. The ceiling is the server's, so the
 *  sentence names it rather than describing it vaguely. */
export const messageTooLong = (max: number): string =>
  `This message is over the ${max.toLocaleString()} character limit.`;
/** Read at render, so the brand the product registers names the agent. */
const defaultPlaceholder = () => `Ask ${currentBrand().productName} to plan, build, or run something…`;
// ── Glyphs ────────────────────────────────────────────────────────────────────
function EffortBars({ bars }: { bars: 0 | 1 | 2 | 3 }): ReactNode {
  return (
    <svg
      className="chat-composer-bars"
      viewBox="0 0 14 14"
      width="15"
      height="15"
      aria-hidden="true"
    >
      {[0, 1, 2].map((i) => (
        <rect
          key={i}
          x={2 + i * 4.3}
          y={9.5 - i * 3.1}
          width="2.8"
          height={3 + i * 3.1}
          rx="0.9"
          data-on={i < bars ? "" : undefined}
        />
      ))}
    </svg>
  );
}
function ContextRing({ percent }: { percent: number }): ReactNode {
  const r = 5.2;
  const c = 2 * Math.PI * r;
  const used = Math.max(0, Math.min(100, percent)) / 100;
  return (
    <svg viewBox="0 0 14 14" width="15" height="15" aria-hidden="true">
      <circle className="chat-composer-ring__track" cx="7" cy="7" r={r} />
      <circle
        className="chat-composer-ring__arc"
        cx="7"
        cy="7"
        r={r}
        strokeDasharray={`${(c * used).toFixed(2)} ${c.toFixed(2)}`}
        transform="rotate(-90 7 7)"
      />
    </svg>
  );
}
function Check(): ReactNode {
  return (
    <svg
      className="chat-composer-check"
      viewBox="0 0 14 14"
      width="14"
      height="14"
      aria-hidden="true"
    >
      <path d="M2.8 7.3 5.7 10.2 11.2 4.2" />
    </svg>
  );
}
function UpArrow(): ReactNode {
  return (
    <svg
      className="chat-composer-send__glyph"
      viewBox="0 0 14 14"
      width="15"
      height="15"
      aria-hidden="true"
    >
      <path d="M7 11V3.4M3.6 6.8 7 3.4l3.4 3.4" />
    </svg>
  );
}
function PaperclipGlyph(): ReactNode {
  return (
    <svg
      className="chat-composer-glyph"
      viewBox="0 0 24 24"
      width="16"
      height="16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {/* A clip drawn wide enough that its inner loop stays open at 16 px. */}
      <path d="M15 7l-6.5 6.5a1.5 1.5 0 0 0 3 3l6.5-6.5a3 3 0 0 0-6-6l-6.5 6.5a4.5 4.5 0 0 0 9 9l6.5-6.5" />
    </svg>
  );
}
function StopGlyph(): ReactNode {
  return (
    <svg viewBox="0 0 14 14" width="15" height="15" aria-hidden="true">
      <rect
        x="4.1"
        y="4.1"
        width="5.8"
        height="5.8"
        rx="1.3"
        fill="currentColor"
      />
    </svg>
  );
}
// ── Meta options ──────────────────────────────────────────────────────────────
interface MetaOption {
  value: string;
  label: string;
  icon: ReactNode;
  description?: string;
  /** Compact trigger word for a chip whose full label cannot fit the rail at
   *  the squeezed widths (a long mode name, a model name's family word). */
  short?: string;
  /** The row cannot be picked, for this reason (shown as its description). */
  unavailable?: string;
  /** A second action the row offers, beside it. */
  escape?: { label: string; onSelect: () => void };
}
/** A captioned option group a menu carries for the widths where the group's
 *  own trigger has folded away. */
interface MetaSection {
  title: string;
  options: MetaOption[];
  value: string;
  onChange: (value: string) => void;
  moded?: boolean;
}
function OptionRow({
  option,
  selected,
  moded,
  onPick,
}: {
  option: MetaOption;
  selected: boolean;
  moded?: boolean;
  onPick: () => void;
}): ReactNode {
  if (option.unavailable && option.escape) {
    // Not the model, but the way to it: a row of its own that opens a new
    // chat on it. Its reason is the group caption above it.
    return (
      <button
        type="button"
        role="option"
        aria-selected={false}
        aria-label={option.escape.label}
        className="chat-composer-opt"
        data-unavailable=""
        onClick={option.escape.onSelect}
      >
        <span className="chat-composer-opt__icon">{option.icon}</span>
        <span className="chat-composer-opt__label">{option.label}</span>
        <span className="chat-composer-opt__escape" aria-hidden="true">
          New chat
        </span>
      </button>
    );
  }
  if (option.unavailable) {
    return (
      <div
        role="option"
        aria-selected={false}
        aria-disabled="true"
        tabIndex={-1}
        className="chat-composer-opt"
        data-unavailable=""
      >
        <span className="chat-composer-opt__icon">{option.icon}</span>
        <span className="chat-composer-opt__label">{option.label}</span>
        <span className="chat-composer-opt__check" />
      </div>
    );
  }
  return (
    <button
      type="button"
      role="option"
      aria-selected={selected}
      className="chat-composer-opt"
      onClick={onPick}
    >
      <span
        className="chat-composer-opt__icon"
        data-mode={moded ? option.value : undefined}
      >
        {option.icon}
      </span>
      <span className="chat-composer-opt__label">{option.label}</span>
      <span className="chat-composer-opt__check">
        {selected ? <Check /> : null}
      </span>
      {option.description ? (
        <Text className="chat-composer-opt__desc" tooltip="truncate">
          {option.description}
        </Text>
      ) : null}
    </button>
  );
}
// A controlled menu trigger: the value lives with the host, so the unit outline
// follows the mode and the value survives a label folding away.
function MetaMenu({
  category,
  options,
  value,
  onChange,
  moded,
  rail,
  end,
  side,
  hint,
  sections,
  lockedReason,
}: {
  category: string;
  options: MetaOption[];
  value: string;
  onChange?: (value: string) => void;
  moded?: boolean;
  /** Which control this is. The rung ladder and the shrink tiers both key off
   *  it, so one attribute names a chip for every rule that has to find it. */
  rail: "model" | "effort" | "mode";
  /** This chip belongs to the rail's closing group. */
  end?: boolean;
  /** Which end of the rail the menu aligns to -- the end its trigger sits at,
   *  so the menu never has to reach across the panel to stay inside it. */
  side: "left" | "right";
  hint?: string;
  /** Captioned option groups that live in this menu at the squeezed widths
   *  where their own triggers have folded away. Always in the DOM; the
   *  container query decides when they show. */
  sections?: MetaSection[];
  /** Why a locked chip is locked, shown as its title. */
  lockedReason?: string;
}): ReactNode {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const current = options.find((o) => o.value === value) ?? options[0];
  useDismiss(ref, open, () => setOpen(false));
  useEffect(() => {
    if (!open || !listRef.current) return;
    const selected = listRef.current.querySelector<HTMLElement>(
      '[role="option"][aria-selected="true"]',
    );
    // preventScroll: the menu opens at its top (the mode list leads the
    // grouped panel); the focus ring lands without yanking the scroll.
    (
      selected ?? listRef.current.querySelector<HTMLElement>('[role="option"]')
    )?.focus({ preventScroll: true });
  }, [open]);
  const commit = (next: string): void => {
    onChange?.(next);
    setOpen(false);
    triggerRef.current?.focus();
  };
  const onListKey = useRovingFocus(listRef, "option", () => {
    setOpen(false);
    triggerRef.current?.focus();
  });
  if (!current) return null;
  // No handler means the shell fixes this setting: the chip is a readout of what
  // the session actually runs under, with no menu behind it.
  const locked = onChange === undefined;
  const chipLabel =
    locked && lockedReason
      ? `${category}: ${current.label}. ${lockedReason}`
      : `${category}: ${current.label}`;
  return (
    <div
      className="chat-composer-ctl"
      data-rail={rail}
      data-rail-end={end ? "" : undefined}
      ref={ref}
    >
      {/* A folded label leaves the a11y tree, so the accessible name and the
          tooltip carry the value at every width. */}
      <Tooltip label={chipLabel}>
        {(tip) => (
          <button
            type="button"
            className="chat-composer-trigger"
            data-mode={moded ? current.value : undefined}
            data-locked={locked ? "" : undefined}
            disabled={locked}
            title={locked ? lockedReason : undefined}
            aria-haspopup={locked ? undefined : "listbox"}
            aria-expanded={locked ? undefined : open}
            aria-label={chipLabel}
            onClick={locked ? undefined : () => setOpen((v) => !v)}
            {...tip}
            ref={(element) => {
              triggerRef.current = element;
              tip.ref(element);
            }}
          >
            <span className="chat-composer-trigger__icon">{current.icon}</span>
            <span className="chat-composer-trigger__label">
              {current.short ? (
                <>
                  <span className="chat-composer-trigger__wide">
                    {current.label}
                  </span>
                  <span className="chat-composer-trigger__narrow">
                    {current.short}
                  </span>
                </>
              ) : (
                current.label
              )}
            </span>
          </button>
        )}
      </Tooltip>
      {open && !locked ? (
        <div className="chat-composer-pop" data-side={side} role="presentation">
          {/* Only the options scroll. The hint below them is the menu's footer
              and stays put, so a menu the window is too short to show whole
              still says what Shift+Tab does. */}
          <div className="chat-composer-pop__scroll">
            {sections ? (
              <p className="chat-composer-ovsec__cap chat-composer-pop__cap">
                {category}
              </p>
            ) : null}
            <ul
              className="chat-composer-pop__list"
              role="listbox"
              aria-label={category}
              ref={listRef}
              onKeyDown={onListKey}
            >
              {options.map((option, at) => (
                <Fragment key={option.value}>
                  {option.unavailable && option.unavailable !== options[at - 1]?.unavailable ? (
                    <li role="presentation" className="chat-composer-opt-group">
                      {option.unavailable}
                    </li>
                  ) : null}
                  <li>
                    <OptionRow
                      option={option}
                      selected={option.value === value}
                      moded={moded}
                      onPick={() => commit(option.value)}
                    />
                  </li>
                </Fragment>
              ))}
            </ul>
            {sections?.map((section) => (
              <div
                key={section.title}
                className="chat-composer-ovsec"
                role="group"
                aria-label={section.title}
              >
                <p className="chat-composer-ovsec__cap">{section.title}</p>
                <ul
                  className="chat-composer-pop__list"
                  role="listbox"
                  aria-label={section.title}
                >
                  {section.options.map((option) => (
                    <li key={option.value}>
                      <OptionRow
                        option={option}
                        selected={option.value === section.value}
                        moded={section.moded}
                        onPick={() => {
                          section.onChange(option.value);
                          setOpen(false);
                          triggerRef.current?.focus();
                        }}
                      />
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
          {hint ? <p className="chat-composer-pop__hint">{hint}</p> : null}
        </div>
      ) : null}
    </div>
  );
}
/** The rail width from which the context read is given its full cell — its own
 *  gap, its padding, and the closing group's margin. Below it the read stays on
 *  the rail in a tightened form; it is never taken off. */
const CONTEXT_FULL_RAIL_PX = 556;

/** How wide the rail actually is. The composer is drawn in a dock the reader
 *  drags and in a window they resize, so a viewport media query answers for the
 *  wrong box: a 1440px screen with the files pane open leaves the rail well
 *  under 556. Zero until the browser has laid it out, which reads as "not
 *  measured yet" and keeps the first paint on the roomy form. */
function useRailWidth(ref: RefObject<HTMLElement | null>): number {
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const node = ref.current;
    if (!node) return;
    const read = (): void => setWidth(node.clientWidth);
    read();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(read);
    observer.observe(node);
    return () => observer.disconnect();
  }, [ref]);
  return width;
}

/** How full the model's window is. It is the one number on the rail that says
 *  when a compaction is coming, so it is on screen at every width the rail is:
 *  a narrow rail tightens the cell to the ring and its share, it does not drop
 *  the read. The whole figure rides the accessible name and the tooltip at both
 *  forms. */
function ContextReadout({
  context,
  percent,
  title,
  compact,
}: {
  context: string;
  percent?: number;
  title?: string;
  compact: boolean;
}): ReactNode {
  const label = `Context used: ${title ?? context}`;
  return (
    <Tooltip label={label}>
      {(tip) => (
        <span
          className="chat-composer-readout"
          data-rail-end=""
          data-context-form={compact ? "compact" : "full"}
          aria-label={label}
          {...tip}
        >
          {typeof percent === "number" ? (
            <span className="chat-composer-readout__icon">
              <ContextRing percent={percent} />
            </span>
          ) : null}
          <span className="chat-composer-readout__value">{context}</span>
        </span>
      )}
    </Tooltip>
  );
}

/** The commands door remains present at every rail width. */
function SlashDoor({
  open,
  onToggle,
}: {
  open: boolean;
  onToggle: () => void;
}): ReactNode {
  return (
    <Tooltip label={SLASH_LABEL}>
      {(tip) => (
        <button
          type="button"
          className="chat-composer-trigger chat-composer-slashkey"
          aria-haspopup="listbox"
          aria-expanded={open}
          aria-label={SLASH_LABEL}
          onClick={onToggle}
          {...tip}
        >
          /
        </button>
      )}
    </Tooltip>
  );
}

/** The door to the file picker. The `<input>` is the real control and stays
 *  hidden because a browser file input cannot be dressed to sit on the rail;
 *  the button carries the accessible name, and the input is `aria-hidden` so a
 *  screen reader is offered exactly one "Attach files", not two. Picking the
 *  same file twice in a row must still be a pick, so the input's value is
 *  cleared after every change -- otherwise the second pick fires no event. */
function AttachDoor({
  onPick,
  disabled = false,
}: {
  onPick: (files: File[]) => void;
  /** The composer cannot send, so nothing may be staged for it: a file picked
   *  onto a message that cannot go is a pill with nowhere to arrive. */
  disabled?: boolean;
}): ReactNode {
  const inputRef = useRef<HTMLInputElement>(null);
  return (
    <>
      <Tooltip label={ATTACH_LABEL}>
        {(tip) => (
          <button
            type="button"
            className="chat-composer-trigger chat-composer-attachkey"
            aria-label={ATTACH_LABEL}
            disabled={disabled}
            onClick={() => inputRef.current?.click()}
            {...tip}
          >
            <PaperclipGlyph />
          </button>
        )}
      </Tooltip>
      <input
        ref={inputRef}
        type="file"
        multiple
        className="chat-composer-attachinput"
        tabIndex={-1}
        aria-hidden="true"
        disabled={disabled}
        onChange={(event) => {
          const picked = Array.from(event.target.files ?? []);
          event.target.value = "";
          if (picked.length > 0) onPick(picked);
        }}
      />
    </>
  );
}

/** What the host made of the picks, above the rail so the reader can see the
 *  message they are about to send in full. A chip that failed states why and
 *  wears the failure; it is still removable, because the only thing left to do
 *  with it is take it off. */
function AttachChips({
  attachments,
  onRemove,
}: {
  attachments: ComposerAttachment[];
  onRemove?: (id: string) => void;
}): ReactNode {
  return (
    <ul className="chat-composer-chips" aria-label={ATTACH_LABEL}>
      {attachments.map((file) => (
        <li
          key={file.id}
          className="chat-composer-chip"
          data-state={
            file.error ? "error" : file.progress === undefined ? "kept" : "sending"
          }
        >
          <Text className="chat-composer-chip__name">{file.name}</Text>
          {file.error ? (
            <Text className="chat-composer-chip__error">{file.error}</Text>
          ) : file.progress !== undefined ? (
            <progress
              className="chat-composer-chip__progress"
              max={100}
              value={file.progress}
              aria-label={`Uploading ${file.name}`}
            />
          ) : null}
          {onRemove ? (
            <button
              type="button"
              className="chat-composer-chip__remove"
              aria-label={`Remove ${file.name}`}
              onClick={() => onRemove(file.id)}
            >
              ×
            </button>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

/** The inline uploads, one pill each, beneath the field. The pill states what
 *  the reader needs to know before they send: still going up (a bar), landed
 *  (a check), or refused this attempt (a mark — it retries on its own). Each
 *  is removable; the token in the field goes with it. */
function UploadPills({
  uploads,
  onRemove,
}: {
  uploads: StagedUpload[];
  onRemove: (id: string) => void;
}): ReactNode {
  return (
    <ul className="chat-composer-uploads" aria-label="Uploads">
      {uploads.map((u) => {
        const token = uploadToken(u.kind, u.n);
        const state =
          u.state === "uploaded"
            ? "Uploaded"
            : u.state === "failed"
              ? `Failed, retrying: ${u.error ?? ""}`
              : u.state === "held"
                ? "Attached, uploads on send"
                : "Uploading";
        return (
          <li
            key={u.id}
            className="chat-composer-upload"
            data-state={u.state}
            data-kind={u.kind}
            title={`${u.name} · ${describeUploadSize(u.file.size)} · ${state}`}
          >
            {u.previewUrl ? <img className="chat-composer-upload__thumb" src={u.previewUrl} alt="" /> : null}
            <span className="chat-composer-upload__token">{token}</span>
            <Text className="chat-composer-upload__name">{u.name}</Text>
            {u.state === "uploading" ? (
              <progress
                className="chat-composer-upload__progress"
                max={100}
                value={u.progress}
                aria-label={`Uploading ${u.name}`}
              />
            ) : (
              <span className="chat-composer-upload__state" role="img" aria-label={state}>
                {u.state === "uploaded" ? <Check /> : u.state === "held" ? "·" : "!"}
              </span>
            )}
            <button
              type="button"
              className="chat-composer-upload__remove"
              aria-label={`Remove ${u.name} (${uploadTokenLabel(u.kind, u.n)})`}
              onClick={() => onRemove(u.id)}
            >
              ×
            </button>
          </li>
        );
      })}
    </ul>
  );
}

/** The model this session is fixed to. A readout, not a control: no menu, no
 *  press, and not a button, so nothing about it invites a pick that the shell
 *  has no way to honour. It borrows the rail's locked-chip dress so it reads as
 *  one of the row rather than a new kind of thing. */
function StaticModelChip({ model }: { model: { id: string; label: string } }): ReactNode {
  return (
    <div className="chat-composer-ctl" data-rail="model">
      <span
        className="chat-composer-trigger"
        data-locked=""
        role="img"
        aria-label={`${MODEL_LABEL}: ${model.label}`}
        title={model.id}
      >
        <span className="chat-composer-trigger__icon">
          {resolveModelMark(model.id, model.label)}
        </span>
        <span className="chat-composer-trigger__label">{model.label}</span>
      </span>
    </div>
  );
}

/** Send is inert for empty text; while busy it becomes the live Stop key. A
 *  `reason` names why it is inert for a message that is not empty — an upload
 *  still in flight — so the key says so instead of merely refusing.
 *
 *  A stop already on its way holds the key shut whatever the turn is doing: the
 *  host releases the composer the moment Stop is pressed, so without it the key
 *  would flip straight back to Send and the press would have shown the reader
 *  nothing at all. */
function SendKey({
  busy,
  stopping,
  dead,
  reason,
  onPress,
}: {
  busy: boolean;
  stopping: boolean;
  dead: boolean;
  reason?: string | null;
  onPress: () => void;
}): ReactNode {
  const label = stopping
    ? STOPPING_LABEL
    : busy
      ? STOP_LABEL
      : reason
        ? `${SEND_LABEL}: ${reason}`
        : SEND_LABEL;
  const stops = busy || stopping;
  return (
    <Tooltip label={label}>
      {(tip) => (
        <button
          type="button"
          className="chat-composer-send"
          data-rail-end=""
          data-busy={stops ? "" : undefined}
          disabled={dead || stopping}
          aria-label={label}
          onClick={onPress}
          {...tip}
        >
          {stops ? <StopGlyph /> : <UpArrow />}
        </button>
      )}
    </Tooltip>
  );
}
// ── Slash command menu ────────────────────────────────────────────────────────
// Opens when "/" starts the input, anchored to the unit so it clears the unit's
// own frame instead of landing on it. Described rows, keyboard navigation,
// Escape / outside dismiss; picking a row runs its command, and Tab completes
// one into the field for a user who wants to add arguments.
/** What a key leaves the menu doing. `run` sends the command, `complete` writes
 *  it into the field for a user who wants to add arguments, `extend` types as
 *  far as the matches agree, `pass` is a key the menu has no answer for. */
type SlashAction =
  | { kind: "move"; delta: 1 | -1 }
  | { kind: "close" }
  | { kind: "run" }
  | { kind: "complete" }
  | { kind: "extend" }
  | { kind: "pass" };
interface SlashKeyInput {
  key: string;
  shift: boolean;
  items: SlashCommand[];
  active: number;
  /** The arrows moved the highlight, so the active row is one the user chose. */
  chosen: boolean;
  /** A turn is running, so nothing can send. */
  busy: boolean;
}
const SLASH_MOVES: Record<string, 1 | -1 | undefined> = {
  ArrowDown: 1,
  ArrowUp: -1,
};
/** The CLI's usage convention: "()" marks a required argument, "[]" an optional
 *  one. A command that needs an argument cannot run bare, so it completes. */
function needsArgument(usage: string | undefined): boolean {
  return usage?.trimStart().startsWith("(") ?? false;
}
/** Acting on one row -- the pointer's click, and Enter once it has a row it
 *  trusts. Nothing that cannot send runs. */
function pickAction(
  item: SlashCommand | undefined,
  busy: boolean,
): SlashAction {
  return !item || busy || needsArgument(item.usage)
    ? { kind: "complete" }
    : { kind: "run" };
}
/** Enter runs the command, but only against a row it can trust -- the one match
 *  the filter left, or the one the arrows walked to. Several matches and no walk
 *  types what they share and leaves the menu up, because the wrong command
 *  running is worse than a keystroke that only narrows. Tab always completes, so
 *  the two keys never mean the same thing. */
function slashKey(input: SlashKeyInput): SlashAction {
  const { key, shift, items, active, chosen, busy } = input;
  const delta = SLASH_MOVES[key];
  if (delta) return { kind: "move", delta };
  if (key === "Escape") return { kind: "close" };
  if (key === "Tab") return { kind: "complete" };
  if (key !== "Enter" || shift) return { kind: "pass" };
  if (items.length > 1 && !chosen) return { kind: "extend" };
  return pickAction(items[active], busy);
}
/** How far the matches agree: the longest name every one of them starts with. */
function commonPrefix(items: SlashCommand[]): string {
  let prefix = items[0]?.command ?? "";
  const shares = (item: SlashCommand): boolean =>
    item.command.toLowerCase().startsWith(prefix.toLowerCase());
  while (prefix.length > 0 && !items.every(shares))
    prefix = prefix.slice(0, -1);
  return prefix;
}
function SlashMenu({
  items,
  active,
  onHover,
  onPick,
}: {
  items: SlashCommand[];
  active: number;
  onHover: (i: number) => void;
  onPick: (i: number) => void;
}): ReactNode {
  return (
    <ul
      className="chat-composer-slash"
      role="listbox"
      aria-label="Slash commands"
    >
      {items.map((item, i) => (
        <li key={item.command}>
          <button
            type="button"
            role="option"
            aria-selected={i === active}
            className="chat-composer-slash__opt"
            data-active={i === active ? "" : undefined}
            onMouseEnter={() => onHover(i)}
            onClick={() => onPick(i)}
          >
            <Text className="chat-composer-slash__cmd" tooltip="truncate">
              {item.command}
            </Text>
            <Text className="chat-composer-slash__summary" tooltip="truncate">
              {item.summary}
            </Text>
            {item.usage ? (
              <Text className="chat-composer-slash__usage" tooltip="truncate">
                {item.usage}
              </Text>
            ) : null}
          </button>
        </li>
      ))}
    </ul>
  );
}
/** UTF-8 bytes of `text`: what a shared draft's size is measured in. */
function utf8Length(text: string): number {
  return new TextEncoder().encode(text).length;
}
/** Why a shared draft refused to grow. */
export function sharedDraftTooLong(maxBytes: number): string {
  return `A shared draft holds at most ${Math.floor(maxBytes / 1024)} KB.`;
}
// ── Remote carets ─────────────────────────────────────────────────────────────
// A textarea will not let anything be drawn INSIDE it, so a colleague's caret
// is drawn over it: a mirror div holding the same text, wrapped the same way by
// the same font, padding and width, with a zero-width marker spliced in at the
// caret's offset. The browser does the hard part — where a character lands once
// the line has wrapped is exactly the thing that cannot be computed by counting
// — and the marker rides along with it.
//
// One layer per peer rather than one shared layer: two people selecting
// overlapping ranges would otherwise have to be merged into nested spans, and
// the merge is both fiddly and invisible to the reader, who sees two translucent
// bands either way.
function CaretLayer({
  text,
  caret,
}: {
  text: string;
  caret: RemoteCaret;
}): ReactNode {
  const { offset, anchor } = anchorCaret(text, caret);
  const start = Math.min(offset, anchor);
  const end = Math.max(offset, anchor);
  const bar = (
    <span className="chat-composer-caret" data-caret-name={caret.name}>
      <span className="chat-composer-caret__flag">{caret.name}</span>
    </span>
  );
  const range =
    end > start ? (
      <span className="chat-composer-caret__range">{text.slice(start, end)}</span>
    ) : null;
  return (
    <div
      className="chat-composer-mirror"
      data-caret-peer={caret.id}
      style={{ "--chat-caret-hue": caret.hue } as CSSProperties}
    >
      {text.slice(0, start)}
      {/* The bar sits at the HEAD of the selection, which is whichever end the
          peer is dragging from — so a backwards selection puts it on the left. */}
      {offset <= anchor ? bar : null}
      {range}
      {offset <= anchor ? null : bar}
      {text.slice(end)}
      {/* A trailing newline has no height of its own; this gives the last line
          one so a caret parked on it is not drawn a line too high. */}
      {"​"}
    </div>
  );
}
// ── The unit ──────────────────────────────────────────────────────────────────
export function Composer({
  busy = false,
  unavailable = false,
  unavailableReason,
  placeholder,
  context,
  contextPercent,
  contextTitle,
  modes,
  mode,
  onModeChange,
  modeLockedReason,
  models,
  model,
  onModelChange,
  modelHint,
  staticModel,
  efforts,
  effort,
  onEffortChange,
  slashCommands = [],
  onSend,
  maxMessageChars,
  onStop,
  stopping = false,
  onAttach,
  attachments = [],
  onRemoveAttachment,
  uploader,
  onUploadFailed,
  draft,
  onDraftChange,
  onDraftBlur,
  remoteCarets = [],
  onSelectionChange,
  binding,
  draftNotice,
  queuedBehindTurn = false,
  queued = [],
  onQueue,
  onEditQueued,
  onRemoveQueued,
  onSendQueued,
  offer,
}: ComposerProps): ReactNode {
  const shownPlaceholder = placeholder ?? defaultPlaceholder();
  const fieldRef = useRef<HTMLTextAreaElement>(null);
  const caretsRef = useRef<HTMLDivElement>(null);
  const railRef = useRef<HTMLDivElement>(null);
  const railWidth = useRailWidth(railRef);
  const [text, setText] = useState("");
  // The inline uploads, in pill order. Held in a ref as well so an upload that
  // settles after a removal reads the current list, not the one it closed over.
  const [uploads, setUploads] = useState<StagedUpload[]>([]);
  const uploadsRef = useRef<StagedUpload[]>([]);
  uploadsRef.current = uploads;
  const textRef = useRef("");
  textRef.current = text;
  // What the field itself holds, which trails `text` until React draws it: a
  // key pressed in between lands on this, and a bound field reports its edit
  // against it.
  const drawnRef = useRef("");
  // A commit that is opening its chat and uploading the pills it held: Send is
  // closed for its length, so a second press cannot open a second chat.
  const [committingHeld, setCommittingHeld] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  // The line a refused file left, so the reader taking pills off clears it:
  // it is about the files they were adding, not about the message.
  const refusalRef = useRef<string | null>(null);
  // Enter landed mid-turn on a composer with nowhere to hold the message. The
  // words stayed in the field under the line that says when they go; this is
  // what keeps that promise on the turn's end.
  const deferredRef = useRef(false);
  // Held messages come in kinds that must never be drawn under one line: the
  // ones this session queued go out on the turn's end, while a message a
  // previous session left behind, or one the reader's Stop cancelled, has not
  // been sent and will not go until they say so. The stop is named where it
  // applies, because "queued" would be the opposite of what the press did.
  const unsentQueue = queued.filter((held) => held.unsent);
  const stoppedQueue = queued.filter((held) => held.stopped && !held.unsent);
  const restoredQueue = queued.filter((held) => held.restored && !held.stopped && !held.unsent);
  const sendingQueue = queued.filter((held) => !held.restored && !held.stopped && !held.unsent);
  // The stamp of the shared draft this field is already showing. Keyed on the
  // stamp rather than the text so that a remote edit BACK to what is on screen
  // still counts as adopted, and a re-render with the same draft never fights
  // the caret.
  const adopted = useRef<number | null>(null);
  const adoptedText = useRef<string | null>(null);
  // The live binding's side of the field: whether an input method is
  // composing (a remote edit waits for it), the selection a remote edit asked
  // to be restored once React has rendered its text, and the other people's
  // carets and anything the binding needs to tell the reader.
  const composingRef = useRef(false);
  const pendingSelection = useRef<TextSelection | null>(null);
  const [bindingCarets, setBindingCarets] = useState<RemoteCaret[]>([]);
  const [bindingNotice, setBindingNotice] = useState<BindingNotice | null>(null);
  const [slashActive, setSlashActive] = useState(0);
  // The arrows moved the highlight, so Enter is acting on a row the user chose
  // rather than on whatever the filter happened to put first.
  const [slashChosen, setSlashChosen] = useState(false);
  const [slashDismissed, setSlashDismissed] = useState(false);
  // The rail key holds the menu open on its own, so opening it never has to
  // write a "/" into the user's message to have somewhere to keep that state.
  const [slashPinned, setSlashPinned] = useState(false);
  const modeItems: MetaOption[] = modes.map((m) => ({
    value: m.value,
    label: m.label,
    description: m.description,
    short: m.short,
    icon: (
      <ModeGlyph mode={m.value} className="chat-composer-glyph" size={15} />
    ),
  }));
  const modelItems: MetaOption[] = models.map((m) => ({
    value: m.value,
    label: m.label,
    short: shortModelLabel(m.label),
    icon: resolveModelMark(m.value, m.label),
    unavailable: m.unavailable,
    escape: m.escape,
  }));
  const effortItems: MetaOption[] = efforts.map((e) => ({
    value: e.value,
    label: e.label,
    icon: <EffortBars bars={e.bars} />,
  }));
  // The menu has two doors and one open state. Typing "/" filters by the token
  // after it; the rail's key opens the same menu on an empty filter. Deriving
  // the state from EITHER door is what lets the key open the menu without
  // putting a character in the message.
  const typedQuery =
    text.startsWith("/") && !text.slice(1).includes(" ")
      ? text.slice(1).toLowerCase()
      : null;
  const slashQuery = typedQuery ?? (slashPinned ? "" : null);
  const slashItems =
    slashQuery !== null
      ? slashCommands.filter((c) =>
          c.command.slice(1).toLowerCase().startsWith(slashQuery),
        )
      : [];
  const slashOpen =
    slashQuery !== null && !slashDismissed && slashItems.length > 0;
  const activeIndex = Math.min(slashActive, Math.max(0, slashItems.length - 1));
  // Every close routes here. Stable so the outside-press effect can call it
  // without re-listening on each render.
  const closeSlash = useCallback((): void => {
    setSlashDismissed(true);
    setSlashPinned(false);
    setSlashChosen(false);
  }, []);
  // Outside means outside the MENU and its KEY: a press anywhere else, the
  // field and the rail's other controls included, closes the menu. The key is
  // excluded so its own opening press cannot close what it opened.
  useEffect(() => {
    if (!slashOpen) return;
    const onDown = (event: PointerEvent): void => {
      const target = event.target as Element | null;
      if (target?.closest(".chat-composer-slash, .chat-composer-slashkey"))
        return;
      closeSlash();
    };
    document.addEventListener("pointerdown", onDown, true);
    return () => document.removeEventListener("pointerdown", onDown, true);
  }, [slashOpen, closeSlash]);
  const grow = (): void => {
    const el = fieldRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 132)}px`;
  };
  // Where this reader's caret is now, with the text either side of it so the
  // people who receive it can re-find it after a merge. The head of a selection
  // is the end being dragged: `selectionDirection` says which, and a browser
  // that reports "none" is treating the end as the head.
  const fieldSelection = (): TextSelection | null => {
    const el = fieldRef.current;
    if (!el || document.activeElement !== el) return null;
    const start = el.selectionStart ?? 0;
    const end = el.selectionEnd ?? start;
    const direction = el.selectionDirection === "backward" ? "backward" : start === end ? "none" : "forward";
    return { start, end, direction };
  };
  const reportSelection = (): void => {
    if (binding) {
      const selection = fieldSelection();
      // A caret the reader moves while somebody else's edit waits to be drawn
      // is where the field puts it once drawn, not the one captured before.
      // Only a caret outside the text that changed can be moved onto the new
      // text by offset; inside it, the binding placed the caret by the
      // characters around it, and that placement stands.
      if (selection && pendingSelection.current) {
        const gap = spliceFor(drawnRef.current, textRef.current);
        const move = (offset: number): number | null =>
          gap === null || offset <= gap.index
            ? offset
            : offset >= gap.index + gap.remove
              ? offset + gap.insert.length - gap.remove
              : null;
        const start = move(selection.start);
        const end = move(selection.end);
        if (start !== null && end !== null) pendingSelection.current = { ...selection, start, end };
      }
      binding.select(selection);
      return;
    }
    const el = fieldRef.current;
    if (!el || !onSelectionChange) return;
    const from = el.selectionStart ?? 0;
    const to = el.selectionEnd ?? from;
    const backward = el.selectionDirection === "backward";
    const offset = backward ? from : to;
    const anchor = backward ? to : from;
    onSelectionChange({
      offset,
      anchor,
      before: el.value.slice(Math.max(0, offset - CARET_CONTEXT_CHARS), offset),
      after: el.value.slice(offset, offset + CARET_CONTEXT_CHARS),
    });
  };
  // `selectionchange` is the only event that fires for a caret moved by the
  // keyboard's own navigation on every browser; the field's own select/keyup/
  // click cover the rest and cost nothing when this document never fires it.
  // Held in a ref so a keystroke does not re-bind a document listener.
  const reporter = useRef(reportSelection);
  reporter.current = reportSelection;
  useEffect(() => {
    if (!onSelectionChange && !binding) return;
    const onSelectionEvent = (): void => {
      if (document.activeElement === fieldRef.current) reporter.current();
    };
    document.addEventListener("selectionchange", onSelectionEvent);
    return () =>
      document.removeEventListener("selectionchange", onSelectionEvent);
  }, [onSelectionChange, binding]);
  // The binding drives the field: it shows the shared text on attach and after
  // every edit somebody else makes, with this reader's selection carried
  // across the change rather than thrown to the end of the text.
  useEffect(() => {
    if (!binding) return;
    const initial = binding.text();
    textRef.current = initial;
    setText(initial);
    requestAnimationFrame(grow);
    const detach = binding.attach({
      // What the field holds, the text its selection counts in.
      getText: () => drawnRef.current,
      getSelection: fieldSelection,
      setText: (next, selection) => {
        textRef.current = next;
        pendingSelection.current = selection;
        setText(next);
        requestAnimationFrame(grow);
      },
      isComposing: () => composingRef.current,
    });
    const stopCarets = binding.subscribeCarets(setBindingCarets);
    const stopNotice = binding.subscribeNotice(setBindingNotice);
    return () => {
      detach();
      stopCarets();
      stopNotice();
      setBindingCarets([]);
      setBindingNotice(null);
    };
    // `grow` and `fieldSelection` read refs; the binding is what matters.
  }, [binding]);
  useLayoutEffect(() => {
    const selection = pendingSelection.current;
    const el = fieldRef.current;
    if (el) drawnRef.current = el.value;
    pendingSelection.current = null;
    if (!selection || !el || document.activeElement !== el) return;
    el.setSelectionRange(selection.start, selection.end, selection.direction);
  }, [text]);
  // The browser's own undo would replay keystrokes against a field the shared
  // document has rewritten under it; undo is the binding's, and only ever
  // undoes this reader's own edits.
  useEffect(() => {
    const el = fieldRef.current;
    if (!binding || !el) return;
    const onBeforeInput = (event: InputEvent): void => {
      if (event.inputType === "historyUndo" || event.inputType === "historyRedo") {
        event.preventDefault();
        if (event.inputType === "historyUndo") binding.undo();
        else binding.redo();
      }
    };
    el.addEventListener("beforeinput", onBeforeInput);
    return () => el.removeEventListener("beforeinput", onBeforeInput);
  }, [binding]);
  // Whether the field was last a view of a live binding: when the live lane
  // lets it go, what it holds is the draft, not any draft it last saw unbound.
  const wasBound = useRef(false);
  useEffect(() => {
    if (binding) wasBound.current = true;
  }, [binding]);
  // Adopt a shared draft the host says is newer than the one on screen. It is
  // NOT reported back through `onDraftChange`: this text came from the shared
  // document and writing it back would be an echo, which on two composers is a
  // loop. The first draft a chat opens with adopts the same way, which is what
  // makes a reload (or a resume) come back with what was being typed.
  useEffect(() => {
    if (!binding && wasBound.current) {
      // Just let go by the live lane: the field keeps its text (a draft the
      // page opened with is older than anything typed since) and reports it,
      // so the host edits on from what the reader sees.
      wasBound.current = false;
      if (draft) {
        adopted.current = draft.at;
        adoptedText.current = draft.text;
      }
      onDraftChange?.(textRef.current);
      return;
    }
    // A draft is new when its stamp or its text is: the server stamps a merge
    // with the newer stamp, so a colleague's words can arrive under one this
    // field has already taken.
    if (!draft || (adopted.current === draft.at && adoptedText.current === draft.text)) return;
    adopted.current = draft.at;
    adoptedText.current = draft.text;
    if (binding) {
      // A bound field takes a draft only as the reader's own words handed back
      // (a send that could not start the chat): an edit like any other, so the
      // other people in the draft see it too.
      setValue(draft.text);
      return;
    }
    setText(draft.text);
    requestAnimationFrame(grow);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `grow` reads a ref; the draft's stamp alone decides whether this runs
  }, [draft?.at, draft?.text, binding]);
  const setValue = (v: string, drawn?: string): void => {
    const before = drawn ?? textRef.current;
    if (
      binding &&
      binding.maxBytes > 0 &&
      utf8Length(v) > binding.maxBytes &&
      utf8Length(v) > utf8Length(before)
    ) {
      // The shared draft has a size it will not grow past; the field keeps
      // what it had rather than diverging from the document it shows.
      setNotice(sharedDraftTooLong(binding.maxBytes));
      drawnRef.current = textRef.current;
      setText(textRef.current);
      return;
    }
    // Synchronously too: an upload settling or a pill removal reads the text
    // through the ref before React has re-rendered the state.
    textRef.current = v;
    setText(v);
    if (binding) binding.edit(before, v, fieldSelection());
    else onDraftChange?.(v);
    setSlashDismissed(false);
    // Typing hands the menu back to the text: a line that no longer starts with
    // "/" closes it, even if the rail key was what opened it.
    setSlashPinned(false);
    setSlashActive(0);
    // A keystroke re-filters the list, so the row the arrows chose is gone and the
    // highlight is back on a first row nobody picked. Enter must not run that.
    setSlashChosen(false);
    requestAnimationFrame(grow);
  };
  // ── Inline uploads ──────────────────────────────────────────────────────────
  // Replace the upload list, renumbering the pills in the text to match, so a
  // removed `[Image 1]` leaves the next one reading `[Image 1]` everywhere.
  const setUploadList = (next: StagedUpload[]): void => {
    const numbered = numberedUploads(next);
    const before = uploadsRef.current;
    uploadsRef.current = numbered;
    setUploads(numbered);
    const rewritten = renumberTokens(textRef.current, before, numbered);
    if (rewritten !== textRef.current) setValue(rewritten);
  };
  const patchUpload = (id: string, patch: Partial<StagedUpload>): void => {
    if (!uploadsRef.current.some((u) => u.id === id)) return;
    setUploadList(uploadsRef.current.map((u) => (u.id === id ? { ...u, ...patch } : u)));
  };
  // Withdraw one pill: its token leaves the text, its preview is released, and
  // the pills after it close ranks.
  const removeUpload = (id: string): void => {
    const gone = uploadsRef.current.find((u) => u.id === id);
    if (!gone) return;
    if (gone.previewUrl) URL.revokeObjectURL(gone.previewUrl);
    setValue(removeUploadToken(textRef.current, uploadToken(gone.kind, gone.n)));
    setUploadList(uploadsRef.current.filter((u) => u.id !== id));
  };
  // One file's whole life: up to UPLOAD_ATTEMPTS sends of the SAME File (a
  // fresh body each time — a File is re-readable, a consumed stream is not),
  // then either a landed path or a withdrawn pill and a stated reason.
  const runUpload = async (entry: StagedUpload, send: ComposerUploader): Promise<void> => {
    const delay = send.retryDelayMs ?? defaultRetryDelayMs;
    let reason = "the upload failed";
    for (let attempt = 1; attempt <= UPLOAD_ATTEMPTS; attempt += 1) {
      if (!uploadsRef.current.some((u) => u.id === entry.id)) return;
      if (attempt > 1) await new Promise((r) => setTimeout(r, delay(attempt)));
      try {
        const landed = await send.upload(entry.file, {
          kind: entry.kind,
          n: entry.n,
          onProgress: (percent) => patchUpload(entry.id, { progress: percent }),
        });
        patchUpload(entry.id, { state: "uploaded", path: landed.path, progress: undefined, error: undefined });
        return;
      } catch (err) {
        reason = err instanceof Error && err.message ? err.message : String(err);
        patchUpload(entry.id, { state: "failed", error: reason });
      }
    }
    if (!uploadsRef.current.some((u) => u.id === entry.id)) return;
    removeUpload(entry.id);
    const said = `${entry.name} could not be uploaded: ${reason}`;
    setNotice(said);
    onUploadFailed?.(entry.name, reason);
  };
  // Stage files from a paste, a drop or the picker: a pill per file at the
  // caret, an upload started at once — or held until the send, where the
  // uploader says the chat is only opened for a message. A file over a cap the
  // uploader declares gets one line and no pill; where the uploader declares
  // none, the deployment's ceiling is the server's to refuse and its words are
  // what the reader reads.
  //
  // An unavailable composer stages nothing, by any door: the pill would sit on a
  // message that cannot be sent, and leave with the page.
  const stageFiles = (files: File[]): void => {
    if (!uploader || files.length === 0 || unavailable) return;
    const held = uploader.timing === "on-send";
    const cap = uploader.maxBytes;
    const accept = uploader.accept;
    // The same file twice is one pill: a reader who drops a selection they had
    // already dropped meant to add it, not to add it again, and two pills are
    // two uploads and two tokens in the message.
    const distinct = dedupeUploads(
      files,
      uploadsRef.current.map((u) => u.file),
    );
    const refused = distinct.map((file) => refuseUpload(file, cap, accept)).find((why) => why !== null);
    if (refused) {
      refusalRef.current = refused;
      setNotice(refused);
    }
    const accepted = distinct.filter((file) => refuseUpload(file, cap, accept) === null);
    if (accepted.length === 0) return;
    const count: Record<UploadKind, number> = {
      image: uploadsRef.current.filter((u) => u.kind === "image").length,
      file: uploadsRef.current.filter((u) => u.kind === "file").length,
    };
    const entries: StagedUpload[] = accepted.map((file) => {
      const kind: UploadKind = isChatImageName(file.name) || file.type.startsWith("image/") ? "image" : "file";
      count[kind] += 1;
      return {
        id: `u${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`,
        kind,
        n: count[kind],
        file,
        name: file.name || (kind === "image" ? "image.png" : "file"),
        state: held ? "held" : "uploading",
        previewUrl: kind === "image" && typeof URL.createObjectURL === "function" ? URL.createObjectURL(file) : undefined,
      };
    });
    const el = fieldRef.current;
    const at = el?.selectionStart ?? textRef.current.length;
    const current = textRef.current;
    const tokens = entries.map((u) => uploadToken(u.kind, u.n)).join(" ");
    const before = current.slice(0, at);
    const after = current.slice(at);
    // A space after the pills so typing carries on past them.
    const next = `${before}${before && !/\s$/.test(before) ? " " : ""}${tokens}${/^\s/.test(after) ? "" : " "}${after}`;
    setValue(next);
    setUploadList([...uploadsRef.current, ...entries]);
    if (!held) for (const entry of entries) void runUpload(entry, uploader);
    requestAnimationFrame(() => fieldRef.current?.focus());
  };
  /** Files out of a clipboard or a drag, ignoring the text they came with. */
  const filesFrom = (transfer: DataTransfer | null): File[] =>
    transfer ? Array.from(transfer.files ?? []) : [];
  const uploading = uploads.some((u) => u.state === "uploading") || committingHeld;
  // The server's own ceiling on a message, and how close this one is to it.
  // Nothing is said until the reader is near it — a counter over an ordinary
  // sentence is noise — and past it the key is dead with the reason under the
  // field, so a long paste is refused here rather than after the round trip.
  const overLimit = maxMessageChars !== undefined && text.length > maxMessageChars;
  const countdown =
    maxMessageChars !== undefined && text.length >= maxMessageChars * MESSAGE_COUNTER_AT
      ? `${text.length.toLocaleString()} / ${maxMessageChars.toLocaleString()} characters`
      : null;
  const tooLong = maxMessageChars === undefined ? "" : messageTooLong(maxMessageChars);
  // A pill is content: a message may be only the files on it, whether or not
  // their tokens are still in the words (they ride after the words when not).
  const nothingToSend = text.trim().length === 0 && uploads.length === 0;
  const sendBlockedReason = uploading
    ? "Waiting for uploads to finish"
    : overLimit
      ? tooLong
      : null;
  // A run commits the command as its own line, so the host routes it the way it
  // routes a typed one. A completion leaves the caret after the name instead.
  const applySlash = (action: SlashAction, index = activeIndex): void => {
    const item = slashItems[index];
    if (action.kind === "move") {
      setSlashActive(
        (a) =>
          (Math.min(a, slashItems.length - 1) +
            action.delta +
            slashItems.length) %
          slashItems.length,
      );
      setSlashChosen(true);
      return;
    }
    if (action.kind === "close") {
      closeSlash();
      return;
    }
    if (action.kind === "extend") {
      setValue(commonPrefix(slashItems));
      return;
    }
    if (!item) return;
    if (action.kind === "complete") {
      setText(`${item.command} `);
      closeSlash();
    } else {
      setValue("");
      onSend(item.command);
    }
    fieldRef.current?.focus();
    requestAnimationFrame(grow);
  };
  // The rail's slash key is the pointer's door to the same menu the "/"
  // character opens. It never writes to the field: a press opens the menu on an
  // empty filter, a second press closes it. On a line that already starts with
  // "/" the typed token keeps filtering and the press just lifts a dismissal.
  const toggleSlash = (): void => {
    if (slashOpen) {
      closeSlash();
      return;
    }
    setSlashDismissed(false);
    setSlashPinned(true);
    setSlashActive(0);
    setSlashChosen(false);
    fieldRef.current?.focus();
  };
  const cycleMode = (): void => {
    if (modes.length === 0 || !onModeChange || modeLockedReason) return;
    const at = modes.findIndex((m) => m.value === mode);
    onModeChange(modes[(at + 1 + modes.length) % modes.length].value);
  };
  // Send is the one commit action: a no-op on an empty message, or while an
  // upload is still in flight (a pill whose path is not known yet cannot be
  // written out). While the agent works it is never a DEAD key — see below.
  const commit = (): void => {
    // The one gate every way into a send passes: the key, Enter, a deferred
    // send on a turn's end and an offer from outside the field alike.
    if (unavailable) return;
    if (uploading) return;
    if (overLimit) {
      setNotice(tooLong);
      return;
    }
    if (nothingToSend) return;
    const held = uploads.filter((u) => u.state === "held");
    if (busy) {
      // The words cannot go into a running turn, and a keypress that does
      // nothing and says nothing reads as a message sent. So either the host
      // holds them where the reader can see them, or they stay in the field
      // under a line saying when they go — and this composer sends them then.
      // A pill still waiting on the chat it will be uploaded into cannot be
      // written out yet, so that message stays in the field too.
      if (!onQueue || held.length > 0) {
        deferredRef.current = true;
        setNotice(WORKING_HINT);
        return;
      }
      const message = takeMessage();
      if (message === null) return;
      onQueue(message);
      setValue("");
      return;
    }
    if (held.length > 0 && uploader) {
      void commitHeld(held, uploader);
      return;
    }
    sendOut();
  };
  // The message as it goes: every landed pill written out as its markdown, the
  // previews released, the staged list emptied. Null when there is nothing to
  // send. A reason stated for a pill withdrawn on the way out stays under the
  // field — the message went, and the reader is still owed the word on what
  // did not go with it.
  const takeMessage = (keepNotice = false): string | null => {
    const current = uploadsRef.current;
    const message = replaceUploadTokens(textRef.current, current).trim();
    if (message.length === 0) return null;
    for (const u of current) if (u.previewUrl) URL.revokeObjectURL(u.previewUrl);
    uploadsRef.current = [];
    setUploads([]);
    if (!keepNotice) setNotice(null);
    return message;
  };
  const sendOut = (keepNotice = false): void => {
    const message = takeMessage(keepNotice);
    if (message === null) return;
    onSend(message);
    setValue("");
  };
  // A commit with held pills: the uploader opens what the files go into, the
  // held pills go up as one batch under the same retries as an at-once pill,
  // and the message leaves with the ones that landed. A pill that failed for
  // good has withdrawn itself and said why; the reader's words still go — a
  // chat opened for this message is not left with none — unless the failed
  // pill was the whole message, which stays in the field with the reason.
  const commitHeld = async (held: StagedUpload[], send: ComposerUploader): Promise<void> => {
    setCommittingHeld(true);
    setNotice(null);
    try {
      // The message as it stands, without the pills: what the chat is titled
      // for is what the reader said, not the tokens the field draws.
      const said = held.reduce((t, u) => removeUploadToken(t, uploadToken(u.kind, u.n)), textRef.current);
      if (send.beforeSend) await send.beforeSend(said.trim());
    } catch (err) {
      const reason = err instanceof Error && err.message ? err.message : String(err);
      setNotice(`Couldn't start the chat: ${reason}`);
      setCommittingHeld(false);
      return;
    }
    const ids = new Set(held.map((u) => u.id));
    setUploadList(uploadsRef.current.map((u) => (ids.has(u.id) ? { ...u, state: "uploading" } : u)));
    await Promise.all(
      uploadsRef.current.filter((u) => ids.has(u.id)).map((entry) => runUpload(entry, send)),
    );
    setCommittingHeld(false);
    sendOut(true);
  };
  // The reader taking a pill off is done with the batch a refusal was about.
  const withdrawUpload = (id: string): void => {
    removeUpload(id);
    const refusal = refusalRef.current;
    if (refusal !== null) {
      setNotice((shown) => (shown === refusal ? null : shown));
      refusalRef.current = null;
    }
  };
  // Backspace over a pill takes the whole pill, not its closing bracket.
  const backspacePill = (): boolean => {
    const el = fieldRef.current;
    if (!el || el.selectionStart !== el.selectionEnd) return false;
    const head = text.slice(0, el.selectionStart ?? 0);
    const pill = uploadsRef.current.find((u) => head.endsWith(uploadToken(u.kind, u.n)));
    if (!pill) return false;
    withdrawUpload(pill.id);
    return true;
  };
  // Held in a ref so the turn-end effect below can call the CURRENT commit
  // without re-arming itself on every keystroke.
  const commitRef = useRef(commit);
  commitRef.current = commit;
  useEffect(() => {
    // A composer that went unavailable meanwhile keeps the promise for when it
    // is back, rather than spending it on a send the gate would refuse.
    if (busy || unavailable || !deferredRef.current) return;
    deferredRef.current = false;
    commitRef.current();
  }, [busy, unavailable]);
  // An offer from outside the field. The words are written first and the
  // commit runs on the render that shows them, so it reads the message the
  // field now holds — pills included — exactly as a press of Send would.
  // An offer already there when this composer mounts was made to the one before
  // it — a remount must never send the same ask a second time.
  const offeredAt = useRef<number | null>(offer?.at ?? null);
  const [offerCommit, setOfferCommit] = useState(0);
  useEffect(() => {
    if (!offer || offeredAt.current === offer.at) return;
    offeredAt.current = offer.at;
    if (unavailable) return;
    const staged = uploadsRef.current;
    const own = staged
      .reduce((t, u) => removeUploadToken(t, uploadToken(u.kind, u.n)), textRef.current)
      .trim();
    const current = textRef.current.trim();
    if (own.length > 0) {
      setValue(`${offer.text} ${current}`);
      requestAnimationFrame(() => fieldRef.current?.focus());
      return;
    }
    setValue(current ? `${offer.text} ${current}` : offer.text);
    setOfferCommit((n) => n + 1);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `setValue` and the refs are stable; the offer's stamp alone decides whether this runs
  }, [offer?.at, offer?.text, unavailable]);
  useEffect(() => {
    if (offerCommit > 0) commitRef.current();
  }, [offerCommit]);
  const onFieldKeyDown = (
    event: ReactKeyboardEvent<HTMLTextAreaElement>,
  ): void => {
    if (binding && (event.metaKey || event.ctrlKey) && !event.altKey) {
      const key = event.key.toLowerCase();
      if (key === "z" || key === "y") {
        event.preventDefault();
        if (key === "y" || event.shiftKey) binding.redo();
        else binding.undo();
        return;
      }
    }
    // The binding the menu hint states.
    if (event.key === "Tab" && event.shiftKey) {
      event.preventDefault();
      cycleMode();
      return;
    }
    if (event.key === "Backspace" && backspacePill()) {
      event.preventDefault();
      return;
    }
    if (slashOpen) {
      const action = slashKey({
        key: event.key,
        shift: event.shiftKey,
        items: slashItems,
        active: activeIndex,
        chosen: slashChosen,
        busy,
      });
      if (action.kind !== "pass") {
        event.preventDefault();
        applySlash(action);
        return;
      }
    }
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      commit();
    }
  };
  const shownCarets = binding ? bindingCarets : remoteCarets;
  return (
    <div className="chat-composer-root">
      <form
        className="chat-composer-unit"
        data-mode={mode}
        aria-label="Message composer"
        onSubmit={(event) => {
          event.preventDefault();
          commit();
        }}
      >
        <div
          className="chat-composer-fieldwrap"
          onDragOver={uploader ? (event) => event.preventDefault() : undefined}
          onDrop={
            uploader
              ? (event) => {
                  const files = filesFrom(event.dataTransfer);
                  if (files.length === 0) return;
                  event.preventDefault();
                  stageFiles(files);
                }
              : undefined
          }
        >
          <textarea
            ref={fieldRef}
            className="chat-composer-field"
            placeholder={unavailable ? (unavailableReason ?? shownPlaceholder) : shownPlaceholder}
            rows={2}
            aria-label={`Message ${currentBrand().productName}`}
            disabled={unavailable}
            value={text}
            onChange={(event) => {
              const drawn = drawnRef.current;
              drawnRef.current = event.target.value;
              setValue(event.target.value, drawn);
              reportSelection();
            }}
            onPaste={
              uploader
                ? (event) => {
                    // A pasted picture or file becomes a pill; pasted text
                    // keeps the browser's own paste.
                    const files = filesFrom(event.clipboardData);
                    if (files.length === 0) return;
                    event.preventDefault();
                    stageFiles(files);
                  }
                : undefined
            }
            onKeyDown={onFieldKeyDown}
            onKeyUp={reportSelection}
            onClick={reportSelection}
            onSelect={reportSelection}
            onScroll={(event) => {
              // The mirror is not itself scrollable — it is the whole text at
              // full height, slid up by as much as the field has scrolled.
              caretsRef.current?.style.setProperty(
                "--chat-caret-scroll",
                `${-event.currentTarget.scrollTop}px`,
              );
            }}
            // A bound field that loses focus takes its caret off everyone
            // else's screen; an unbound one persists what it was holding.
            onBlur={binding ? () => binding.select(null) : onDraftBlur}
            onCompositionStart={() => {
              composingRef.current = true;
            }}
            onCompositionEnd={() => {
              composingRef.current = false;
              binding?.compositionEnded();
            }}
          />
          {/* Decoration over somebody else's text: it can neither be clicked
              through to nor read out, and it is absent entirely where nobody
              else is in the draft. */}
          {shownCarets.length > 0 ? (
            <div
              ref={caretsRef}
              className="chat-composer-carets"
              aria-hidden="true"
            >
              {shownCarets.map((caret) => (
                <CaretLayer key={caret.id} text={text} caret={caret} />
              ))}
            </div>
          ) : null}
        </div>
        {slashOpen ? (
          <SlashMenu
            items={slashItems}
            active={activeIndex}
            onHover={setSlashActive}
            onPick={(i) => applySlash(pickAction(slashItems[i], busy), i)}
          />
        ) : null}
        {attachments.length > 0 ? (
          <AttachChips attachments={attachments} onRemove={onRemoveAttachment} />
        ) : null}
        {uploads.length > 0 ? <UploadPills uploads={uploads} onRemove={withdrawUpload} /> : null}
        {countdown ? (
          <p
            className="chat-composer-count"
            data-over={overLimit ? "" : undefined}
            role="status"
          >
            {countdown}
          </p>
        ) : null}
        {bindingNotice ? (
          <div className="chat-composer-notice" role="alert">
            {bindingNotice.message}
            {bindingNotice.restorable ? (
              <>
                {" "}
                <button
                  type="button"
                  className="chat-composer-notice__action"
                  onClick={() => {
                    const restorable = bindingNotice.restorable ?? "";
                    setBindingNotice(null);
                    setValue(textRef.current + restorable);
                  }}
                >
                  Restore my text
                </button>{" "}
                <button
                  type="button"
                  className="chat-composer-notice__action"
                  onClick={() => {
                    void navigator.clipboard?.writeText(bindingNotice.restorable ?? "");
                  }}
                >
                  Copy
                </button>
              </>
            ) : null}
          </div>
        ) : null}
        {draftNotice && !binding ? (
          <p className="chat-composer-notice" data-tone="working" role="status">
            {draftNotice}
          </p>
        ) : null}
        {notice ? (
          // The working cue is not a failure — nothing went wrong and nothing
          // was lost — so it is neither drawn in the refusal tone nor read out
          // over whatever the reader is doing.
          <p
            className="chat-composer-notice"
            data-tone={notice === WORKING_HINT ? "working" : undefined}
            role={notice === WORKING_HINT ? "status" : "alert"}
          >
            {notice}
          </p>
        ) : null}
        {/* A message already accepted, waiting for the turn in front of it.
            Nothing went wrong, so it wears the same quiet tone as the working
            cue rather than the refusal one. */}
        {queuedBehindTurn ? (
          <p className="chat-composer-notice" data-tone="working" role="status">
            {QUEUED_BEHIND_TURN}
          </p>
        ) : null}
        {/* The rail is FLAT: every control is a direct child, so the stylesheet
            can rank what gives way first across the whole row. A left/right
            group wrapper would put the model and the effort gauge in different
            boxes and make that ranking inexpressible. The closing group is an
            auto margin on the first item carrying `data-rail-end`, not a box. */}
        <div className="chat-composer-rail" ref={railRef}>
          {/* Leads the rail and never folds, so the door to the commands is
              there at every width. */}
          {slashCommands.length > 0 ? (
            <SlashDoor open={slashOpen} onToggle={toggleSlash} />
          ) : null}
          {/* Beside the slash key, because both are doors onto what the message
              is made of rather than settings on how it is answered. */}
          {uploader ? (
            <AttachDoor onPick={stageFiles} disabled={unavailable} />
          ) : onAttach ? (
            <AttachDoor onPick={onAttach} disabled={unavailable} />
          ) : null}
          <MetaMenu
            category={MODEL_LABEL}
            options={modelItems}
            value={model}
            onChange={onModelChange}
            rail="model"
            side="left"
            hint={modelHint}
          />
          {/* What governs the turn and what commits it close the rail: the
              context read, effort, then the mode, reading as the settings on
              the send. At the squeezed widths the mode's menu also carries the
              folded model and effort groups. */}
          {context ? (
            <ContextReadout
              context={context}
              percent={contextPercent}
              title={contextTitle}
              compact={railWidth > 0 && railWidth < CONTEXT_FULL_RAIL_PX}
            />
          ) : null}
          <MetaMenu
            category={EFFORT_LABEL}
            options={effortItems}
            value={effort}
            onChange={onEffortChange}
            rail="effort"
            end
            side="right"
          />
          {staticModel ? <StaticModelChip model={staticModel} /> : null}
          <MetaMenu
            category={MODE_LABEL}
            options={modeItems}
            value={mode}
            onChange={modeLockedReason ? undefined : onModeChange}
            lockedReason={modeLockedReason}
            moded
            rail="mode"
            end
            side="right"
            hint={MODE_CYCLE_HINT}
            // The folded groups carry the model and effort pickers at the
            // widths where their own chips have given way. A setting with no
            // handler has no picker at any width, so it is not listed here
            // either — a menu that opens onto a choice nobody can make is the
            // same lie as a chip that does.
            sections={[
              ...(onModelChange
                ? [
                    {
                      title: MODEL_LABEL,
                      options: modelItems,
                      value: model,
                      onChange: onModelChange,
                    },
                  ]
                : []),
              ...(onEffortChange
                ? [
                    {
                      title: EFFORT_LABEL,
                      options: effortItems,
                      value: effort,
                      onChange: onEffortChange,
                    },
                  ]
                : []),
            ]}
          />
          {/* Nothing to send is a dead key; while busy it is Stop and stays
              live, the one way out of a running turn — until a stop of its own
              is on the wire, which is the one thing it waits for. */}
          <SendKey
            busy={busy}
            stopping={stopping}
            // A running turn's Stop is never dead for want of a machine: it is
            // how the reader withdraws a message nothing has picked up yet,
            // and the server records that withdrawal without any box to hear
            // it. Only Send waits on the composer being available.
            dead={!busy && (unavailable || uploading || overLimit || nothingToSend)}
            reason={busy ? null : sendBlockedReason}
            onPress={() => (busy ? onStop?.() : commit())}
          />
        </div>
      </form>
      {/* The groups are drawn apart and each carries its own line: one goes on
          its own, the others are waiting on the reader, and a single heading
          over them would be false for half of them. */}
      {unsentQueue.length > 0 ? (
        <QueuedMessages
          label={UNSENT_LABEL}
          queued={unsentQueue}
          onEdit={onEditQueued}
          onRemove={onRemoveQueued}
          onSend={unavailable ? undefined : onSendQueued}
          sendLabel={RETRY_LABEL}
        />
      ) : null}
      {stoppedQueue.length > 0 ? (
        <QueuedMessages
          label={STOPPED_LABEL}
          queued={stoppedQueue}
          onEdit={onEditQueued}
          onRemove={onRemoveQueued}
          onSend={unavailable ? undefined : onSendQueued}
        />
      ) : null}
      {restoredQueue.length > 0 ? (
        <QueuedMessages
          label={RESTORED_LABEL}
          queued={restoredQueue}
          onEdit={onEditQueued}
          onRemove={onRemoveQueued}
          onSend={unavailable ? undefined : onSendQueued}
        />
      ) : null}
      {sendingQueue.length > 0 ? (
        <QueuedMessages
          label={QUEUED_LABEL}
          queued={sendingQueue}
          onEdit={onEditQueued}
          onRemove={onRemoveQueued}
        />
      ) : null}
    </div>
  );
}
/** A queued message longer than this folds to a few lines behind "Show more",
 *  so a long paste cannot cover the chat column or push the field off screen. */
const QUEUED_FOLD_CHARS = 280;
const QUEUED_FOLD_LINES = 4;

function queuedFolds(text: string): boolean {
  return text.length > QUEUED_FOLD_CHARS || text.split("\n").length > QUEUED_FOLD_LINES;
}

function QueuedRow({
  held,
  onEdit,
  onRemove,
  onSend,
  sendLabel = SEND_LABEL,
}: {
  held: QueuedComposerMessage;
  onEdit?: (id: string, text: string) => void;
  onRemove?: (id: string) => void;
  onSend?: (id: string) => void;
  sendLabel?: string;
}): ReactNode {
  const [open, setOpen] = useState(false);
  const folds = queuedFolds(held.text);
  return (
    <li className="chat-composer-queued__row">
      <div className="chat-composer-queued__body">
        <textarea
          className="chat-composer-queued__text"
          aria-label="Queued message"
          rows={1}
          value={held.text}
          readOnly={!onEdit}
          data-clamped={folds && !open ? "" : undefined}
          onChange={(event) => onEdit?.(held.id, event.target.value)}
        />
        {folds ? (
          <button
            type="button"
            className="chat-composer-queued__more"
            aria-expanded={open}
            onClick={() => setOpen((v) => !v)}
          >
            {open ? "Show less" : "Show more"}
          </button>
        ) : null}
      </div>
      {onSend ? (
        <button
          type="button"
          className="chat-composer-queued__send"
          aria-label={sendLabel === SEND_LABEL ? "Send queued message" : "Retry sending message"}
          onClick={() => onSend(held.id)}
        >
          {sendLabel}
        </button>
      ) : null}
      {onRemove ? (
        <button
          type="button"
          className="chat-composer-queued__remove"
          aria-label="Remove queued message"
          onClick={() => onRemove(held.id)}
        >
          ×
        </button>
      ) : null}
    </li>
  );
}

/** Messages held under the field they were typed in. Each is still the
 *  reader's until it goes: they can rewrite it or take it back, and one that
 *  will not send itself carries the key that sends it. */
function QueuedMessages({
  label,
  queued,
  onEdit,
  onRemove,
  onSend,
  sendLabel,
}: {
  label: string;
  queued: QueuedComposerMessage[];
  onEdit?: (id: string, text: string) => void;
  onRemove?: (id: string) => void;
  onSend?: (id: string) => void;
  sendLabel?: string;
}): ReactNode {
  return (
    <div className="chat-composer-queued">
      {/* Enter during a turn moves the words out of the field and into this
          list, which is the only sign it went anywhere. The line names the
          group and the count, so holding a second message says so rather than
          repeating the first announcement. */}
      <p className="chat-composer-queued__label" role="status">
        {queued.length === 1 ? label : `${label} · ${queued.length}`}
      </p>
      <ul className="chat-composer-queued__list">
        {queued.map((held) => (
          <QueuedRow
            key={held.id}
            held={held}
            onEdit={onEdit}
            onRemove={onRemove}
            onSend={onSend}
            sendLabel={sendLabel}
          />
        ))}
      </ul>
    </div>
  );
}
