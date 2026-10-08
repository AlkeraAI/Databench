"""The 20-cell fixture notebook the runtime import matrix runs in every
environment.

Each cell names the libraries it needs (``requires``); a cell whose library
the environment lacks is skipped and counted as skipped. ``expect`` is what
the cell must produce to count as passed: a substring of its stdout, of its
stderr, or of the ``text/plain`` of its displayed value.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Cell:
    cell_id: str
    source: str
    requires: tuple[str, ...] = ()
    expect: dict[str, str] = field(default_factory=dict)


CELLS: list[Cell] = [
    Cell("plain", "x = 1 + 1\nx", expect={"plain": "2"}),
    Cell("define", "def add(a, b):\n    return a + b\n"),
    Cell("use", "add(x, 40)", expect={"plain": "42"}),
    Cell(
        "klass",
        "class Point:\n"
        "    def __init__(self, a, b):\n"
        "        self.a, self.b = a, b\n"
        "    def norm2(self):\n"
        "        return self.a ** 2 + self.b ** 2\n"
        "p = Point(1, 2)\n"
        "p.norm2()",
        expect={"plain": "5"},
    ),
    Cell(
        "await",
        "import asyncio\n"
        "await asyncio.sleep(0.01)\n"
        "awaited = await asyncio.sleep(0, result='done')\n"
        "awaited",
        expect={"plain": "'done'"},
    ),
    Cell("print", "print('hello from a cell')", expect={"stdout": "hello from a cell"}),
    Cell(
        "stderr", "import sys\nprint('a warning', file=sys.stderr)", expect={"stderr": "a warning"}
    ),
    Cell("listcomp", "squares = [i * i for i in range(10)]\nsum(squares)", expect={"plain": "285"}),
    Cell(
        "caught",
        "try:\n    1 / 0\n"
        "except ZeroDivisionError as exc:\n    caught = type(exc).__name__\n"
        "caught",
        expect={"plain": "'ZeroDivisionError'"},
    ),
    Cell(
        "fstring",
        "name = 'alkera'\nf'{name!r:>10}|{3.14159:.2f}|{x=}'",
        expect={"plain": "'alkera'|3.14|x=2"},
    ),
    Cell(
        "walrus", "if (n := len(squares)) > 5:\n    print('n is', n)", expect={"stdout": "n is 10"}
    ),
    Cell(
        "dataclass",
        "from dataclasses import dataclass, field\n"
        "@dataclass\n"
        "class Row:\n"
        "    id: int\n"
        "    tags: list = field(default_factory=list)\n"
        "Row(7, ['a'])",
        expect={"plain": "Row(id=7, tags=['a'])"},
    ),
    Cell(
        "stdlib",
        "import datetime\nimport json\nimport re\n"
        "d = datetime.date(2026, 10, 5).isoformat()\n"
        "json.dumps({'d': d, 'm': re.match(r'(\\d+)', '42x').group(1)})",
        expect={"plain": '"2026-10-05"'},
    ),
    Cell(
        "match",
        "def kind(v):\n"
        "    match v:\n"
        "        case {'a': a}:\n"
        "            return f'dict a={a}'\n"
        "        case [first, *_]:\n"
        "            return f'seq {first}'\n"
        "        case _:\n"
        "            return 'other'\n"
        "[kind({'a': 1}), kind([3, 4]), kind(5)]",
        expect={"plain": "['dict a=1', 'seq 3', 'other']"},
    ),
    Cell("dict", "{'a': 1, 'b': [1, 2], 'c': {'d': None}}", expect={"plain": "'b': [1, 2]"}),
    Cell(
        "pandas",
        "import pandas as pd\n"
        "df = pd.DataFrame({'a': [1, 2, 3], 'b': ['x', 'y', 'z']})\n"
        "print(int(df['a'].sum()))\n"
        "df",
        requires=("pandas",),
        expect={"stdout": "6", "plain": "a"},
    ),
    Cell(
        "polars",
        "import polars as pl\npdf = pl.DataFrame({'a': [1, 2, 3]})\nprint(pdf['a'].sum())\npdf",
        requires=("polars",),
        expect={"stdout": "6", "plain": "shape: (3, 1)"},
    ),
    Cell(
        "pyarrow",
        "import pyarrow as pa\n"
        "table = pa.Table.from_pandas(df)\n"
        "print(table.num_rows)\n"
        "table.to_pandas().shape",
        requires=("pandas", "pyarrow"),
        expect={"stdout": "3", "plain": "(3, 2)"},
    ),
    Cell(
        "marimo",
        "import marimo as mo\nprint('marimo', mo.__version__)\nmo.md('**hi**')",
        requires=("marimo",),
        expect={"stdout": "marimo 0."},
    ),
    Cell(
        "summary",
        "summary = {'x': x, 'squares': len(squares), 'caught': caught,\n"
        "           'awaited': awaited, 'n': n}\n"
        "print(json.dumps(summary, sort_keys=True))\n"
        "summary",
        expect={"stdout": '"awaited": "done"', "plain": "'squares': 10"},
    ),
]

assert len(CELLS) == 20
