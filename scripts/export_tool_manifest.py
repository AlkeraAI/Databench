"""Export the agent tools as a single JSON Schema document.

Each alkera tool declares its ``Input``/``Output`` as Pydantic models (the
single source of truth, see ``alkera_cli.plugins.plugin_base.tool.Tool``).
This walks every concrete ``Tool`` subclass and emits one schema file the
TypeScript codegen (``packages/chat-model/scripts/gen-tool-schemas.mjs``) turns
into the frontend's tool-card types and name union. A backend tool rename then
changes this file, which regenerates the TS and fails the drift gate or `tsc`,
so the chat tool cards can never silently drift from the real tool surface.

The tools come from a composition, the way the daemon schema's methods do
(``scripts/export_daemon_schema.py``):

* the product (the default): the CLI with the composition ``CLI_INSTALL`` names
  installed (the open CLI's when unset), written to
  ``packages/shared-openapi/tool-manifest.json``;
* the open platform (``--open``): nothing installed, so only the platform's own
  tools, written to ``packages/shared-openapi/open/tool-manifest.json``.

The exporter names no private module: a private tool family reaches the product
manifest only by registering on ``AGENT_TOOLS`` or ``CLI_PLUGINS``. Run each
composition in its own process, so the open run never sees a product install.

Output shape (mirrors ``scripts/export_daemon_schema.py``):

    {
      "$schema": "https://json-schema.org/draft/2020-12/schema",
      "title": "AlkeraToolManifest",
      "$defs": { "SqlQueryInput": {...}, "SqlQueryResult": {...}, ... },
      "x-alkera-tools": {
        "sql.query": { "input": "SqlQueryInput", "output": "SqlQueryResult",
                       "title": "Run SQL", "app": null, "hot": true },
        ...
      }
    }
"""

from __future__ import annotations

import importlib
import json
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from alkera_cli.host.paths import project_directory
from alkera_cli.plugins.plugin_base.agent_tools import AGENT_TOOLS, ToolBuild
from alkera_cli.plugins.plugin_base.plugin import CLI_PLUGINS
from alkera_cli.plugins.plugin_base.registration import RecordingRegistrar
from alkera_cli.plugins.plugin_base.registry import PluginRegistry
from alkera_cli.plugins.plugin_base.tool import Tool, ToolRegistry
from open_subset import (
    OPEN_DIR,
    REPO_ROOT,
    Composition,
    install_composition,
    parse_composition,
)
from pydantic import BaseModel

#: The open platform's own agent tools: the modules whose tools every build of
#: the CLI carries. Importing one defines its Tool subclasses (the walk below
#: reads `Tool.__subclasses__()`). Everything else arrives through the
#: composition: a source registered on `AGENT_TOOLS` imports its tool family,
#: and a plugin registered on `CLI_PLUGINS` declares its tools when it registers.
PLATFORM_TOOL_MODULES: tuple[str, ...] = (
    "alkera_cli.notebooks.tools",
    "alkera_cli.plugins.plugin_base.background_tools",
    "alkera_cli.plugins.plugin_base.bash_tool",
    "alkera_cli.plugins.plugin_base.blob_inspect_tools",
    "alkera_cli.plugins.plugin_base.blob_tool",
    "alkera_cli.plugins.plugin_base.blob_write_tools",
    "alkera_cli.plugins.plugin_base.environment_tool",
    "alkera_cli.plugins.plugin_base.meta_tools",
    "alkera_cli.plugins.plugin_base.skill_tool",
    "alkera_cli.plugins.plugin_base.subagent_tool",
    "alkera_cli.plugins.plugin_base.task_tools",
    "alkera_cli.plugins.plugin_base.web_tools",
)

OUTPUTS: dict[Composition, Path] = {
    "product": REPO_ROOT / "packages" / "shared-openapi" / "tool-manifest.json",
    "open": OPEN_DIR / "tool-manifest.json",
}


def load_composition(composition: Composition) -> list[type[Tool[Any, Any]]]:
    """Load ``composition``'s tools and return the ones its registrations name.

    ``product`` installs the composition ``CLI_INSTALL`` names first (the open
    CLI's when unset), the order the CLI's own entry point keeps. Every tool
    source on ``AGENT_TOOLS`` then registers every family it could light up
    into a listing registry, and every plugin on
    ``CLI_PLUGINS`` registers into a recorder; either way the modules that
    define their tools are loaded. ``open`` installs nothing, so only the
    platform's own tools load."""
    install_composition(composition)
    for module in PLATFORM_TOOL_MODULES:
        importlib.import_module(module)
    named: list[type[Tool[Any, Any]]] = []
    with tempfile.TemporaryDirectory() as workspace:
        root = Path(workspace)
        project = project_directory(root)
        listing = ToolRegistry(project.blobs())
        build = ToolBuild(project=project, plugins=PluginRegistry(project, root), every_family=True)
        for source in AGENT_TOOLS.items():
            source.register(listing, build)
        for spec in listing.all_specs():
            tool = listing.tool_for(spec.name)
            if tool is not None:
                named.append(tool)
    for plugin_cls in CLI_PLUGINS.items():
        record = RecordingRegistrar()
        plugin_cls().register(record)
        named.extend(record.tools)
    return named


def _all_tool_classes() -> list[type[Tool[Any, Any]]]:
    """Every concrete Tool subclass, depth-first, deduped."""
    seen: dict[str, type[Tool[Any, Any]]] = {}

    def walk(cls: type[Tool[Any, Any]]) -> None:
        for sub in cls.__subclasses__():
            walk(sub)
            spec = getattr(sub, "spec", None)
            inp = getattr(sub, "Input", None)
            out = getattr(sub, "Output", None)
            if spec is None or inp is None or out is None:
                continue  # intermediate/abstract base
            if not (isinstance(inp, type) and issubclass(inp, BaseModel)):
                continue
            if not (isinstance(out, type) and issubclass(out, BaseModel)):
                continue
            if spec.name in seen and seen[spec.name] is not sub:
                raise ValueError(
                    f"duplicate tool name {spec.name!r}: "
                    f"{seen[spec.name].__name__} vs {sub.__name__}"
                )
            seen[spec.name] = sub

    walk(Tool)
    return [seen[name] for name in sorted(seen)]


def tool_classes(composition: Composition) -> list[type[Tool[Any, Any]]]:
    """Every tool ``composition`` carries. A tool a source or plugin registers but the
    walk cannot list (no spec, or a non-Pydantic ``Input``/``Output``) is an error,
    never a silent gap in the chat's tool cards."""
    named = load_composition(composition)
    classes = _all_tool_classes()
    unlisted = sorted(cls.__qualname__ for cls in named if cls not in classes)
    if unlisted:
        raise ValueError(f"registered tools the manifest cannot list: {unlisted}")
    return classes


def build_schema(composition: Composition = "product") -> dict[str, Any]:
    defs: dict[str, dict[str, Any]] = {}
    tools_index: dict[str, dict[str, Any]] = {}

    def put(name: str, body: dict[str, Any]) -> None:
        # $defs is keyed by class __name__, so two DIFFERENT models sharing a name would
        # silently mistype one tool (setdefault keeps the first). Fail loudly instead; the
        # fix is to make the model class names unique (e.g. plugin-qualify them).
        existing = defs.get(name)
        if existing is not None and existing != body:
            raise ValueError(
                f"tool-manifest model name collision: two different models are both named "
                f"{name!r}. Rename one (plugin-qualify it) so its schema isn't dropped."
            )
        defs[name] = body

    def register(model_cls: type[BaseModel]) -> str:
        schema = model_cls.model_json_schema(
            ref_template="#/$defs/{model}",
            mode="serialization",
        )
        nested = schema.pop("$defs", {})
        for name, body in nested.items():
            put(name, body)
        put(model_cls.__name__, schema)
        return model_cls.__name__

    for cls in tool_classes(composition):
        spec = cls.spec
        tools_index[spec.name] = {
            "input": register(cls.Input),
            "output": register(cls.Output),
            "title": spec.title,
            "app": spec.app,
            "hot": spec.hot,
        }

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "AlkeraToolManifest",
        "$defs": defs,
        "x-alkera-tools": tools_index,
    }


def render(schema: dict[str, Any]) -> str:
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    composition = parse_composition(argv, "Export the agent tool manifest.")
    schema = build_schema(composition)
    output = OUTPUTS[composition]
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render(schema), encoding="utf-8")
    tools = schema["x-alkera-tools"]
    sys.stderr.write(
        f"wrote {output.relative_to(REPO_ROOT)}: {len(tools)} tools, {len(schema['$defs'])} types\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
