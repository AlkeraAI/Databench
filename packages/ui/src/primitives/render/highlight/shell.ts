/**
 * A lightweight shell tokenizer for the Terminal component — no Prism grammar, just enough structure
 * to color a command line: the command name, its flags, quoted strings, variables, numbers, comments,
 * and operators. The first word of each pipeline segment (at the start, or after an operator) is the
 * command; the rest are arguments. Tokenized into React-safe segments, never dangerouslySetInnerHTML.
 */

export type ShellTokenKind =
  | "command"
  | "arg"
  | "flag"
  | "string"
  | "number"
  | "variable"
  | "comment"
  | "operator";

export interface ShellSeg {
  /** The token class kind, or null for plain unstyled whitespace/text between tokens. */
  kind: ShellTokenKind | null;
  text: string;
}

const OPERATORS = new Set(["&&", "||", "|", ">>", "<<", ">", "<", ";", "&"]);
const TOKEN =
  /("(?:\\.|[^"\\])*"|'[^']*'|#[^\n]*|&&|\|\||>>|<<|[|<>;&]|\$\{[^}]*\}|\$[A-Za-z_]\w*|--?[A-Za-z][\w-]*|[^\s'"|<>;&#$]+)/g;

function classify(token: string, atCommand: boolean): ShellTokenKind {
  if (token.startsWith('"') || token.startsWith("'")) return "string";
  if (token.startsWith("#")) return "comment";
  if (token.startsWith("$")) return "variable";
  if (OPERATORS.has(token)) return "operator";
  if (token.startsWith("-")) return "flag";
  if (/^\d+(?:\.\d+)?$/.test(token)) return "number";
  return atCommand ? "command" : "arg";
}

/** Split a shell command into classified segments (interleaving plain gaps), for the Terminal prompt. */
export function tokenizeShell(command: string): ShellSeg[] {
  const out: ShellSeg[] = [];
  const pattern = new RegExp(TOKEN.source, "g");
  let cursor = 0;
  let atCommand = true;
  for (let match = pattern.exec(command); match !== null; match = pattern.exec(command)) {
    if (match.index > cursor) out.push({ kind: null, text: command.slice(cursor, match.index) });
    const token = match[0];
    out.push({ kind: classify(token, atCommand), text: token });
    atCommand = OPERATORS.has(token);
    cursor = match.index + token.length;
  }
  if (cursor < command.length) out.push({ kind: null, text: command.slice(cursor) });
  return out.length > 0 ? out : [{ kind: null, text: command }];
}
