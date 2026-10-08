"""Daemon methods for global agent instructions — `instructions.get` /
`instructions.set`.

The VS Code extension reads/writes the user's GLOBAL instructions
(`~/.alkera/instructions.md`) through the daemon (same path auth + preferences
take) so the CLI and the extension share ONE concurrency-safe writer:
`alkera_cli.preferences.instructions`, which holds a `FileLock` across the write.

Unlike `preferences.set` (a field MERGE), `instructions.set` REPLACES the whole
document — it's a single free-form Markdown blob, edited wholesale in the panel.
An empty string is valid and clears the instructions.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from alkera_cli.daemon.protocol import _DaemonModel, method
from alkera_cli.preferences.instructions import load_global_instructions, save_global_instructions

if TYPE_CHECKING:
    from alkera_cli.daemon.server import JsonRpcServer


# --- instructions.get ------------------------------------------------------


class InstructionsGetRequest(_DaemonModel):
    pass


class InstructionsGetResponse(_DaemonModel):
    content: str
    """The full stored global instructions (``""`` when none are set)."""


@method("instructions.get")
async def instructions_get(
    server: JsonRpcServer, params: InstructionsGetRequest
) -> InstructionsGetResponse:
    loop = asyncio.get_running_loop()
    content = await loop.run_in_executor(None, load_global_instructions)
    return InstructionsGetResponse(content=content)


# --- instructions.set ------------------------------------------------------


class InstructionsSetRequest(_DaemonModel):
    content: str
    """The full Markdown document to store, replacing any prior content."""


class InstructionsSetResponse(_DaemonModel):
    content: str
    """The stored content after the write."""


@method("instructions.set")
async def instructions_set(
    server: JsonRpcServer, params: InstructionsSetRequest
) -> InstructionsSetResponse:
    incoming = params.content
    loop = asyncio.get_running_loop()
    stored = await loop.run_in_executor(None, save_global_instructions, incoming)
    return InstructionsSetResponse(content=stored)


__all__ = [
    "InstructionsGetRequest",
    "InstructionsGetResponse",
    "InstructionsSetRequest",
    "InstructionsSetResponse",
]
