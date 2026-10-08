import Prism from "prismjs";
import "prismjs/components/prism-markup";
import "prismjs/components/prism-clike";
import "prismjs/components/prism-javascript";
import "prismjs/components/prism-typescript";
import "prismjs/components/prism-sql";
import "prismjs/components/prism-bash";
import "prismjs/components/prism-yaml";
import "prismjs/components/prism-python";
import "prismjs/components/prism-json";
import "prismjs/components/prism-markdown";

/**
 * Prism-backed tokenizer for the CodeBlock plate. Prism owns the GRAMMAR (the token tree a user would
 * see for that language in their editor); the THEME is ours — the leaves render to classed spans
 * (`alk-tok--<type>`) coloured in codeblock.css from the warm brand pigments, so code reads in the
 * aged-workshop palette with zero cold data-neon. Tokenised into React-safe segments — never
 * dangerouslySetInnerHTML.
 */

// Extension → loaded grammar, the one map every highlighter in the library resolves through. An
// unknown extension falls back to plain ink (no grammar).
const EXT_LANG: ReadonlyMap<string, string> = new Map([
  ["js", "javascript"],
  ["jsx", "javascript"],
  ["mjs", "javascript"],
  ["cjs", "javascript"],
  ["ts", "typescript"],
  ["tsx", "typescript"],
  ["sql", "sql"],
  ["py", "python"],
  ["yml", "yaml"],
  ["yaml", "yaml"],
  ["json", "json"],
  ["sh", "bash"],
  ["bash", "bash"],
  ["zsh", "bash"],
  ["md", "markdown"],
  ["markdown", "markdown"],
  ["html", "markup"],
  ["xml", "markup"],
  ["svg", "markup"],
]);

const GRAMMARS: ReadonlySet<string> = new Set(EXT_LANG.values());

/** The grammar a bare extension (`ts`, `MJS`) or a grammar name (`python`) resolves to, or null
 *  when no grammar fits (render as plain ink). */
export function langForExtension(name: string): string | null {
  const key = name.toLowerCase();
  return EXT_LANG.get(key) ?? (GRAMMARS.has(key) ? key : null);
}

/** The grammar a file path/name resolves to, or null when no grammar fits (render as plain ink). */
export function langForPath(path: string): string | null {
  const match = /\.([a-z0-9]+)$/i.exec(path);
  if (!match) return null;
  return EXT_LANG.get(match[1].toLowerCase()) ?? null;
}

export interface CodeSeg {
  /** The space-joined `alk-tok--*` class chain for this leaf run (empty for plain text). */
  classes: string;
  text: string;
}

/** Walk Prism's (possibly nested) token tree into flat leaf segments, carrying the class chain down. */
function flatten(tokens: (string | Prism.Token)[], inherited: string[], out: CodeSeg[]): void {
  for (const tok of tokens) {
    if (typeof tok === "string") {
      out.push({ classes: inherited.join(" "), text: tok });
      continue;
    }
    const aliases = Array.isArray(tok.alias) ? tok.alias : tok.alias ? [tok.alias] : [];
    const classes = [...inherited, `alk-tok--${tok.type}`, ...aliases.map((a) => `alk-tok--${a}`)];
    if (typeof tok.content === "string") {
      out.push({ classes: classes.join(" "), text: tok.content });
    } else {
      const content = Array.isArray(tok.content) ? tok.content : [tok.content];
      flatten(content as (string | Prism.Token)[], classes, out);
    }
  }
}

/** The grammar for a language id, or undefined when it isn't loaded / recognised. */
function grammarFor(lang: string | null): Prism.Grammar | undefined {
  return lang ? Prism.languages[lang] : undefined;
}

/** Tokenise once, then split the leaf stream on newlines into per-line segment lists. */
export function tokenizeToLines(code: string, lang: string | null): CodeSeg[][] {
  const grammar = grammarFor(lang);
  const segs: CodeSeg[] = [];
  if (grammar) flatten(Prism.tokenize(code, grammar), [], segs);
  else segs.push({ classes: "", text: code });

  const lines: CodeSeg[][] = [[]];
  for (const seg of segs) {
    const parts = seg.text.split("\n");
    parts.forEach((part, i) => {
      if (i > 0) lines.push([]);
      if (part) lines[lines.length - 1].push({ classes: seg.classes, text: part });
    });
  }
  return lines;
}

/** Tokenise a single run (no line splitting) — for an inline highlighted value. */
export function tokenizeInline(code: string, lang: string | null): CodeSeg[] {
  const grammar = grammarFor(lang);
  if (!grammar) return [{ classes: "", text: code }];
  const segs: CodeSeg[] = [];
  flatten(Prism.tokenize(code, grammar), [], segs);
  return segs;
}
