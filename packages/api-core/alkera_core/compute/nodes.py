"""The ``node_provider`` names, as aliases of the one provider surface.

:data:`NodeProvider` is
:class:`~alkera_core.compute.provider.ComputeProvider`, and the
``*node_provider*`` functions are the provider registry's own functions, so
there is one protocol and one registry. New code imports from ``provider.py``
directly.
"""

from __future__ import annotations

from alkera_core.compute.provider import (
    ComputeProvider,
    NodeDescription,
    NodeLaunch,
    ProviderFactory,
    provider_for_kind,
    register_provider,
    registered_kinds,
)

#: The lifecycle interface is the provider interface — one protocol.
NodeProvider = ComputeProvider
NodeProviderFactory = ProviderFactory

#: The registry is the provider registry — one registry, reached under the
#: older ``node_provider`` names.
register_node_provider = register_provider
node_provider_for_kind = provider_for_kind
node_provider_kinds = registered_kinds

__all__ = [
    "NodeDescription",
    "NodeLaunch",
    "NodeProvider",
    "NodeProviderFactory",
    "node_provider_for_kind",
    "node_provider_kinds",
    "register_node_provider",
]
