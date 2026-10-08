"""The ``alkera-notebook`` distribution installs and imports on its own."""

from __future__ import annotations

import re
from pathlib import Path

import alkera_notebook

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def test_alkera_notebook_resolves_from_its_distribution() -> None:
    assert Path(alkera_notebook.__file__).resolve().parent == PACKAGE_ROOT / "alkera_notebook"


def test_version_is_a_release_string() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", alkera_notebook.__version__)
