"""What the generators share when they emit an artifact for one composition.

Every generated artifact (the OpenAPI document, the agent tool manifest, the
daemon JSON-RPC schema) is produced from one of two compositions:

* ``product``: whatever ``CLI_INSTALL`` names, as ``module:function`` (the way
  ``BACKEND_INSTALL`` names the backend's); unset, it is the open CLI's own
  composition. A distribution's Makefile sets its own.
* ``open``: the open platform alone, with nothing installed. Its output lands
  under ``packages/shared-openapi/open/``.

Each generator takes the composition as an argument and runs once per
composition in a fresh process (``make gen-open-subset``), so the open run never
sees a registration left behind by the product run.
"""

from __future__ import annotations

import argparse
import importlib
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

REPO_ROOT = Path(__file__).resolve().parents[1]
OPEN_DIR = REPO_ROOT / "packages" / "shared-openapi" / "open"

Composition = Literal["product", "open"]

#: Names the installer a ``product`` run calls, as ``module:function``.
CLI_INSTALL_ENV = "CLI_INSTALL"


class CompositionError(RuntimeError):
    """``CLI_INSTALL`` does not name an installer this process can call."""


def install_composition(composition: Composition) -> None:
    """Install what ``composition`` runs with, before anything reads an
    extension point: for ``product``, the installer ``CLI_INSTALL`` names (the
    open CLI's when unset); for ``open``, nothing."""
    if composition == "open":
        return
    from alkera_cli.entry import OPEN_COMPOSITION

    spec = os.environ.get(CLI_INSTALL_ENV) or OPEN_COMPOSITION
    module_name, _, function_name = spec.partition(":")
    if not module_name or not function_name:
        raise CompositionError(f"{CLI_INSTALL_ENV}={spec!r} is not `module:function`")
    try:
        installer = getattr(importlib.import_module(module_name), function_name)
    except (ImportError, AttributeError) as error:
        raise CompositionError(f"{CLI_INSTALL_ENV}={spec!r} cannot be loaded: {error}") from error
    installer()


def parse_composition(argv: Sequence[str] | None, description: str) -> Composition:
    """``--open`` selects the open composition; the default is the product."""
    return parse_export(argv, description, with_out=False)[0]


def parse_export(
    argv: Sequence[str] | None, description: str, *, with_out: bool = True
) -> tuple[Composition, Path | None]:
    """The composition, and ``--out`` when the caller names where to write.

    A relative ``--out`` is relative to where the caller runs, not to this
    script: a distribution that carries this tree runs it from its own root
    and writes into its own tree."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--open",
        action="store_true",
        help="emit the open subset (nothing private installed) under packages/shared-openapi/open/",
    )
    if with_out:
        parser.add_argument(
            "--out", type=Path, help="where to write, relative to the current directory"
        )
    args = parser.parse_args(argv)
    out = getattr(args, "out", None)
    return ("open" if args.open else "product"), (None if out is None else out.resolve())
