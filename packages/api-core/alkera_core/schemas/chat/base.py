"""Base class for every chat-storage Pydantic model.

`VersionedChatModel` adds a `provider_metadata` slot on top of
`VersionedModel`. This is the open passthrough for provider-specific
fields (Anthropic, OpenAI, Google, ...) that we don't want to collide
with our typed `metadata` bag. Cross-provider replay strips this field so a
fresh call on a different provider isn't poisoned by another's encoding.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import Field

from alkera_core.versioning import VersionedModel


class VersionedChatModel(VersionedModel):
    """Abstract base for chat events/parts/manifests.

    Adds `provider_metadata` to the `VersionedModel` skeleton. Don't
    instantiate directly — `__abstract__=True` marks it as intermediate.
    """

    __abstract__: ClassVar[bool] = True

    provider_metadata: dict[str, Any] = Field(default_factory=dict)
    """Passthrough provider-specific fields (e.g. Anthropic
    `cache_control`, OpenAI tool-result IDs). Isolated from `metadata`
    so cross-provider replay can strip without touching user data.
    """
