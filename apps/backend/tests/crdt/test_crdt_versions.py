"""The Loro version pair is pinned exactly on both sides.

The server (Python ``loro``) and the browser (``loro-crdt``) exchange Loro's
binary update and snapshot encodings, so a silent upgrade on either side is a
wire change: both move only deliberately, together, with the cross-language
peer suite rerun. Python is held at 1.16.2 because that is the newest wheel on
PyPI; the JS 1.16.4 fix for malformed movable-list ops that abort the process
is not in it, which the isolated sandbox validator contains. Move Python to
the matching release once it is published.
"""

from __future__ import annotations

import json
import re
import tomllib
from importlib import metadata
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]

PY_LORO = "1.16.2"
JS_LORO = "1.16.4"


def test_the_backend_pins_python_loro_exactly() -> None:
    deps = tomllib.loads((REPO / "apps/backend/pyproject.toml").read_text())["project"][
        "dependencies"
    ]
    assert f"loro=={PY_LORO}" in deps
    assert metadata.version("loro") == PY_LORO


def test_the_web_pins_loro_crdt_exactly() -> None:
    deps = json.loads((REPO / "apps/web/package.json").read_text())["dependencies"]
    assert deps["loro-crdt"] == JS_LORO, "an exact pin, never a range"
    assert re.fullmatch(r"\d+\.\d+\.\d+", deps["loro-crdt"])


def test_no_yjs_stack_remains() -> None:
    web = json.loads((REPO / "apps/web/package.json").read_text())
    assert "yjs" not in {**web.get("dependencies", {}), **web.get("devDependencies", {})}
    core = (REPO / "packages/api-core/pyproject.toml").read_text()
    assert "pycrdt" not in core
