// The empty transcript's opener: a title and a short numbered list of
// suggested first asks, each a single press.

import type { ReactElement } from "react";

import s from "./suggested-asks.module.css";

export interface SuggestedAsksProps {
  title: string;
  suggestions: string[];
  onPick?: (suggestion: string, index: number) => void;
  /** Nothing can be sent from here right now. A suggestion is a send, so each
   *  one is under the same gate as the composer's own key: still listed, so
   *  the page reads the same, and not pressable. */
  disabled?: boolean;
}

export function SuggestedAsks({ title, suggestions, onPick, disabled = false }: SuggestedAsksProps): ReactElement {
  return (
    <section className={s.root}>
      <h3 className={s.title}>{title}</h3>
      <ul className={s.list}>
        {suggestions.map((suggestion, i) => (
          <li key={i}>
            <button
              type="button"
              className={s.sugg}
              disabled={disabled}
              onClick={() => onPick?.(suggestion, i)}
            >
              <span className={`${s.num} chat-num`}>{String(i + 1).padStart(2, "0")}</span>
              <span className={s.text}>{suggestion}</span>
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
