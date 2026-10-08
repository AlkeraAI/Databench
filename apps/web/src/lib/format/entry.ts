// A figure exactly as a person typed it, or the reason it cannot be one.
//
// A limit field never rewrites what was typed. Stripping a minus from "-5" saves
// $5, and stripping the "e" from "1e3" saves $13 — both a figure the person
// never entered, saved without a word. So the text is kept as typed, read
// against one strict shape (digits, optionally a point and more digits), and
// anything else is refused with a sentence saying why. The route applies the
// same shape and the same ceiling, so a figure this module admits is one the
// server admits too; a figure it refuses never leaves the browser.

import { formatPriceUsd } from "@alkera/chat-model";

/** Digits, optionally a point and more digits. No sign, no exponent, no
 *  thousands separator, no currency mark. */
export const PLAIN_NUMBER = /^[0-9]+(\.[0-9]+)?$/;

/** The largest budget a route accepts, in whole USD — the same bound the
 *  schema names, so the field refuses what the server would. */
export const MAX_BUDGET_USD = 1_000_000_000;

/** How a typed figure read: the value when it is one, else the reason it is
 *  not — `null` for an empty field, which is nothing to explain yet. */
export type Entry = { value: number; reason: null } | { value: null; reason: string | null };

export const NOT_PLAIN = "Enter a plain number, like 12.50 (digits and a point only).";
export const ABOVE_ZERO = "Enter an amount above 0, or use Clear to remove the limit.";
export const TWO_DECIMALS = "Use at most two decimals.";
export const BUDGET_TOO_LARGE = `At most ${formatPriceUsd(MAX_BUDGET_USD)} per cycle.`;

/** A positive figure typed as digits and a point, else the reason. */
export function readPlainNumber(text: string): Entry {
  const trimmed = text.trim();
  if (trimmed === "") return { value: null, reason: null };
  if (!PLAIN_NUMBER.test(trimmed)) return { value: null, reason: NOT_PLAIN };
  const value = Number(trimmed);
  if (value <= 0) return { value: null, reason: ABOVE_ZERO };
  return { value, reason: null };
}

/** A USD budget as typed: a positive plain figure in whole cents, no larger
 *  than the route's bound. */
export function readUsd(text: string): Entry {
  const entry = readPlainNumber(text);
  if (entry.value === null) return entry;
  const decimals = text.trim().split(".")[1]?.length ?? 0;
  if (decimals > 2) return { value: null, reason: TWO_DECIMALS };
  if (entry.value > MAX_BUDGET_USD) return { value: null, reason: BUDGET_TOO_LARGE };
  return entry;
}
