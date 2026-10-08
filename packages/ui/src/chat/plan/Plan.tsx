// Plan surfaces: the transcript's proposed-plan document (PlanBlock) and the
// dock's approval card (PlanApprovalCard), one design. Approving a plan is two
// answers at once -- yes, and under which permission mode the session runs
// afterward -- so the card makes that one choice (rows) plus one commit, and
// every settled state names the mode the session ends up in. The host owns the
// resolution, so the dock card and the transcript block settle together.

import { useId, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type ReactNode } from "react";

import { parseMarkdownBlocks, Text, type MarkdownBlock } from "../sharedUi";

import { DialogCaret } from "../dialog";
import { isPlainEnter } from "../hooks";
import { ModeGlyph } from "../modes";
import { Prose } from "../prose";

// The family sheet loads first: an organ rule overrides a shared one by order.
import "../dialog/dialog.css";
import "./plan.css";

/** A mode the session can continue under: the wire value, its name, and that
 *  mode's own words for what it allows. */
export interface PlanModeChoice {
  mode: string;
  label: string;
  description: string;
}

/** `note` is what the reader wrote for the model with the answer, trimmed;
 *  absent when they wrote nothing. */
export type PlanResolution =
  | { kind: "approved"; mode: PlanModeChoice; note?: string }
  | { kind: "rejected"; note?: string };

const STATUS_TEXT = { pending: "Waiting for approval", approved: "Plan approved", rejected: "Plan rejected" };
const APPROVE_LABEL = "Approve & start";
const REJECT_LABEL = "Reject plan";
const OPEN_LABEL = "Open plan";
const NOTE_PLACEHOLDER = "Add a note for the model (optional)";
const NOTE_LEAD = { approved: "Approved with a note: ", rejected: "Rejected: " };

// ── Glyphs ──────────────────────────────────────────────────────────────────

function Waiting(): ReactNode {
  return (
    <svg className="chat-plan-mark" viewBox="0 0 16 16" width="15" height="15" aria-hidden="true">
      <circle cx="8" cy="8" r="5.4" />
      <path d="M8 4.9V8h2.6" />
    </svg>
  );
}

function Check(): ReactNode {
  return (
    <svg className="chat-plan-mark" viewBox="0 0 16 16" width="15" height="15" aria-hidden="true">
      <path d="M3.4 8.4 6.6 11.6 12.6 4.8" />
    </svg>
  );
}

function Refused(): ReactNode {
  return (
    <svg className="chat-plan-mark" viewBox="0 0 16 16" width="15" height="15" aria-hidden="true">
      <circle cx="8" cy="8" r="5.4" />
      <path d="M4.6 8h6.8" />
    </svg>
  );
}

function TickCheck(): ReactNode {
  return (
    <svg className="chat-plan-opt__tick" viewBox="0 0 16 16" width="15" height="15" aria-hidden="true">
      <path d="M3.4 8.4 6.6 11.6 12.6 4.8" />
    </svg>
  );
}

// A pane with the document leaving it: the convention for handing content to a
// surface outside the panel.
function OpenGlyph(): ReactNode {
  return (
    <svg className="chat-plan-open__glyph" viewBox="0 0 14 14" width="14" height="14" aria-hidden="true">
      <path d="M11.3 8.3v2.4a1 1 0 0 1-1 1H3.3a1 1 0 0 1-1-1V3.7a1 1 0 0 1 1-1h2.4" />
      <path d="M8.5 2.7h3.3V6" />
      <path d="M11.8 2.7 6.9 7.6" />
    </svg>
  );
}

// ── The document, read off its markdown ─────────────────────────────────────

const isHeading = (block: MarkdownBlock): block is Extract<MarkdownBlock, { kind: "heading" }> =>
  block.kind === "heading";

/** The plan's own heading names its surfaces, so the dock still identifies its
 *  subject once the document has scrolled away. */
function planTitle(content: string, fallback: string): string {
  return parseMarkdownBlocks(content).find(isHeading)?.text ?? fallback;
}

/** The preview runs to the plan's first structured block: the heading names
 *  the plan, the opening paragraph carries its argument, and neither holds a
 *  control the clip could strand out of reach. Undefined means "show whole". */
function previewCount(content: string): number | undefined {
  const end = parseMarkdownBlocks(content).findIndex((block) => block.kind !== "heading" && block.kind !== "paragraph");
  return end === -1 ? undefined : end;
}

// ── Shared status ───────────────────────────────────────────────────────────

/** Approval flips the session into the chosen mode; rejection leaves it in the
 *  plan mode the host names. Both settled states describe where the session
 *  ends up, in that mode's own words. */
function resolvedMode(resolution: PlanResolution | null, planMode: PlanModeChoice): PlanModeChoice | null {
  if (resolution === null) return null;
  return resolution.kind === "approved" ? resolution.mode : planMode;
}

function statusText(resolution: PlanResolution | null): string {
  if (resolution === null) return STATUS_TEXT.pending;
  return resolution.kind === "approved" ? STATUS_TEXT.approved : STATUS_TEXT.rejected;
}

function ModeChip({ choice }: { choice: PlanModeChoice }): ReactNode {
  return (
    <span className="chat-plan-chip" data-mode={choice.mode}>
      <span className="chat-plan-chip__icon">
        <ModeGlyph mode={choice.mode} className="chat-plan-glyph" />
      </span>
      {choice.label}
    </span>
  );
}

// The transcript's state line and the card's resolved strip say the same thing:
// the phrase, then the mode the session is in once the answer lands. The
// trailing control hands the plan to the editor, so it renders only when the
// host wires it.
function StatusLine({
  resolution,
  planMode,
  onOpen,
}: {
  resolution: PlanResolution | null;
  planMode: PlanModeChoice;
  onOpen?: () => void;
}): ReactNode {
  const mode = resolvedMode(resolution, planMode);
  return (
    <p className="chat-plan-status" data-state={resolution === null ? "pending" : resolution.kind}>
      <span className="chat-plan-status__said">
        <span className="chat-plan-status__mark">
          {resolution === null ? <Waiting /> : resolution.kind === "approved" ? <Check /> : <Refused />}
        </span>
        <span className="chat-plan-status__text">{statusText(resolution)}</span>
        {mode ? <ModeChip choice={mode} /> : null}
      </span>
      {onOpen ? (
        <button type="button" className="chat-plan-open" title={OPEN_LABEL} aria-label={OPEN_LABEL} onClick={onOpen}>
          <OpenGlyph />
          <span className="chat-plan-open__label">{OPEN_LABEL}</span>
        </button>
      ) : null}
    </p>
  );
}

// The reader's note, under the answer it went with, for everyone in the chat.
function AnswerNote({ resolution }: { resolution: PlanResolution }): ReactNode {
  return (
    <p className="chat-plan-result__note">
      {NOTE_LEAD[resolution.kind]}
      {resolution.note}
    </p>
  );
}

// ── The transcript block: the plan as a document ────────────────────────────

export interface PlanBlockProps {
  /** The plan document, markdown. */
  content: string;
  resolution: PlanResolution | null;
  /** The mode a rejection leaves the session in (named on the status line). */
  planMode: PlanModeChoice;
  /** Open the plan in the host's editor. */
  onOpen?: () => void;
}

// An unanswered plan is being judged, so it rests whole; an answered one is
// history and rests at its preview. The key re-seeds that default when the
// answer lands, so a settled transcript goes quiet without the reader asking.
export function PlanBlock({ content, resolution, planMode, onOpen }: PlanBlockProps): ReactNode {
  return (
    <PlanDocument
      key={resolution === null ? "pending" : "settled"}
      content={content}
      resolution={resolution}
      planMode={planMode}
      onOpen={onOpen}
    />
  );
}

function PlanDocument({ content, resolution, planMode, onOpen }: PlanBlockProps): ReactNode {
  const [open, setOpen] = useState(resolution === null);
  const bodyId = useId();
  return (
    <section
      className="chat-plan-doc"
      data-state={resolution === null ? "pending" : resolution.kind}
      data-open={open ? "" : undefined}
    >
      <div className="chat-plan-doc__main">
        <StatusLine resolution={resolution} planMode={planMode} onOpen={onOpen} />
        {resolution?.note ? <AnswerNote resolution={resolution} /> : null}
        <div className="chat-plan-doc__body" id={bodyId}>
          <Prose content={content} maxBlocks={open ? undefined : previewCount(content)} />
        </div>
      </div>
      <button type="button" className="chat-plan-fold" aria-expanded={open} aria-controls={bodyId} onClick={() => setOpen((v) => !v)}>
        {open ? "Hide full plan" : "Show full plan"}
        <DialogCaret className="chat-plan-caret" open={open} />
      </button>
    </section>
  );
}

// ── The dock card: the decision ─────────────────────────────────────────────

export interface PlanApprovalCardProps {
  /** The plan document, markdown; its first heading is the card's title. */
  content: string;
  /** The ask under the title. Also the title fallback for a heading-less plan. */
  question: string;
  /** The ways to proceed; the first is the least permissive, the resting choice. */
  options: PlanModeChoice[];
  /** The mode a rejection leaves the session in. */
  planMode: PlanModeChoice;
  resolution: PlanResolution | null;
  onResolve: (next: PlanResolution) => void;
  /** The last answer did not reach the machine, in one sentence. The card keeps
   *  its rows and its keys, so the same answer can be sent again. */
  failure?: string;
}

// The answered card, in the permission card's register: mark, phrase, the mode
// the session is now in, and that mode's own words for what it allows.
function Resolved({ resolution, planMode }: { resolution: PlanResolution; planMode: PlanModeChoice }): ReactNode {
  const mode = resolution.kind === "approved" ? resolution.mode : planMode;
  return (
    <div className="chat-plan-result" data-mode={mode.mode} data-kind={resolution.kind}>
      <div className="chat-plan-result__head">
        <span className="chat-plan-result__mark">{resolution.kind === "approved" ? <Check /> : <Refused />}</span>
        <span className="chat-plan-result__label">{statusText(resolution)}</span>
        <ModeChip choice={mode} />
      </div>
      <p className="chat-plan-result__desc">{mode.description}</p>
      {resolution.note ? <AnswerNote resolution={resolution} /> : null}
    </div>
  );
}

export function PlanApprovalCard({
  content,
  question,
  options,
  planMode,
  resolution,
  onResolve,
  failure,
}: PlanApprovalCardProps): ReactNode {
  // The selection is derived at read time: `mode` holds only the user's pick,
  // and a pick that matches no current option falls back to the first (the
  // least permissive), so options that arrive or change after mount always
  // leave a real, approvable choice.
  const [mode, setMode] = useState("");
  const chosen = options.find((option) => option.mode === mode) ?? options[0];
  const uid = useId();
  const askId = `${uid}-ask`;
  const approveRef = useRef<HTMLButtonElement>(null);
  const [draft, setDraft] = useState("");
  const note = draft.trim();
  const withNote = note ? { note } : {};

  const title = planTitle(content, question);
  const pending = resolution === null;
  const approve = (): void => {
    if (chosen) onResolve({ kind: "approved", mode: chosen, ...withNote });
  };
  const reject = (): void => onResolve({ kind: "rejected", ...withNote });

  // The card is the keyboard home: Enter commits the selected row, Escape
  // rejects. A key aimed at the reject button keeps its own native activation.
  const onKeyDown = (event: ReactKeyboardEvent<HTMLElement>): void => {
    if (!pending) return;
    if (event.key === "Escape") {
      event.preventDefault();
      reject();
      return;
    }
    if (!isPlainEnter(event)) return;
    const target = event.target as HTMLElement;
    if (
      target === event.currentTarget ||
      target === approveRef.current ||
      target.matches(".chat-plan-opt__input, .chat-plan-note__input")
    ) {
      event.preventDefault();
      approve();
    }
  };

  return (
    <section className="chat-plan-card" aria-label={title} tabIndex={-1} onKeyDown={onKeyDown}>
      <header className="chat-plan-head">
        {/* The heading is the ask, not the plan's own title: a document heading
            can run any length, and the document already shows it above. */}
        <Text as="h2" className="chat-plan-title" id={askId} tooltip="truncate">
          {question}
        </Text>
      </header>

      {pending ? (
        <>
          <div className="chat-plan-opts" role="radiogroup" aria-labelledby={askId}>
            {options.map((option) => (
              <label key={option.mode} className="chat-plan-opt" data-mode={option.mode}>
                <input
                  type="radio"
                  className="chat-plan-opt__input"
                  name={`${uid}-mode`}
                  value={option.mode}
                  checked={chosen?.mode === option.mode}
                  onChange={() => setMode(option.mode)}
                />
                <span className="chat-plan-opt__icon">
                  <ModeGlyph mode={option.mode} className="chat-plan-glyph" />
                </span>
                <span className="chat-plan-opt__label">{option.label}</span>
                <span className="chat-plan-opt__check">{chosen?.mode === option.mode ? <TickCheck /> : null}</span>
                <span className="chat-plan-opt__desc">{option.description}</span>
              </label>
            ))}
          </div>

          <div className="chat-plan-note">
            <textarea
              className="chat-plan-note__input"
              rows={1}
              value={draft}
              placeholder={NOTE_PLACEHOLDER}
              aria-label={NOTE_PLACEHOLDER}
              onChange={(event) => setDraft(event.target.value)}
            />
          </div>

          {failure ? (
            <p className="chat-plan-failure" role="alert">
              {failure}
            </p>
          ) : null}

          <div className="chat-plan-actions">
            <button type="button" className="chat-plan-reject" onClick={reject}>
              {REJECT_LABEL}
              <kbd className="chat-plan-key">Esc</kbd>
            </button>
            <span className="chat-plan-actions__gap" aria-hidden="true" />
            <button type="button" ref={approveRef} className="chat-plan-approve" data-mode={chosen?.mode} onClick={approve}>
              {APPROVE_LABEL}
              <kbd className="chat-plan-key">Enter</kbd>
            </button>
          </div>
        </>
      ) : (
        <Resolved resolution={resolution} planMode={planMode} />
      )}
    </section>
  );
}
