"""Copy the RPC framing module into the kernel runtime.

``alkera_notebook/rpc/frames.py`` is the reference implementation; the kernel
carries the same code as ``_alkera_kernel/_frames.py`` because it is loaded
into the person's interpreter, where ``alkera_notebook`` does not exist. The
copy is the source plus a one-line banner. Refuses a source that imports
anything outside the standard library, or that does not parse as Python 3.10.

Usage: ``uv run python packages/alkera-kernel/scripts/gen_kernel_rpc.py [--check]``
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

PACKAGES = Path(__file__).resolve().parents[2]
SOURCE = PACKAGES / "alkera-notebook" / "alkera_notebook" / "rpc" / "frames.py"
TARGET = PACKAGES / "alkera-kernel" / "_alkera_kernel" / "_frames.py"
BANNER = (
    "# Generated from packages/alkera-notebook/alkera_notebook/rpc/frames.py by\n"
    "# `make gen-kernel-rpc`. Do not edit; change the source and regenerate.\n"
)
STDLIB = frozenset(sys.stdlib_module_names) | {"__future__"}


def non_stdlib_imports(source: str) -> list[str]:
    tree = ast.parse(source, feature_version=(3, 10))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                found.append("." * node.level + (node.module or ""))
                continue
            names = [node.module or ""]
        else:
            continue
        found.extend(n for n in names if n.partition(".")[0] not in STDLIB)
    return found


def render(source: str) -> str:
    bad = non_stdlib_imports(source)
    if bad:
        raise SystemExit(f"frames.py must import only the standard library; found {', '.join(bad)}")
    return BANNER + source


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the copy is stale")
    args = parser.parse_args(argv)
    rendered = render(SOURCE.read_text(encoding="utf-8"))
    if args.check:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if current != rendered:
            print(f"{TARGET} is stale; run make gen-kernel-rpc", file=sys.stderr)
            return 1
        return 0
    TARGET.write_text(rendered, encoding="utf-8")
    print(f"wrote {TARGET.relative_to(PACKAGES.parent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
