"""Fixtures for the namespace suite."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from tests.files._kit.engine import close_hypothesis_runner


@pytest.fixture(scope="session", autouse=True)
def _close_the_shared_runner() -> Iterator[None]:
    """Close the loop and engine the property test here shares, once, at the end.

    The property test drives its examples through a process-wide runner rather
    than a fixture value, because the state machines elsewhere in the suite
    build theirs from ``__init__`` where no fixture is in reach. Closing it is
    idempotent, so every suite that shares it registers the same thing without
    coordinating.
    """
    yield
    close_hypothesis_runner()
