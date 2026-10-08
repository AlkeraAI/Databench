"""Daemon JSON-RPC methods for plugins and result blobs.

The same per-project plugin registry the CLI uses backs these methods, so the
editor's plugins pane reads and changes exactly what the agent's tools see.
``blob.fetch`` pages a result blob a tool spilled, for the editor's result
viewer.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from pydantic import Field

from alkera_cli.app.runtime_pool import runtime_for
from alkera_cli.daemon.protocol import _DaemonModel, method

if TYPE_CHECKING:
    from alkera_cli.daemon.server import JsonRpcServer


class _ProjectScoped(_DaemonModel):
    project_path: str


class BlobFetchRequest(_ProjectScoped):
    """Page a result blob a tool spilled (the editor analogue of the
    ``fetch_result`` tool — lets the editor dereference a handle a tool result
    carried)."""

    handle: str
    offset: int = 0
    limit: int | None = None


class BlobFetchResponse(_DaemonModel):
    page: dict[str, Any] = Field(default_factory=dict)
    """The paginated envelope (``{kind, columns?, rows?|text?, total, has_more,
    next_offset, …}``), or ``{"error": …}`` for an unknown/invalid handle."""


class PluginInfo(_DaemonModel):
    """A discovered plugin's advertised shape + live state."""

    name: str
    version: str = ""
    description: str = ""
    surfaces: list[str] = Field(default_factory=list)
    active: bool = False
    enabled: bool = True
    """Whether the user has this plugin turned ON (default true). A disabled plugin
    stays listed (so the UI can re-enable it) but its tools are off the agent surface."""
    has_form: bool = False
    """True if the plugin declares a connection form (the UI offers + New connection);
    false ⇒ pure detect-then-add (no form), e.g. local DuckDB/dbt."""
    detected_connections: list[str] = Field(default_factory=list)


class PluginListRequest(_ProjectScoped):
    pass


class PluginListResponse(_DaemonModel):
    plugins: list[PluginInfo] = Field(default_factory=list)


class PluginEnableRequest(_ProjectScoped):
    name: str


class PluginEnableResponse(_DaemonModel):
    enabled: bool


class PluginDisableRequest(_ProjectScoped):
    name: str


class PluginDisableResponse(_DaemonModel):
    disabled: bool


def _plugin_info(registry: Any, plugin: Any) -> PluginInfo:
    m = plugin.manifest
    return PluginInfo(
        name=m.name,
        version=m.version,
        description=m.description,
        surfaces=sorted(str(s) for s in m.surfaces),
        active=registry.is_active(m.name),
        enabled=registry.is_enabled(m.name),
        has_form=plugin.connection_form_schema() is not None,
        detected_connections=[c.handle for c in registry.detected_for_plugin(m.name)],
    )


@method("blob.fetch")
async def blob_fetch(server: JsonRpcServer, params: BlobFetchRequest) -> BlobFetchResponse:
    """Page a result blob by handle (the editor path; same envelope the
    ``fetch_result`` tool returns). An unknown/invalid handle is a clean error
    page, not a JSON-RPC failure."""
    from alkera_cli.plugins.plugin_base.result_blob import paginate

    rt = runtime_for(server, params.project_path)
    try:
        # The reader's fixed-size pages are this RPC's contract: the editor's
        # numbered pager computes offsets from ``limit``, and no inline cap
        # applies on this wire (the model path fits pages through the door).
        page = await asyncio.to_thread(
            paginate, rt.project.blobs(), params.handle, offset=params.offset, limit=params.limit
        )
    except FileNotFoundError:
        return BlobFetchResponse(page={"error": f"no result blob {params.handle!r}"})
    except ValueError as exc:
        return BlobFetchResponse(page={"error": f"invalid blob handle: {exc}"})
    return BlobFetchResponse(page=page)


@method("plugin.list")
async def plugin_list(server: JsonRpcServer, params: PluginListRequest) -> PluginListResponse:
    """Every discovered plugin with its manifest + live (active) state."""
    rt = runtime_for(server, params.project_path)
    registry = await rt.plugin_registry()
    return PluginListResponse(plugins=[_plugin_info(registry, p) for p in registry.plugins()])


@method("plugin.enable")
async def plugin_enable(server: JsonRpcServer, params: PluginEnableRequest) -> PluginEnableResponse:
    """Turn a disabled plugin back ON — its tools/providers/connections return to the
    agent surface."""
    rt = runtime_for(server, params.project_path)
    return PluginEnableResponse(enabled=await rt.enable_plugin(params.name))


@method("plugin.disable")
async def plugin_disable(
    server: JsonRpcServer, params: PluginDisableRequest
) -> PluginDisableResponse:
    """Turn a plugin OFF — its tools/providers/connections drop off the agent surface
    (it stays listed so the UI can re-enable it)."""
    rt = runtime_for(server, params.project_path)
    return PluginDisableResponse(disabled=await rt.disable_plugin(params.name))


__all__ = [
    "BlobFetchRequest",
    "BlobFetchResponse",
    "PluginDisableRequest",
    "PluginDisableResponse",
    "PluginEnableRequest",
    "PluginEnableResponse",
    "PluginInfo",
    "PluginListRequest",
    "PluginListResponse",
    "blob_fetch",
    "plugin_disable",
    "plugin_enable",
    "plugin_list",
]
