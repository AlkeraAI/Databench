/**
 * The bodies a predicate can genuinely be handed.
 *
 * `{}` is the one that actually caused the outage — a blanket stub, and equally
 * a route answering an envelope where a list was declared. A string and a
 * number stand for a transport that handed a parse through. `null` is the case
 * the old `?? []` DID cover, kept so the guard is not allowed to change the
 * answer for it.
 */
export const HOSTILE: ReadonlyArray<[string, unknown]> = [
  ["an empty object", {}],
  ["an error envelope stored as data", { error: { code: "rate_limited" } }],
  ["a string", "not a list"],
  ["a number", 0],
  ["an explicit null", null],
];

