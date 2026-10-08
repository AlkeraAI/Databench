// The one floor every model-, file- or third-party-supplied URL crosses before
// it becomes a navigation. The corpus is the refusals, not the acceptances: a
// scheme that executes in this origin, a scheme that carries its own payload,
// and the spellings a parser has to normalise before it can recognise either.

import { afterEach, describe, expect, it, vi } from "vitest";

import { externalHref, openExternalUrl } from "./externalUrl";

const REFUSED: [string, string][] = [
  ["javascript", "javascript:alert(1)"],
  ["javascript in caps", "JavaScript:alert(1)"],
  ["javascript with leading space", "  javascript:alert(1)"],
  ["javascript with an embedded tab", "java\tscript:alert(1)"],
  ["javascript with an embedded newline", "java\nscript:alert(1)"],
  ["javascript with a stray carriage return", "java\rscript:alert(1)"],
  ["javascript behind a null byte", "java\0script:alert(1)"],
  ["an html entity for the colon", "javascript&#58;alert(1)"],
  ["an encoded colon", "javascript%3Aalert(1)"],
  ["data", "data:text/html,<script>alert(1)</script>"],
  ["data in mixed case", "DaTa:text/html,x"],
  ["vbscript", "vbscript:msgbox(1)"],
  ["vbscript in caps", "VBScript:MsgBox(1)"],
  ["file", "file:///etc/passwd"],
  ["blob", "blob:https://app.example.com/0-0"],
  ["a vscode command", "command:workbench.action.terminal.new"],
  ["a protocol-relative target", "//evil.example"],
  ["a bare host", "evil.example/path"],
  ["a relative path", "/reset-password/abc"],
  ["the empty string", ""],
  ["only whitespace", "   "],
  ["basic credentials", "https://user:secret@evil.example/"],
  ["a username with no password", "https://user@evil.example/"],
];

describe("externalHref", () => {
  it.each(REFUSED)("refuses %s", (_name, url) => {
    expect(externalHref(url)).toBeNull();
  });

  it.each([
    ["https", "https://app.example.com/chats/1?q=a#b"],
    ["http", "http://localhost:8000/health"],
    ["a scheme in caps", "HTTPS://app.example.com/"],
    ["surrounding whitespace", "  https://app.example.com/  "],
  ])("admits %s", (_name, url) => {
    const href = externalHref(url);
    expect(href).not.toBeNull();
    expect(href).toMatch(/^https?:\/\//);
  });

  it("hands back the parsed href, not the string it was given", () => {
    // Whatever reaches a navigation is what a parser agreed the URL means, so a
    // spelling that reads as one target and resolves as another cannot survive.
    expect(externalHref("https://app.example.com/a/../b")).toBe("https://app.example.com/b");
  });
});

describe("openExternalUrl", () => {
  const open = vi.fn();

  afterEach(() => {
    open.mockReset();
    vi.unstubAllGlobals();
  });

  it("opens an admitted URL in a new tab with no opener and no referrer", () => {
    vi.stubGlobal("window", { open });
    expect(openExternalUrl("https://app.example.com/x")).toBe(true);
    expect(open).toHaveBeenCalledWith("https://app.example.com/x", "_blank", "noopener,noreferrer");
  });

  it.each(REFUSED)("opens nothing for %s", (_name, url) => {
    vi.stubGlobal("window", { open });
    expect(openExternalUrl(url)).toBe(false);
    expect(open).not.toHaveBeenCalled();
  });
});
