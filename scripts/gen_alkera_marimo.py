"""Generate ``alkera_notebook._marimo``, the private copy of the marimo fork.

``vendor/marimo`` is a subtree of upstream marimo with Alkera's fenced edits.
The engine and the format library use it as a normal package under a private
name, so a person's own ``marimo`` install never collides with it. This script
copies ``vendor/marimo/marimo`` to
``packages/alkera-notebook/alkera_notebook/_marimo`` and

- rewrites every ``marimo`` import statement (and every string literal that
  names one of the package's own modules, such as ``"marimo._ipc.launch_kernel"``)
  to the private name, and nothing else: strings such as ``"import marimo"`` in
  generated notebooks or the ``marimo.sql`` call name stay as they are;
- keeps every original header and adds one notice line to each Python file it
  changed; marimo notebooks shipped as data (tutorials, snippets) are copied
  byte for byte, since their ``import marimo`` is notebook content;
- pins ``_version.py`` to the vendored release, since the private copy is not
  the ``marimo`` distribution and must not report a stock install's version;
- copies marimo's ``LICENSE`` beside the package (its third-party notices are
  inside the package already);
- leaves out the frontend bundle ``_static`` and marimo's manual QA notebooks
  ``_smoke_tests``, which nothing imports, and ``AGENTS.md``, the upstream
  contributor guide, whose commands run in marimo's own checkout;
- rewrites the few comments that point at a path in marimo's repository
  (:data:`REFERENCE_REWRITES`), which means nothing inside the copy.

Run through ``make gen-alkera-marimo``. ``--check`` reports drift and exits 1
without writing.
"""

from __future__ import annotations

import argparse
import ast
import filecmp
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "vendor" / "marimo"
SOURCE = VENDOR / "marimo"
TARGET = ROOT / "packages" / "alkera-notebook" / "alkera_notebook" / "_marimo"
PRIVATE = "alkera_notebook._marimo"
# `_static` is the frontend bundle and `_smoke_tests` marimo's manual QA notebooks;
# no module imports either.
EXCLUDED_DIRS = frozenset({"_static", "_smoke_tests", "__pycache__"})
#: Top-level files the copy leaves out: marimo's contributor guide.
EXCLUDED_FILES = frozenset({"AGENTS.md"})
#: Comments that name a path in marimo's repository, by file: the upstream text
#: and what the copy says instead. Generation fails when the upstream text is
#: gone, so a bump that moves one is noticed.
REFERENCE_REWRITES: dict[str, tuple[tuple[str, str], ...]] = {
    "_plugins/ui/_impl/chat/chat.py": (
        (
            # Split so this file holds no path to marimo's examples, which are not vendored.
            "Refer to examples" + "/ai/chat/pydantic-ai-chat.py for a complete example.",
            "Refer to marimo's pydantic-ai chat example for a complete example.",
        ),
    ),
    "_server/api/endpoints/file_explorer.py": (
        (
            "# See marimo's security model and disclosure policy in docs" + "/security.md\n",
            "# See marimo's security model and disclosure policy\n",
        ),
    ),
}
NOTICE = "# Modified by Alkera: import paths rewritten; see vendor/marimo/README.alkera.md"


VERSION_NOTICE = (
    "# Modified by Alkera: version pinned to the vendored release;"
    " see vendor/marimo/README.alkera.md"
)


def vendored_version() -> str:
    match = re.search(r'^version = "([^"]+)"$', (VENDOR / "pyproject.toml").read_text(), re.M)
    if match is None:
        raise GenerationError("vendor/marimo/pyproject.toml has no version")
    return match.group(1)


class GenerationError(Exception):
    """The source holds a form the rewriter refuses to guess about."""


def _module_names(source: Path) -> frozenset[str]:
    names: set[str] = set()
    for path in source.rglob("*.py"):
        rel = path.relative_to(source).with_suffix("")
        parts = ("marimo", *rel.parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        names.add(".".join(parts))
    return frozenset(names)


def _line_offsets(text: str) -> list[int]:
    offsets = [0]
    for line in text.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    return offsets


def _span(text: str, offsets: list[int], node: ast.AST) -> tuple[int, int]:
    lineno = getattr(node, "lineno")  # noqa: B009 - every node passed here has positions
    end_lineno = getattr(node, "end_lineno")  # noqa: B009
    col = getattr(node, "col_offset")  # noqa: B009
    end_col = getattr(node, "end_col_offset")  # noqa: B009
    start_line = text[offsets[lineno - 1] : offsets[lineno]]
    end_line = text[offsets[end_lineno - 1] : offsets[end_lineno]]
    # ast offsets are UTF-8 byte offsets within the line.
    start = offsets[lineno - 1] + len(start_line.encode()[:col].decode())
    end = offsets[end_lineno - 1] + len(end_line.encode()[:end_col].decode())
    return start, end


def _is_marimo(module: str) -> bool:
    return module == "marimo" or module.startswith("marimo.")


def _private(module: str) -> str:
    return PRIVATE + module[len("marimo") :]


def rewrite_source(text: str, modules: frozenset[str], where: str) -> str:
    """Rewrite marimo import statements and own-module strings in ``text``."""
    tree = ast.parse(text, filename=where)
    offsets = _line_offsets(text)
    edits: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if not _is_marimo(node.module):
                continue
            start, end = _span(text, offsets, node)
            segment = text[start:end]
            new, count = re.subn(
                r"^from(\s+)" + re.escape(node.module) + r"(?=[\s(])",
                lambda m, mod=node.module: f"from{m.group(1)}{_private(mod)}",
                segment,
                count=1,
            )
            if count != 1:
                raise GenerationError(f"{where}:{node.lineno}: unexpected from-import form")
            edits.append((start, end, new))
        elif isinstance(node, ast.Import):
            if not any(_is_marimo(alias.name) for alias in node.names):
                continue
            parts: list[str] = []
            for alias in node.names:
                if not _is_marimo(alias.name):
                    parts.append(alias.name + (f" as {alias.asname}" if alias.asname else ""))
                elif alias.asname:
                    parts.append(f"{_private(alias.name)} as {alias.asname}")
                elif alias.name == "marimo":
                    parts.append(f"{PRIVATE} as marimo")
                else:
                    raise GenerationError(
                        f"{where}:{node.lineno}: `import {alias.name}` binds `marimo`"
                        " implicitly; write it with `as` or a from-import"
                    )
            start, end = _span(text, offsets, node)
            edits.append((start, end, "import " + ", ".join(parts)))
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in modules
            and node.value != "marimo"
        ):
            start, end = _span(text, offsets, node)
            literal = text[start:end]
            if node.value not in literal:
                raise GenerationError(
                    f"{where}:{node.lineno}: module string is not a plain literal"
                )
            edits.append((start, end, literal.replace(node.value, _private(node.value), 1)))
    for start, end, new in sorted(edits, reverse=True):
        text = text[:start] + new + text[end:]
    return text


def is_notebook(text: str) -> bool:
    """A marimo notebook file: a module-level ``app = marimo.App(...)``."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    return any(
        isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "App"
        and any(isinstance(t, ast.Name) and t.id == "app" for t in node.targets)
        for node in tree.body
    )


def stamp(text: str, notice: str) -> str:
    """Insert ``notice`` after the file's leading comment block."""
    lines = text.splitlines(keepends=True)
    index = 0
    while index < len(lines) and lines[index].startswith("#"):
        index += 1
    lines.insert(index, notice + "\n")
    return "".join(lines)


def rewrite_references(text: str, rel: str) -> str:
    """``text`` with the :data:`REFERENCE_REWRITES` for ``rel`` applied."""
    for upstream, copy in REFERENCE_REWRITES.get(rel, ()):
        if upstream not in text:
            raise GenerationError(f"vendor/marimo/marimo/{rel} no longer says {upstream!r}")
        text = text.replace(upstream, copy)
    return text


def generate(target: Path) -> None:
    modules = _module_names(SOURCE)
    if target.exists():
        shutil.rmtree(target)
    for path in sorted(SOURCE.rglob("*")):
        rel = path.relative_to(SOURCE)
        if any(part in EXCLUDED_DIRS for part in rel.parts) or path.is_dir():
            continue
        if rel.as_posix() in EXCLUDED_FILES:
            continue
        if path.suffix == ".pyc":
            continue
        out = target / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix == ".py":
            original = path.read_text(encoding="utf-8")
            where = f"vendor/marimo/marimo/{rel.as_posix()}"
            if is_notebook(original):
                shutil.copyfile(path, out)
                continue
            if rel.as_posix() == "_version.py":
                header = "".join(
                    line for line in original.splitlines(keepends=True)[:1] if line.startswith("#")
                )
                text = (
                    f"{header}{VERSION_NOTICE}\nfrom __future__ import annotations\n\n"
                    f"__version__ = {vendored_version()!r}\n"
                )
                out.write_text(text, encoding="utf-8", newline="")
                continue
            text = rewrite_references(rewrite_source(original, modules, where), rel.as_posix())
            if text != original:
                text = stamp(text, NOTICE)
            out.write_text(text, encoding="utf-8", newline="")
        else:
            shutil.copyfile(path, out)
    shutil.copyfile(VENDOR / "LICENSE", target / "LICENSE")


def _diff(left: Path, right: Path) -> list[str]:
    found: list[str] = []
    cmp = filecmp.dircmp(left, right, ignore=["__pycache__"])
    stack = [(cmp, Path())]
    while stack:
        node, rel = stack.pop()
        found += [str(rel / n) for n in node.left_only + node.right_only + node.diff_files]
        found += [str(rel / n) for n in node.funny_files]
        stack += [(sub, rel / name) for name, sub in node.subdirs.items()]
    return sorted(found)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="report drift, write nothing")
    args = parser.parse_args()
    if not args.check:
        generate(TARGET)
        print(f"generated {TARGET.relative_to(ROOT)}")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        fresh = Path(tmp) / "_marimo"
        generate(fresh)
        drift = _diff(fresh, TARGET) if TARGET.exists() else ["(missing)"]
    if drift:
        print("alkera_notebook._marimo is stale; run make gen-alkera-marimo:", file=sys.stderr)
        for name in drift[:50]:
            print(f"  {name}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
