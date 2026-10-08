"""Where the daemon protocol exporter (``scripts/export_daemon_schema.py``) writes.

The open protocol is the open tree's own artifact. The product's protocol has
no default output: the product names it, so after the move the open tree's
exporter never writes into a folder outside it. Each run is a subprocess, as
the Makefile runs it, so the registry is the one that process builds.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spread]

REPO_ROOT = Path(__file__).resolve().parents[4]
EXPORTER = REPO_ROOT / "scripts" / "export_daemon_schema.py"
OPEN_ARTIFACT = REPO_ROOT / "packages" / "shared-openapi" / "open" / "daemon-schema.json"


def _export(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(EXPORTER), *args], capture_output=True, text=True, cwd=cwd
    )


def test_the_open_protocol_is_the_committed_open_artifact(tmp_path: Path) -> None:
    out = tmp_path / "open.json"
    done = _export("--open", "--out", str(out))
    assert done.returncode == 0, done.stderr
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written == json.loads(OPEN_ARTIFACT.read_text(encoding="utf-8"))
    assert "ping" in written["x-alkera-methods"]


def test_the_product_protocol_has_no_default_output(tmp_path: Path) -> None:
    done = _export(cwd=tmp_path)
    assert done.returncode == 2
    assert "pass --out" in done.stderr
    assert list(tmp_path.iterdir()) == []


def test_a_relative_output_lands_under_the_callers_directory(tmp_path: Path) -> None:
    # The product runs the open tree's exporter from its own root and names a
    # path in its own tree; resolving it against the script's checkout would
    # write the product's protocol into the open tree.
    done = _export("--out", "protocol/schema.json", cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    written = json.loads((tmp_path / "protocol" / "schema.json").read_text(encoding="utf-8"))
    assert "ping" in written["x-alkera-methods"]
    assert not (REPO_ROOT / "protocol").exists()
