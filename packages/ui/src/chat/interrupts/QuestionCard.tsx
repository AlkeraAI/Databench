// The question interrupt: one question at a time over the wire's
// QuestionPromptView set, with the pager and a dual-mode primary in the action
// bar. No dismiss affordance: an accidental click would throw the answers away.
// Answers travel back through onSubmit as one label array per question (the
// Other row resolves to its trimmed text), and every state change publishes
// through onLive so the transcript's mirror card follows the answering live.

import {
  useEffect,
  useId,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
  type UIEvent,
} from "react";

import type { QuestionPromptView } from "@alkera/chat-model";

import { isPlainEnter } from "../hooks";
import { Text } from "../sharedUi";

// The family sheet loads first: an organ rule overrides a shared one by order.
import "../dialog/dialog.css";
import "./question.css";

const OTHER_LABEL = "Other…";
const OTHER_PLACEHOLDER = "Type your own answer…";
const CONTINUE_LABEL = "Continue";
const SUBMIT_LABEL = "Submit";

// --- Glyphs -----------------------------------------------------------------

function Chevron({ back }: { back?: boolean }): ReactNode {
  return (
    <svg className="chat-question-chev" viewBox="0 0 14 14" width="14" height="14" aria-hidden="true">
      <path d={back ? "M8.6 3.6 5.2 7l3.4 3.4" : "M5.4 3.6 8.8 7l-3.4 3.4"} />
    </svg>
  );
}

// --- Model ------------------------------------------------------------------

/** One prompt's working answer. `picked` holds option indices (at most one when
 *  the prompt is single-select); the Other row contributes only its trimmed text. */
interface Answer {
  picked: number[];
  otherOn: boolean;
  otherText: string;
}

const BLANK: Answer = { picked: [], otherOn: false, otherText: "" };

/** The labels that travel back for one prompt. Free text counts only when it is
 *  non-blank after trimming, so an Other row left empty answers nothing. */
function labelsOf(prompt: QuestionPromptView, answer: Answer): string[] {
  const chosen = answer.picked.map((i) => prompt.options[i].label);
  const typed = answer.otherText.trim();
  return answer.otherOn && typed ? [...chosen, typed] : chosen;
}

/** What the transcript's question card mirrors, published on every change. */
export interface QuestionLiveView {
  questions: { header?: string | null; question: string; answered: boolean; labels: string[] }[];
  settled: boolean;
}

/** The transcript's record of a question: the moment the run turned to the
 *  reader. It is an exchange, not a ledger: each ask carries its own state mark
 *  and the reply the reader gave it, under a turn arm, so it reads as neither a
 *  tool group nor a document. Nothing published yet is the honest pending record: every ask
 *  listed, each still waiting. */
export function QuestionTranscriptCard({
  questions,
  live,
}: {
  questions: QuestionPromptView[];
  live?: QuestionLiveView | null;
}): ReactNode {
  const rows =
    live?.questions ??
    questions.map((q) => ({ header: q.header, question: q.question, answered: false, labels: [] as string[] }));
  const total = rows.length;
  const done = rows.filter((row) => row.answered).length;
  const answered = done === total;
  const phrase = answered ? "Answered" : "Waiting for an answer";
  return (
    <section className="chat-asked-card" data-state={answered ? "answered" : "open"} aria-label={`${phrase}. ${total} asked.`}>
      <p className="chat-asked-status">
        <span className="chat-asked-status__text">{phrase}</span>
        {total > 1 ? (
          <span className="chat-asked-status__count">
            {done} of {total}
          </span>
        ) : null}
      </p>
      <ul className="chat-asked-asks">
        {rows.map((row, i) => (
          <li className="chat-asked-ask" key={i} data-answered={row.answered ? "" : undefined}>
            {/* Every ask carries its own state, on the first row of its own
                text: a ring while it waits, a checked ring once answered. */}
            <span className="chat-asked-mark" role="img" aria-label={row.answered ? "Answered" : "Not answered yet"}>
              <svg className="chat-asked-glyph" viewBox="0 0 24 24" width="15" height="15">
                <circle cx="12" cy="12" r="9" />
                {row.answered ? <path d="M9 12l2 2l4 -4" /> : null}
              </svg>
            </span>
            <span className="chat-asked-say">
              <span className="chat-asked-q">{row.question}</span>
              <span className="chat-asked-reply">
                {/* The turn mark: the arm that carries the reader's answer back
                    under the ask it settles. */}
                <svg className="chat-asked-turn" viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
                  <path d="M4 3v4.5a1.5 1.5 0 0 0 1.5 1.5H12" />
                  <path d="M9.5 6.5 12 9l-2.5 2.5" />
                </svg>
                <span className="chat-asked-reply__text">
                  {row.labels.length > 0 ? row.labels.join(", ") : "No answer yet"}
                </span>
              </span>
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}

// --- Card -------------------------------------------------------------------

export interface QuestionCardProps {
  questions: QuestionPromptView[];
  /** One label array per question, in order. Fired once, when the whole set
   *  commits. */
  onSubmit: (labels: string[][]) => void;
  /** The transcript mirror's feed, published from state on every change. */
  onLive?: (view: QuestionLiveView) => void;
  /** The last answer did not reach the machine, in one sentence. The picks are
   *  kept and the card unlocks, so the same answer can be sent again. */
  failure?: string;
}

export function QuestionCard({ questions, onSubmit, onLive, failure }: QuestionCardProps): ReactNode {
  const uid = useId();
  const questionId = `${uid}-question`;
  const [answers, setAnswers] = useState<Answer[]>(() => questions.map(() => BLANK));
  const [index, setIndex] = useState(0);
  const [settled, setSettled] = useState(false);

  // A new `questions` array (compared by identity) is a new interrupt: the
  // working answers, the pager, and the settled lock all re-seed, so a longer
  // set can never index past the answers and a same-length set can never
  // inherit the previous set's picks.
  const [seededFrom, setSeededFrom] = useState(questions);
  if (seededFrom !== questions) {
    setSeededFrom(questions);
    setAnswers(questions.map(() => BLANK));
    setIndex(0);
    setSettled(false);
  }

  // The mirror publishes from state, mount included, so the transcript card
  // never disagrees with the dock card.
  useEffect(() => {
    onLive?.({
      questions: questions.map((q, i) => {
        const labels = labelsOf(q, answers[i]);
        return { header: q.header, question: q.question, answered: labels.length > 0, labels };
      }),
      settled,
    });
  }, [questions, answers, settled, onLive]);

  // The answer did not land, so the set is still the reader's to settle: the
  // options unlock and the primary comes back, over the picks they already made.
  // Unlocked during render, not in an effect, so the sentence and the live key
  // commit together; an effect left one commit that said "try again" beside a
  // locked key.
  const [failureSeen, setFailureSeen] = useState(failure);
  if (failureSeen !== failure) {
    setFailureSeen(failure);
    if (failure) setSettled(false);
  }

  const bodyRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const otherRef = useRef<HTMLInputElement>(null);
  // True while rows sit below the list's fold, which only happens in a squeezed
  // panel; the list wears its fade only then.
  const [more, setMore] = useState(false);

  // Row heights follow the panel width, so the fold is re-measured on resize
  // and on every question, not only on scroll.
  useEffect(() => {
    const list = listRef.current;
    if (!list) return;
    const sync = (): void => setMore(list.scrollHeight - list.clientHeight - list.scrollTop > 1);
    sync();
    const observer = new ResizeObserver(sync);
    observer.observe(list);
    return () => observer.disconnect();
  }, [index]);

  const prompt = questions[index];
  const answer = answers[index];
  const answered = questions.map((q, i) => labelsOf(q, answers[i]).length > 0);
  const currentAnswered = answered[index];
  const firstOpen = answered.indexOf(false);
  const allAnswered = firstOpen === -1;

  if (!prompt) return null;

  // Once the set has committed, the answers are the record the harness
  // received: every mutator routes through here, so the lock holds them all.
  const update = (next: Answer): void => {
    if (settled) return;
    setAnswers((prev) => prev.map((a, i) => (i === index ? next : a)));
  };

  // A move the user did not ask for lands on the new question's first option,
  // so the group label (the question) is announced and the keyboard stays in
  // the card.
  const landOnQuestion = (): void => {
    requestAnimationFrame(() => bodyRef.current?.querySelector("input")?.focus({ preventScroll: true }));
  };

  const focusOther = (): void => {
    requestAnimationFrame(() => otherRef.current?.focus({ preventScroll: true }));
  };

  const pickOne = (option: number): void => {
    const wasAnswered = currentAnswered;
    update({ ...answer, picked: [option], otherOn: false });
    if (wasAnswered) return;
    const ahead = answered.findIndex((done, i) => i > index && !done);
    if (ahead === -1) return;
    setIndex(ahead);
    landOnQuestion();
  };

  const toggleOne = (option: number): void => {
    const picked = answer.picked.includes(option)
      ? answer.picked.filter((i) => i !== option)
      : [...answer.picked, option].sort((a, b) => a - b);
    update({ ...answer, picked });
  };

  const chooseOther = (): void => {
    if (prompt.multiple) {
      const on = !answer.otherOn;
      update({ ...answer, otherOn: on });
      if (on) focusOther();
      return;
    }
    update({ ...answer, picked: [], otherOn: true });
    focusOther();
  };

  const typeOther = (text: string): void => {
    update(prompt.multiple ? { ...answer, otherOn: true, otherText: text } : { picked: [], otherOn: true, otherText: text });
  };

  const goTo = (next: number): void => setIndex(Math.min(questions.length - 1, Math.max(0, next)));

  const commit = (): void => {
    if (!currentAnswered || settled) return;
    if (allAnswered) {
      setSettled(true);
      onSubmit(questions.map((q, i) => labelsOf(q, answers[i])));
      return;
    }
    setIndex(firstOpen);
    landOnQuestion();
  };

  // The card is the keyboard home: Enter commits the primary while it is
  // enabled. A key aimed at a button keeps its own native activation.
  const onCardKeyDown = (event: ReactKeyboardEvent<HTMLElement>): void => {
    if (settled) return;
    if (!isPlainEnter(event)) return;
    if ((event.target as HTMLElement).closest("button")) return;
    if (!currentAnswered) return;
    event.preventDefault();
    commit();
  };

  return (
    <div className="chat-question-frame">
      <section
        className="chat-question-card"
        data-settled={settled ? "" : undefined}
        aria-label={prompt.question}
        tabIndex={-1}
        onKeyDown={onCardKeyDown}
      >
        <div className="chat-question-body" key={index} ref={bodyRef}>
          <div className="chat-question-ask">
            <p className="chat-question-meta">
              {prompt.header ? (
                <>
                  <Text className="chat-question-meta__head" tooltip="truncate">
                    {prompt.header}
                  </Text>
                  <span className="chat-question-meta__sep" aria-hidden="true">
                    ·
                  </span>
                </>
              ) : null}
              <span className="chat-question-meta__count">
                {index + 1} of {questions.length}
              </span>
            </p>
            <h2 className="chat-question-question" id={questionId}>
              {prompt.question}
            </h2>
          </div>

          <div
            className="chat-question-options"
            ref={listRef}
            data-more={more ? "" : undefined}
            onScroll={(event: UIEvent<HTMLDivElement>) => {
              const list = event.currentTarget;
              setMore(list.scrollHeight - list.clientHeight - list.scrollTop > 1);
            }}
            role={prompt.multiple ? "group" : "radiogroup"}
            aria-labelledby={questionId}
          >
            {prompt.options.map((option, i) => {
              const picked = answer.picked.includes(i);
              return (
                <label className="chat-question-opt" key={option.label} data-picked={picked ? "" : undefined}>
                  <input
                    className="chat-question-box"
                    type={prompt.multiple ? "checkbox" : "radio"}
                    name={`${uid}-q${index}`}
                    checked={picked}
                    disabled={settled}
                    onChange={() => (prompt.multiple ? toggleOne(i) : pickOne(i))}
                  />
                  <span className="chat-question-opt__text">
                    <span className="chat-question-opt__label">{option.label}</span>
                    {option.description ? <span className="chat-question-opt__desc">{option.description}</span> : null}
                  </span>
                </label>
              );
            })}

            {prompt.custom ? (
              <div className="chat-question-other" data-picked={answer.otherOn ? "" : undefined}>
                <label className="chat-question-other__row">
                  <input
                    className="chat-question-box"
                    type={prompt.multiple ? "checkbox" : "radio"}
                    name={`${uid}-q${index}`}
                    checked={answer.otherOn}
                    disabled={settled}
                    onChange={chooseOther}
                  />
                  <span className="chat-question-opt__label">{OTHER_LABEL}</span>
                </label>
                <input
                  ref={otherRef}
                  className="chat-question-other__field"
                  type="text"
                  value={answer.otherText}
                  placeholder={OTHER_PLACEHOLDER}
                  aria-label={OTHER_LABEL}
                  disabled={settled}
                  onChange={(event) => typeOther(event.target.value)}
                />
              </div>
            ) : null}
          </div>
        </div>

        <div className="chat-question-foot">
          {failure ? (
            <p className="chat-question-failure" role="alert">
              {failure}
            </p>
          ) : null}
          <div className="chat-question-bar">
            {questions.length > 1 ? (
              <div className="chat-question-pager">
                <button
                  type="button"
                  className="chat-question-page"
                  onClick={() => goTo(index - 1)}
                  disabled={index === 0}
                  aria-label="Previous question"
                  title="Previous question"
                >
                  <Chevron back />
                </button>
                <button
                  type="button"
                  className="chat-question-page"
                  onClick={() => goTo(index + 1)}
                  disabled={index === questions.length - 1}
                  aria-label="Next question"
                  title="Next question"
                >
                  <Chevron />
                </button>
              </div>
            ) : null}

            <span className="chat-question-bar__gap" aria-hidden="true" />

            <button type="button" className="chat-question-primary" onClick={commit} disabled={!currentAnswered || settled}>
              {allAnswered ? SUBMIT_LABEL : CONTINUE_LABEL}
            </button>
          </div>
        </div>
      </section>
    </div>
  );
}
