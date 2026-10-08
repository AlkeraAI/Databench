// The working vocabulary shared across the transcript: the three-dot beat, the
// working status line, and the streamed-word entrance. Defined once so
// thinking, prose, slash lines, and the composer all pulse the same way. No
// cursor exists here: streaming text arrives by easing its new words in.

import "./indicators.css";

export function WorkingDots() {
  return (
    <span className="chat-dots" aria-hidden="true">
      <span />
      <span />
      <span />
    </span>
  );
}

/** A turning arc in currentColor. The caller sizes it with `size` and colors
 *  it by ink; reduced motion parks it as a static arc. */
export function Spinner({ size = 13 }: { size?: number }) {
  const r = (size - 2) / 2;
  const c = size / 2;
  return (
    <svg className="chat-spin" width={size} height={size} viewBox={`0 0 ${size} ${size}`} aria-hidden="true">
      <circle className="chat-spin__track" cx={c} cy={c} r={r} />
      <circle className="chat-spin__arc" cx={c} cy={c} r={r} pathLength={100} />
    </svg>
  );
}

/** Streamed text as word spans with STABLE keys: the reveal only appends, so
 *  an already-mounted word never re-keys and only the newly arrived words run
 *  the entrance animation. */
export function StreamText({ text }: { text: string }) {
  return (
    <>
      {text.split(" ").map((word, i) => (
        <span key={i} className="chat-stream-w">
          {i > 0 ? " " : ""}
          {word}
        </span>
      ))}
    </>
  );
}
