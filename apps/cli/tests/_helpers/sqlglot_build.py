"""Differential harness for the mypyc-compiled sqlglot build (the ``sqlglot[c]`` extra).

``sqlglotc`` ships no importable module of its own — it drops compiled ``.so`` /
``.pyd`` modules straight into the installed ``sqlglot`` package directory, next to
the ``.py`` sources they were compiled from. Python's import machinery prefers an
extension module over a source file of the same name in the same directory, so every
``import sqlglot.parser`` silently resolves to the compiled half.

Both halves stay on disk, and that is what makes an A/B comparison possible:
:func:`force_pure_python_sqlglot` installs a meta-path finder that routes every
``sqlglot.*`` import back to its ``.py`` source, so the same corpus can be run through
the interpreted build and compared against the compiled one.

The finder only has an effect BEFORE ``sqlglot`` is imported, which is why this module
doubles as a child entry point::

    python -m _helpers.sqlglot_build   # JSON corpus on stdin, JSON digests on stdout

with ``apps/cli/tests`` on ``PYTHONPATH``.
"""

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import os
import sys
from collections.abc import Sequence
from importlib.abc import MetaPathFinder
from importlib.machinery import ModuleSpec
from typing import Any

#: sqlglot modules `sqlglotc` compiles. If the wheel is installed and import order is
#: intact, every one of these resolves to an extension module. Chosen to span the whole
#: pipeline (tokenize -> parse -> AST -> generate) rather than a single sentinel, so a
#: partially-shadowed install is caught too.
COMPILED_MODULES: tuple[str, ...] = (
    "sqlglot.tokenizer_core",
    "sqlglot.parser",
    "sqlglot.generator",
    "sqlglot.expressions.core",
    "sqlglot.optimizer.qualify",
)

#: One corpus entry: a stable id, the dialect to read it as, and the SQL text.
Case = tuple[str, str, str]


def module_origin(module_name: str) -> str:
    """Absolute file backing ``module_name``, imported if necessary."""
    module = importlib.import_module(module_name)
    origin = getattr(module, "__file__", None)
    if origin is None:  # pragma: no cover - namespace packages have no origin
        raise AssertionError(f"{module_name} has no __file__")
    return origin


def is_compiled(module_name: str) -> bool:
    """True when ``module_name`` resolved to a native extension rather than a ``.py``."""
    origin = module_origin(module_name)
    return origin.endswith(tuple(importlib.machinery.EXTENSION_SUFFIXES))


class PurePythonSqlglotFinder(MetaPathFinder):
    """Meta-path finder that resolves every ``sqlglot`` module to its ``.py`` source.

    Sits ahead of the normal path finders, so it wins over the compiled extension that
    shares the directory. Returns ``None`` (deferring to the default machinery) for
    anything outside the ``sqlglot`` namespace, and for a sqlglot module that genuinely
    has no source on disk.
    """

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None = None,
        target: Any = None,
    ) -> ModuleSpec | None:
        if fullname != "sqlglot" and not fullname.startswith("sqlglot."):
            return None

        search = list(path) if path is not None else list(sys.path)
        leaf = fullname.rpartition(".")[2]
        for entry in search:
            base = os.path.join(entry, leaf)
            package_init = os.path.join(base, "__init__.py")
            if os.path.isfile(package_init):
                return importlib.util.spec_from_file_location(
                    fullname, package_init, submodule_search_locations=[base]
                )
            source = base + ".py"
            if os.path.isfile(source):
                return importlib.util.spec_from_file_location(fullname, source)
        return None


def force_pure_python_sqlglot() -> None:
    """Route later ``sqlglot`` imports at the interpreted sources.

    Must run before anything imports sqlglot — already-imported modules are cached in
    ``sys.modules`` and the finder never sees them.
    """
    if "sqlglot" in sys.modules:
        raise RuntimeError("sqlglot was already imported; the finder would be a no-op")
    sys.meta_path.insert(0, PurePythonSqlglotFinder())


def describe_failure(stage: str, exc: BaseException) -> str:
    """How a failure at ``stage`` enters the fingerprint.

    An error sqlglot *declares* (``SqlglotError`` and its subclasses — ``ParseError``,
    ``TokenError``, ``UnsupportedError``, …) is part of its contract, so the class AND
    the message have to match between the builds.

    Anything else means the statement reached a path sqlglot itself does not support,
    and there the exception CLASS legitimately differs: mypyc inserts runtime type
    checks at compiled-function boundaries, so the compiled build can raise ``TypeError``
    where the interpreted one gets a little further and raises its own ``ValueError``.
    Both refuse the same statement at the same stage — that refusal is the contract, and
    it is what the fingerprint records. ``test_unsupported_generation_refuses_in_both
    _builds`` pins the one real corpus statement this applies to.
    """
    import sqlglot.errors

    if isinstance(exc, sqlglot.errors.SqlglotError):
        return f"{stage}-error:{type(exc).__name__}:{exc}"
    return f"{stage}-error:unsupported"


def _digest_one(dialect: str, sql: str) -> str:
    """Fingerprint of everything sqlglot observably derives from ``sql``.

    Covers the token stream (type, text and every offset), the parsed AST's structural
    dump, and the SQL the generator writes back out. Failures are folded in via
    :func:`describe_failure`, so "the compiled build accepts SQL the interpreted one
    rejects" (or vice versa) reads as a difference rather than an error in the harness.
    """
    import sqlglot
    from sqlglot.dialects.dialect import Dialect

    parts: list[str] = []
    reader = Dialect.get_or_raise(dialect)
    try:
        tokens = reader.tokenize(sql)
    except Exception as exc:
        return describe_failure("tokenize", exc)
    parts.append(
        "|".join(
            f"{tok.token_type.name},{tok.text},{tok.line},{tok.col},{tok.start},{tok.end}"
            for tok in tokens
        )
    )
    try:
        trees = sqlglot.parse(sql, read=dialect)
    except Exception as exc:
        parts.append(describe_failure("parse", exc))
    else:
        for tree in trees:
            if tree is None:
                parts.append("<empty>")
                continue
            parts.append(repr(tree))
            try:
                parts.append(tree.sql(dialect=dialect))
            except Exception as exc:
                parts.append(describe_failure("generate", exc))
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def classify_generation(dialect: str, sql: str) -> str:
    """How generating ``sql`` back out ends, as ``ok`` or ``refused:<class>:<declared?>``.

    Used by the test that documents the mypyc exception-class divergence; it lives here
    so the child process can run the same classification under the interpreted build.
    """
    import sqlglot
    import sqlglot.errors

    tree = sqlglot.parse_one(sql, read=dialect)
    try:
        tree.sql(dialect=dialect)
    except Exception as exc:
        declared = "declared" if isinstance(exc, sqlglot.errors.SqlglotError) else "undeclared"
        return f"refused:{type(exc).__name__}:{declared}"
    return "ok"


def digest_corpus(cases: Sequence[Case]) -> dict[str, str]:
    """Map each case id to its fingerprint under whichever sqlglot build is imported."""
    return {case_id: _digest_one(dialect, sql) for case_id, dialect, sql in cases}


def main() -> None:
    """Child entry point: run a corpus through the INTERPRETED sqlglot build.

    Reads ``{"mode": "digest"|"classify", "cases": [[id, dialect, sql], …]}`` on stdin
    and writes ``{"origins": …, "results": {id: value}}`` on stdout. ``origins`` lets the
    caller verify the fallback actually took, so a hook that silently stopped working
    cannot pass the comparison by running the compiled build twice.
    """
    request = json.load(sys.stdin)
    cases: list[Case] = [(case_id, dialect, sql) for case_id, dialect, sql in request["cases"]]
    force_pure_python_sqlglot()
    origins = {name: module_origin(name) for name in COMPILED_MODULES}
    if request["mode"] == "classify":
        results = {case_id: classify_generation(dialect, sql) for case_id, dialect, sql in cases}
    else:
        results = digest_corpus(cases)
    json.dump({"origins": origins, "results": results}, sys.stdout)


if __name__ == "__main__":
    main()
