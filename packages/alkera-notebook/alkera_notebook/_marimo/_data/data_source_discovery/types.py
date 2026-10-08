# Copyright 2026 Marimo. All rights reserved.
# Modified by Alkera: import paths rewritten; see vendor/marimo/README.alkera.md
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from alkera_notebook._marimo._data.data_source_discovery.models import DetectedDataSource

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence


@dataclass(frozen=True)
class DiscoveryContext:
    """Kernel-local inputs available to datasource discovery plugins."""

    environment: Mapping[str, str]


@dataclass(frozen=True)
class DiscoveryPlugin:
    """A self-contained, independently testable datasource detector."""

    id: str
    discover: Callable[
        [DiscoveryContext],
        Sequence[DetectedDataSource],
    ]
