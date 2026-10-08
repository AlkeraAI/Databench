import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { langForExtension, langForPath } from "../highlight";
import { renderHighlightedCode } from "./highlightCode";

// The resource preview and the CodeBlock resolve a language through one extension map, so a file
// that highlights in one highlights in the other. The extensions below once highlighted only in the
// CodeBlock.

afterEach(cleanup);

/** The token classes a highlighted preview line renders, deduplicated and sorted. */
function tokenClasses(line: string, language: string): string[] {
  const { container } = render(<pre>{renderHighlightedCode(line, language)}</pre>);
  const classes = new Set<string>();
  container.querySelectorAll("span[class]").forEach((span) => {
    span.classList.forEach((name) => classes.add(name));
  });
  return [...classes].sort();
}

describe("renderHighlightedCode", () => {
  it.each([
    { language: "mjs", line: "export const answer = 42;", token: "alk-tok--keyword" },
    { language: "cjs", line: "const fs = require('fs');", token: "alk-tok--keyword" },
    { language: "zsh", line: 'if [ -n "$HOME" ]; then echo hi; fi', token: "alk-tok--keyword" },
    { language: "html", line: '<a href="/x">x</a>', token: "alk-tok--tag" },
    { language: "xml", line: '<item id="1"/>', token: "alk-tok--tag" },
    { language: "svg", line: '<svg viewBox="0 0 1 1"></svg>', token: "alk-tok--tag" },
    { language: "PY", line: "def f(): return None", token: "alk-tok--keyword" },
    { language: "python", line: "def f(): return None", token: "alk-tok--keyword" },
  ])("highlights a $language line", ({ language, line, token }) => {
    expect(tokenClasses(line, language)).toContain(token);
  });

  it.each(["text", "terminal", "diff", "unknown"])("renders a %s line plain", (language) => {
    expect(tokenClasses("const x = 1; <a/>", language)).toEqual([]);
  });
});

describe("langForExtension", () => {
  const EXTENSIONS = [
    ...["js", "jsx", "mjs", "cjs", "ts", "tsx", "sql", "py", "yml", "yaml", "json"],
    ...["sh", "bash", "zsh", "md", "markdown", "html", "xml", "svg"],
  ];

  it.each(EXTENSIONS)("resolves .%s the way a file path does", (ext) => {
    expect(langForExtension(ext)).not.toBeNull();
    expect(langForExtension(ext)).toBe(langForPath(`notes/file.${ext}`));
  });

  it.each([
    { name: "python", want: "python" },
    { name: "TypeScript", want: "typescript" },
    { name: "text", want: null },
    { name: "constructor", want: null },
    { name: "", want: null },
  ])("resolves the name $name to $want", ({ name, want }) => {
    expect(langForExtension(name)).toBe(want);
  });
});

describe("langForPath", () => {
  it.each(["notes/file.constructor", "notes/file.toString", "notes/README", "notes/file.txt"])(
    "finds no grammar for %s",
    (path) => {
      expect(langForPath(path)).toBeNull();
    },
  );
});
