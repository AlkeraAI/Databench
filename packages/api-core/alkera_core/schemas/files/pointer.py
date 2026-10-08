"""Pointer files — how a row-backed object is materialized on a real filesystem.

A pointer is derived, never authoritative: `push` skips it and `pull` refreshes
it. It is signed anyway, because a pointer that has been edited on disk must be
recognisable as edited (it is then warned about and ignored) rather than acted
on as if the server had written it.

The shape itself lives in the library (`alkera_core.files.providers.pointer`),
because it is persisted in the node's extension and the library may never import
its own API shapes. This module is the schemas-layer name for it, so every
surface that already reads `PointerFile`/`sign`/`verify`/`POINTER_EXTENSIONS`
from here keeps working — and the fixture generator keeps finding
`FIXTURE_EXAMPLES` where it walks, `alkera_core.schemas.files`.
"""

from __future__ import annotations

from alkera_core.files.providers.pointer import (
    FIXTURE_EXAMPLES,
    POINTER_EXTENSIONS,
    SIGNATURE_KEY,
    InvalidPointer,
    PointerFile,
    sign,
    verify,
)

__all__ = [
    "FIXTURE_EXAMPLES",
    "POINTER_EXTENSIONS",
    "SIGNATURE_KEY",
    "InvalidPointer",
    "PointerFile",
    "sign",
    "verify",
]
