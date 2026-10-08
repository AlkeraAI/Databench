# Copyright 2026 Marimo. All rights reserved.
# Modified by Alkera: import paths rewritten; see vendor/marimo/README.alkera.md
from __future__ import annotations

from alkera_notebook._marimo._metadata.opengraph import (
    DEFAULT_OPENGRAPH_PLACEHOLDER_IMAGE_GENERATOR,
    OpenGraphMetadata,
    default_opengraph_image,
    resolve_opengraph_metadata,
)

__all__ = [
    "DEFAULT_OPENGRAPH_PLACEHOLDER_IMAGE_GENERATOR",
    "OpenGraphMetadata",
    "default_opengraph_image",
    "resolve_opengraph_metadata",
]
