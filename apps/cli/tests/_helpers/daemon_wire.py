"""Helpers for tests of what the daemon serves: what a fresh interpreter with no
extension registers, the names a generated protocol schema carries, and one
request over a real server's pipes."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

from alkera_cli.daemon import (
    JsonRpcServer,
    connect_pipe_reader,
    connect_pipe_writer,
    read_frame_async,
    write_frame_async,
)

#: The root of the open tree this helper ships in.
OPEN_ROOT = Path(__file__).resolve().parents[4]
#: What the open daemon serves, generated with the rest of the open subset.
OPEN_SCHEMA = OPEN_ROOT / "packages" / "shared-openapi" / "open" / "daemon-schema.json"
SCHEMA_INDEXES = ("x-alkera-methods", "x-alkera-notifications", "x-alkera-clientRequests")
REPLY_SECONDS = 30.0


OPEN_PROBE = """
import json
from typer.testing import CliRunner
from alkera_core.extensions import installed_extensions
from alkera_cli.main import app
from alkera_cli.daemon.methods import load_methods
from alkera_cli.daemon.protocol import CLIENT_REQUESTS, METHODS, NOTIFICATIONS

import ast
import inspect
import textwrap

import typer


def body_imports(callback):
    # The modules a command's own body imports when it runs.
    try:
        source = textwrap.dedent(inspect.getsource(inspect.unwrap(callback)))
    except (OSError, TypeError):
        return []
    names = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.append(node.module)
            names += [f"{node.module}.{alias.name}" for alias in node.names]
    return names


def leaves(command, path):
    if hasattr(command, "commands"):
        for name, child in command.commands.items():
            yield from leaves(child, [*path, name])
    elif command.callback is not None:
        yield " ".join(path), inspect.unwrap(command.callback).__module__, body_imports(
            command.callback
        )


contributed = load_methods()
runner = CliRunner()
report = {
    "installed": list(installed_extensions()),
    "contributed": [c.name for c in contributed],
    "methods": {name: spec.handler.__module__ for name, spec in METHODS.items()},
    "notifications": {name: cls.__module__ for name, cls in NOTIFICATIONS.items()},
    "client_requests": {name: pair[0].__module__ for name, pair in CLIENT_REQUESTS.items()},
    "leaves": [list(leaf) for leaf in leaves(typer.main.get_command(app), [])],
    "commands": {
        name: runner.invoke(app, [name, "--help"]).exit_code
        for name in ("context", "lineage", "gate", "tests", "test", "login", "files")
    },
}
print("REPORT" + json.dumps(report))
"""


def open_boot_report(home: Path) -> dict[str, Any]:
    """What the daemon registers and the CLI mounts in a fresh interpreter that
    installs no extension, run with ``home`` as its ``ALKERA_HOME`` and cwd."""
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(OPEN_PROBE)],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        env={**os.environ, "ALKERA_HOME": str(home), "NO_COLOR": "1"},
        cwd=home,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    line = next(line for line in result.stdout.splitlines() if line.startswith("REPORT"))
    report: dict[str, Any] = json.loads(line.removeprefix("REPORT"))
    return report


def schema_names(path: Path) -> dict[str, set[str]]:
    """The method, notification and client request names a schema declares."""
    schema = json.loads(path.read_text(encoding="utf-8"))
    return {index: set(schema[index]) for index in SCHEMA_INDEXES}


async def rpc_error(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Send one request to a real server over pipes and return its ``error``."""
    c2s_r, c2s_w = os.pipe()
    server_reader = await connect_pipe_reader(os.fdopen(c2s_r, "rb", buffering=0))
    client_writer = await connect_pipe_writer(os.fdopen(c2s_w, "wb", buffering=0))
    s2c_r, s2c_w = os.pipe()
    client_reader = await connect_pipe_reader(os.fdopen(s2c_r, "rb", buffering=0))
    server_writer = await connect_pipe_writer(os.fdopen(s2c_w, "wb", buffering=0))

    server = JsonRpcServer(reader=server_reader, writer=server_writer)
    task = asyncio.create_task(server.serve())
    envelope = {"jsonrpc": "2.0", "id": 7, "method": method, "params": params}
    await write_frame_async(client_writer, json.dumps(envelope).encode("utf-8"))
    frame = await asyncio.wait_for(read_frame_async(client_reader), REPLY_SECONDS)
    server.request_shutdown()
    client_writer.close()
    await asyncio.wait_for(task, timeout=REPLY_SECONDS)
    error: dict[str, Any] = json.loads(frame.decode("utf-8"))["error"]
    return error
