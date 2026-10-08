import { describe, expect, it } from "vitest";
import { buildFilterSql, cellDisplayText, cellText, containsPattern, filterRowsLocally, quoteIdent, quoteLiteral, shortColumnType, sortRowsLocally, tableToCsv } from "./tableQuery";

type Token = { kind: "ident" | "string" | "word" | "punct"; value: string };

/** A small SQL lexer: double-quoted identifiers and single-quoted strings
 *  with doubled-quote escapes, words, and single punctuation. Throws on an
 *  unterminated quote, which is what a broken-out literal looks like. */
function lex(sql: string): Token[] {
  const tokens: Token[] = [];
  let i = 0;
  while (i < sql.length) {
    const ch = sql[i];
    if (/\s/.test(ch)) {
      i += 1;
    } else if (ch === '"' || ch === "'") {
      let value = "";
      let j = i + 1;
      for (;;) {
        if (j >= sql.length) throw new Error(`unterminated ${ch} at ${i}`);
        if (sql[j] === ch) {
          if (sql[j + 1] === ch) {
            value += ch;
            j += 2;
            continue;
          }
          break;
        }
        value += sql[j];
        j += 1;
      }
      tokens.push({ kind: ch === '"' ? "ident" : "string", value });
      i = j + 1;
    } else if (/\w/.test(ch)) {
      let j = i;
      while (j < sql.length && /\w/.test(sql[j])) j += 1;
      tokens.push({ kind: "word", value: sql.slice(i, j).toUpperCase() });
      i = j;
    } else {
      tokens.push({ kind: "punct", value: ch });
      i += 1;
    }
  }
  return tokens;
}

/** The token shape of one "column contains term" clause. */
function clause(column: string, pattern: string): Token[] {
  return [
    { kind: "word", value: "CAST" },
    { kind: "punct", value: "(" },
    { kind: "ident", value: column },
    { kind: "word", value: "AS" },
    { kind: "word", value: "VARCHAR" },
    { kind: "punct", value: ")" },
    { kind: "word", value: "ILIKE" },
    { kind: "string", value: pattern },
    { kind: "word", value: "ESCAPE" },
    { kind: "string", value: "\\" },
  ];
}

describe("SQL quoting", () => {
  it.each([
    ["plain", "amount", '"amount"'],
    ["a double quote", 'we"ird', '"we""ird"'],
    ["a breakout attempt", 'a" OR 1=1 --', '"a"" OR 1=1 --"'],
  ])("quotes an identifier with %s", (_name, input, expected) => {
    expect(quoteIdent(input)).toBe(expected);
  });

  it.each([
    ["plain", "x", "'x'"],
    ["a single quote", "o'brien", "'o''brien'"],
    ["a breakout attempt", "x' OR '1'='1", "'x'' OR ''1''=''1'"],
    ["a NUL byte", "a\u0000b", "'ab'"],
  ])("quotes a literal with %s", (_name, input, expected) => {
    expect(quoteLiteral(input)).toBe(expected);
  });

  it("escapes LIKE wildcards and the escape character", () => {
    expect(containsPattern("50%_off\\")).toBe("%50\\%\\_off\\\\%");
  });
});

/** Every filter is one SELECT over the inspection's table, `frame`. */
const SELECT = () => lex("SELECT * FROM frame WHERE");

describe("buildFilterSql", () => {
  it("returns null for an empty filter or an unknown column", () => {
    expect(buildFilterSql("   ", ["a"])).toBeNull();
    expect(buildFilterSql("x", ["a"], "missing")).toBeNull();
    expect(buildFilterSql("x", [])).toBeNull();
  });

  it("filters one column", () => {
    expect(lex(buildFilterSql("north", ["region", "amount"], "region")!)).toEqual([...SELECT(), ...clause("region", "%north%")]);
  });

  it("ORs every column", () => {
    const tokens = lex(buildFilterSql("x", ["a", "b"])!);
    expect(tokens).toEqual([...SELECT(), ...clause("a", "%x%"), { kind: "word", value: "OR" }, ...clause("b", "%x%")]);
  });

  it.each([
    ["quote breakout in the term", "x' OR 1=1 --", ["c"]],
    ["semicolon and a second statement", "'; DROP TABLE t; --", ["c"]],
    ["hostile column names", "x", ['a" IS NOT NULL OR "b', "c'd"]],
    ["quotes everywhere", `"'"'`, [`'"`, `"'`]],
  ])("keeps %s inside its literal or identifier", (_name, term, columns) => {
    const sql = buildFilterSql(term, columns)!;
    const tokens = lex(sql);
    const expected = [
      ...SELECT(),
      ...columns.flatMap((column, i) => [
        ...(i > 0 ? [{ kind: "word", value: "OR" } as Token] : []),
        ...clause(column, containsPattern(term.trim())),
      ]),
    ];
    expect(tokens).toEqual(expected);
  });
});

describe("local sort and filter", () => {
  const rows = [
    { name: "beta", n: 10 },
    { name: "Alpha", n: 2 },
    { name: null, n: 5 },
    { name: "gamma", n: null },
  ];

  it("sorts numbers numerically both ways with nulls last", () => {
    expect(sortRowsLocally(rows, { column: "n", descending: false }).map((r) => r.n)).toEqual([2, 5, 10, null]);
    expect(sortRowsLocally(rows, { column: "n", descending: true }).map((r) => r.n)).toEqual([10, 5, 2, null]);
  });

  it("sorts text naturally with nulls last", () => {
    expect(sortRowsLocally(rows, { column: "name", descending: false }).map((r) => r.name)).toEqual(["Alpha", "beta", "gamma", null]);
  });

  it("leaves order alone without a sort and never mutates", () => {
    const copy = [...rows];
    expect(sortRowsLocally(rows, null)).toEqual(rows);
    sortRowsLocally(rows, { column: "n", descending: true });
    expect(rows).toEqual(copy);
  });

  it("filters case-insensitively, on one column or all", () => {
    expect(filterRowsLocally(rows, "ALP", ["name", "n"]).map((r) => r.name)).toEqual(["Alpha"]);
    expect(filterRowsLocally(rows, "5", ["name", "n"]).map((r) => r.n)).toEqual([5]);
    expect(filterRowsLocally(rows, "5", ["name", "n"], "name")).toEqual([]);
  });
});

describe("tableToCsv", () => {
  const fields = [
    { name: "plain", type: "str" },
    { name: "needs,quote", type: "str" },
  ];

  it.each([
    ["plain", "a", "a"],
    ["a comma", "a,b", '"a,b"'],
    ["a quote", 'say "hi"', '"say ""hi"""'],
    ["a newline", "l1\nl2", '"l1\nl2"'],
    ["a carriage return", "l1\rl2", '"l1\rl2"'],
    ["null", null, ""],
    ["a number", 3.5, "3.5"],
    ["an object", { a: [1, 2] }, '"{""a"":[1,2]}"'],
  ])("writes %s", (_name, value, cell) => {
    expect(tableToCsv(fields, [{ plain: value, "needs,quote": "x" }])).toBe(`plain,"needs,quote"\r\n${cell},x\r\n`);
  });

  it("writes the header alone for no rows", () => {
    expect(tableToCsv(fields, [])).toBe('plain,"needs,quote"\r\n');
  });
});

describe("what a table shows", () => {
  it("rounds a float's binary noise for display and keeps the exact value for copying", () => {
    expect(cellDisplayText(10267.249999999995)).toBe("10267.25");
    expect(cellText(10267.249999999995)).toBe("10267.249999999995");
    expect(cellDisplayText(0.1 + 0.2)).toBe("0.3");
  });

  it("leaves integers, strings and other values as they are", () => {
    expect(cellDisplayText(8976)).toBe("8976");
    expect(cellDisplayText("chatgpt")).toBe("chatgpt");
    expect(cellDisplayText(Number.NaN)).toBe("NaN");
  });

  it("shortens a column type by dropping its parameter names", () => {
    expect(shortColumnType("Decimal(precision=4, scale=2)")).toBe("Decimal(4, 2)");
    expect(shortColumnType("Datetime(time_unit='us', time_zone=None)")).toBe("Datetime('us', None)");
    expect(shortColumnType("Float64")).toBe("Float64");
  });
});
