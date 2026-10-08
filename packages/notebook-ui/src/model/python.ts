// A small Python lexer, enough to rename a name safely and to tell whether a
// cell defines one. It is not a parser: it knows strings (every prefix, triple
// quotes, f-string replacement fields), comments, brackets, logical lines and
// indentation, and reads statements from those. Offsets are UTF-16 code units,
// the same units CodeMirror and a JavaScript string use.

export type TokenKind = "name" | "number" | "string" | "op" | "comment" | "newline";

export interface Token {
  kind: TokenKind;
  text: string;
  from: number;
  to: number;
}

export const PYTHON_KEYWORDS: ReadonlySet<string> = new Set([
  "False",
  "None",
  "True",
  "and",
  "as",
  "assert",
  "async",
  "await",
  "break",
  "class",
  "continue",
  "def",
  "del",
  "elif",
  "else",
  "except",
  "finally",
  "for",
  "from",
  "global",
  "if",
  "import",
  "in",
  "is",
  "lambda",
  "nonlocal",
  "not",
  "or",
  "pass",
  "raise",
  "return",
  "try",
  "while",
  "with",
  "yield",
]);

const IDENT_START = /[\p{L}\p{Nl}_]/u;
const IDENT_PART = /[\p{L}\p{Nl}\p{Mn}\p{Mc}\p{Nd}\p{Pc}]/u;
const IDENTIFIER = /^[\p{L}\p{Nl}_][\p{L}\p{Nl}\p{Mn}\p{Mc}\p{Nd}\p{Pc}]*$/u;
const STRING_PREFIX = /^(?:r|u|b|f|t|br|rb|fr|rf|tr|rt)$/i;
const NUMBER = /0[xXoObB][0-9a-fA-F_]+|(?:\d[\d_]*(?:\.[\d_]*)?|\.\d[\d_]*)(?:[eE][+-]?\d[\d_]*)?[jJ]?/y;
const OPERATORS = [
  "**=", "//=", ">>=", "<<=", "...",
  "->", ":=", "==", "!=", "<=", ">=", "**", "//", "<<", ">>",
  "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "@=",
];
const AUGMENTED = new Set(["+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "@=", "**=", "//=", ">>=", "<<="]);

/** A valid Python identifier that is not a keyword. */
export function isIdentifier(name: string): boolean {
  return IDENTIFIER.test(name) && !PYTHON_KEYWORDS.has(name);
}

/** Read one code point at `pos` (an astral letter is two code units). */
function codePointAt(src: string, pos: number): string {
  const cp = src.codePointAt(pos);
  return cp === undefined ? "" : String.fromCodePoint(cp);
}

class Lexer {
  readonly tokens: Token[] = [];
  private pos = 0;
  private depth = 0;

  constructor(private readonly src: string) {}

  run(): Token[] {
    this.lexCode(false);
    return this.tokens;
  }

  private emit(kind: TokenKind, from: number, to: number): void {
    this.tokens.push({ kind, text: this.src.slice(from, to), from, to });
  }

  private readIdentifier(): number {
    const src = this.src;
    let end = this.pos;
    let ch = codePointAt(src, end);
    if (!IDENT_START.test(ch)) return end;
    end += ch.length;
    for (;;) {
      ch = codePointAt(src, end);
      if (ch === "" || !IDENT_PART.test(ch)) return end;
      end += ch.length;
    }
  }

  /** Lex Python code. In a replacement field, stop before a `}`, a `:` or a
   *  conversion `!` that sits at the field's own bracket level. */
  private lexCode(field: boolean): void {
    const src = this.src;
    let local = 0;
    while (this.pos < src.length) {
      const ch = src[this.pos];
      if (ch === " " || ch === "\t" || ch === "\f") {
        this.pos++;
        continue;
      }
      if (ch === "\\" && (src[this.pos + 1] === "\n" || src[this.pos + 1] === "\r")) {
        this.pos += src.startsWith("\r\n", this.pos + 1) ? 3 : 2;
        continue;
      }
      if (ch === "\n" || ch === "\r") {
        const start = this.pos;
        this.pos += src.startsWith("\r\n", this.pos) ? 2 : 1;
        const last = this.tokens[this.tokens.length - 1];
        if (!field && this.depth === 0 && last && last.kind !== "newline") this.emit("newline", start, this.pos);
        continue;
      }
      if (ch === "#") {
        const start = this.pos;
        while (this.pos < src.length && src[this.pos] !== "\n" && src[this.pos] !== "\r") this.pos++;
        this.emit("comment", start, this.pos);
        continue;
      }
      if (field && local === 0) {
        if (ch === "}" || ch === ":") return;
        if (ch === "!" && src[this.pos + 1] !== "=") return;
      }
      if (ch === "'" || ch === '"') {
        this.lexString(this.pos, "");
        continue;
      }
      const identEnd = this.readIdentifier();
      if (identEnd > this.pos) {
        const word = src.slice(this.pos, identEnd);
        if (STRING_PREFIX.test(word) && (src[identEnd] === "'" || src[identEnd] === '"')) {
          const start = this.pos;
          this.pos = identEnd;
          this.lexString(start, word);
        } else {
          this.emit("name", this.pos, identEnd);
          this.pos = identEnd;
        }
        continue;
      }
      NUMBER.lastIndex = this.pos;
      const number = NUMBER.exec(src);
      if (number && number[0].length > 0) {
        this.emit("number", this.pos, this.pos + number[0].length);
        this.pos += number[0].length;
        continue;
      }
      const op = OPERATORS.find((candidate) => src.startsWith(candidate, this.pos)) ?? codePointAt(src, this.pos);
      if (op === "(" || op === "[" || op === "{") {
        this.depth++;
        local++;
      } else if (op === ")" || op === "]" || op === "}") {
        this.depth = Math.max(0, this.depth - 1);
        local = Math.max(0, local - 1);
      }
      this.emit("op", this.pos, this.pos + op.length);
      this.pos += op.length;
    }
  }

  /** `start` is where the prefix begins; `this.pos` is at the opening quote. */
  private lexString(start: number, prefix: string): void {
    const src = this.src;
    const quote = src[this.pos];
    const triple = src[this.pos + 1] === quote && src[this.pos + 2] === quote;
    const close = triple ? quote.repeat(3) : quote;
    const formatted = /[ft]/i.test(prefix);
    const raw = /r/i.test(prefix);
    this.pos += close.length;
    let literal = start;
    while (this.pos < src.length) {
      const ch = src[this.pos];
      if (ch === "\\") {
        const next = src[this.pos + 1];
        if (!raw && next === "N" && src[this.pos + 2] === "{") {
          // `\N{NAME}` is one escape, not a replacement field.
          const end = src.indexOf("}", this.pos);
          this.pos = end === -1 ? src.length : end + 1;
        } else if (formatted && (next === "{" || next === "}")) {
          // A brace after a backslash still opens or closes a field.
          this.pos += 1;
        } else {
          // Even in a raw string a backslash keeps the next quote from closing it.
          this.pos += 2;
        }
        continue;
      }
      if (!triple && (ch === "\n" || ch === "\r")) break;
      if (src.startsWith(close, this.pos)) {
        this.pos += close.length;
        break;
      }
      if (formatted && ch === "{") {
        if (src[this.pos + 1] === "{") {
          this.pos += 2;
          continue;
        }
        if (this.pos > literal) this.emit("string", literal, this.pos);
        this.lexField(quote, triple);
        literal = this.pos;
        continue;
      }
      if (formatted && ch === "}" && src[this.pos + 1] === "}") {
        this.pos += 2;
        continue;
      }
      this.pos++;
    }
    if (this.pos > literal) this.emit("string", literal, Math.min(this.pos, src.length));
  }

  /** At a field's `{`: the expression, a `!r` conversion, a format spec with
   *  its own nested fields, then the closing `}`. */
  private lexField(quote: string, triple: boolean): void {
    const src = this.src;
    this.emit("op", this.pos, this.pos + 1);
    this.pos++;
    this.depth++;
    this.lexCode(true);
    if (src[this.pos] === "!") {
      const start = this.pos;
      this.pos++;
      this.pos = this.readIdentifier();
      this.emit("string", start, this.pos);
    }
    if (src[this.pos] === ":") {
      let literal = this.pos;
      while (this.pos < src.length && src[this.pos] !== "}") {
        const ch = src[this.pos];
        if (ch === quote || (!triple && (ch === "\n" || ch === "\r"))) break;
        if (ch === "{") {
          if (this.pos > literal) this.emit("string", literal, this.pos);
          this.lexField(quote, triple);
          literal = this.pos;
          continue;
        }
        this.pos++;
      }
      if (this.pos > literal) this.emit("string", literal, this.pos);
    }
    this.depth = Math.max(0, this.depth - 1);
    if (src[this.pos] === "}") {
      this.emit("op", this.pos, this.pos + 1);
      this.pos++;
    }
  }
}

/** Python tokens, comments and strings included, logical newlines only. */
export function tokenize(code: string): Token[] {
  return new Lexer(code).run();
}

// -- what each name occurrence is -------------------------------------------

/** How a name occurrence relates to the variable of that name:
 *  - `plain`: it is the variable (read, assigned, declared, a binding);
 *  - `attribute`: `obj.name`, someone else's namespace;
 *  - `keyword_arg`: `f(name=1)`, the callee's parameter;
 *  - `module`: a module path or the source name in an import;
 *  - `import_binding`: `import name` / `from m import name`, which binds the
 *    variable but also names what is imported;
 *  - `import_dotted`: `import name.sub`, which binds `name` but cannot be
 *    renamed without changing what it binds. */
export type NameRole = "plain" | "attribute" | "keyword_arg" | "module" | "import_binding" | "import_dotted";

interface Bracket {
  char: string;
  call: boolean;
}

function significant(tokens: readonly Token[]): Token[] {
  return tokens.filter((token) => token.kind !== "comment");
}

function isOp(token: Token | undefined, text: string): boolean {
  return token !== undefined && token.kind === "op" && token.text === text;
}

function isWord(token: Token | undefined, text: string): boolean {
  return token !== undefined && token.kind === "name" && token.text === text;
}

function atStatementStart(prev: Token | undefined): boolean {
  return prev === undefined || prev.kind === "newline" || isOp(prev, ";") || isOp(prev, ":");
}

/** Role of each import-statement name, keyed by index into `toks`. Returns the
 *  index after the statement. */
function classifyImport(toks: readonly Token[], start: number, roles: Map<number, NameRole>): number {
  let i = start;
  const end = (): boolean => i >= toks.length || toks[i].kind === "newline" || isOp(toks[i], ";");
  if (isWord(toks[i], "from")) {
    i++;
    while (!end() && !isWord(toks[i], "import")) {
      if (toks[i].kind === "name") roles.set(i, "module");
      i++;
    }
    i++;
    while (!end()) {
      const token = toks[i];
      if (token.kind === "name" && !PYTHON_KEYWORDS.has(token.text)) {
        if (isWord(toks[i + 1], "as") && toks[i + 2]?.kind === "name") {
          roles.set(i, "module");
          roles.set(i + 2, "plain");
          i += 3;
          continue;
        }
        roles.set(i, "import_binding");
      }
      i++;
    }
    return i;
  }
  i++;
  while (!end()) {
    const path: number[] = [];
    while (!end() && (toks[i].kind === "name" || isOp(toks[i], ".")) && !isWord(toks[i], "as")) {
      if (toks[i].kind === "name") path.push(i);
      i++;
    }
    if (isWord(toks[i], "as") && toks[i + 1]?.kind === "name") {
      path.forEach((at) => roles.set(at, "module"));
      roles.set(i + 1, "plain");
      i += 2;
    } else if (path.length === 1) {
      roles.set(path[0], "import_binding");
    } else if (path.length > 1) {
      roles.set(path[0], "import_dotted");
      path.slice(1).forEach((at) => roles.set(at, "module"));
    }
    if (!end()) i++;
  }
  return i;
}

/** Every name occurrence in `tokens` (comments removed) with its role. */
function classifyNames(toks: readonly Token[]): Map<number, NameRole> {
  const roles = new Map<number, NameRole>();
  const stack: Bracket[] = [];
  for (let i = 0; i < toks.length; i++) {
    const token = toks[i];
    const prev = toks[i - 1];
    if (token.kind === "op") {
      if (token.text === "(" || token.text === "[" || token.text === "{") {
        const call =
          (prev?.kind === "name" && !PYTHON_KEYWORDS.has(prev.text)) || isOp(prev, ")") || isOp(prev, "]");
        stack.push({ char: token.text, call: token.text === "(" && call });
      } else if (token.text === ")" || token.text === "]" || token.text === "}") {
        stack.pop();
      }
      continue;
    }
    if (token.kind !== "name" || roles.has(i)) continue;
    if ((token.text === "import" || token.text === "from") && atStatementStart(prev)) {
      classifyImport(toks, i, roles);
      continue;
    }
    if (PYTHON_KEYWORDS.has(token.text)) continue;
    if (isOp(prev, ".")) {
      roles.set(i, "attribute");
      continue;
    }
    const top = stack[stack.length - 1];
    if (top?.call && isOp(toks[i + 1], "=") && (isOp(prev, "(") || isOp(prev, ","))) {
      roles.set(i, "keyword_arg");
      continue;
    }
    roles.set(i, "plain");
  }
  return roles;
}

export interface RenameResult {
  code: string;
  /** How many occurrences were rewritten. */
  count: number;
}

/** Rename the variable `from` to `to` everywhere it is the variable: not in
 *  strings or comments (f-string fields excepted), not as `obj.from`, not as a
 *  call's `from=` keyword, not as a module path. `import from` becomes
 *  `import from as to`. A dotted `import from.sub` is left alone, so the cell
 *  still defines `from` afterwards; check with `definesName`. */
export function renameIdentifier(code: string, from: string, to: string): RenameResult {
  if (from === to || !IDENTIFIER.test(from)) return { code, count: 0 };
  const toks = significant(tokenize(code));
  const roles = classifyNames(toks);
  const edits: { from: number; to: number; text: string }[] = [];
  roles.forEach((role, index) => {
    const token = toks[index];
    if (token.text !== from) return;
    if (role === "plain") edits.push({ from: token.from, to: token.to, text: to });
    else if (role === "import_binding") edits.push({ from: token.from, to: token.to, text: `${from} as ${to}` });
  });
  edits.sort((a, b) => b.from - a.from);
  let out = code;
  for (const edit of edits) out = out.slice(0, edit.from) + edit.text + out.slice(edit.to);
  return { code: out, count: edits.length };
}

/** Whether `name` occurs in `code` as the variable at all (a read, a write or
 *  a binding), as opposed to only in strings, comments or attributes. */
export function mentionsName(code: string, name: string): boolean {
  const toks = significant(tokenize(code));
  for (const [index, role] of classifyNames(toks)) {
    if (toks[index].text === name && (role === "plain" || role === "import_binding" || role === "import_dotted")) return true;
  }
  return false;
}

// -- what a cell defines ------------------------------------------------------

interface Scope {
  indent: number;
  globals: Set<string>;
}

const COMPOUND = new Set(["if", "elif", "else", "while", "for", "try", "except", "finally", "with", "def", "class"]);

/** Split `toks` at `sep` ops that sit at bracket depth 0. */
function splitTop(toks: readonly Token[], isSep: (token: Token) => boolean): Token[][] {
  const parts: Token[][] = [[]];
  let depth = 0;
  for (const token of toks) {
    if (token.kind === "op" && "([{".includes(token.text)) depth++;
    else if (token.kind === "op" && ")]}".includes(token.text)) depth = Math.max(0, depth - 1);
    if (depth === 0 && isSep(token)) {
      parts.push([]);
      continue;
    }
    parts[parts.length - 1].push(token);
  }
  return parts;
}

/** The names an assignment target binds: `a`, `a, (b, *c)`, `[d, e]`; not
 *  `obj.a`, `a[i]` or anything inside a subscript or call. */
function targetNames(toks: readonly Token[]): string[] {
  const names: string[] = [];
  const stack: boolean[] = [];
  for (let i = 0; i < toks.length; i++) {
    const token = toks[i];
    const prev = toks[i - 1];
    if (token.kind === "op") {
      if ("([{".includes(token.text)) {
        const grouping = !(prev?.kind === "name" && !PYTHON_KEYWORDS.has(prev.text)) && !isOp(prev, ")") && !isOp(prev, "]");
        stack.push(grouping);
      } else if (")]}".includes(token.text)) {
        stack.pop();
      }
      continue;
    }
    if (token.kind !== "name" || PYTHON_KEYWORDS.has(token.text)) continue;
    if (!stack.every(Boolean)) continue;
    if (isOp(prev, ".")) continue;
    const next = toks[i + 1];
    if (isOp(next, ".") || isOp(next, "[") || isOp(next, "(")) continue;
    names.push(token.text);
  }
  return names;
}

function walrusNames(toks: readonly Token[]): string[] {
  const names: string[] = [];
  toks.forEach((token, i) => {
    if (token.kind === "name" && isOp(toks[i + 1], ":=")) names.push(token.text);
  });
  return names;
}

/** Index of the colon ending a compound statement's header. */
function headerColon(toks: readonly Token[]): number {
  let depth = 0;
  let lambdas = 0;
  for (let i = 0; i < toks.length; i++) {
    const token = toks[i];
    if (token.kind === "op" && "([{".includes(token.text)) depth++;
    else if (token.kind === "op" && ")]}".includes(token.text)) depth = Math.max(0, depth - 1);
    else if (depth === 0 && isWord(token, "lambda")) lambdas++;
    else if (depth === 0 && isOp(token, ":")) {
      if (lambdas > 0) lambdas--;
      else return i;
    }
  }
  return -1;
}

/** The names a cell's code binds at module level: assignment, augmented and
 *  annotated targets, `def` and `class` names, import bindings, `for` and
 *  `with ... as` targets, walrus targets, `type` aliases, and names a function
 *  declares `global` and assigns. Function and class bodies are their own
 *  scopes. */
export function definedNames(code: string): Set<string> {
  const toks = significant(tokenize(code));
  const defined = new Set<string>();
  const scopes: Scope[] = [];

  const bind = (name: string): void => {
    const inner = scopes[scopes.length - 1];
    if (!inner || inner.globals.has(name)) defined.add(name);
  };

  const statement = (stmt: Token[], indent: number): void => {
    let s = stmt;
    if (s.length === 0) return;
    if (isWord(s[0], "async")) s = s.slice(1);
    const first = s[0];
    if (!first) return;
    if (isOp(first, "@")) return;
    const soft = (first.text === "match" || first.text === "case") && first.kind === "name";
    const colon = COMPOUND.has(first.text) || soft ? headerColon(s) : -1;
    const compound = first.kind === "name" && (COMPOUND.has(first.text) || (soft && colon > 1 && !s.slice(0, colon).some((t) => isOp(t, "="))));
    if (compound && colon !== -1) {
      const header = s.slice(0, colon);
      const body = s.slice(colon + 1);
      walrusNames(header).forEach(bind);
      if (first.text === "def" || first.text === "class") {
        if (s[1]?.kind === "name") bind(s[1].text);
        scopes.push({ indent, globals: new Set() });
      } else if (first.text === "for") {
        const inAt = header.findIndex((t, i) => i > 0 && isWord(t, "in"));
        targetNames(header.slice(1, inAt === -1 ? header.length : inAt)).forEach(bind);
      } else if (first.text === "with") {
        header.forEach((t, i) => {
          if (!isWord(t, "as")) return;
          const target: Token[] = [];
          let depth = 0;
          for (let j = i + 1; j < header.length; j++) {
            const u = header[j];
            if (u.kind === "op" && "([{".includes(u.text)) depth++;
            if (u.kind === "op" && ")]}".includes(u.text)) {
              if (depth === 0) break;
              depth--;
            }
            if (depth === 0 && isOp(u, ",")) break;
            target.push(u);
          }
          targetNames(target).forEach(bind);
        });
      }
      for (const part of splitTop(body, (t) => isOp(t, ";"))) statement(part, indent + 1);
      return;
    }
    if (first.kind === "name" && (first.text === "global" || first.text === "nonlocal")) {
      const inner = scopes[scopes.length - 1];
      if (first.text === "global") {
        for (const t of s.slice(1)) {
          if (t.kind === "name" && inner) inner.globals.add(t.text);
        }
      }
      return;
    }
    if (isWord(first, "import") || isWord(first, "from")) {
      const roles = new Map<number, NameRole>();
      classifyImport(s, 0, roles);
      roles.forEach((role, i) => {
        if (role === "plain" || role === "import_binding" || role === "import_dotted") bind(s[i].text);
      });
      return;
    }
    walrusNames(s).forEach(bind);
    if (isWord(first, "type") && s[1]?.kind === "name" && (isOp(s[2], "=") || isOp(s[2], "["))) {
      bind(s[1].text);
      return;
    }
    // Targets come before the value, and a lambda only appears in a value:
    // `g = lambda x=1: x` binds `g`, not `x`.
    const lambdaAt = splitTop(s, (t) => isWord(t, "lambda"))[0].length;
    const head = s.slice(0, lambdaAt);
    const parts = splitTop(head, (t) => t.kind === "op" && (t.text === "=" || AUGMENTED.has(t.text)));
    if (parts.length > 1) {
      for (const target of parts.slice(0, -1)) {
        const annotated = splitTop(target, (t) => isOp(t, ":"));
        targetNames(annotated[0]).forEach(bind);
      }
      return;
    }
    const annotation = splitTop(head, (t) => isOp(t, ":"));
    if (annotation.length > 1) targetNames(annotation[0]).forEach(bind);
  };

  let lineStart = 0;
  for (let i = 0; i <= toks.length; i++) {
    if (i < toks.length && toks[i].kind !== "newline") continue;
    const line = toks.slice(lineStart, i);
    lineStart = i + 1;
    if (line.length === 0) continue;
    const before = code.lastIndexOf("\n", line[0].from - 1);
    const indent = line[0].from - (before + 1);
    while (scopes.length > 0 && scopes[scopes.length - 1].indent >= indent) scopes.pop();
    for (const part of splitTop(line, (t) => isOp(t, ";"))) statement(part, indent);
  }
  return defined;
}

/** Whether `code` binds `name` at module level (see `definedNames`). */
export function definesName(code: string, name: string): boolean {
  return definedNames(code).has(name);
}
