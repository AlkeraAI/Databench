"""Forgetting to authorize is a type error, not a missing line of code.

``Authorized[A]`` is one class at runtime, so nothing an assertion can reach
distinguishes a read handle from a write handle — the distinction exists only
for ``mypy``, and the only honest way to test it is to run ``mypy``. Each case
below type-checks a snippet in a subprocess against the repo's own config and
asserts the checker's verdict, so the day the aliases collapse to ``Any`` the
negative case starts passing and this test fails.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[5]
CONFIG = REPO_ROOT / "pyproject.toml"

PRELUDE = """
from __future__ import annotations

from alkera_core.files.authz.authorize import Read, Share, Write


def rename(handle: Write) -> str:
    return handle.node.name_display
"""

#: A snippet, and whether ``mypy`` must accept it.
CASES = {
    "write-handle-into-a-write-parameter": (
        PRELUDE + "\n\ndef use(handle: Write) -> str:\n    return rename(handle)\n",
        True,
    ),
    "read-handle-into-a-write-parameter": (
        PRELUDE + "\n\ndef use(handle: Read) -> str:\n    return rename(handle)\n",
        False,
    ),
    "share-handle-into-a-write-parameter": (
        PRELUDE + "\n\ndef use(handle: Share) -> str:\n    return rename(handle)\n",
        False,
    ),
    "a-bare-node-into-a-write-parameter": (
        PRELUDE + "\n\nfrom alkera_core.models.files.tree import FileNode\n\n\n"
        "def use(node: FileNode) -> str:\n    return rename(node)\n",
        False,
    ),
}


def _mypy(snippet: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--config-file",
            str(CONFIG),
            "--no-incremental",
            "--cache-dir",
            str(snippet.parent / "cache"),
            str(snippet),
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        check=False,
    )


@pytest.mark.parametrize(
    ("source", "must_pass"),
    [pytest.param(src, ok, id=name) for name, (src, ok) in CASES.items()],
)
def test_a_handle_only_satisfies_its_own_action(
    tmp_path: Path, source: str, must_pass: bool
) -> None:
    snippet = tmp_path / "snippet.py"
    snippet.write_text(source, encoding="utf-8")
    result = _mypy(snippet)
    if must_pass:
        assert result.returncode == 0, result.stdout + result.stderr
    else:
        assert result.returncode != 0, result.stdout + result.stderr
        assert "arg-type" in result.stdout, result.stdout
