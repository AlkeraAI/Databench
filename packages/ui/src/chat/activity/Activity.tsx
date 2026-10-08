// The activity group: one run of tool calls as one transcript unit. The digest
// names the outcome and toggles the run; the steps sit on a single hairline
// ledger; each opened step carves a well whose interior its tool contributed
// through the step contract.
//
// Two moves carry the design. FIRST, the collapsed row reads as a sentence:
// the verb line, then the result figure and a worded disclosure, never a
// caret. SECOND, openness belongs to the payload: a bounded answer that IS the
// answer opens itself, an unbounded stream waits behind its word. Every rule
// that follows from those is a DEFAULT, and what the reader last did to a well
// or a run outranks it for as long as the client runs.

import { useEffect, useState, type ReactElement, type ReactNode } from "react";

import { IconX } from "@tabler/icons-react";

import { Text } from "../sharedUi";

import { recallDisclosure, rememberDisclosure } from "../hooks";
import { highlightBash } from "../syntax";
import { FailLine, StopLine, splitLeaf } from "../tools/shared";
import type { CardStep, StepAction, StepFigure } from "../tools";
import "./activity.css";

/** The step's badge: the tool's own glyph at the transcript body edge. A
 *  settled success carries no mark; a failure tints danger and attaches a
 *  small X. A stopped call is neither: it keeps the quiet ink and no mark, and
 *  its status line says what happened. */
function Badge({ step }: { step: CardStep }): ReactElement {
  return (
    <span className="chat-activity-badge" data-status={step.status} aria-hidden="true">
      {step.glyph}
      {step.status === "error" ? (
        <span className="chat-activity-badge__x">
          <IconX size={10} stroke={2.6} />
        </span>
      ) : null}
    </span>
  );
}

/** What a stopped call's status line says. The group cannot tell whose Stop it
 *  was, so the sentence states only the fact: the call ended without a result. */
const STOPPED_LINE = "This tool was stopped before it finished.";

/** The argument reads by what it names: a command is bash-highlighted, a path
 *  keeps only the file's own name (the full path lives in the well's band), a
 *  graph subject reads data-blue (from its kind class), and a pattern renders
 *  verbatim. */
function Argument({ step }: { step: CardStep }): ReactNode {
  if (step.objectKind === "command") return highlightBash(step.object);
  if (step.objectKind === "path") return splitLeaf(step.object).leaf;
  return step.object;
}

/** A result figure: a diff keeps mono so its signs line up on character width,
 *  a toned outcome carries its semantic hue, a plain count reads muted with
 *  tabular numerals. */
function Figure({ data }: { data: StepFigure }): ReactElement {
  if (data.kind === "diff") {
    return (
      <span className="chat-activity-fig chat-activity-mono">
        <span className="chat-activity-add">+{data.added}</span>
        {data.removed > 0 ? <> <span className="chat-activity-del">−{data.removed}</span></> : null}
      </span>
    );
  }
  return (
    <Text className="chat-activity-fig" data-tone={data.tone ?? "plain"} tooltip="truncate">
      {data.text}
    </Text>
  );
}

/** Mount state that survives the exit: `open` flips off, the node stays for the
 *  conceal animation, then leaves. The timer is the reduced-motion fallback --
 *  with the animation disabled, animationend never fires. */
function useDeparture(open: boolean): { mounted: boolean; closing: boolean; end: () => void } {
  const [phase, setPhase] = useState(open ? "open" : "shut");
  useEffect(() => {
    setPhase((prev) => (open ? "open" : prev === "open" ? "closing" : prev));
  }, [open]);
  useEffect(() => {
    if (phase !== "closing") return;
    const timer = window.setTimeout(() => setPhase("shut"), 220);
    return () => clearTimeout(timer);
  }, [phase]);
  return { mounted: phase !== "shut", closing: phase === "closing", end: () => setPhase("shut") };
}

/** Whether this animationend is the node's OWN conceal run: the keyframe is
 *  global so the name is stable, and a child well's conceal bubbles, so the
 *  target check keeps a folding well from ending its log's departure. */
function concealEnded(event: { animationName: string; target: unknown; currentTarget: unknown }): boolean {
  return event.animationName === "chat-activity-conceal" && event.target === event.currentTarget;
}

/** The well frame: ground, edge, radius, the status tint, and the extent line.
 *  The interior is the tool's own. The slot around it is what animates size,
 *  so the layout under the well slides with the toggle instead of jumping. */
function Well({ step, closing, onGone }: { step: CardStep; closing?: boolean; onGone?: () => void }): ReactElement | null {
  if (!step.body) return null;
  return (
    <div
      className="chat-activity-wellslot"
      data-closing={closing ? "" : undefined}
      onAnimationEnd={(event) => concealEnded(event) && onGone?.()}
    >
      <div className="chat-activity-well" data-status={step.status}>
        {step.body}
        {step.footer ? <p className="chat-activity-well__foot">{step.footer}</p> : null}
      </div>
    </div>
  );
}

function Step({ step, open, onToggle }: { step: CardStep; open: boolean; onToggle: () => void }): ReactElement {
  // A call still in flight has not answered: it carries no result figure, and
  // nothing to disclose. Its body, if a card built one from the arguments so
  // far, waits behind the loading sweep rather than under a "Show result" the
  // reader can open onto a result that does not exist yet.
  const inFlight = step.status === "running" || step.status === "pending";
  const expandable = Boolean(step.body) && !inFlight;
  const departure = useDeparture(open);
  const line2 = (
    <span className="chat-activity-result">
      {step.data ? <Figure data={step.data} /> : null}
      {expandable ? (
        <span className="chat-activity-disclose">
          {open ? "Hide" : "Show"} {step.disclosure ?? "details"}
        </span>
      ) : null}
    </span>
  );
  const head = (
    <>
      <Badge step={step} />
      <span className="chat-activity-say">
        <span className="chat-activity-line1">
          <span className="chat-activity-verb">{step.verb}</span>
          <Text className={`chat-activity-arg chat-activity-arg--${step.objectKind}`} tooltip="truncate" tooltipLabel={step.object}>
            <Argument step={step} />
          </Text>
        </span>
        {!inFlight && (step.data || expandable) ? line2 : null}
      </span>
    </>
  );
  // A call in flight holds its body open on a loading sweep; the settle takes
  // the well down with it, so a finished call reads as one collapsed row. A
  // pending call is in flight too -- its arguments are still streaming -- so it
  // takes the same sweep rather than rendering as a call that has answered.
  if (inFlight) {
    return (
      <div className="chat-activity-step" data-open="">
        <div className="chat-activity-head">{head}</div>
        {step.waiting ? null : (
          <div className="chat-activity-wellslot">
            <div className="chat-activity-well" data-status="running" aria-hidden="true">
              <div className="chat-activity-well__work" />
            </div>
          </div>
        )}
      </div>
    );
  }
  // A reason has nowhere to hide: it reads under the head, unfolded, whether or
  // not the step has a well. Behind the disclosure, a refused command read as
  // "1 failed" and nothing else, and the model was left to guess why.
  // A stopped call says so once, in the group's words: the stop is the whole
  // of its outcome, so no harness text or empty-result notice rides beside it.
  const failure =
    step.status === "stopped" ? (
      <StopLine>{STOPPED_LINE}</StopLine>
    ) : step.failure ? (
      <FailLine>{step.failure}</FailLine>
    ) : null;
  const action = step.action ? <ActionButton action={step.action} /> : null;
  if (!expandable) {
    return (
      <div className="chat-activity-step">
        <HeadRow action={action}>
          <div className="chat-activity-head">{head}</div>
        </HeadRow>
        {failure}
      </div>
    );
  }
  return (
    <div className="chat-activity-step" data-open={open ? "" : undefined}>
      <HeadRow action={action}>
        <button type="button" className="chat-activity-head" aria-expanded={open} onClick={() => !selectedText() && onToggle()}>
          {head}
        </button>
      </HeadRow>
      {failure}
      {departure.mounted ? <Well step={step} closing={departure.closing} onGone={departure.end} /> : null}
    </div>
  );
}

/** A step's own control, beside the head rather than inside it: the head is
 *  the disclosure's button, and a button inside a button is not a button. */
function ActionButton({ action }: { action: StepAction }): ReactElement {
  return (
    <button type="button" className="chat-activity-action" aria-label={action.ariaLabel} onClick={action.onAction}>
      {action.label}
    </button>
  );
}

function HeadRow({ action, children }: { action: ReactElement | null; children: ReactElement }): ReactElement {
  if (!action) return children;
  return (
    <div className="chat-activity-headrow">
      {children}
      {action}
    </div>
  );
}

/** A drag that selected text is a read, not a click. The head's words are the
 *  step's own, so selecting them must not also fold the step away. */
function selectedText(): boolean {
  const selection = typeof window === "undefined" ? null : window.getSelection();
  return selection !== null && !selection.isCollapsed && selection.toString().trim() !== "";
}

/** One step's disclosure key. Call ids are unique across the client, so a well
 *  the reader opened is found again by identity, never by where it sat. */
function wellKey(id: string): string {
  return `well:${id}`;
}

/** Openness keyed on step identity, in this order: what the reader last did to
 *  this well, else the payload's own `expanded` flag. A step that arrives after
 *  mount seeds from that flag, so a live run's bounded answers still open
 *  themselves. A group mounted already-finished is history: it seeds nothing,
 *  so its bounded answers wait for the reader. */
function useOpenSet(steps: CardStep[], seedExpanded: boolean): { open: Set<string>; toggle: (id: string) => void } {
  const [known, setKnown] = useState<Set<string>>(() => new Set(steps.map((step) => step.id)));
  const [open, setOpen] = useState<Set<string>>(() => {
    const seeded = new Set<string>();
    for (const step of steps) {
      if (recallDisclosure(wellKey(step.id)) ?? (seedExpanded && Boolean(step.expanded))) seeded.add(step.id);
    }
    return seeded;
  });
  const fresh = steps.filter((step) => !known.has(step.id));
  if (fresh.length > 0) {
    setKnown(new Set(steps.map((step) => step.id)));
    const next = new Set(open);
    for (const step of fresh) if (recallDisclosure(wellKey(step.id)) ?? step.expanded) next.add(step.id);
    setOpen(next);
  }
  const toggle = (id: string): void => {
    const opening = !open.has(id);
    rememberDisclosure(wellKey(id), opening);
    setOpen((prev) => {
      const next = new Set(prev);
      if (opening) next.add(id);
      else next.delete(id);
      return next;
    });
  };
  return { open, toggle };
}

/** The digest's head. A run of one speaks its step's own words; a run of
 *  several speaks the caller's summary unless a call in it was refused, since
 *  a refused call never ran: the head then counts only the calls that did
 *  ("Ran 1 tool" beside "1 refused"), or says the whole run was refused. */
function digestHead(summary: string, steps: CardStep[], refusals: number): string {
  if (steps.length === 1) return steps[0].loneSummary;
  if (refusals === 0) return summary;
  const ran = steps.length - refusals;
  if (ran === 0) return `Refused ${refusals} tool calls`;
  return `Ran ${ran} ${ran === 1 ? "tool" : "tools"}`;
}

export interface ActivityProps {
  /** The run's header once it holds more than one call ("3 tool calls"); a
   *  group of one speaks its step's own `loneSummary`. */
  summary: string;
  /** The run's calls, each resolved through the tool registry. */
  steps: CardStep[];
  /** True once the run's calls are done. A live group folds on the transition;
   *  a group mounted already-done loads collapsed. */
  folded?: boolean;
}

/** The host's ONE activity renderer, driven by props, so every surface reads
 *  the same run. The digest names the outcome and toggles the whole run; it
 *  carries no caret, takes the thinking row's size, and suffixes a failure
 *  with the X then the count, no dot separators. */
export function Activity({ summary, steps, folded }: ActivityProps): ReactElement {
  // The run-level fold is a DEFAULT, never a veto over the payload: a step that
  // declares itself open IS the answer (a query's rows, a described relation),
  // and a settled turn must leave those facts on screen rather than behind a
  // click the reader has to know to make. A run of unbounded output (a shell
  // command, a search) declares nothing and folds away once its calls are done.
  // A failed step with a reason holds the run open too: folded away, the run
  // reads as "1 failed" with the reason behind a click the reader has to know
  // to make, and a refused command looks like a missing shell.
  const holdsOpen = steps.some((step) => step.expanded || Boolean(step.failure));
  // Read off the steps on every render rather than latched when the run
  // settled. A reason can land AFTER the turn is over — a Stop writes the
  // aborted terminal's row a tenth of a second behind its own note — and a
  // fold latched on the settle then held the run shut for the reader who
  // watched it while the same rows, read back from the log on a reload, opened
  // it. What the run says may not depend on the order its rows reached this tab.
  const openByDefault = !folded || holdsOpen;
  const runKey = `run:${steps[0]?.id ?? summary}`;
  // What the READER last did to this run outranks the default, for as long as
  // the client runs — and only the reader's own hands are recorded, so an
  // automatic fold can never be mistaken for one later.
  const [chosen, setChosen] = useState<boolean | undefined>(() => recallDisclosure(runKey));
  const shown = chosen ?? openByDefault;
  const setShown = (open: boolean): void => {
    rememberDisclosure(runKey, open);
    setChosen(open);
  };
  const { open, toggle } = useOpenSet(steps, openByDefault);
  // A refused call never ran and did not fail: it is counted apart, and a run
  // of one says it in its head ("Refused 1 terminal command") with no count.
  const refusals = steps.filter((step) => step.refused).length;
  const header = digestHead(summary, steps, refusals);
  const failures = steps.filter((step) => step.status === "error" && !step.refused).length;
  // A call the reader's Stop cut off did not fail, so it is counted apart.
  const stops = steps.filter((step) => step.status === "stopped").length;
  const logDeparture = useDeparture(shown);
  return (
    <section className="chat-activity-group">
      <button type="button" className="chat-activity-digest" aria-expanded={shown} onClick={() => !selectedText() && setShown(!shown)}>
        <span className="chat-activity-digest__stat">{header}</span>
        {failures > 0 ? (
          /* The mark and its count travel together, so a squeezed digest folds them onto
             the next line as one phrase instead of stranding the mark. */
          <span className="chat-activity-digest__fail">
            <span className="chat-activity-digest__x" aria-hidden="true">
              <IconX size={13} stroke={2.4} />
            </span>
            <span className="chat-activity-digest__count">{failures} failed</span>
          </span>
        ) : null}
        {refusals > 0 && refusals < steps.length ? (
          <span className="chat-activity-digest__refused">{refusals} refused</span>
        ) : null}
        {stops > 0 ? <span className="chat-activity-digest__stopped">{stops} stopped</span> : null}
      </button>
      {logDeparture.mounted ? (
        <div
          className="chat-activity-logslot"
          data-closing={logDeparture.closing ? "" : undefined}
          onAnimationEnd={(event) => concealEnded(event) && logDeparture.end()}
        >
          <div className="chat-activity-log">
            {steps.map((step) => (
              <Step key={step.id} step={step} open={open.has(step.id)} onToggle={() => toggle(step.id)} />
            ))}
          </div>
        </div>
      ) : null}
    </section>
  );
}
