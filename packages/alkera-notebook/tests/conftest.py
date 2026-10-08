"""Puts this directory on ``sys.path`` so test modules in ``cases/`` can
import the shared helpers (``nbeng_*``) by name, and names the actors the way
the package names them standalone."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from alkera_notebook import actors

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


@pytest.fixture(autouse=True)
def _standalone_actor_names() -> Iterator[None]:
    """The package's own names (``Databench``, ``Agent``), whatever brand a
    platform composed into this process handed in."""
    handed_in = actors.name_actors_with(lambda: actors.OPEN_ACTOR_NAMES)
    yield
    actors.name_actors_with(handed_in)
