// The one rule for what a chat message may point at inside its own folder.
//
// A user's pasted image and an agent's chart are both `![label](outputs/x.png)`;
// a file either side handed the other is `[File 1: report.csv](scratch/f.csv)`.
// Both are paths RELATIVE TO THE CHAT'S WORKING FOLDER, resolved by whichever
// shell renders the transcript. An agent also writes the absolute path its box
// sees (`/opt/alkera-work/.alkera/chats/<chat>/scratch/report.csv`, which is
// what `blob.materialize` answers) and percent-escapes a name with a space in
// it (`my%20chart.png`); both name a file in the chat as surely as the short
// form, so both resolve.
//
// Everything else is deliberately not a chat file: a `http(s)://` URL (the
// transcript never loads a third-party resource on the reader's behalf), a
// `data:` URI (megabytes of base64 the model would re-read every turn), a
// `blob:` handle (a tool result, routed elsewhere), an absolute path that is not
// THIS chat's folder, or a `..` escape (nothing outside the chat folder is the
// chat's to show). Those stay the text they are, so a reader sees exactly what
// was written rather than a broken picture or a dead link.
//
// This is `alkera_core.chat_paths.chat_path` — the rule the box and the Slack
// thread apply to the same text — spelled for the browser. Both are pinned
// against the same cases, so a reference one surface opens is one every
// surface opens.

/** The image types a chat renders inline. This list is the renderer's half of
 *  the agent's brief (the IMAGES block of the harness system prompt); a type
 *  added here without the prompt saying so is one the agent will never emit.
 *  SVG is on it because it always goes through `<img>`, never an inline `<svg>`
 *  (see `ChatImage`). */
export const CHAT_IMAGE_EXTENSIONS: ReadonlySet<string> = new Set([
  "png",
  "jpg",
  "jpeg",
  "gif",
  "webp",
  "svg",
]);

/** A box's absolute path into a chat's folder: `<anything>/.alkera/chats/<chat>/<rest>`. */
const BOX_CHAT_ROOT = /\/\.alkera\/chats\/([^/]+)\/(.+)$/;
const SCHEME = /^[a-z][a-z0-9+.-]*:/i;

/** Percent-escapes decoded, as Python's `unquote` does: a malformed escape is
 *  kept as written rather than refusing the whole target. */
function unquote(target: string): string {
  try {
    return decodeURIComponent(target);
  } catch {
    return target;
  }
}

/** `raw` as clean steps down, or `null` when any step climbs or is empty. */
function steps(raw: string): string | null {
  if (raw.includes("\\") || /[?#]/.test(raw)) return null;
  const segments = raw.split("/");
  if (segments.some((segment) => segment === "" || segment === "." || segment === "..")) return null;
  return segments.join("/");
}

/** Whether a target is a box's absolute path into SOME chat's folder. Which
 *  chat can only be checked by a caller that knows its own chat id, so the
 *  block parser keeps such a target as written and the renderer decides. */
export function isBoxChatPath(target: string): boolean {
  if (/\s/.test(target)) return false;
  const decoded = unquote(target);
  return decoded.startsWith("/") && BOX_CHAT_ROOT.test(decoded);
}

/** The path a link target names inside chat `chatId`'s folder, normalized
 *  (`./a` → `a`, `a%20b` → `a b`, a box path into this chat → the steps below
 *  its folder), or `null` when the target reaches outside the chat folder or is
 *  not a path at all. Without a `chatId` no absolute path is the chat's. Pure
 *  string logic: no filesystem, no network.
 *
 *  A target with raw whitespace in it is refused: a Markdown link destination
 *  cannot hold one, and the box's and Slack's reference scanners stop at it. */
export function chatRelativePath(target: string, chatId: string | null = null): string | null {
  if (target === "" || /\s/.test(target)) return null;
  if (SCHEME.test(target)) return null;
  let decoded = unquote(target);
  if (decoded.startsWith("/")) {
    const match = BOX_CHAT_ROOT.exec(decoded);
    if (match === null || chatId === null || match[1] !== chatId) return null;
    return steps(match[2] ?? "");
  }
  while (decoded.startsWith("./")) decoded = decoded.slice(2);
  return steps(decoded);
}

/** A path a block or a ticket already carries, resolved for chat `chatId`. A
 *  relative path was normalized when it was parsed and is taken as it is (never
 *  decoded twice); a box's absolute path is checked against the chat now,
 *  because only the renderer knows which chat it is in. */
export function resolveChatPath(path: string, chatId: string | null): string | null {
  return path.startsWith("/") ? chatRelativePath(path, chatId) : path;
}

/** The path an IMAGE target names, or `null` when the target is not an image
 *  this transcript can show — either not a chat path at all, or a path whose
 *  type is not on `CHAT_IMAGE_EXTENSIONS`. A relative target comes back
 *  normalized; a box's absolute path comes back as written, for the renderer to
 *  check against its chat (`resolveChatPath`). */
export function chatImagePath(target: string): string | null {
  const path = isBoxChatPath(target) ? target : chatRelativePath(target);
  if (path === null) return null;
  return isChatImageName(path.slice(path.lastIndexOf("/") + 1)) ? path : null;
}

/** Whether a file, by its name, is one the chat renders as an image. The
 *  composer uses this to decide between an image pill and a file pill. */
export function isChatImageName(name: string): boolean {
  const dot = name.lastIndexOf(".");
  return dot > 0 && CHAT_IMAGE_EXTENSIONS.has(name.slice(dot + 1).toLowerCase());
}
