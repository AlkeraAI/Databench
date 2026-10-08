// Vega expressions the renderer will evaluate: the same grammar and allowlist
// as the chart profile (`alkera_core.charts.profile`, "Expressions").
//
// Vega-Lite reads a string as an expression in a `calculate`, a `filter`
// written as text and a condition's `test`, and Altair writes all three every
// day. The runtime evaluates them with vega-interpreter over a parsed AST (no
// `eval`) behind a loader that refuses every load, so an expression cannot
// fetch or generate code; this decides what one may read. It tokenizes and
// parses vega-expression's grammar, then walks the tree: literals, `datum`
// fields by name or string key, operators, Vega's constants and its pure
// functions are admitted; any other identifier (Vega would resolve it as a
// signal), member access on anything but `datum` (a numeric index into a value
// excepted), calls on anything but a function name, object and regular
// expression literals, assignment, and every prototype name in any spelling an
// escape can produce are refused.
//
// The Python profile and this module are held to one corpus
// (`packages/api-core/tests/fixtures/charts/profile/expressions`), including
// the allowlist itself. A reason never repeats the expression.

export const MAX_EXPRESSION_CHARS = 1000;
export const MAX_EXPRESSION_DEPTH = 32;
export const MAX_EXPRESSION_ARGS = 8;
export const MAX_PAD_LENGTH = 1000;
const MAX_ARRAY_ITEMS = 64;

export const EXPRESSION_CONSTANTS: ReadonlySet<string> = new Set([
  "NaN",
  "E",
  "LN2",
  "LN10",
  "LOG2E",
  "LOG10E",
  "PI",
  "SQRT1_2",
  "SQRT2",
  "MIN_VALUE",
  "MAX_VALUE",
]);

export const EXPRESSION_FUNCTIONS: ReadonlySet<string> = new Set([
  // control
  "if",
  // type checks and coercion
  "isArray",
  "isBoolean",
  "isDate",
  "isDefined",
  "isNumber",
  "isObject",
  "isRegExp",
  "isString",
  "isValid",
  "toBoolean",
  "toDate",
  "toNumber",
  "toString",
  // math
  "isNaN",
  "isFinite",
  "abs",
  "acos",
  "asin",
  "atan",
  "atan2",
  "ceil",
  "clamp",
  "cos",
  "exp",
  "floor",
  "hypot",
  "log",
  "max",
  "min",
  "pow",
  "random",
  "round",
  "sin",
  "sqrt",
  "tan",
  // statistics
  "cumulativeNormal",
  "cumulativeLogNormal",
  "cumulativeUniform",
  "densityNormal",
  "densityLogNormal",
  "densityUniform",
  "quantileNormal",
  "quantileLogNormal",
  "quantileUniform",
  "sampleNormal",
  "sampleLogNormal",
  "sampleUniform",
  // dates and times
  "now",
  "datetime",
  "date",
  "day",
  "dayofyear",
  "year",
  "quarter",
  "month",
  "week",
  "hours",
  "minutes",
  "seconds",
  "milliseconds",
  "time",
  "timezoneoffset",
  "timeOffset",
  "utc",
  "utcdate",
  "utcday",
  "utcdayofyear",
  "utcyear",
  "utcquarter",
  "utcmonth",
  "utcweek",
  "utchours",
  "utcminutes",
  "utcseconds",
  "utcmilliseconds",
  "utcOffset",
  // strings and arrays
  "length",
  "lower",
  "upper",
  "substring",
  "slice",
  "split",
  "join",
  "trim",
  "truncate",
  "replace",
  "pad",
  "indexof",
  "lastindexof",
  "reverse",
  "sort",
  "extent",
  "clampRange",
  "inrange",
  "lerp",
  "peek",
  "span",
  "parseFloat",
  "parseInt",
  // formatting and parsing
  "format",
  "timeFormat",
  "timeParse",
  "utcFormat",
  "utcParse",
  "monthFormat",
  "monthAbbrevFormat",
  "dayFormat",
  "dayAbbrevFormat",
  "timeUnitSpecifier",
]);

/** Property names that reach an object's prototype: never a datum key and
 *  never a string literal, however an escape spells them. */
const FORBIDDEN_NAMES: ReadonlySet<string> = new Set([
  "__proto__",
  "constructor",
  "prototype",
  "__defineGetter__",
  "__defineSetter__",
  "__lookupGetter__",
  "__lookupSetter__",
]);

const EXACT_ARITY: ReadonlyMap<string, number> = new Map([
  ["if", 3],
  ["clamp", 3],
]);

const PUNCTUATORS = [
  ">>>", "===", "!==", "<<", ">>", "<=", ">=", "==", "!=", "&&", "||", "++", "--",
  "+", "-", "*", "/", "%", "<", ">", "!", "~", "&", "|", "^", "?", ":", ",", ".", "(", ")", "[", "]",
];

const BINARY_PRECEDENCE: ReadonlyMap<string, number> = new Map([
  ["||", 1],
  ["&&", 2],
  ["|", 3],
  ["^", 4],
  ["&", 5],
  ["==", 6],
  ["!=", 6],
  ["===", 6],
  ["!==", 6],
  ["<", 7],
  [">", 7],
  ["<=", 7],
  [">=", 7],
  ["<<", 8],
  [">>", 8],
  [">>>", 8],
  ["+", 9],
  ["-", 9],
  ["*", 11],
  ["/", 11],
  ["%", 11],
]);

const UNARY = new Set(["+", "-", "!", "~"]);
const SIMPLE_ESCAPES: ReadonlyMap<string, string> = new Map([
  ["\\", "\\"],
  ["'", "'"],
  ['"', '"'],
  ["n", "\n"],
  ["r", "\r"],
  ["t", "\t"],
  ["b", "\b"],
  ["f", "\f"],
  ["v", "\v"],
]);

const isDigit = (ch: string | undefined): boolean => ch !== undefined && ch >= "0" && ch <= "9";
const isHex = (ch: string | undefined): boolean => ch !== undefined && /^[0-9a-fA-F]$/.test(ch);
const isIdStart = (ch: string | undefined): boolean => ch !== undefined && /^[A-Za-z_$]$/.test(ch);
const isIdPart = (ch: string | undefined): boolean => isIdStart(ch) || isDigit(ch);
const SPACE = new Set([" ", "\t", "\n", "\r", "\f", "\v"]);
const LINE_BREAKS = new Set(["\n", "\r", "\u2028", "\u2029"]);

const MALFORMED = "is not an expression the chart profile can read";

class ExpressionRefusal extends Error {}

type Token =
  | { kind: "num"; value: number }
  | { kind: "str"; value: string }
  | { kind: "id"; value: string }
  | { kind: "punc"; value: string }
  | { kind: "end" };

type Node =
  | { kind: "num"; value: number }
  | { kind: "str"; value: string }
  | { kind: "lit" }
  | { kind: "id"; name: string }
  | { kind: "member"; object: Node; property: Node; computed: boolean }
  | { kind: "call"; name: string; args: Node[] }
  | { kind: "array"; items: Node[] }
  | { kind: "unary"; argument: Node }
  | { kind: "binary"; left: Node; right: Node }
  | { kind: "cond"; test: Node; yes: Node; no: Node };

function scanNumber(text: string, start: number): [number, number] {
  let j = start;
  let value: number;
  if (text[j] === "0" && (text[j + 1] === "x" || text[j + 1] === "X")) {
    j += 2;
    while (isHex(text[j])) j++;
    if (j === start + 2) throw new ExpressionRefusal(MALFORMED);
    value = parseInt(text.slice(start + 2, j), 16);
  } else {
    while (isDigit(text[j])) j++;
    if (j - start > 1 && text[start] === "0") throw new ExpressionRefusal("writes a number with a leading zero");
    if (text[j] === ".") {
      j++;
      while (isDigit(text[j])) j++;
    }
    if (text[j] === "e" || text[j] === "E") {
      let k = j + 1;
      if (text[k] === "+" || text[k] === "-") k++;
      if (!isDigit(text[k])) throw new ExpressionRefusal(MALFORMED);
      while (isDigit(text[k])) k++;
      j = k;
    }
    value = Number(text.slice(start, j));
  }
  if (isIdPart(text[j])) throw new ExpressionRefusal(MALFORMED);
  return [j, value];
}

function scanHex(text: string, at: number, count: number): number {
  const digits = text.slice(at, at + count);
  if (digits.length !== count || ![...digits].every(isHex)) throw new ExpressionRefusal(MALFORMED);
  return parseInt(digits, 16);
}

function scanString(text: string, start: number): [number, string] {
  const quote = text[start];
  let out = "";
  let j = start + 1;
  for (;;) {
    if (j >= text.length) throw new ExpressionRefusal(MALFORMED);
    const ch = text[j];
    if (ch === quote) return [j + 1, out];
    if (LINE_BREAKS.has(ch)) throw new ExpressionRefusal(MALFORMED);
    if (ch !== "\\") {
      out += ch;
      j++;
      continue;
    }
    j++;
    if (j >= text.length) throw new ExpressionRefusal(MALFORMED);
    const esc = text[j];
    const simple = SIMPLE_ESCAPES.get(esc);
    if (simple !== undefined) {
      out += simple;
      j++;
    } else if (esc === "0" && !isDigit(text[j + 1])) {
      out += "\0";
      j++;
    } else if (esc === "x") {
      out += String.fromCharCode(scanHex(text, j + 1, 2));
      j += 3;
    } else if (esc === "u" && text[j + 1] === "{") {
      const end = text.indexOf("}", j + 2);
      const digits = end === -1 ? "" : text.slice(j + 2, end);
      if (digits.length < 1 || digits.length > 6 || ![...digits].every(isHex)) throw new ExpressionRefusal(MALFORMED);
      const code = parseInt(digits, 16);
      if (code > 0x10ffff) throw new ExpressionRefusal(MALFORMED);
      out += String.fromCodePoint(code);
      j = end + 1;
    } else if (esc === "u") {
      out += String.fromCharCode(scanHex(text, j + 1, 4));
      j += 5;
    } else {
      // Octal escapes, line continuations and identity escapes are refused
      // rather than decoded one way here and another in Vega.
      throw new ExpressionRefusal(MALFORMED);
    }
  }
}

function tokenize(text: string): Token[] {
  const tokens: Token[] = [];
  let i = 0;
  while (i < text.length) {
    const ch = text[i];
    if (SPACE.has(ch)) {
      i++;
    } else if (isIdStart(ch)) {
      let j = i + 1;
      while (isIdPart(text[j])) j++;
      tokens.push({ kind: "id", value: text.slice(i, j) });
      i = j;
    } else if (isDigit(ch) || (ch === "." && isDigit(text[i + 1]))) {
      const [next, value] = scanNumber(text, i);
      tokens.push({ kind: "num", value });
      i = next;
    } else if (ch === "'" || ch === '"') {
      const [next, value] = scanString(text, i);
      tokens.push({ kind: "str", value });
      i = next;
    } else {
      const punctuator = PUNCTUATORS.find((p) => text.startsWith(p, i));
      if (punctuator === undefined) throw new ExpressionRefusal(MALFORMED);
      tokens.push({ kind: "punc", value: punctuator });
      i += punctuator.length;
    }
  }
  tokens.push({ kind: "end" });
  return tokens;
}

/** Recursive descent over vega-expression's grammar. */
class Parser {
  private pos = 0;
  private depth = 0;

  constructor(private readonly tokens: Token[]) {}

  private peek(): Token {
    return this.tokens[this.pos];
  }

  private at(punctuator: string): boolean {
    const token = this.peek();
    return token.kind === "punc" && token.value === punctuator;
  }

  private expect(punctuator: string): void {
    if (!this.at(punctuator)) throw new ExpressionRefusal(MALFORMED);
    this.pos++;
  }

  private enter(): void {
    this.depth++;
    if (this.depth > MAX_EXPRESSION_DEPTH) throw new ExpressionRefusal(`nests deeper than ${MAX_EXPRESSION_DEPTH} levels`);
  }

  parse(): Node {
    const node = this.conditional();
    if (this.peek().kind !== "end") throw new ExpressionRefusal(MALFORMED);
    return node;
  }

  private conditional(): Node {
    this.enter();
    let node = this.binary(1);
    if (this.at("?")) {
      this.pos++;
      const yes = this.conditional();
      this.expect(":");
      const no = this.conditional();
      node = { kind: "cond", test: node, yes, no };
    }
    this.depth--;
    return node;
  }

  private binary(minPrecedence: number): Node {
    let left = this.unary();
    for (;;) {
      const token = this.peek();
      const precedence = token.kind === "punc" ? BINARY_PRECEDENCE.get(token.value) : undefined;
      if (precedence === undefined || precedence < minPrecedence) return left;
      this.pos++;
      const right = this.binary(precedence + 1);
      left = { kind: "binary", left, right };
    }
  }

  private unary(): Node {
    const token = this.peek();
    if (token.kind === "punc" && UNARY.has(token.value)) {
      this.pos++;
      this.enter();
      const argument = this.unary();
      this.depth--;
      return { kind: "unary", argument };
    }
    return this.postfix();
  }

  private list(close: string): Node[] {
    const items: Node[] = [];
    if (this.at(close)) {
      this.pos++;
      return items;
    }
    for (;;) {
      items.push(this.conditional());
      if (this.at(",")) {
        this.pos++;
        continue;
      }
      this.expect(close);
      return items;
    }
  }

  private postfix(): Node {
    let node = this.primary();
    for (;;) {
      if (this.at(".")) {
        this.pos++;
        const token = this.peek();
        if (token.kind !== "id") throw new ExpressionRefusal(MALFORMED);
        this.pos++;
        node = { kind: "member", object: node, property: { kind: "str", value: token.value }, computed: false };
      } else if (this.at("[")) {
        this.pos++;
        const property = this.conditional();
        this.expect("]");
        node = { kind: "member", object: node, property, computed: true };
      } else if (this.at("(")) {
        if (node.kind !== "id") throw new ExpressionRefusal("calls something that is not a function name");
        this.pos++;
        const args = this.list(")");
        if (args.length > MAX_EXPRESSION_ARGS) {
          throw new ExpressionRefusal(`passes more than ${MAX_EXPRESSION_ARGS} arguments to a function`);
        }
        node = { kind: "call", name: node.name, args };
      } else {
        return node;
      }
    }
  }

  private primary(): Node {
    const token = this.peek();
    this.pos++;
    if (token.kind === "num" || token.kind === "str") return token;
    if (token.kind === "id") {
      if (token.value === "true" || token.value === "false" || token.value === "null") return { kind: "lit" };
      return { kind: "id", name: token.value };
    }
    if (token.kind === "punc" && token.value === "(") {
      const node = this.conditional();
      this.expect(")");
      return node;
    }
    if (token.kind === "punc" && token.value === "[") {
      const items = this.list("]");
      if (items.length > MAX_ARRAY_ITEMS) throw new ExpressionRefusal(`writes an array of more than ${MAX_ARRAY_ITEMS} items`);
      return { kind: "array", items };
    }
    throw new ExpressionRefusal(MALFORMED);
  }
}

const isIndex = (node: Node): node is { kind: "num"; value: number } =>
  node.kind === "num" && node.value >= 0 && Number.isInteger(node.value);

function checkTree(root: Node): string[] {
  const fields: string[] = [];
  const stack: Node[] = [root];
  while (stack.length > 0) {
    const node = stack.pop() as Node;
    switch (node.kind) {
      case "num":
      case "lit":
        break;
      case "str":
        if (FORBIDDEN_NAMES.has(node.value)) throw new ExpressionRefusal("names a property no expression may read");
        break;
      case "id":
        if (node.name === "datum") throw new ExpressionRefusal("reads the whole row; read one field by name");
        if (!EXPRESSION_CONSTANTS.has(node.name)) throw new ExpressionRefusal("reads a name outside the expression allowlist");
        break;
      case "member": {
        const { object, property, computed } = node;
        if (object.kind === "id" && object.name === "datum") {
          if (property.kind !== "str") throw new ExpressionRefusal("reads a row field by a computed key; name it");
          if (!property.value || FORBIDDEN_NAMES.has(property.value)) {
            throw new ExpressionRefusal("names a property no expression may read");
          }
          fields.push(property.value);
        } else if (computed && isIndex(property)) {
          stack.push(object);
        } else {
          throw new ExpressionRefusal("reads a property of something other than the row");
        }
        break;
      }
      case "call": {
        if (!EXPRESSION_FUNCTIONS.has(node.name)) throw new ExpressionRefusal("calls a function outside the expression allowlist");
        const arity = EXACT_ARITY.get(node.name);
        if (arity !== undefined && node.args.length !== arity) {
          throw new ExpressionRefusal(`calls a function that takes ${arity} arguments`);
        }
        if (node.name === "pad") {
          const length = node.args[1];
          if (length === undefined || !isIndex(length) || length.value > MAX_PAD_LENGTH) {
            throw new ExpressionRefusal(`pads to a length that is not a number of at most ${MAX_PAD_LENGTH}`);
          }
        }
        stack.push(...node.args);
        break;
      }
      case "array":
        stack.push(...node.items);
        break;
      case "unary":
        stack.push(node.argument);
        break;
      case "binary":
        stack.push(node.left, node.right);
        break;
      case "cond":
        stack.push(node.test, node.yes, node.no);
        break;
    }
  }
  return fields;
}

export type ExpressionVerdict = { ok: true; fields: string[] } | { ok: false; reason: string };

/** Whether `text` is an expression the chart profile admits, and the row
 *  fields it reads (sorted, each once). */
export function checkExpression(text: unknown): ExpressionVerdict {
  if (typeof text !== "string") return { ok: false, reason: "must be an expression string" };
  if (text.length > MAX_EXPRESSION_CHARS) return { ok: false, reason: `is longer than ${MAX_EXPRESSION_CHARS} characters` };
  try {
    const tokens = tokenize(text);
    if (tokens.length === 1) return { ok: false, reason: "is empty" };
    const fields = checkTree(new Parser(tokens).parse());
    return { ok: true, fields: [...new Set(fields)].sort() };
  } catch (error) {
    if (error instanceof ExpressionRefusal) return { ok: false, reason: error.message };
    throw error;
  }
}
