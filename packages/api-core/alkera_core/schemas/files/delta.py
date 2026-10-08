"""The change-feed page, and the wire re-export of the delta token.

The token itself lives in `alkera_core.files.delta_token`: the library mints it,
signs it and honours it, so there is one shape rather than a library struct and
a schemas model kept in step by hand. This module re-exports it — the schemas
layer may import the library, never the reverse — so every importer of
`alkera_core.schemas.files.delta` keeps working, and the fixture generator keeps
discovering the token's `FIXTURE_EXAMPLES` here beside the rest of the wire.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from alkera_core.files.delta_token import (
    DeltaToken as DeltaToken,
)
from alkera_core.files.delta_token import (
    InvalidDeltaToken as InvalidDeltaToken,
)
from alkera_core.files.delta_token import (
    decode_token as decode_token,
)
from alkera_core.files.delta_token import (
    encode_token as encode_token,
)
from alkera_core.versioning import VersionedModel

from .item import Item


class ResyncCode(StrEnum):
    """What a `410` tells the client to do with the state it already has."""

    APPLY_DIFFERENCES = "resync_apply_differences"
    UPLOAD_DIFFERENCES = "resync_upload_differences"


class DeltaPage(BaseModel):
    """One page of the feed: latest state by id, plus where to read next."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    items: list[Item] = Field(default_factory=list)
    next_link: str | None = None
    delta_link: str | None = None


FIXTURE_EXAMPLES: list[tuple[str, Callable[[], VersionedModel]]] = [
    (
        "delta_token",
        lambda: DeltaToken(
            outbox_id=4_294_967_296,
            issued_at=datetime.fromisoformat("2026-01-01T12:00:00+00:00"),
            generation=3,
            cursor_xid=4_294_967_000,
        ),
    ),
]

__all__ = [
    "FIXTURE_EXAMPLES",
    "DeltaPage",
    "DeltaToken",
    "InvalidDeltaToken",
    "ResyncCode",
    "decode_token",
    "encode_token",
]
