"""An in-memory release host for :mod:`alkera_core.compute.box_builds`.

Serves the documents the bootstrap resolution reads (the stable pointer, a
node channel, a build's box manifest) from a dict, and records every path it
was asked for, so a test drives the resolution with no network.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from alkera_core.compute.box_contract import (
    BOX_MANIFEST_PATH,
    STABLE_CHANNEL_PATH,
    FetchJson,
    source_build,
)

#: The stable release the default host names.
FAKE_STABLE_VERSION = "1.4.2"


@dataclass
class FakeReleaseHost:
    """Documents by path under the release base. A path mapped to ``None`` is
    absent; one mapped to an exception raises it (an unreachable host)."""

    documents: dict[str, Mapping[str, Any] | BaseException | None] = field(default_factory=dict)
    asked: list[str] = field(default_factory=list)

    @classmethod
    def serving(cls, version: str = FAKE_STABLE_VERSION) -> FakeReleaseHost:
        """A host whose stable release is ``version``, a build with every
        command this checkout has."""
        manifest = source_build().to_manifest() | {"version": version}
        return cls(
            documents={
                STABLE_CHANNEL_PATH: {"version": version},
                BOX_MANIFEST_PATH.format(version=version): manifest,
            }
        )

    def fetcher(self, base_url: str) -> FetchJson:
        async def fetch(path: str) -> Mapping[str, Any] | None:
            self.asked.append(path)
            doc = self.documents.get(path)
            if isinstance(doc, BaseException):
                raise doc
            return doc

        return fetch


__all__ = ["FAKE_STABLE_VERSION", "FakeReleaseHost"]
