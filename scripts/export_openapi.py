"""Export the backend's OpenAPI document for one composition.

The composition is the app the document describes:

* the open app (the default): ``backend.app_factory.create_app()`` with
  nothing installed, which is the backend the open repository ships. Its
  document is what the open tree commits and generates its clients from.
* a composition root named with ``--app MODULE:ATTR``, the same value the
  Makefile's ``BACKEND_APP`` serves. This script names no root of its own,
  so it never imports a private module.

``--out`` names the file; a relative path is relative to the root of the tree
this script is in. Without it the open app's document goes to
``packages/shared-openapi/open/openapi.json`` and a named root's to
``packages/shared-openapi/openapi.json``.

Run it from ``apps/backend`` (``ops/scripts/gen-openapi.sh`` does). The
document is the same on every machine: its title is the composition's brand,
never the ``BRAND_PRODUCT_NAME`` of whatever env file the settings read.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from alkera_core.brand import current_brand
from fastapi import FastAPI

#: This script imports nothing beside it (the subset tooling reads the private
#: boundary manifest), so it runs in the open tree alone.
REPO_ROOT = Path(__file__).resolve().parents[1]
NAMED_ROOT_OUTPUT = REPO_ROOT / "packages" / "shared-openapi" / "openapi.json"
OPEN_OUTPUT = NAMED_ROOT_OUTPUT.parent / "open" / "openapi.json"


def build_app(root: str | None = None) -> FastAPI:
    """The app at ``root`` (``MODULE:ATTR``), or the open app when None.

    Import it in a fresh process: installing a composition's extensions is
    global, so an open app built after them would serve their routes too."""
    if root is None:
        from backend.app_factory import create_app

        return create_app()
    module, _, attr = root.partition(":")
    if not module or not attr:
        raise ValueError(f"--app takes MODULE:ATTR, got {root!r}")
    app = getattr(importlib.import_module(module), attr)
    if not isinstance(app, FastAPI):
        raise TypeError(f"{root} is not a FastAPI app")
    return app


def document(app: FastAPI) -> dict[str, Any]:
    """The app's document, titled with the composition's brand. The app's own
    title follows the deployment's product name, which make exports from the
    developer's .env."""
    built = dict(app.openapi())
    built["info"] = {**built["info"], "title": f"{current_brand().product_name} API"}
    return built


def render(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2) + "\n"


def output_path(root: str | None, out: Path | None) -> Path:
    if out is None:
        return OPEN_OUTPUT if root is None else NAMED_ROOT_OUTPUT
    return out if out.is_absolute() else REPO_ROOT / out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export the backend's OpenAPI document.")
    parser.add_argument(
        "--app", metavar="MODULE:ATTR", help="the composition root; the open app when omitted"
    )
    parser.add_argument("--out", type=Path, help="where to write, relative to this tree's root")
    args = parser.parse_args(argv)
    out = output_path(args.app, args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(document(build_app(args.app))), encoding="utf-8")
    sys.stderr.write(f"wrote {out} ({out.stat().st_size} bytes)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
