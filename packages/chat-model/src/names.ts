// The one table of naming rules every surface that takes a name reads from.
//
// A file or folder name is bytes on a real filesystem — the drive is mirrored
// onto a workspace machine and synced back, so a name this table lets through
// has to survive Linux, the sync, and a person reading it. Every rule here is
// one the Files namespace itself refuses (`files.invalid_name.<rule>`), spelled
// under the SAME rule name, so a field that refuses locally and a server that
// refuses on the wire cannot disagree about which rule was broken.
//
// One thing the field says is NOT a refusal: a name Windows cannot hold
// (`CON`, a trailing dot, `<>:"|?*`). One drive is shared by machines that
// disagree about names, so the drive stores those and marks them
// `windowsSafe: false` — refusing them at the field would cost a Linux user a
// file they can see. `nameNote` is that sentence, shown beside a name that is
// going to be accepted.
//
// A chat title is not a filename: it is display text with no filesystem under
// it, so it is bounded in characters and admits `/`, `.` and `CON`.
//
// Pure functions, no imports: the web portal, the webview and any future
// surface import this rather than re-deriving the limits.

/** `NAME_MAX` — the longest single path component a filesystem will write. */
export const FS_NAME_MAX_BYTES = 255;

/** The sidecar `alkera files pull` streams a file into beside its target. */
export const PULL_PART_SUFFIX = ".alkera-part";

/**
 * The byte ceiling on a file or folder name — the server's own `NAME_MAX_BYTES`.
 *
 * Not `NAME_MAX`. A 255-byte name is legal on the filesystem and can never be
 * *pulled* onto one: the pull writes `<name>.alkera-part` next to the target
 * and promotes it once the hash matches, so the sidecar for a name at
 * `NAME_MAX` is itself over it. The name would exist in the drive and be
 * unreachable from every machine. Derived from the suffix on both sides rather
 * than written down twice.
 */
export const NAME_MAX_BYTES = FS_NAME_MAX_BYTES - PULL_PART_SUFFIX.length;

/** A chat title's bounds, in characters (not bytes). */
export const CHAT_TITLE_MIN_CHARS = 1;
export const CHAT_TITLE_MAX_CHARS = 200;

/** Which rule a name broke. All but `reserved` are the server's own vocabulary;
 *  `reserved` is the note, and never a refusal. */
export type NameRule =
  | "empty"
  | "nul"
  | "separator"
  | "dot"
  | "too_long"
  | "control"
  | "surrounding_space"
  | "reserved";

/** The rules the Files namespace refuses on the wire, as `files.invalid_name.<rule>`.
 *  Exactly the set `validateName` can return: a field that refuses says what the
 *  server would say, and a rule with no code here does not refuse at all. */
export const SERVER_ENFORCED_NAME_RULES = [
  "empty",
  "nul",
  "separator",
  "dot",
  "too_long",
  "control",
  "surrounding_space",
] as const satisfies readonly NameRule[];

/** The API error code for a rule the server enforces. */
export function filesInvalidNameCode(rule: (typeof SERVER_ENFORCED_NAME_RULES)[number]): string {
  return `files.invalid_name.${rule}`;
}

export interface NameRefusal {
  rule: NameRule;
  /** One sentence, ready to show beside the field. */
  message: string;
}

/** Windows resolves a device before the extension, so `NUL.txt` is the NUL
 *  device while `COM10` and `CONtract.md` are ordinary names. */
const WINDOWS_DEVICE_STEMS: ReadonlySet<string> = new Set([
  "CON",
  "PRN",
  "AUX",
  "NUL",
  ...Array.from({ length: 9 }, (_, i) => `COM${i + 1}`),
  ...Array.from({ length: 9 }, (_, i) => `LPT${i + 1}`),
]);

/** C0, DEL and C1 — the code points no name may carry. */
function hasControlCharacter(text: string): boolean {
  for (const ch of text) {
    const cp = ch.codePointAt(0) ?? 0;
    if (cp <= 0x1f || (cp >= 0x7f && cp <= 0x9f)) return true;
  }
  return false;
}

/** How many bytes this name occupies once encoded as UTF-8 — the measure the
 *  filesystem uses. Counted rather than encoded: this package is pure model
 *  code and runs where `TextEncoder` is not declared, and a name is at most a
 *  few hundred characters. A lone surrogate counts as the three bytes its
 *  replacement character takes, which is what an encoder would write. */
export function byteLength(text: string): number {
  let bytes = 0;
  for (const ch of text) {
    const cp = ch.codePointAt(0) ?? 0;
    bytes += cp <= 0x7f ? 1 : cp <= 0x7ff ? 2 : cp <= 0xffff ? 3 : 4;
  }
  return bytes;
}

/** The byte-order mark, which JavaScript counts as a space and the server does
 *  not. */
const ZWNBSP = "﻿";

/**
 * Trim the characters the SERVER trims.
 *
 * The server strips what Python's `str.strip()` strips, and `String.trim()`
 * strips a slightly different set. Every character the two disagree about is a
 * control character — refused above, by both — except one: JavaScript counts
 * U+FEFF as whitespace and Python does not. A name that begins with a byte-order
 * mark is accepted on the wire, so the field has to accept it too, or the field
 * refuses a name the drive is holding.
 */
function trimAsTheServerDoes(text: string): string {
  const isSpace = (ch: string): boolean => ch !== ZWNBSP && ch.trim() === "";
  let start = 0;
  let end = text.length;
  while (start < end && isSpace(text[start]!)) start += 1;
  while (end > start && isSpace(text[end - 1]!)) end -= 1;
  return text.slice(start, end);
}

/**
 * Why this file or folder name is refused, or `null` when it is legal.
 *
 * The order is the order a person reads: a blank field is empty rather than
 * "starts with a space", and a name is only called reserved once it is
 * otherwise well formed.
 */
export function validateName(name: string): NameRefusal | null {
  // Empty is empty — no bytes. A name of only spaces is a name the server
  // refuses for its spaces (`surrounding_space`), not for being blank, and a
  // field that called it `empty` would name a different rule than the wire.
  if (name === "") return { rule: "empty", message: "Enter a name." };
  if (name.includes("\u0000")) {
    return { rule: "nul", message: "A name can’t contain a null character." };
  }
  if (name.includes("/")) return { rule: "separator", message: "A name can’t contain “/”." };
  if (name === "." || name === "..") {
    return { rule: "dot", message: "“.” and “..” are reserved by the filesystem." };
  }
  if (byteLength(name) > NAME_MAX_BYTES) {
    return { rule: "too_long", message: `A name is limited to ${NAME_MAX_BYTES} bytes.` };
  }
  if (hasControlCharacter(name)) {
    return { rule: "control", message: "A name can’t contain control characters." };
  }
  const trimmed = trimAsTheServerDoes(name);
  if (name !== trimmed) {
    return {
      rule: "surrounding_space",
      // A field holding only spaces reads as blank. "Take the spaces out",
      // followed by "enter a name" once they have, is two steps for one
      // mistake — so the sentence is the blank one. The RULE stays the
      // server's, which is the half the wire and the field have to agree on.
      message: trimmed === "" ? "Enter a name." : "A name can’t start or end with a space.",
    };
  }
  return null;
}

/** Characters Windows refuses outright inside a name. */
const WINDOWS_FORBIDDEN = new Set('<>:"/\\|?*');

/** Whether Windows could hold this name — the server's `nameFlags.windows_safe`,
 *  spelled the same way so a field and a listing never disagree about a row. */
export function windowsSafe(name: string): boolean {
  for (const ch of name) {
    const cp = ch.codePointAt(0) ?? 0;
    if (WINDOWS_FORBIDDEN.has(ch) || cp <= 0x1f) return false;
  }
  if (name.endsWith(".") || name.endsWith(" ")) return false;
  const stem = name.split(".", 1)[0] ?? "";
  return !WINDOWS_DEVICE_STEMS.has(stem.toUpperCase());
}

/**
 * What to say beside a name that is going to be accepted, or `null`.
 *
 * Not a refusal and not a reason the field is invalid: the drive takes these
 * names, because one drive is shared by machines that disagree about what a
 * name may be and refusing here would cost a Linux user a file they can see.
 * Returns `null` for a name that is refused outright, so the one line under the
 * field is never a note arguing with a refusal.
 */
export function nameNote(name: string): NameRefusal | null {
  if (validateName(name) !== null || windowsSafe(name)) return null;
  return {
    rule: "reserved",
    message:
      "Windows cannot hold this name. It will be renamed when pulled to a Windows machine.",
  };
}

/**
 * Why this chat title is refused, or `null` when it is legal.
 *
 * Bounded in characters, because nothing writes a title to a filesystem: a
 * 200-character title of three-byte characters is fine, and a title may carry
 * the `/` and `..` a name may not.
 */
export function validateChatTitle(title: string): NameRefusal | null {
  if (title.trim().length < CHAT_TITLE_MIN_CHARS) {
    return { rule: "empty", message: "Enter a title." };
  }
  if (hasControlCharacter(title)) {
    return { rule: "control", message: "A title can’t contain control characters." };
  }
  if ([...title].length > CHAT_TITLE_MAX_CHARS) {
    return {
      rule: "too_long",
      message: `A title is limited to ${CHAT_TITLE_MAX_CHARS} characters.`,
    };
  }
  if (title !== title.trim()) {
    return { rule: "surrounding_space", message: "A title can’t start or end with a space." };
  }
  return null;
}
