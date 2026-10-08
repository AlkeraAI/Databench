"""Alkera daemon — JSON-RPC 2.0 server over stdio.

Spawned by editor extensions (VS Code, JetBrains) as a child process. Reads
LSP-style Content-Length-framed JSON-RPC from stdin, writes responses to
stdout, logs to stderr + ``~/.alkera/logs/daemon.log``.

The wire format matches the Language Server Protocol's framing exactly so
extension authors can use battle-tested clients (`vscode-jsonrpc`,
`LSP4J jsonrpc`) without any custom framing code on the client side.

Public surface from this package:
- `JsonRpcServer`              — the dispatcher
- `read_frame` / `write_frame` — raw framing
- `method`                     — decorator registering request handlers
- `METHODS`                    — the registry
"""

from __future__ import annotations

from alkera_cli.daemon.framing import (
    FramingError,
    read_frame,
    read_frame_async,
    write_frame,
    write_frame_async,
)
from alkera_cli.daemon.pipes import PipeWriter, connect_pipe_reader, connect_pipe_writer
from alkera_cli.daemon.protocol import METHODS, MethodSpec, method
from alkera_cli.daemon.server import JsonRpcServer, run_stdio

__all__ = [
    "METHODS",
    "FramingError",
    "JsonRpcServer",
    "MethodSpec",
    "PipeWriter",
    "connect_pipe_reader",
    "connect_pipe_writer",
    "method",
    "read_frame",
    "read_frame_async",
    "run_stdio",
    "write_frame",
    "write_frame_async",
]
