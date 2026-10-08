import { describe, expect, it } from "vitest";

import {
  CHAT_TITLE_MAX_CHARS,
  FS_NAME_MAX_BYTES,
  NAME_MAX_BYTES,
  PULL_PART_SUFFIX,
  SERVER_ENFORCED_NAME_RULES,
  byteLength,
  filesInvalidNameCode,
  nameNote,
  validateChatTitle,
  validateName,
  type NameRule,
} from "./names";

/** Every refusal, with the shortest input that provokes it. */
const REFUSED: ReadonlyArray<readonly [string, string, NameRule]> = [
  ["empty", "", "empty"],
  // Only the server's own verdict: a name of spaces has bytes, so it is refused
  // for its spaces rather than for being blank.
  ["whitespace only", "   ", "surrounding_space"],
  ["a NUL byte", "a\u0000b", "nul"],
  ["a separator", "a/b", "separator"],
  ["a separator alone", "/", "separator"],
  ["one dot", ".", "dot"],
  ["two dots", "..", "dot"],
  ["one byte over the ceiling", "a".repeat(NAME_MAX_BYTES + 1), "too_long"],
  // The ceiling is bytes, not characters: this is 122 characters and 244 bytes.
  [
    "a short name that is one byte over",
    "é".repeat(Math.floor(NAME_MAX_BYTES / 2)) + "aa",
    "too_long",
  ],
  ["the filesystem's own limit", "a".repeat(FS_NAME_MAX_BYTES), "too_long"],
  ["a tab", "a\tb", "control"],
  ["a newline", "a\nb", "control"],
  ["a DEL", "a\u007fb", "control"],
  ["a leading space", " notes.md", "surrounding_space"],
  ["a trailing space", "notes.md ", "surrounding_space"],
];

describe("validateName", () => {
  it.each(REFUSED)("refuses %s", (_label, input, rule) => {
    expect(validateName(input)?.rule).toBe(rule);
  });

  it.each(REFUSED)("explains %s in a sentence", (_label, input) => {
    const refusal = validateName(input);
    expect(refusal?.message).toMatch(/[.]$/);
    expect(refusal?.message.length).toBeGreaterThan(8);
  });

  const ACCEPTED = [
    "notes.md",
    "a",
    "a".repeat(NAME_MAX_BYTES),
    "é".repeat(Math.floor(NAME_MAX_BYTES / 2)),
    "My Report (final).pdf",
    // Not the device itself: Windows resolves the stem, and "COM10" is not one.
    "COM10",
    "CONtract.md",
    "a.b.c",
    "…ellipsis…",
    "файл.txt",
    "back\\slash.txt",
    // Windows cannot hold any of these; the drive stores them and says so.
    "CON",
    "nul",
    "COM1.txt",
    "trailing.",
    'a<b>c:d"e|f?g*h.txt',
  ];
  it.each(ACCEPTED)("accepts %s", (input) => {
    expect(validateName(input)).toBeNull();
  });

  it("names the byte ceiling rather than a character count", () => {
    expect(validateName("a".repeat(NAME_MAX_BYTES + 1))?.message).toContain(String(NAME_MAX_BYTES));
  });

  it("refuses the emptiest failure first, so a blank field is not called reserved", () => {
    expect(validateName("")?.rule).toBe("empty");
  });

  // A field holding only spaces reads as blank. Telling the reader to take the
  // spaces out and then telling them to enter a name is two steps for one
  // mistake, so the sentence is the blank one while the RULE stays the
  // server's, which is what the wire and the field have to agree on.
  it.each([
    ["only spaces", "   "],
    ["one space", " "],
    ["a non-breaking space", " "],
  ])("asks for a name when the field holds %s", (_label, input) => {
    expect(validateName(input)).toEqual({ rule: "surrounding_space", message: "Enter a name." });
  });

  it("says what is actually wrong when the name has characters in it", () => {
    expect(validateName(" a")?.message).toContain("start or end with a space");
    expect(validateName("a ")?.message).toContain("start or end with a space");
  });
});

describe("the byte ceiling", () => {
  it("is NAME_MAX less the sidecar a pull writes beside the file", () => {
    expect(NAME_MAX_BYTES).toBe(FS_NAME_MAX_BYTES - PULL_PART_SUFFIX.length);
    expect(NAME_MAX_BYTES).toBeLessThan(FS_NAME_MAX_BYTES);
  });

  it("counts the bytes a UTF-8 encoder would write", () => {
    // The ceiling is only the server's if the measure is: a field counting
    // characters would send a 122-character name the drive refuses at 244
    // bytes, and refuse a 243-character ASCII name it takes.
    expect(byteLength("a")).toBe(1);
    expect(byteLength("é")).toBe(2);
    expect(byteLength("日")).toBe(3);
    expect(byteLength("😀")).toBe(4);
    const twoByte = "é".repeat(Math.floor(NAME_MAX_BYTES / 2));
    expect(byteLength(twoByte + "a")).toBe(NAME_MAX_BYTES);
    expect(validateName(twoByte + "a")).toBeNull();
    expect(validateName(twoByte + "aa")?.rule).toBe("too_long");
    // …and 123 characters is over it while 243 ASCII characters are not.
    expect([...(twoByte + "aa")]).toHaveLength(123);
    expect(validateName("a".repeat(NAME_MAX_BYTES))).toBeNull();
  });
});

describe("nameNote", () => {
  const NOTED = [
    "CON",
    "nul",
    "NUL.txt",
    "COM1.txt",
    "trailing.",
    "a<b",
    'a"b',
    "a|b",
    "a?b",
    "a*b",
    "a:b",
  ];
  it.each(NOTED)("warns about %s without refusing it", (input) => {
    expect(validateName(input)).toBeNull();
    expect(nameNote(input)?.rule).toBe("reserved");
    expect(nameNote(input)?.message).toMatch(/Windows/);
  });

  const SILENT = ["notes.md", "COM10", "CONtract.md", "f oo", "a.b.c", "файл.txt", "COM¹"];
  it.each(SILENT)("says nothing about %s", (input) => {
    expect(nameNote(input)).toBeNull();
  });

  it("says nothing about a name that is refused outright", () => {
    // The note is for a name that will be stored. A refusal already occupies
    // the one line under the field, and two sentences there contradict.
    expect(nameNote("CON ")).toBeNull();
    expect(nameNote("a/b")).toBeNull();
  });
});

describe("filesInvalidNameCode", () => {
  it("spells the server's code for every rule the server itself enforces", () => {
    for (const rule of SERVER_ENFORCED_NAME_RULES) {
      expect(filesInvalidNameCode(rule)).toBe(`files.invalid_name.${rule}`);
    }
  });

  it("covers every rule the Files namespace refuses and nothing else", () => {
    expect([...SERVER_ENFORCED_NAME_RULES].sort()).toEqual([
      "control",
      "dot",
      "empty",
      "nul",
      "separator",
      "surrounding_space",
      "too_long",
    ]);
  });

  it("is exactly the set of rules the field can actually refuse", () => {
    // `reserved` is the one rule with no server code, because it does not
    // refuse at all — it is a note. If a rule ever refuses without a code, a
    // field would show a sentence the server would then contradict.
    const refusable = new Set<NameRule>(REFUSED.map(([, , rule]) => rule));
    expect([...refusable].sort()).toEqual([...SERVER_ENFORCED_NAME_RULES].sort());
  });
});

describe("validateChatTitle", () => {
  it("refuses an empty title", () => {
    expect(validateChatTitle("")?.rule).toBe("empty");
    expect(validateChatTitle("   ")?.rule).toBe("empty");
  });

  it("refuses past the character ceiling, counting characters and not bytes", () => {
    expect(validateChatTitle("a".repeat(CHAT_TITLE_MAX_CHARS))).toBeNull();
    expect(validateChatTitle("a".repeat(CHAT_TITLE_MAX_CHARS + 1))?.rule).toBe("too_long");
    // 200 three-byte characters is 600 bytes and still a legal title.
    expect(validateChatTitle("あ".repeat(CHAT_TITLE_MAX_CHARS))).toBeNull();
  });

  it("refuses control characters and surrounding spaces", () => {
    expect(validateChatTitle("a\nb")?.rule).toBe("control");
    expect(validateChatTitle(" a")?.rule).toBe("surrounding_space");
    expect(validateChatTitle("a ")?.rule).toBe("surrounding_space");
  });

  it("accepts what a filesystem would refuse — a title is not a filename", () => {
    expect(validateChatTitle("q3/q4 plan")).toBeNull();
    expect(validateChatTitle("..")).toBeNull();
    expect(validateChatTitle("CON")).toBeNull();
  });

  it("names the character ceiling in its message", () => {
    expect(validateChatTitle("a".repeat(CHAT_TITLE_MAX_CHARS + 1))?.message).toContain(
      String(CHAT_TITLE_MAX_CHARS),
    );
  });
});
