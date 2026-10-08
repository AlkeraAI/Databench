// An interpreter for Vega expressions over vega-expression's parse tree: the
// language ipydatagrid compiles with Function(), evaluated without eval. It
// refuses the three member names that reach a prototype or a constructor.
import { parseExpression } from "vega-expression";

export interface ExprNode {
  type: string;
  value?: unknown;
  name?: string;
  operator?: string;
  computed?: boolean;
  object?: ExprNode;
  property?: ExprNode;
  argument?: ExprNode;
  left?: ExprNode;
  right?: ExprNode;
  test?: ExprNode;
  consequent?: ExprNode;
  alternate?: ExprNode;
  elements?: ExprNode[];
  properties?: { key: ExprNode; value: ExprNode }[];
  callee?: ExprNode;
  arguments?: ExprNode[];
  expressions?: ExprNode[];
}

export interface ExprEnv {
  vars: Record<string, unknown>;
  functions?: Record<string, unknown>;
}

const FORBIDDEN = new Set(["__proto__", "constructor", "prototype"]);

const CONSTANTS: Record<string, number> = {
  NaN: NaN,
  E: Math.E,
  LN2: Math.LN2,
  LN10: Math.LN10,
  LOG2E: Math.LOG2E,
  LOG10E: Math.LOG10E,
  PI: Math.PI,
  SQRT1_2: Math.SQRT1_2,
  SQRT2: Math.SQRT2,
  MIN_VALUE: Number.MIN_VALUE,
  MAX_VALUE: Number.MAX_VALUE,
};

type Num = number;
const BINARY: Record<string, (a: unknown, b: unknown) => unknown> = {
  "+": (a, b) => (a as Num) + (b as Num),
  "-": (a, b) => (a as Num) - (b as Num),
  "*": (a, b) => (a as Num) * (b as Num),
  "/": (a, b) => (a as Num) / (b as Num),
  "%": (a, b) => (a as Num) % (b as Num),
  "==": (a, b) => a == b,
  "!=": (a, b) => a != b,
  "===": (a, b) => a === b,
  "!==": (a, b) => a !== b,
  "<": (a, b) => (a as Num) < (b as Num),
  "<=": (a, b) => (a as Num) <= (b as Num),
  ">": (a, b) => (a as Num) > (b as Num),
  ">=": (a, b) => (a as Num) >= (b as Num),
  "&": (a, b) => (a as Num) & (b as Num),
  "|": (a, b) => (a as Num) | (b as Num),
  "^": (a, b) => (a as Num) ^ (b as Num),
  "<<": (a, b) => (a as Num) << (b as Num),
  ">>": (a, b) => (a as Num) >> (b as Num),
  ">>>": (a, b) => (a as Num) >>> (b as Num),
};
const UNARY: Record<string, (a: unknown) => unknown> = {
  "-": (a) => -(a as Num),
  "+": (a) => +(a as Num),
  "!": (a) => !a,
  "~": (a) => ~(a as Num),
};

function memberKey(node: ExprNode, env: ExprEnv): string | number {
  // vega-expression parses the literal in `x["__proto__"]` as an identifier.
  const property = node.property as ExprNode;
  if (property.type === "Identifier" && FORBIDDEN.has(property.name as string)) {
    throw new Error(`the expression may not use ${property.name}`);
  }
  const key = node.computed ? interpret(node.property as ExprNode, env) : (node.property as ExprNode).name;
  const text = String(key);
  if (FORBIDDEN.has(text)) throw new Error(`the expression may not use ${text}`);
  return key as string | number;
}

function isPath(node: ExprNode, path: string[]): boolean {
  let current: ExprNode = node;
  for (let i = path.length - 1; i > 0; i--) {
    if (current.type !== "MemberExpression" || current.computed || current.property?.name !== path[i]) return false;
    current = current.object as ExprNode;
  }
  return current.type === "Identifier" && current.name === path[0];
}

export function interpret(node: ExprNode, env: ExprEnv): unknown {
  const ev = (n: ExprNode | undefined): unknown => interpret(n as ExprNode, env);
  switch (node.type) {
    case "Literal":
      return node.value;
    case "Identifier": {
      const name = node.name as string;
      if (Object.prototype.hasOwnProperty.call(env.vars, name)) return env.vars[name];
      if (Object.prototype.hasOwnProperty.call(CONSTANTS, name)) return CONSTANTS[name];
      throw new Error(`unknown identifier ${name}`);
    }
    case "MemberExpression": {
      // ipydatagrid's own codegen turns cell.metadata.data[col] into a call.
      if (node.computed && isPath(node.object as ExprNode, ["cell", "metadata", "data"])) {
        const cell = env.vars.cell as { row: unknown; metadata: { data: (row: unknown, col: unknown) => unknown } };
        return cell.metadata.data(cell.row, ev(node.property));
      }
      const target = ev(node.object) as Record<string | number, unknown> | null | undefined;
      const key = memberKey(node, env);
      return target == null ? undefined : target[key];
    }
    case "UnaryExpression": {
      const op = UNARY[node.operator as string];
      if (!op) throw new Error(`unsupported operator ${node.operator}`);
      return op(ev(node.argument));
    }
    case "BinaryExpression": {
      const op = BINARY[node.operator as string];
      if (!op) throw new Error(`unsupported operator ${node.operator}`);
      return op(ev(node.left), ev(node.right));
    }
    case "LogicalExpression":
      return node.operator === "&&" ? ev(node.left) && ev(node.right) : ev(node.left) || ev(node.right);
    case "ConditionalExpression":
      return ev(node.test) ? ev(node.consequent) : ev(node.alternate);
    case "ArrayExpression":
      return (node.elements ?? []).map(ev);
    case "ObjectExpression": {
      const out: Record<string, unknown> = {};
      for (const p of node.properties ?? []) {
        const key = p.key.type === "Identifier" ? (p.key.name as string) : String(p.key.value);
        if (FORBIDDEN.has(key)) throw new Error(`the expression may not use ${key}`);
        out[key] = ev(p.value);
      }
      return out;
    }
    case "SequenceExpression": {
      let last: unknown;
      for (const e of node.expressions ?? []) last = ev(e);
      return last;
    }
    case "CallExpression": {
      const callee = node.callee as ExprNode;
      const args = node.arguments ?? [];
      if (callee.type === "Identifier") {
        const name = callee.name as string;
        if (name === "if") return ev(args[0]) ? ev(args[1]) : ev(args[2]);
        const table = env.functions ?? {};
        const fn = Object.prototype.hasOwnProperty.call(table, name)
          ? table[name]
          : typeof (Math as unknown as Record<string, unknown>)[name] === "function"
            ? (Math as unknown as Record<string, unknown>)[name]
            : null;
        if (typeof fn !== "function") throw new Error(`unknown function ${name}`);
        return (fn as (...a: unknown[]) => unknown).apply(env.functions, args.map(ev));
      }
      if (callee.type !== "MemberExpression") throw new Error("unsupported call");
      const target = ev(callee.object) as Record<string | number, unknown>;
      const key = memberKey(callee, env);
      const method = target?.[key];
      if (typeof method !== "function") throw new Error(`${String(key)} is not a function`);
      return (method as (...a: unknown[]) => unknown).apply(target, args.map(ev));
    }
    default:
      throw new Error(`unsupported expression ${node.type}`);
  }
}

/** Parses once; the returned function evaluates the tree per call. */
export function compileExpression(source: string): (env: ExprEnv) => unknown {
  const ast = parseExpression(source) as unknown as ExprNode;
  return (env) => interpret(ast, env);
}
