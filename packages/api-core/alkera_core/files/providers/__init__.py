"""Where a node's bytes come from.

A node's content is served by exactly one provider: the store (``bytes``), a
deterministic rendering of Postgres rows (``rows:<object type>``), or the small
signed JSON that stands for a row-backed object on a real filesystem
(``pointer``). Content routes, ``pull``, WebDAV, search previews and the box all
read through :class:`alkera_core.files.providers.registry.ProviderRegistry`, so
nothing outside this package branches on "is this a chat".

Modules are imported by path (``from alkera_core.files.providers.registry import
ProviderRegistry``).
"""

from __future__ import annotations
