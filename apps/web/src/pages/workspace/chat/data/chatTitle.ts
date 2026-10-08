// The opening title a chat is given from its first message.
//
// It is DERIVED text, not typed text: nobody has named this chat yet, so the
// first line of what they asked for is used instead and is tidied the way a
// title is. The tidying is deliberately timid, because the words are still the
// reader's own — see `capitalised` below.

/** The first word, capitalised — but only when capitalising it cannot be wrong.
 *
 *  A leading word that is plain lower-case letters is prose, and a title reads
 *  better with it capitalised. Anything else is a NAME the reader chose and
 *  meant: a branch or ticket id (`qa-c2-alpha`), a file (`main.py`), a product
 *  (`iPhone`, `eBay`), a flag (`--dry-run`), a path. Capitalising those changes
 *  what they say, so they are left exactly as typed. */
function capitalised(text: string): string {
  const first = text.split(" ", 1)[0] ?? "";
  // Trailing punctuation belongs to the sentence, not to the word: "hello," is
  // still a word, and `qa-c2-alpha:` is still an identifier.
  const word = first.replace(/[^\p{L}\p{N}]+$/u, "");
  // Every lower-case letter, not just the twenty-six: `étude`, `über` and
  // `naïve` are plain prose and read as badly uncapitalised as `hello` would.
  if (!/^\p{Ll}+$/u.test(word)) return text;
  return text.charAt(0).toLocaleUpperCase() + text.slice(1);
}

/** How long a derived title may be before it is cut at a word boundary. */
const TITLE_MAX = 70;

/** A chat's opening title, derived from its first message the way the editor
 *  derives it, so a chat reads the same in both lists. */
export function titleFrom(message: string): string | null {
  const trimmed = message.replace(/\s+/g, " ").trim();
  if (!trimmed) return null;
  const cap = capitalised(trimmed);
  if (cap.length <= TITLE_MAX) return cap;
  const cut = cap.slice(0, TITLE_MAX);
  const lastSpace = cut.lastIndexOf(" ");
  return `${cut.slice(0, lastSpace > 40 ? lastSpace : TITLE_MAX)}…`;
}
