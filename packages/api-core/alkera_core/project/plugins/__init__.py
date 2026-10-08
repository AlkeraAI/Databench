"""Plugin state persistence — the api-core side.

``PluginStore`` lives here (not in ``alkera_cli.plugins.plugin_base``) because
``ProjectDirectory.plugins()`` constructs it and api-core must NEVER import
``alkera_cli``. It mirrors how ``ChatStore`` lives here and is returned by
``ProjectDirectory.chats()``. The ``PluginState`` base model (the plugin-
author contract) lives in ``plugin_base/state.py``, which re-exports
``PluginStore`` so authors have one import root.
"""

from __future__ import annotations

from alkera_core.project.plugins.store import PluginStore

__all__ = ["PluginStore"]
