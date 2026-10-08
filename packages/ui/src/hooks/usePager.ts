import { useState, type ChangeEvent, type KeyboardEvent } from "react";

export interface UsePagerOptions {
  /** Current page, 1-based. */
  page: number;
  /** Total pages; clamped to >= 1. */
  pageCount: number;
  /** Commit a page move; the value is clamped to [1, pages] before this fires. */
  onPage: (page: number) => void;
}

export interface UsePagerResult {
  /** Page count, never below 1. */
  pages: number;
  /** The committed page, clamped to [1, pages]. */
  current: number;
  /** Spread onto the type-a-page input: the draft-backed value plus its handlers. */
  input: {
    value: string;
    onChange: (event: ChangeEvent<HTMLInputElement>) => void;
    onBlur: () => void;
    onKeyDown: (event: KeyboardEvent<HTMLInputElement>) => void;
  };
  /** Spread onto the previous / next steppers. */
  prev: { onClick: () => void; disabled: boolean };
  next: { onClick: () => void; disabled: boolean };
}

/**
 * The controlled pager state machine behind `TablePager` and `DataTablePager`. The caller owns
 * `page`; the hook clamps it and runs the type-a-page draft. The input edits free text (so it can
 * sit empty mid-edit) separate from the committed page, accepts digits only, and commits on blur
 * (Enter blurs); committing parses and clamps. The consumers own their markup and labels.
 */
export function usePager({ page, pageCount, onPage }: UsePagerOptions): UsePagerResult {
  // null means "not editing -- mirror the committed page".
  const [draft, setDraft] = useState<string | null>(null);
  const pages = Math.max(1, pageCount);
  const current = Math.min(Math.max(1, page), pages);
  const go = (target: number) => onPage(Math.min(Math.max(1, target), pages));
  const commit = () => {
    if (draft != null && draft !== "") go(Number(draft));
    setDraft(null);
  };
  return {
    pages,
    current,
    input: {
      value: draft ?? String(current),
      onChange: (event) => {
        // Digits only, or empty while mid-edit -- never a partial non-number.
        const raw = event.target.value;
        if (raw === "" || /^\d+$/.test(raw)) setDraft(raw);
      },
      onBlur: commit,
      onKeyDown: (event) => {
        if (event.key === "Enter") event.currentTarget.blur();
      },
    },
    prev: { onClick: () => go(current - 1), disabled: current <= 1 },
    next: { onClick: () => go(current + 1), disabled: current >= pages },
  };
}
