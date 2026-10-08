// The reasoning disclosure. While streaming it reads "Thinking…" under the
// working sheen and holds open, its words easing in as they arrive. Settled,
// it reads "Thought for <duration>" and stays reachable after the answer
// starts.

import { useState } from "react";

import { StreamText } from "../indicators";
import s from "./thinking.module.css";

export function Thinking({
  duration,
  body,
  defaultOpen,
  streaming,
}: {
  duration: string;
  body: string[];
  defaultOpen: boolean;
  streaming?: boolean;
}) {
  const [userOpen, setUserOpen] = useState(defaultOpen);
  const open = streaming ? true : userOpen;
  return (
    <section>
      {/* The whole line is the toggle, with no caret glyph. aria-expanded
          carries reachability. */}
      <button
        type="button"
        className={s.toggle}
        aria-expanded={open}
        disabled={streaming}
        onClick={() => setUserOpen((v) => !v)}
      >
        <span className={streaming ? `${s.label} chat-shimmer` : s.label}>
          {streaming ? "Thinking…" : `Thought for ${duration}`}
        </span>
      </button>
      {open ? (
        <div className={s.body}>
          {body.map((para, i) => (
            <p key={i}>{streaming ? <StreamText text={para} /> : para}</p>
          ))}
        </div>
      ) : null}
    </section>
  );
}
