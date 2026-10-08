"""Daemon method for a workspace's environment spec: ``environment.get``.

Read-only. Capturing runs the environment's interpreter and recreating
installs packages, both on the person's own machine with no sandbox, so
neither is offered here until a surface exists that asks the person first;
the CLI (``alkera env``, the person's own command) and the agent's tools
(through their permission gate) are the ways to do either.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

from alkera_cli.daemon.protocol import _DaemonModel, method
from alkera_cli.environment import (
    read_spec,
    render_environment_instructions,
    summarize_spec,
)

if TYPE_CHECKING:
    from alkera_cli.daemon.server import JsonRpcServer


# --- environment.get ---------------------------------------------------------


class EnvironmentGetRequest(_DaemonModel):
    project_path: str


class EnvironmentGetResponse(_DaemonModel):
    spec: dict[str, Any] | None = None
    """The workspace's ``.alkera-environment.json``, or ``None`` when it has none."""
    summary: str = ""
    instructions: str = ""
    """The environment block a session starting in this workspace is given."""


@method("environment.get")
async def environment_get(
    server: JsonRpcServer, params: EnvironmentGetRequest
) -> EnvironmentGetResponse:
    root = Path(params.project_path)
    spec = await asyncio.to_thread(read_spec, root)
    instructions = await asyncio.to_thread(render_environment_instructions, root, tools=False)
    return EnvironmentGetResponse(
        spec=spec.model_dump(mode="json") if spec is not None else None,
        summary=summarize_spec(spec) if spec is not None else "",
        instructions=instructions,
    )


__all__ = ["EnvironmentGetRequest", "EnvironmentGetResponse"]
