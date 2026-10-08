/** Every number the UI library picks for itself, in one place.
 *
 *  A shared component cannot read a deployment setting, and it must not restate
 *  one either: the largest file an upload may carry is the deployment's, served
 *  with the refusal in words the reader is shown, and a second copy here would be
 *  a second ceiling that drifts. What lives here is only what the drawing side
 *  decides — how many times it re-sends one file before it gives up, how full a
 *  message is before it is counted out loud.
 *
 *  Each value is named with its unit and carries the line that says why it is
 *  what it is. A host that needs a different answer overrides at the seam named
 *  beside the value; nothing here is read from a global.
 */

/** How many times one attachment is sent before its pill is withdrawn from the
 *  message. Every attempt sends the whole body again from the same `File`, so
 *  the figure trades a recovered upload against re-sending bytes that a real
 *  refusal will refuse again. */
export const UPLOAD_ATTEMPTS = 3;

/** The pause before attempt `attempt` (2-based), doubling from this base. Short
 *  enough that a retry is invisible on a transient blip; a shell that wants a
 *  longer ladder passes its own `retryDelayMs`. */
export const UPLOAD_RETRY_BASE_MS = 400;

/** What a chat message may carry, by file extension.
 *
 *  The server publishes no accepted list, so this is the client's own and the
 *  sentence a reader gets names it in their words. It is the reader's guard,
 *  not a security boundary — the server has the final say on every byte — and
 *  it exists because an attachment that will never be useful to the model is
 *  better refused at the pill than uploaded and ignored.
 *
 *  An uploader that knows better overrides it wholesale (`ComposerUploader.accept`),
 *  which is how a deployment that does publish a list carries it here. */
export const UPLOAD_ACCEPTED_EXTENSIONS: readonly string[] = [
  // Pictures the model can look at. The same set the transcript draws inline
  // (`CHAT_IMAGE_EXTENSIONS`): an SVG goes through `<img>` there and is served
  // under the content origin's SVG policy, never as live DOM on the chat's.
  "png",
  "jpg",
  "jpeg",
  "gif",
  "webp",
  "svg",
  // Documents and data it can read.
  "pdf",
  "txt",
  "md",
  "markdown",
  "csv",
  "tsv",
  "json",
  "jsonl",
  "yaml",
  "yml",
  "toml",
  "xml",
  "html",
  "log",
  "sql",
  "xlsx",
  "xls",
  "docx",
  "parquet",
  // Code.
  "ts",
  "tsx",
  "js",
  "jsx",
  "py",
  "rb",
  "go",
  "rs",
  "java",
  "kt",
  "c",
  "h",
  "cpp",
  "hpp",
  "cs",
  "sh",
  "ipynb",
];

/** The accepted kinds as one phrase, for the sentence a refusal shows. The
 *  image types are named because "images" alone reads as a contradiction to a
 *  reader whose picture was just refused for its type. */
export const UPLOAD_ACCEPTED_SUMMARY =
  "images (PNG, JPEG, GIF, WebP, SVG), PDFs, documents, data files and code";

/** How full a message must be before the composer starts counting it out loud.
 *  Below this a counter is noise over an ordinary sentence; above it the reader
 *  is pasting something long and wants to know where the edge is. */
export const MESSAGE_COUNTER_AT = 0.9;
