"""``PluginState`` — the base every plugin's persisted state extends.

So ``SCHEMA_VERSION`` + ``MIGRATIONS`` + ``extra="allow"`` + fixture-lineage
come for free (the always-VersionedModel rule). The ``PluginStore[TState]``
*mechanism* lives in api-core (so ``ProjectDirectory`` can construct it
without importing ``alkera_cli``); we re-export it here so plugin authors
have one import root.

    from alkera_cli.plugins.plugin_base.state import PluginState, PluginStore
"""

from __future__ import annotations

from alkera_core.project.plugins import PluginStore
from alkera_core.versioning import VersionedModel


class PluginState(VersionedModel):
    """Base for a plugin's private bookkeeping (discovered connections,
    refresh watermarks/cursors, sync metadata, cached introspection,
    settings). Subclass + set ``SCHEMA_VERSION``; every field defaulted so
    partial/corrupt data fails gracefully.

    NOT for the KB items / lineage facts a plugin *produces* — those live in
    the context/ and lineage/ engines. Credentials are NEVER state — only
    ``credential_ref`` pointers persist.
    """

    __abstract__ = True


__all__ = ["PluginState", "PluginStore"]
