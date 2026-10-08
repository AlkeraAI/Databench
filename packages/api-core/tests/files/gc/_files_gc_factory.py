"""The store factory a janitor test hands the crossing role.

The janitor takes a `ScopedStoreFactory` and asks it for the bucket-wide handle
itself, so no request-path signature ever names `ObjectStore`. A test that
already holds an admin store (a `FilesystemStore` rooted above every domain, or
a recording wrapper around one) wraps it here rather than reaching past the
constructor.

Its own module rather than `conftest.py`: the test folders have no packages, so a helper
imported by name must have a basename nothing else in the tree shares.
"""

from __future__ import annotations

from typing import Any


class AdminOnlyFactory:
    """A `ScopedStoreFactory` that only ever hands out the bucket-wide handle."""

    def __init__(self, store: Any) -> None:
        self._store = store

    async def for_domain(self, domain_id: Any) -> Any:
        """Refused: a janitor that asked for one would be crossing the wrong way."""
        raise AssertionError("the janitor never takes a domain-bound handle")

    def admin(self) -> Any:
        return self._store
