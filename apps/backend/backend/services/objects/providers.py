"""The content providers the API host resolves a node's bytes through.

The library ships the registry and every provider; this is the one place the
backend process fills one. Without it a node whose bytes are *derived* has no
reachable content at all: the ``spec.json`` and ``README.md`` inside a
``.alkerareport`` / ``.alkeraquery`` folder are re-rendered from the object's
rows on every read and never stored, so there is no head version for the
content route to mint a signed URL against, and the half of a replication
context a human (or a fresh agent) is told to read first answers 404.

Only the ``rows:<object type>`` providers are registered here. Stored bytes
never reach this registry on the API host — they are served from the content
origin under a signed URL, which is what keeps a user's upload off this
hostname — so registering the store-backed provider beside these would offer a
second way to serve them that no caller should take.

Built per request rather than at import: a provider is bound to the caller's
own repo, and therefore to the caller's org, so a registry shared across
requests would be a registry bound to whichever org built it first.
"""

from __future__ import annotations

from typing import Any

from alkera_core.files.providers.registry import ProviderRegistry
from alkera_core.files.providers.rows import register_rows_providers, standard_renderers
from alkera_core.models.workspace_object import WorkspaceObject
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.files.context import FilesContext
from backend.services.objects import object_service

__all__ = ["rendered_providers", "update_query_spec"]


async def update_query_spec(
    session: AsyncSession, obj: WorkspaceObject, spec: dict[str, Any]
) -> None:
    """Apply a spec edited through the file surface as the object service applies it.

    The query rendering never writes rows itself. Injecting the object
    service's own update is what makes an edit made in a mount take the same
    lock, the same version check and the same node bump an edit made on the
    object page takes.
    """
    await object_service.apply_update(session, obj=obj, expected_version=obj.version, spec=spec)


def rendered_providers(files: FilesContext) -> ProviderRegistry:
    """The providers for content this request's org renders rather than stores."""
    providers = ProviderRegistry()
    register_rows_providers(
        providers, standard_renderers(update_query=update_query_spec), files.repo
    )
    return providers
