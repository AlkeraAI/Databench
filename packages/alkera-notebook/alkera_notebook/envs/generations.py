"""Builds of a managed environment kept as generations.

An environment the registry manages outside the workspace tree (``default``
and ``script``) is a symbolic link at its prefix to the build in use, one of
the generations in ``<prefix>.gens/``. A build goes into a new generation; only
once it succeeded (its interpreter started and is the build asked for) does
the link move to it, in one ``rename``. A failed or cancelled build removes
only its own generation, so the environment in use is never half changed.
The previous generation is kept, whole, for a kernel still running on it;
older ones are removed.

The link is relative, so the environment reads the same wherever its root is
mounted. A project's own ``.venv`` (a ``uv_project``) is the person's
directory in the tree and is built in place, as ``uv`` does.
"""

from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path

from alkera_notebook.tree_io import Tree

GENERATIONS_SUFFIX = ".gens"


def generations_dir(prefix: Path) -> Path:
    return prefix.parent / f"{prefix.name}{GENERATIONS_SUFFIX}"


def new_generation(prefix: Path) -> Path:
    """Where the next build goes (not made: the build makes it)."""
    return generations_dir(prefix) / f"g{time.time_ns()}"


def in_use(prefix: Path) -> Path | None:
    """The build the prefix stands for now, or None when there is none."""
    if prefix.is_symlink():
        target = prefix.resolve()
        return target if target.exists() else None
    return prefix if prefix.exists() else None


def adopt(tree: Tree, prefix: Path, generation: Path) -> None:
    """Make ``generation`` the build in use, then remove all but it and the
    build it replaced. ``tree`` is the env root the prefix lies in: a link
    the tree's writers placed there is never followed."""
    gens = generations_dir(prefix)
    previous = in_use(prefix)
    if tree.is_dir(prefix):
        # A build made before generations: it becomes the previous one.
        legacy = gens / "g0"
        discard(tree, legacy)
        tree.make_dirs(gens)
        tree.replace(prefix, legacy)
        previous = legacy
    link = prefix.parent / f".{prefix.name}.next"
    tree.unlink(link)
    tree.symlink(os.path.relpath(generation, prefix.parent), link)
    tree.replace(link, prefix)
    keep = {generation.name, *([previous.name] if previous is not None else [])}
    for name in tree.dirs(gens):
        if name not in keep:
            discard(tree, gens / name)


def discard(tree: Tree, generation: Path) -> None:
    with contextlib.suppress(OSError):
        tree.rmtree(generation)
