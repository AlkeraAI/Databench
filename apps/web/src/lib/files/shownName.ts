/**
 * A name as it may be put on screen: every character that could make it read
 * as something else is shown as its escape instead.
 *
 * A right-to-left override turns `report‮gnp.exe` into something that reads
 * `report.exe.png`, so a reader trusts a `.png` that is an executable. The server
 * already escapes the name it sends as `nameDisplay`, but a name reaches the
 * screen by other roads too — the raw path in the details pane, a chat's title,
 * the file a person just dropped into the upload tray, which the server has
 * never seen. Every one of them goes through this, so no surface has to know
 * which roads are already safe.
 *
 * The set is the server's (`alkera_core.files.names`): C0, DEL and C1 controls,
 * the invisibles, the embedding/override/isolate controls — plus the rest of
 * Unicode's Bidi_Control set, so the Arabic letter mark cannot slip through
 * where the server's list stops. The escape is the server's spelling
 * (`‮`, `\U000e0001`), and the helper never touches a backslash, so a name
 * the server already escaped passes through unchanged: running it twice is
 * running it once.
 */

/** Unicode's Bidi_Control property, every code point of it. */
export const BIDI_CONTROLS: readonly number[] = [
  0x061c, // ARABIC LETTER MARK
  0x200e, // LEFT-TO-RIGHT MARK
  0x200f, // RIGHT-TO-LEFT MARK
  0x202a, // LEFT-TO-RIGHT EMBEDDING
  0x202b, // RIGHT-TO-LEFT EMBEDDING
  0x202c, // POP DIRECTIONAL FORMATTING
  0x202d, // LEFT-TO-RIGHT OVERRIDE
  0x202e, // RIGHT-TO-LEFT OVERRIDE
  0x2066, // LEFT-TO-RIGHT ISOLATE
  0x2067, // RIGHT-TO-LEFT ISOLATE
  0x2068, // FIRST STRONG ISOLATE
  0x2069, // POP DIRECTIONAL ISOLATE
];

const BIDI = new Set(BIDI_CONTROLS);

/** A code point no name may be drawn with as itself. */
export function isSuspicious(cp: number): boolean {
  return (
    cp <= 0x1f ||
    (cp >= 0x7f && cp <= 0x9f) ||
    BIDI.has(cp) ||
    (cp >= 0x200b && cp <= 0x200f) ||
    cp === 0x2060 ||
    cp === 0xfeff ||
    cp === 0xe0001 ||
    (cp >= 0xe0020 && cp <= 0xe007f) ||
    // A lone surrogate: half a character, which some renderers draw as
    // nothing at all.
    (cp >= 0xd800 && cp <= 0xdfff)
  );
}

function escape(cp: number): string {
  return cp <= 0xffff
    ? `\\u${cp.toString(16).padStart(4, "0")}`
    : `\\U${cp.toString(16).padStart(8, "0")}`;
}

/** `text` with every suspicious code point written as its escape. */
export function shownName(text: string): string {
  let out = "";
  for (const ch of text) {
    const cp = ch.codePointAt(0) ?? 0;
    out += isSuspicious(cp) ? escape(cp) : ch;
  }
  return out;
}
