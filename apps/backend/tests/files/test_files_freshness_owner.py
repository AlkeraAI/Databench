"""Only the freshness rule decides that a file was left on the machine.

``unsynced`` tells a reader a file is not saved. A second place deciding it --
the payload builder once folded a gone lease into ``unsynced`` for every
reported row, files with bytes on the drive included -- is how a saved file
came to read "not saved" after a box restart. So the word is produced in
``alkera_core.files.freshness`` alone; everything else asks it
(``content_state`` / ``under_lease``).

The scan reads the Python sources of the backend and the shared Files library
for the string constant ``"unsynced"``. The allowlist names the files that
spell it for another reason, and may only shrink.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
SCANNED = (
    REPO_ROOT / "apps" / "backend" / "backend",
    REPO_ROOT / "packages" / "api-core" / "alkera_core",
)
OWNER = Path("packages/api-core/alkera_core/files/freshness.py")
#: Files that spell the word for something other than deciding a row's state.
ALLOWED = {
    # The wire vocabulary itself.
    Path("packages/api-core/alkera_core/schemas/files/item.py"),
    # The release's audit payload key for how many files the drain left.
    Path("packages/api-core/alkera_core/files/leases.py"),
}


def _spellers() -> set[Path]:
    found: set[Path] = set()
    for root in SCANNED:
        for source in root.rglob("*.py"):
            tree = ast.parse(source.read_text(encoding="utf-8"))
            if any(
                isinstance(node, ast.Constant) and node.value == "unsynced"
                for node in ast.walk(tree)
            ):
                found.add(source.relative_to(REPO_ROOT))
    return found


def test_only_the_freshness_rule_says_a_file_was_left_on_the_machine() -> None:
    assert _spellers() - ALLOWED == {OWNER}


@pytest.mark.parametrize("allowed", sorted(ALLOWED), ids=str)
def test_every_allowlisted_file_still_needs_its_entry(allowed: Path) -> None:
    assert allowed in _spellers()
