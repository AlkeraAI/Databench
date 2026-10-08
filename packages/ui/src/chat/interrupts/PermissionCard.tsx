// The permission interrupt: a bottom-anchored dialog. The ask sits on top
// (queue count, the question, the gated subject in a mono inset); one action
// bar reads left-to-right as Deny then "Always allow..." then the filled allow
// anchored bottom-right. The decisive buttons wear their shortcut as a keycap,
// and Enter/Esc actually fire them -- from the card, and from anywhere else on
// the page while the card is the thing waiting on the reader (see cardKeys).
// The always disclosure is a real menu whose scope rows commit directly, and
// the mode switch beside Deny moves the chat's stance without answering: the
// machine decides the ask again under the new stance, and the host swaps the
// card out if that settles it.
// Pending-only: once the host has a decision it swaps the card out.

import {
  useEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
  type RefObject,
} from "react";

import type { PermissionConversationPart, PresentedNotebook } from "@alkera/chat-model";
import { Text, UrlLink, useDismiss } from "../sharedUi";
import { IconCheck } from "@tabler/icons-react";

import { DialogCaret } from "../dialog";
import { isPlainEnter, useRovingFocus } from "../hooks";
import { useCardKeys } from "./cardKeys";
import { NotebookAsk, NotebookTitle } from "./NotebookAsk";
import { codeLines, highlightBash, paintLeaves, sqlLines } from "../syntax";
import { PatchDiff } from "../tools/edit";
import { LeafPath } from "../tools/shared";

// The family sheet loads first: an organ rule overrides a shared one by order.
import "../dialog/dialog.css";
import "./permission.css";
import { Prose } from "../prose";

/** A wire decision option: the id travels back, the label is what renders. */
export interface PermissionAction {
  optionId: string;
  label: string;
}

export interface PermissionScopeOption extends PermissionAction {
  /** Picks the scope glyph: a shell prompt for the exact command, a box for the
   *  whole project. */
  scope: "exact" | "project";
}

/** A knowledge item an ask would send: shown in the subject's place, the title
 *  as a heading and the body as prose, because the reader is approving the
 *  note the team receives, not a command. */
export interface PermissionNote {
  title?: string;
  body: string;
  /** The machine cut the body before sending it; the team receives all of it. */
  truncated?: boolean;
}

export interface PermissionCardProps {
  title: string;
  /** What the ask gates, as the wire sends it: a command, a path or glob, a
   *  URL. `kind` names which, and the inset renders it in its own register.
   *  Empty when the ask named nothing, and then `missingSubject` is shown
   *  instead — an empty inset reads as an approved blank. */
  pattern: string;
  /** What to say when `pattern` is empty: the ask's remaining concrete fact,
   *  which is the tool that raised it. The inset would otherwise be a bare
   *  shell prompt, which asks a reader to approve something they cannot see.
   *  Omit where the ask carries no such fact — the title says the rest. */
  missingSubject?: string;
  /** The subject is still on its way — the ask reached the transcript before
   *  the call naming it. Shown in the inset's place, with every action
   *  disabled: a reader never approves what they were not shown. */
  waiting?: string;
  kind: PermissionConversationPart["canonicalKind"];
  /** The subject's syntax when it is code the inset can paint: a warehouse
   *  statement, or Python the agent wrote against an SDK. */
  language?: "sql" | "python";
  /** How the subject reads. `code` is the mono inset; `sentence` sets it in
   *  the reading face — a knowledge withdrawal or a control-plane action
   *  arrives as a sentence, and a mono box would ask the reader to approve it
   *  as a command. Defaults to `code`. */
  register?: "code" | "sentence";
  /** The note a knowledge share would send, rendered instead of `pattern`. */
  note?: PermissionNote;
  /** A proposed file change, rendered as a diff under the subject. */
  preview?: { title?: string; content: string };
  /** The classified targets and estimated cost, each one short phrase. */
  facts?: string[];
  /** The gated call itself, for an ask that names nothing else: the tool's
   *  name and its whole input, so the reader approves what they can see. */
  details?: { tool: string; input: string };
  /** A notebook tool's ask: the title names the notebook, and the cells it
   *  would run take the subject's place. */
  notebook?: PresentedNotebook;
  /** Open a file by its workspace path; omit where the host cannot, and the
   *  notebook's name is plain text. */
  onOpenFile?: (path: string) => void;
  /** This ask's place in the pending queue; omit to hide the pill. */
  queue?: { position: number; of: number };
  /** The approving action. Omit where approving could not reach the agent —
   *  a read-only workspace refuses a write-class ask before anyone is asked —
   *  and pass `refusal` to say so; declining stays offered either way, because
   *  that is what releases the turn. */
  allow?: PermissionAction;
  /** Why no approval is offered. Rendered where the allow key would be. */
  refusal?: string;
  deny: PermissionAction;
  /** The always-allow disclosure; omit (or pass no options) to hide it. */
  always?: { label: string; options: PermissionScopeOption[] };
  /** What a standing grant would actually cover, under the subject. The rule
   *  recorded is wider than the command shown, and a reader approving one
   *  command has no other way to learn that. */
  alwaysScope?: string;
  /** An answer is on its way: every action is refused so one press cannot
   *  become two answers, and the card says what it is doing. */
  busy?: boolean;
  /** Nothing can be answered right now and why — the machine holding the ask
   *  is gone. Every action is disabled and the reason is shown, rather than
   *  offering keys that resolve to nothing. */
  unavailable?: string;
  /** The last answer did not reach the machine, in one sentence. Shown with
   *  the retry, which sends the same answer again. */
  failure?: string;
  onRetry?: () => void;
  /** The one decision seam: every button resolves by its option id. */
  onDecide: (optionId: string) => void;
  /** Route a network subject's open through the host (a webview CSP blocks a
   *  plain anchor); omit to render a normal external link. */
  onOpenUrl?: (url: string) => void;
  /** Switch the chat's permission mode while the ask is up; omit where the
   *  reader may not move it. */
  mode?: PermissionModeSwitch;
}

/** The chat's permission mode, switchable from the card. */
export interface PermissionModeSwitch {
  /** What the control is, for its accessible name ("Permission mode"). */
  label: string;
  value: string;
  options: { value: string; label: string }[];
  onChange: (value: string) => void;
}

/** The shortcuts the card actually binds, worn as keycaps. */
const KBD = { allow: "Enter", deny: "Esc" };

// The exact command keeps a shell prompt; project-wide approval wears a check.
function ScopeGlyph({ scope }: { scope: "exact" | "project" }): ReactNode {
  if (scope === "project") {
    return (
      <IconCheck
        className="chat-permission-scope__glyph"
        size={14}
        aria-hidden="true"
      />
    );
  }
  return (
    <svg
      className="chat-permission-scope__glyph"
      viewBox="0 0 14 14"
      width="14"
      height="14"
      aria-hidden="true"
    >
      <path d="M3 4.2 5.6 7 3 9.8" />
      <path d="M7.2 9.8h3.8" />
    </svg>
  );
}

// The inset speaks the subject's own register: a shell command in bash colors,
// a path in the trail/leaf treatment, a URL as a link, anything else plain.
function subjectInset(
  props: Pick<
    PermissionCardProps,
    "kind" | "pattern" | "language" | "onOpenUrl"
  >,
): ReactNode {
  const { kind, pattern, language, onOpenUrl } = props;
  if (language === "sql") {
    return sqlLines(pattern).map((line, index) => (
      <span key={index} className="chat-permission-cmd__l">
        {paintLeaves(line)}
      </span>
    ));
  }
  if (language === "python") {
    return codeLines(pattern, "python").map((line, index) => (
      <span key={index} className="chat-permission-cmd__l">
        {paintLeaves(line)}
      </span>
    ));
  }
  switch (kind) {
    case "shell":
      return highlightBash(pattern);
    case "edit":
      return <LeafPath path={pattern} />;
    case "network":
      return (
        <UrlLink
          className="chat-permission-cmd__link"
          url={pattern}
          onOpen={onOpenUrl}
        >
          {pattern}
        </UrlLink>
      );
    default:
      return pattern;
  }
}

/** The classified targets and estimated cost, absent when there are none. */
function SubjectFacts({ facts }: { facts: string[] | undefined }): ReactNode {
  if (!facts || facts.length === 0) return null;
  return (
    <p className="chat-permission-facts">
      {facts.map((fact) => (
        <Text
          key={fact}
          className="chat-permission-facts__f"
          tooltip="truncate"
        >
          {fact}
        </Text>
      ))}
    </p>
  );
}

/** The gated call's tool and input, for an ask that named nothing else. */
function CallDetails({
  details,
}: {
  details: PermissionCardProps["details"];
}): ReactNode {
  if (!details) return null;
  return (
    <div
      className="chat-permission-details"
      role="group"
      aria-label={`Input to ${details.tool}`}
    >
      <p className="chat-permission-details__tool">{details.tool}</p>
      <pre className="chat-permission-details__input">
        <code>{details.input}</code>
      </pre>
    </div>
  );
}

/** A sentence subject raises its first word, as every line composed from a
 *  fragment does, and nothing else: a name the machine spelled keeps its case. */
function sentence(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** Past either of these a note is folded until the reader asks for the rest.
 *  Decided on the text, not the layout, so the fold is the same on every host
 *  and a note the card folds always has the control that opens it. */
const NOTE_FOLD_CHARS = 700;
const NOTE_FOLD_LINES = 8;

export function noteFolds(body: string): boolean {
  return body.length > NOTE_FOLD_CHARS || body.split("\n").length > NOTE_FOLD_LINES;
}

/** The knowledge item in the subject's place: heading, prose, and the fold. */
function NoteBlock({ note }: { note: PermissionNote }): ReactNode {
  const [open, setOpen] = useState(false);
  const folds = noteFolds(note.body);
  return (
    <div
      className="chat-permission-item"
      data-folded={folds && !open ? "" : undefined}
    >
      {note.title ? (
        <h3 className="chat-permission-item__title">{note.title}</h3>
      ) : null}
      {/* The note's markup renders (a bold word stays bold) and its line breaks
          stay where the person put them: the paragraph keeps each newline it
          was written with (`white-space: pre-line` on the body). */}
      <div className="chat-permission-item__body">
        <Prose content={note.body} />
      </div>
      {note.truncated ? (
        <p className="chat-permission-item__cut">
          The note continues past what is shown here.
        </p>
      ) : null}
      {folds ? (
        <button
          type="button"
          className="chat-permission-item__more"
          aria-expanded={open}
          onClick={() => setOpen((v) => !v)}
        >
          {open ? "Show less" : "Show more"}
        </button>
      ) : null}
    </div>
  );
}

/** The change an edit ask would make, rendered before approval. */
function Proposed({
  preview,
}: {
  preview: PermissionCardProps["preview"];
}): ReactNode {
  if (!preview) return null;
  const label = preview.title
    ? `Proposed change to ${preview.title}`
    : "Proposed change";
  return (
    <div
      className="chat-permission-preview"
      data-tool="edit"
      role="group"
      aria-label={label}
    >
      <PatchDiff patch={preview.content} />
    </div>
  );
}

export function PermissionCard({
  title,
  pattern,
  missingSubject,
  waiting,
  kind,
  language,
  register,
  note,
  preview,
  facts,
  details,
  notebook,
  onOpenFile,
  queue,
  allow,
  refusal,
  deny,
  always,
  alwaysScope,
  busy,
  unavailable,
  failure,
  onRetry,
  onDecide,
  onOpenUrl,
  mode,
}: PermissionCardProps): ReactNode {
  const [scopeOpen, setMenuOpen] = useState(false);
  const [modeOpen, setModeOpen] = useState(false);
  // Either of the card's menus holds the keys while it is open.
  const menuOpen = scopeOpen || modeOpen;
  // This ask has had its answer. The host normally swaps the card out, but
  // until it does the card must not take a second one -- a reader leaning on
  // Enter would otherwise answer the ask behind this one too.
  const [answered, setAnswered] = useState(false);
  const shut = Boolean(busy) || unavailable !== undefined || waiting !== undefined;

  const cardRef = useRef<HTMLElement>(null);
  const primaryRef = useRef<HTMLButtonElement>(null);
  const discRef = useRef<HTMLButtonElement>(null);
  const discBoxRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const modeRef = useRef<HTMLButtonElement>(null);

  const scopes = always?.options ?? [];

  const decide = (optionId: string): void => {
    if (shut) return;
    setMenuOpen(false);
    setAnswered(true);
    onDecide(optionId);
  };

  // The answer did not land, so the ask is still the reader's to settle and the
  // keys are theirs again -- Retry is offered, and so is pressing Enter.
  useEffect(() => {
    if (failure) setAnswered(false);
  }, [failure]);

  // Enter and Escape answer this ask wherever the reader's focus is, for as
  // long as it is the front ask and can still be answered.
  useCardKeys(
    shut || answered
      ? null
      : {
          allow: allow ? () => decide(allow.optionId) : undefined,
          deny: () => decide(deny.optionId),
          card: () => cardRef.current,
          menuOpen,
        },
  );

  // Escape stays card-handled (not in the dismiss hook) so it can fall through
  // to Deny when no menu is open.
  useDismiss(discBoxRef, scopeOpen, () => setMenuOpen(false), { escape: false });

  useEffect(() => {
    if (!scopeOpen || !listRef.current) return;
    listRef.current.querySelector<HTMLElement>('[role="menuitem"]')?.focus();
  }, [scopeOpen]);

  // The card is the keyboard home: Enter commits the primary, Esc denies. A key
  // aimed at Deny/disclosure keeps its own native activation.
  const onCardKeyDown = (event: ReactKeyboardEvent<HTMLElement>): void => {
    if (event.key === "Escape") {
      if (modeOpen) {
        setModeOpen(false);
        modeRef.current?.focus();
        return;
      }
      if (scopeOpen) {
        setMenuOpen(false);
        discRef.current?.focus();
        return;
      }
      event.preventDefault();
      decide(deny.optionId);
      return;
    }
    if (isPlainEnter(event)) {
      if (menuOpen || shut) return;
      if (!allow) return;
      const target = event.target as HTMLElement;
      if (target === event.currentTarget || target === primaryRef.current) {
        event.preventDefault();
        decide(allow.optionId);
      }
    }
  };

  const onListKeyDown = useRovingFocus(listRef, "menuitem", () => {
    setMenuOpen(false);
    discRef.current?.focus();
  });

  return (
    <section
      className="chat-permission-card"
      data-preview={preview ? "1" : undefined}
      aria-label={title}
      tabIndex={-1}
      ref={cardRef}
      onKeyDown={onCardKeyDown}
    >
      <header className="chat-permission-head">
        <h2 className="chat-permission-title">
          {notebook ? <NotebookTitle notebook={notebook} onOpenFile={onOpenFile} /> : title}
        </h2>
        {queue ? (
          <span
            className="chat-permission-queue"
            aria-label={`Request ${queue.position} of ${queue.of}`}
          >
            <span className="chat-permission-queue__n">{queue.position}</span>
            <span className="chat-permission-queue__sep">/</span>
            <span className="chat-permission-queue__n">{queue.of}</span>
          </span>
        ) : null}
      </header>

      {notebook ? (
        <NotebookAsk notebook={notebook} />
      ) : note ? (
        <NoteBlock note={note} />
      ) : pattern && register === "sentence" ? (
        <p className="chat-permission-statement">{sentence(pattern)}</p>
      ) : pattern ? (
        <div className="chat-permission-cmd" data-kind={kind}>
          {kind === "shell" ? (
            <span className="chat-permission-cmd__prompt" aria-hidden="true">
              $
            </span>
          ) : null}
          <code className="chat-permission-cmd__text">
            {subjectInset({ kind, pattern, language, onOpenUrl })}
          </code>
        </div>
      ) : waiting ? (
        <p className="chat-permission-waiting" role="status">
          {waiting}
        </p>
      ) : missingSubject ? (
        <p className="chat-permission-unnamed">{missingSubject}</p>
      ) : null}
      {/* Under the command, because it is a fact about the command: the rule a
          standing grant records is wider than the one line above it. */}
      {always && alwaysScope ? (
        <p className="chat-permission-note">{alwaysScope}</p>
      ) : null}
      <Proposed preview={preview} />
      <CallDetails details={details} />
      <SubjectFacts facts={facts} />

      {failure ? (
        <p className="chat-permission-failure" role="alert">
          {failure}
          {onRetry ? (
            <button type="button" className="chat-permission-failure__retry" onClick={onRetry}>
              Retry
            </button>
          ) : null}
        </p>
      ) : null}

      <div className="chat-permission-bar" data-busy={busy ? "" : undefined}>
        {mode ? (
          <ModeSwitch
            mode={mode}
            open={modeOpen}
            setOpen={setModeOpen}
            triggerRef={modeRef}
            disabled={shut}
          />
        ) : null}
        <button
          type="button"
          className="chat-permission-deny"
          disabled={shut}
          onClick={() => decide(deny.optionId)}
        >
          {deny.label}
          <kbd className="chat-permission-key">{KBD.deny}</kbd>
        </button>

        <span className="chat-permission-bar__gap" aria-hidden="true" />

        {/* Where the allow key would be, when approving could not reach the
            agent: what refused it, rather than a key that resolves to nothing. */}
        {allow ? null : (
          <p className="chat-permission-refusal" role="status">
            {refusal}
          </p>
        )}

        {/* The machine that raised the ask is gone: the keys stay on screen so
            the card still reads as the decision it is, and say why they do
            nothing rather than answering a press with silence. */}
        {unavailable ? (
          <p className="chat-permission-refusal" role="status">
            {unavailable}
          </p>
        ) : null}

        {/* Split button: the primary allow plus a caret segment opening the
            always-allow menu, one shared outline on the group, an internal
            divider between them. */}
        {allow ? (
        <div className="chat-permission-allow" ref={discBoxRef}>
          <button
            type="button"
            ref={primaryRef}
            className="chat-permission-allow__main"
            disabled={shut}
            onClick={() => decide(allow.optionId)}
          >
            {allow.label}
            <kbd className="chat-permission-key">{KBD.allow}</kbd>
          </button>
          {always && scopes.length > 0 ? (
            <button
              type="button"
              ref={discRef}
              className="chat-permission-allow__caret"
              aria-haspopup="menu"
              aria-expanded={scopeOpen}
              disabled={shut}
              onClick={() => setMenuOpen((v) => !v)}
            >
              <span className="chat-permission-vh">{always.label}</span>
              <DialogCaret className="chat-permission-caret" open={scopeOpen} />
            </button>
          ) : null}
          {scopeOpen && always ? (
            <div className="chat-permission-menu" role="presentation">
              <ul
                className="chat-permission-menu__list"
                role="menu"
                aria-label={always.label}
                ref={listRef}
                onKeyDown={onListKeyDown}
              >
                {scopes.map((option) => (
                  <li key={option.optionId}>
                    <button
                      type="button"
                      role="menuitem"
                      className="chat-permission-scope"
                      onClick={() => decide(option.optionId)}
                    >
                      <span className="chat-permission-scope__icon">
                        <ScopeGlyph scope={option.scope} />
                      </span>
                      <Text
                        className="chat-permission-scope__label"
                        tooltip="truncate"
                      >
                        {option.label}
                      </Text>
                    </button>
                  </li>
                ))}
              </ul>
              {/* The menu opens over the note under the command, so the rule
                  it records is repeated where the choice is made. */}
              {alwaysScope ? <p className="chat-permission-menu__note">{alwaysScope}</p> : null}
            </div>
          ) : null}
        </div>
        ) : null}
      </div>
    </section>
  );
}

/** The chat's mode, moved from the card. Choosing one answers nothing: the
 *  machine decides the ask again under the new mode, which may settle it. */
function ModeSwitch({
  mode,
  open,
  setOpen,
  triggerRef,
  disabled,
}: {
  mode: PermissionModeSwitch;
  open: boolean;
  setOpen: (open: boolean) => void;
  triggerRef: RefObject<HTMLButtonElement | null>;
  disabled: boolean;
}): ReactNode {
  const boxRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const current = mode.options.find((option) => option.value === mode.value);
  useDismiss(boxRef, open, () => setOpen(false), { escape: false });
  useEffect(() => {
    if (!open || !listRef.current) return;
    (
      listRef.current.querySelector<HTMLElement>('[aria-checked="true"]') ??
      listRef.current.querySelector<HTMLElement>('[role="menuitemradio"]')
    )?.focus();
  }, [open]);
  const onListKeyDown = useRovingFocus(listRef, "menuitemradio", () => {
    setOpen(false);
    triggerRef.current?.focus();
  });
  const choose = (value: string): void => {
    setOpen(false);
    triggerRef.current?.focus();
    if (value !== mode.value) mode.onChange(value);
  };
  return (
    <div className="chat-permission-mode" ref={boxRef}>
      <button
        type="button"
        ref={triggerRef}
        className="chat-permission-mode__trigger"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={`${mode.label}: ${current?.label ?? mode.value}`}
        disabled={disabled}
        onClick={() => setOpen(!open)}
      >
        {current?.label ?? mode.value}
        <DialogCaret className="chat-permission-caret" open={open} />
      </button>
      {open ? (
        <div className="chat-permission-menu chat-permission-mode__menu" role="presentation">
          <ul
            className="chat-permission-menu__list"
            role="menu"
            aria-label={mode.label}
            ref={listRef}
            onKeyDown={onListKeyDown}
          >
            {mode.options.map((option) => (
              <li key={option.value}>
                <button
                  type="button"
                  role="menuitemradio"
                  aria-checked={option.value === mode.value}
                  className="chat-permission-scope"
                  onClick={() => choose(option.value)}
                >
                  <span className="chat-permission-scope__icon">
                    {option.value === mode.value ? <IconCheck size={14} aria-hidden="true" /> : null}
                  </span>
                  <Text className="chat-permission-scope__label" tooltip="truncate">
                    {option.label}
                  </Text>
                </button>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}
