"""The ``alkera`` package runs on the oldest Python its ``requires-python`` admits.

``alkera`` is imported into the person's own environment, whatever interpreter
that is, so a feature newer than the declared floor breaks ``import alkera``
for them while every test in this repo (on 3.13) stays green. This gate reads
the floor from ``packages/alkera-py/pyproject.toml`` and scans the source (AST,
no imports) for what the floor cannot run:

- syntax newer than the floor (``match``, ``except*``, ``type`` statements),
  by parsing with ``feature_version``, and parenthesized context managers
  (3.9), which that parse admits;
- a module without ``from __future__ import annotations``, which is what lets
  annotations keep the modern spelling (``dict[str, int]``, ``X | None``);
- outside annotations, where code really runs: a subscripted builtin or
  ``collections.abc`` generic (3.9), a ``|`` union of types (3.10) and a
  ``|`` merge of dicts (3.9);
- standard library names, methods and keyword arguments newer than the floor,
  from the table below.

Annotations are skipped because the future import keeps them as strings, and
so are ``if TYPE_CHECKING:`` blocks, which never run. The table is curated,
not exhaustive. The run-time proof is
``packages/alkera-py/tests/test_nbkrn_py_ratchet.py``, which imports and
drives the package on the floor interpreter when uv has one.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[2] / "alkera-py"
sys.path.insert(0, str(PACKAGE / "tests"))

from alkera_py_floor import declared_floor  # noqa: E402

Version = tuple[int, int]

# Names a module gained after 3.8, as ``module.name`` (``from module import name``
# and the attribute spelling both count). A module path alone means the module
# itself is new.
NEW_NAMES: dict[str, Version] = {
    "graphlib": (3, 9),
    "zoneinfo": (3, 9),
    "tomllib": (3, 11),
    "asyncio.to_thread": (3, 9),
    "asyncio.TaskGroup": (3, 11),
    "asyncio.timeout": (3, 11),
    "asyncio.Runner": (3, 11),
    "contextlib.aclosing": (3, 10),
    "contextlib.chdir": (3, 11),
    "dataclasses.KW_ONLY": (3, 10),
    "datetime.UTC": (3, 11),
    "enum.StrEnum": (3, 11),
    "enum.verify": (3, 11),
    "enum.member": (3, 11),
    "enum.nonmember": (3, 11),
    "functools.cache": (3, 9),
    "hashlib.file_digest": (3, 11),
    "importlib.resources.files": (3, 9),
    "importlib.resources.as_file": (3, 9),
    "inspect.get_annotations": (3, 10),
    "itertools.pairwise": (3, 10),
    "itertools.batched": (3, 12),
    "math.lcm": (3, 9),
    "math.ulp": (3, 9),
    "math.nextafter": (3, 9),
    "math.cbrt": (3, 11),
    "math.exp2": (3, 11),
    "math.sumprod": (3, 12),
    "operator.call": (3, 11),
    "os.waitstatus_to_exitcode": (3, 9),
    "random.randbytes": (3, 9),
    "statistics.correlation": (3, 10),
    "statistics.covariance": (3, 10),
    "statistics.linear_regression": (3, 10),
    "sys.stdlib_module_names": (3, 10),
    "sys.orig_argv": (3, 10),
    "sys.exception": (3, 11),
    "types.GenericAlias": (3, 9),
    "types.NoneType": (3, 10),
    "types.UnionType": (3, 10),
    "types.EllipsisType": (3, 10),
    "typing.Annotated": (3, 9),
    "typing.TypeAlias": (3, 10),
    "typing.ParamSpec": (3, 10),
    "typing.Concatenate": (3, 10),
    "typing.TypeGuard": (3, 10),
    "typing.is_typeddict": (3, 10),
    "typing.Self": (3, 11),
    "typing.LiteralString": (3, 11),
    "typing.Never": (3, 11),
    "typing.assert_never": (3, 11),
    "typing.assert_type": (3, 11),
    "typing.reveal_type": (3, 11),
    "typing.Required": (3, 11),
    "typing.NotRequired": (3, 11),
    "typing.TypeVarTuple": (3, 11),
    "typing.Unpack": (3, 11),
    "typing.dataclass_transform": (3, 11),
    "typing.override": (3, 12),
    "typing.TypeIs": (3, 13),
    "typing.ReadOnly": (3, 13),
}

# Builtins added after 3.8.
NEW_BUILTINS: dict[str, Version] = {
    "aiter": (3, 10),
    "anext": (3, 10),
    "EncodingWarning": (3, 10),
    "ExceptionGroup": (3, 11),
    "BaseExceptionGroup": (3, 11),
}

# Methods distinctive enough to flag on any receiver.
NEW_METHODS: dict[str, Version] = {
    "removeprefix": (3, 9),
    "removesuffix": (3, 9),
    "is_relative_to": (3, 9),
    "with_stem": (3, 9),
    "bit_count": (3, 10),
    "hardlink_to": (3, 10),
    "add_note": (3, 11),
}

# Keyword arguments a callable gained after 3.8, keyed by the callable's last name.
NEW_KEYWORDS: dict[tuple[str, str], Version] = {
    ("zip", "strict"): (3, 10),
    ("dataclass", "slots"): (3, 10),
    ("dataclass", "kw_only"): (3, 10),
    ("dataclass", "match_args"): (3, 10),
    ("dataclass", "weakref_slot"): (3, 11),
    ("field", "kw_only"): (3, 10),
}

BUILTIN_GENERICS = frozenset({"dict", "list", "tuple", "set", "frozenset", "type"})
TYPE_NAMES = BUILTIN_GENERICS | {"int", "float", "complex", "str", "bytes", "bool", "object"}


@dataclass(frozen=True)
class Violation:
    path: Path
    line: int
    feature: str
    why: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line} {self.feature} {self.why}"


def _needs(version: Version) -> str:
    return f"needs Python {version[0]}.{version[1]}"


def _dotted(node: ast.expr) -> str:
    """``a.b.c`` for a Name/Attribute chain, else an empty string."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return ""
    parts.append(node.id)
    return ".".join(reversed(parts))


def _is_type_checking(test: ast.expr) -> bool:
    return _dotted(test) in {"TYPE_CHECKING", "typing.TYPE_CHECKING"}


def _annotation_ids(tree: ast.AST) -> set[int]:
    """Ids of every node inside an annotation, which the future import never evaluates."""
    roots: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.arg) and node.annotation is not None:
            roots.append(node.annotation)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.returns:
            roots.append(node.returns)
        elif isinstance(node, ast.AnnAssign):
            roots.append(node.annotation)
    return {id(inner) for root in roots for inner in ast.walk(root)}


def _runtime_nodes(tree: ast.Module, skip: set[int]) -> Iterator[ast.AST]:
    """Every node that runs: not in an annotation, not under ``if TYPE_CHECKING:``."""
    stack: list[ast.AST] = [tree]
    while stack:
        node = stack.pop()
        if id(node) in skip:
            continue
        yield node
        if isinstance(node, ast.If) and _is_type_checking(node.test):
            stack.extend(node.orelse)
            continue
        stack.extend(ast.iter_child_nodes(node))


def _has_future_annotations(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == "__future__"
        and any(alias.name == "annotations" for alias in node.names)
        for node in tree.body
    )


def _abc_names(tree: ast.Module) -> set[str]:
    """Local names bound to ``collections.abc`` (or ``collections``) classes."""
    return {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module in {"collections.abc", "collections"}
        for alias in node.names
    }


def _is_type_operand(node: ast.expr, abc: set[str]) -> bool:
    if isinstance(node, ast.Constant):
        return node.value is None
    if isinstance(node, ast.Subscript):
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _is_type_operand(node.left, abc) or _is_type_operand(node.right, abc)
    name = _dotted(node)
    return name in TYPE_NAMES or name in abc or name.startswith(("typing.", "collections.abc."))


def _is_dict_operand(node: ast.expr) -> bool:
    if isinstance(node, ast.Dict | ast.DictComp):
        return True
    return isinstance(node, ast.Call) and _dotted(node.func) == "dict"


def _next_significant(lines: list[bytes], line: int, col: int) -> str:
    """The first character after (line, col) that is not space, a comma, a line join or a comment.

    ``line`` is 1-based and ``col`` a UTF-8 byte offset, as the AST reports them."""
    rest = lines[line - 1][col:]
    for following in [rest, *lines[line:]]:
        # A trailing comma may sit between the last item and the parenthesis.
        text = following.decode("utf-8").partition("#")[0].lstrip(" \t\r\n\\,")
        if text:
            return text[0]
    return ""


def _parenthesized_with(node: ast.With | ast.AsyncWith, lines: list[bytes]) -> bool:
    """``with (a as b, c):``: the items share one pair of parentheses (3.9 grammar).

    A single item with no ``as`` reads the same in 3.8 (a parenthesized
    expression), and ``with (a) as b:`` closes its parenthesis before ``as``."""
    last = node.items[-1]
    if len(node.items) == 1 and last.optional_vars is None:
        return False
    end = last.optional_vars or last.context_expr
    assert end.end_lineno is not None and end.end_col_offset is not None
    return _next_significant(lines, end.end_lineno, end.end_col_offset) == ")"


def _features(tree: ast.Module, lines: list[bytes]) -> Iterator[tuple[int, str, Version]]:
    abc = _abc_names(tree)
    for node in _runtime_nodes(tree, _annotation_ids(tree)):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.With | ast.AsyncWith) and _parenthesized_with(node, lines):
            yield line, "parenthesized context managers", (3, 9)
        elif isinstance(node, ast.Subscript):
            target = _dotted(node.value)
            if target in BUILTIN_GENERICS or target in abc or target.startswith("collections."):
                yield line, f"subscripted {target}[...] at run time", (3, 9)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            if _is_dict_operand(node.left) or _is_dict_operand(node.right):
                yield line, "dict | dict merge", (3, 9)
            elif _is_type_operand(node.left, abc) and _is_type_operand(node.right, abc):
                yield line, "X | Y type union at run time", (3, 10)
        elif isinstance(node, ast.AugAssign) and isinstance(node.op, ast.BitOr):
            if _is_dict_operand(node.value):
                yield line, "dict |= merge", (3, 9)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if node.module in NEW_NAMES:
                yield line, f"module {node.module}", NEW_NAMES[node.module]
            for alias in node.names:
                name = f"{node.module}.{alias.name}"
                if name in NEW_NAMES:
                    yield line, name, NEW_NAMES[name]
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in NEW_NAMES:
                    yield line, f"module {alias.name}", NEW_NAMES[alias.name]
        elif isinstance(node, ast.Attribute):
            name = _dotted(node)
            if name in NEW_NAMES:
                yield line, name, NEW_NAMES[name]
            elif node.attr in NEW_METHODS:
                yield line, f".{node.attr}()", NEW_METHODS[node.attr]
        elif isinstance(node, ast.Name) and node.id in NEW_BUILTINS:
            yield line, f"builtin {node.id}", NEW_BUILTINS[node.id]
        if isinstance(node, ast.Call):
            callee = _dotted(node.func).rpartition(".")[2]
            for keyword in node.keywords:
                key = (callee, keyword.arg or "")
                if key in NEW_KEYWORDS:
                    yield line, f"{callee}({keyword.arg}=...)", NEW_KEYWORDS[key]


def scan_source(source: str, path: Path, floor: Version) -> list[Violation]:
    try:
        tree = ast.parse(source, filename=str(path), feature_version=floor)
    except SyntaxError as exc:
        return [Violation(path, exc.lineno or 0, f"syntax ({exc.msg})", "is newer than the floor")]
    found: list[Violation] = []
    if not _has_future_annotations(tree):
        why = "(the floor would evaluate its annotations)"
        found.append(Violation(path, 1, "no 'from __future__ import annotations'", why))
    lines = source.encode("utf-8").splitlines(keepends=True)
    for line, feature, needs in sorted(_features(tree, lines)):
        if needs > floor:
            found.append(Violation(path, line, feature, _needs(needs)))
    return found


def scan(root: Path, floor: Version) -> list[Violation]:
    found: list[Violation] = []
    for path in sorted(root.rglob("*.py")):
        found.extend(scan_source(path.read_text(encoding="utf-8"), path, floor))
    return found


def test_alkera_declares_python_3_8() -> None:
    # The floor is a product promise to notebook users, not an implementation detail.
    assert declared_floor() == (3, 8)


def test_alkera_runs_on_its_declared_floor() -> None:
    violations = scan(PACKAGE / "alkera", declared_floor())
    assert not violations, "\n".join(map(str, violations))


FUTURE = "from __future__ import annotations\n"

PLANTED = [
    pytest.param(FUTURE + "Bundle = dict[str, int]\n", "subscripted dict", id="alias-dict"),
    pytest.param(
        FUTURE + "from collections.abc import Callable\nF = Callable[..., int]\n",
        "subscripted Callable",
        id="alias-abc",
    ),
    pytest.param(FUTURE + "x = isinstance(1, int | None)\n", "type union", id="isinstance-union"),
    pytest.param(
        FUTURE + "import typing\nx = typing.cast(list[int], [])\n",
        "subscripted list",
        id="cast-generic",
    ),
    pytest.param(FUTURE + "m = {'a': 1} | dict(b=2)\n", "dict | dict", id="dict-merge"),
    pytest.param(FUTURE + "m = {}\nm |= {'a': 1}\n", "dict |=", id="dict-merge-in-place"),
    pytest.param(FUTURE + "pairs = list(zip([1], [2], strict=True))\n", "zip(strict", id="zip"),
    pytest.param(
        FUTURE + "from dataclasses import dataclass\n@dataclass(frozen=True, slots=True)\n"
        "class A:\n    x: int\n",
        "dataclass(slots",
        id="dataclass-slots",
    ),
    pytest.param(
        FUTURE + "import dataclasses\n@dataclasses.dataclass(kw_only=True)\nclass A:\n    x: int\n",
        "dataclass(kw_only",
        id="dataclass-kw-only",
    ),
    pytest.param(FUTURE + "s = 'ab'.removeprefix('a')\n", ".removeprefix", id="removeprefix"),
    pytest.param(FUTURE + "from functools import cache\n", "functools.cache", id="from-cache"),
    pytest.param(FUTURE + "import math\nx = math.lcm(2, 3)\n", "math.lcm", id="math-lcm"),
    pytest.param(
        FUTURE + "import importlib.resources\np = importlib.resources.files('alkera')\n",
        "importlib.resources.files",
        id="resources-files",
    ),
    pytest.param(FUTURE + "from typing import TypeAlias\n", "typing.TypeAlias", id="type-alias"),
    pytest.param(FUTURE + "import zoneinfo\n", "module zoneinfo", id="new-module"),
    pytest.param(FUTURE + "x = anext\n", "builtin anext", id="new-builtin"),
    pytest.param(FUTURE + "match 1:\n    case 1:\n        pass\n", "syntax", id="match-statement"),
    pytest.param(
        FUTURE + "with (open('a') as a, open('b') as b):\n    pass\n",
        "parenthesized context managers",
        id="parenthesized-with",
    ),
    pytest.param(
        FUTURE + "with (\n    open('a'),  # first\n    open('b'),\n):\n    pass\n",
        "parenthesized context managers",
        id="parenthesized-with-multiline",
    ),
    pytest.param("x: dict[str, int] = {}\n", "__future__", id="missing-future-import"),
]


@pytest.mark.parametrize(("source", "feature"), PLANTED)
def test_scan_catches_a_planted_feature(source: str, feature: str) -> None:
    found = scan_source(source, Path("planted.py"), (3, 8))
    assert len(found) == 1, found
    assert feature in found[0].feature


ALLOWED = [
    pytest.param(
        FUTURE + "from collections.abc import Callable\n"
        "def f(x: dict[str, int] | None, g: Callable[[int], str]) -> list[int] | None:\n"
        "    y: tuple[int, ...] | None = None\n    return None\n",
        id="annotations-only",
    ),
    pytest.param(
        FUTURE + "from typing import Dict, Optional, Union, cast\n"
        "Bundle = Dict[str, int]\nx = cast('dict[str, int] | None', None)\n"
        "y = isinstance(1, (int, type(None)))\nz = Optional[Union[int, str]]\n",
        id="typing-spellings",
    ),
    pytest.param(
        FUTURE + "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n"
        "    from typing import TypeAlias\n    Bundle: TypeAlias = dict[str, int]\n",
        id="type-checking-block",
    ),
    pytest.param(FUTURE + "flags = 1 | 2\nmask = a | b\n", id="integer-or"),
    pytest.param(
        FUTURE + "with (open('a')):\n    pass\nwith (open('a')) as a, (open('b')) as b:\n"
        "    pass\nwith (open('a'), open('b')) as pair:\n    pass\n",
        id="parenthesized-expressions-in-with",
    ),
    pytest.param(FUTURE + "s = 'ab'[1:]\nrows = [[1]][0]\n", id="plain-indexing"),
]


@pytest.mark.parametrize("source", ALLOWED)
def test_scan_admits_3_8_code(source: str) -> None:
    assert scan_source(source, Path("ok.py"), (3, 8)) == []


def test_a_newer_floor_admits_what_it_runs() -> None:
    source = FUTURE + "Bundle = dict[str, int]\ns = 'ab'.removeprefix('a')\n"
    assert scan_source(source, Path("ok.py"), (3, 9)) == []
    assert [v.feature for v in scan_source(source, Path("ok.py"), (3, 8))] == [
        "subscripted dict[...] at run time",
        ".removeprefix()",
    ]
