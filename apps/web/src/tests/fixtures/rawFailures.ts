// The two failures whose own words are written for a developer, never for the person on the page:
// a server that fell over with its exception in the body, and a request the network never carried.
// A page that reads `error.message` onto the screen shows these words; one that says its failures
// through `refusalSentence` shows its own sentence instead.

import { expect } from "vitest";
import { screen } from "@testing-library/react";

/** What a 500's body can carry: the exception, a table name, a column. */
export const DEVELOPER_SENTENCE = 'psycopg.errors.UndefinedColumn: column "orgs.slug" does not exist';

/** The browser's own words for a request that never reached the server. */
export const TRANSPORT_SENTENCE = "Failed to fetch";

/** A 500 whose envelope carries {@link DEVELOPER_SENTENCE}. */
export function serverFellOver(): Response {
  return new Response(
    JSON.stringify({ error: { code: "internal_error", message: DEVELOPER_SENTENCE, trace_id: "t" } }),
    { status: 500, headers: { "content-type": "application/json" } },
  );
}

/** A `fetch` answer that never came: the TypeError the browser rejects with. */
export function transportFailed(): Response {
  throw new TypeError(TRANSPORT_SENTENCE);
}

/** Both failures, for `it.each`: a label and the answer the stubbed `fetch` gives. */
export const RAW_FAILURES: ReadonlyArray<readonly [string, () => Response]> = [
  ["a 500 whose body is a developer's", serverFellOver],
  ["a failed transport", transportFailed],
];

/** Neither failure's own words are anywhere on the page. */
export function expectNoRawFailureText(): void {
  expect(screen.queryByText(DEVELOPER_SENTENCE, { exact: false })).toBeNull();
  expect(screen.queryByText(TRANSPORT_SENTENCE, { exact: false })).toBeNull();
  expect(document.body.textContent ?? "").not.toMatch(/\(\d{3}\)|psycopg|Failed to fetch/);
}
