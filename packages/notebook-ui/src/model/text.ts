// Words that arrive from a server or a library and are shown as a sentence of
// their own: the first letter is raised, nothing else is touched, so a name the
// message carries keeps its own spelling.

/** `message` as the start of a sentence: "filtering needs duckdb" becomes
 *  "Filtering needs duckdb". Leading whitespace is dropped. */
export function sentenceStart(message: string): string {
  const text = message.trimStart();
  return text.charAt(0).toUpperCase() + text.slice(1);
}
