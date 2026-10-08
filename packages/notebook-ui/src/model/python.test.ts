import { definedNames, definesName, isIdentifier, mentionsName, renameIdentifier, tokenize } from "./python";

describe("renameIdentifier", () => {
  it.each([
    ["a plain read", "y = df + 1", "y = data + 1", 1],
    ["an assignment target", "df = load()", "data = load()", 1],
    ["every occurrence", "df = 1\nprint(df, df)", "data = 1\nprint(data, data)", 3],
    ["a call's value, not its keyword", "f(df=df)", "f(df=data)", 1],
    ["a keyword after a positional", "f(1, df=2, x=df)", "f(1, df=2, x=data)", 1],
    ["a class keyword", "class A(B, df=df): pass", "class A(B, df=data): pass", 1],
    ["a subscript base", "df[0] = df['a']", "data[0] = data['a']", 2],
    ["an attribute's base, not the attribute", "df.df = obj.df", "data.df = obj.df", 1],
    ["an attribute across a line break in brackets", "(obj\n  .df)", "(obj\n  .df)", 0],
    ["a dict key and value", "{df: df}", "{data: data}", 2],
    ["a parenthesized tuple, which is not a call", "x = (df=1)", "x = (data=1)", 1],
    ["a comparison inside a call", "f(df == 1)", "f(data == 1)", 1],
    ["a walrus inside a call", "f(df := 1)", "f(data := 1)", 1],
    ["a def's name", "def df(): pass", "def data(): pass", 1],
    ["a global line", "def f():\n    global df\n    df = 1", "def f():\n    global data\n    data = 1", 2],
    ["a nonlocal line", "def f():\n    nonlocal df\n    df += 1", "def f():\n    nonlocal data\n    data += 1", 2],
    ["a decorator", "@df\ndef g(): pass", "@data\ndef g(): pass", 1],
    ["a whole word only", "dfx = df_ + xdf", "dfx = df_ + xdf", 0],
    ["a Unicode neighbour", "dfé = df", "dfé = data", 1],
  ])("renames %s", (_label, code, expected, count) => {
    expect(renameIdentifier(code, "df", "data")).toEqual({ code: expected, count });
  });

  it.each([
    ["a comment", "x = 1  # df is here", 0],
    ["a single-quoted string", "x = 'df'", 0],
    ["a double-quoted string", 'x = "df"', 0],
    ["a triple-quoted string", 'x = """\ndf\n"""', 0],
    ["a triple single-quoted string", "x = '''df ' df'''", 0],
    ["an escaped quote", 'x = "a\\" df"', 0],
    ["a raw string ending in an escaped quote", 'x = r"\\" df"', 0],
    ["a bytes string", "x = b'df'", 0],
    ["a raw bytes string", "x = Rb'df' + bR'df'", 0],
    ["a unicode string", "x = u'df'", 0],
    ["an f-string's literal text", "x = f'df {1}'", 0],
    ["a doubled brace", "x = f'{{df}}'", 0],
    ["a named Unicode escape", "x = f'\\N{df}'", 0],
  ])("does not rename inside %s", (_label, code, count) => {
    expect(renameIdentifier(code, "df", "data")).toEqual({ code, count });
  });

  it.each([
    ["a field", "f'{df}'", "f'{data}'"],
    ["a field among literal text", "f'n={df} rows'", "f'n={data} rows'"],
    ["an upper-case prefix", "F'{df}'", "F'{data}'"],
    ["a raw f-string", "rf'\\d{df}' + Fr'{df}'", "rf'\\d{data}' + Fr'{data}'"],
    ["a template string", "t'{df}'", "t'{data}'"],
    ["a triple-quoted f-string", 'f"""\n{df}\n"""', 'f"""\n{data}\n"""'],
    ["a field next to doubled braces", "f'{{{df}}}'", "f'{{{data}}}'"],
    ["a field with a conversion", "f'{df!r}'", "f'{data!r}'"],
    ["a field with a format spec", "f'{df:>10}'", "f'{data:>10}'"],
    ["a nested field in a format spec", "f'{x:{df}}'", "f'{x:{data}}'"],
    ["a self-documenting field", "f'{df=}'", "f'{data=}'"],
    ["a field with an inner string", "f'{df[\"df\"]}'", "f'{data[\"df\"]}'"],
    ["a field with a dict", "f'{ {df: 1}[df] }'", "f'{ {data: 1}[data] }'"],
    ["a comparison field", "f'{df != 1}'", "f'{data != 1}'"],
    ["a brace after a backslash", "f'\\{df}'", "f'\\{data}'"],
  ])("renames inside an f-string's %s", (_label, code, expected) => {
    const result = renameIdentifier(code, "df", "data");
    expect(result.code).toBe(expected);
    expect(result.count).toBeGreaterThan(0);
  });

  it("does not rename an attribute inside an f-string field", () => {
    expect(renameIdentifier("f'{obj.df}'", "df", "data")).toEqual({ code: "f'{obj.df}'", count: 0 });
  });

  it("does not rename a keyword argument inside an f-string field", () => {
    expect(renameIdentifier("f'{g(df=df)}'", "df", "data")).toEqual({ code: "f'{g(df=data)}'", count: 1 });
  });

  it("keeps going after an unterminated single-quoted string", () => {
    expect(renameIdentifier("x = 'df\ny = df", "df", "data").code).toBe("x = 'df\ny = data");
  });

  it.each([
    ["import df", "import df as data"],
    ["from m import df", "from m import df as data"],
    ["from m import (a, df)", "from m import (a, df as data)"],
    ["import a, df", "import a, df as data"],
    ["import m as df", "import m as data"],
    ["from m import x as df", "from m import x as data"],
    ["from df import x", "from df import x"],
    ["import df.sub as y", "import df.sub as y"],
    ["from m import df as y", "from m import df as y"],
    ["from .df import x", "from .df import x"],
    ["import a.df", "import a.df"],
  ])("handles the import %s", (code, expected) => {
    expect(renameIdentifier(code, "df", "data").code).toBe(expected);
  });

  it("leaves a dotted import binding alone, which still defines the name", () => {
    const result = renameIdentifier("import df.sub", "df", "data");
    expect(result).toEqual({ code: "import df.sub", count: 0 });
    expect(definesName(result.code, "df")).toBe(true);
  });

  it("does not take `yield from` or `raise from` for an import", () => {
    expect(renameIdentifier("def g():\n    yield from df", "df", "data").code).toBe("def g():\n    yield from data");
    expect(renameIdentifier("raise E from df", "df", "data").code).toBe("raise E from data");
  });

  it.each([
    ["the same name", "df", "df"],
    ["a source that is not an identifier", "1df", "x"],
  ])("does nothing for %s", (_label, from, to) => {
    expect(renameIdentifier("df = 1", from, to)).toEqual({ code: "df = 1", count: 0 });
  });

  it("never renames a keyword", () => {
    expect(renameIdentifier("for x in y: pass", "in", "into")).toEqual({ code: "for x in y: pass", count: 0 });
  });

  it("keeps offsets right past astral characters", () => {
    expect(renameIdentifier("s = '𝔘'; df = 1", "df", "data").code).toBe("s = '𝔘'; data = 1");
    expect(renameIdentifier("𝔘df = 1; df", "df", "data").code).toBe("𝔘df = 1; data");
  });

  it("follows a backslash continuation and CRLF line ends", () => {
    expect(renameIdentifier("x = 1 + \\\r\n    df\r\ny = df", "df", "data").code).toBe("x = 1 + \\\r\n    data\r\ny = data");
  });

  it("does not rename a number's exponent or hex digits", () => {
    expect(renameIdentifier("x = 1e5 + 0xdf + .5j", "df", "data").count).toBe(0);
  });
});

describe("definesName", () => {
  it.each([
    ["a plain assignment", "df = 1"],
    ["a chained assignment", "a = df = 1"],
    ["a tuple target", "a, df = 1, 2"],
    ["a nested starred target", "a, (b, *df) = x"],
    ["a list target", "[a, df] = x"],
    ["an augmented assignment", "df += 1"],
    ["an annotated assignment", "df: int = 1"],
    ["a bare annotation", "df: int"],
    ["a def", "def df(x):\n    return x"],
    ["an async def", "async def df():\n    pass"],
    ["a decorated def", "@cache\ndef df(): pass"],
    ["a class", "class df(Base):\n    pass"],
    ["an import", "import df"],
    ["an import alias", "import pandas as df"],
    ["a from-import", "from m import df"],
    ["a from-import alias", "from m import x as df"],
    ["a parenthesized from-import", "from m import (\n    a,\n    df,\n)"],
    ["a dotted import's root", "import df.sub"],
    ["a for target", "for df in rows:\n    pass"],
    ["a for tuple target", "for i, df in enumerate(rows): pass"],
    ["an async for target", "async for df in rows: pass"],
    ["a with target", "with open(p) as df:\n    pass"],
    ["a second with target", "with a() as x, b() as df: pass"],
    ["a parenthesized with target", "with (a() as x, b() as df): pass"],
    ["a with tuple target", "with a() as (x, df): pass"],
    ["a walrus", "if (df := load()):\n    pass"],
    ["a walrus in an expression", "print(df := 3)"],
    ["a type alias", "type df = list[int]"],
    ["an assignment inside if", "if ok:\n    df = 1"],
    ["an assignment on an if line", "if ok: df = 1"],
    ["an assignment after a semicolon", "x = 1; df = 2"],
    ["an assignment in try", "try:\n    df = 1\nexcept Exception:\n    pass"],
    ["a global assignment in a function", "def f():\n    global df\n    df = 1"],
    ["an assignment after a function body", "def f():\n    x = 1\ndf = x"],
    ["a lambda's binding", "df = lambda x=1: x"],
    ["`match` used as a name", "match = 1\ndf = match"],
  ])("finds %s", (_label, code) => {
    expect(definesName(code, "df")).toBe(true);
  });

  it.each([
    ["a read", "print(df)"],
    ["an attribute assignment", "obj.df = 1"],
    ["a subscript assignment", "df[0] = 1"],
    ["a subscripted attribute", "obj.x[df] = 1"],
    ["a comparison", "df == 1"],
    ["a keyword argument", "f(df=1)"],
    ["a function's local", "def f():\n    df = 1"],
    ["a one-line function's local", "def f(): df = 1"],
    ["a function parameter", "def f(df): pass"],
    ["a method's local", "class A:\n    def m(self):\n        df = 1"],
    ["a class attribute", "class A:\n    df = 1"],
    ["a nonlocal assignment", "def f():\n    def g():\n        nonlocal df\n        df = 1"],
    ["a module-level global line alone", "global df"],
    ["a comprehension variable", "xs = [df for df in rows]"],
    ["a lambda parameter", "g = lambda df: df"],
    ["a lambda default", "g = lambda df=1: df"],
    ["a string", "x = 'df = 1'"],
    ["a comment", "# df = 1"],
    ["an import's source name", "from m import df as x"],
    ["an import's module", "import df.sub as x"],
    ["a from-import's module", "from df import x"],
    ["a match subject", "match df:\n    case 1:\n        pass"],
    ["an annotation's type", "x: df = 1"],
    ["a call result", "df() == 1"],
  ])("does not count %s", (_label, code) => {
    expect(definesName(code, "df")).toBe(false);
  });

  it("collects every module-level name at once", () => {
    const code = [
      "import numpy as np",
      "from m import a, b as c",
      "x, y = 1, 2",
      "def f(p):",
      "    q = p",
      "    return q",
      "class K:",
      "    attr = 1",
      "for i in range(3):",
      "    total = i",
    ].join("\n");
    expect([...definedNames(code)].sort()).toEqual(["K", "a", "c", "f", "i", "np", "total", "x", "y"]);
  });
});

describe("mentionsName", () => {
  it.each([
    ["a read", "print(df)", true],
    ["a dotted import", "import df.sub", true],
    ["an attribute only", "obj.df", false],
    ["a string only", "'df'", false],
    ["a keyword argument only", "f(df=1)", false],
    ["an f-string field", "f'{df}'", true],
  ])("answers for %s", (_label, code, expected) => {
    expect(mentionsName(code, "df")).toBe(expected);
  });
});

describe("isIdentifier", () => {
  it.each([
    ["df", true],
    ["_df", true],
    ["données", true],
    ["df2", true],
    ["2df", false],
    ["d-f", false],
    ["", false],
    ["class", false],
    ["None", false],
    ["match", true],
  ])("judges %j", (name, expected) => {
    expect(isIdentifier(name)).toBe(expected);
  });
});

describe("tokenize", () => {
  it("emits one newline per logical line and none inside brackets", () => {
    const kinds = tokenize("a = (1,\n 2)\n\n\nb = 3\n").map((t) => t.kind);
    expect(kinds.filter((k) => k === "newline")).toHaveLength(2);
  });

  it("keeps a comment as a token", () => {
    expect(tokenize("x  # note").map((t) => [t.kind, t.text])).toEqual([
      ["name", "x"],
      ["comment", "# note"],
    ]);
  });
});
