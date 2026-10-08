// The short name a model chip wears where its full name does not fit.
//
// A model's display name leads with its maker's family word ("Claude Sonnet
// 5.5"). On a phone's rail that word is the one thing every Claude model has in
// common, so cut to fit it read "Claud…" for all of them. The short name drops
// it: "Sonnet 5.5". A family is taught here by registration; a name that starts
// with none of them is its own short name.

const FAMILY_WORDS: readonly string[] = ["Claude"];

/** The name without its family word, or `undefined` when it has none to drop
 *  (or dropping it would leave nothing). */
export function shortModelLabel(label: string): string | undefined {
  for (const family of FAMILY_WORDS) {
    const prefix = `${family} `;
    if (label.startsWith(prefix)) {
      const rest = label.slice(prefix.length).trim();
      return rest === "" ? undefined : rest;
    }
  }
  return undefined;
}
