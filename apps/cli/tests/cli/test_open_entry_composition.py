"""The open entry points install the open platform's composition.

The open wheel's console script (``alkera_cli.entry:console_main``), ``python -m
alkera_cli`` in the open tree (``alkera_cli.entry.main``) and the node bundle's
launcher all call the open entry with no installer of their own. Before the fix
that ran the CLI and the box with no extension: no connector, no SQL tools, no
connection record, and a box chat answered "Unknown tool 'sql.connections'".

Each check runs the real target in a fresh interpreter, the way a process
starts, then builds the tools the way a chat's runtime builds them.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.xdist_group("open_entry_composition")

_PROBE = """
import asyncio, json, sys, tempfile
from pathlib import Path

sys.argv = ["alkera", "--version"]
from alkera_cli import entry

try:
    entry.{target}()
except SystemExit:
    pass

from alkera_core.extensions import installed_extensions

from alkera_cli.harness import HarnessRuntime
from alkera_cli.harness.extension_points import connection_records
from alkera_cli.host.paths import project_directory
from alkera_cli.plugins.plugin_base.agent_tools import AGENT_TOOLS, ToolBuild
from alkera_cli.plugins.plugin_base.plugin import CLI_PLUGINS
from alkera_cli.plugins.plugin_base.registry import PluginRegistry
from alkera_cli.plugins.plugin_base.tool import ToolRegistry

root = Path(tempfile.mkdtemp())
project = project_directory(root)
# What a chat on this process is served.
chat_tools = asyncio.run(HarnessRuntime(project).tool_registry())
# Every family the installed tool sources light up once a connection can run SQL.
listing = ToolRegistry(project.blobs())
build = ToolBuild(project=project, plugins=PluginRegistry(project, root), every_family=True)
for source in AGENT_TOOLS.items():
    source.register(listing, build)
report = {{
    "installed": list(installed_extensions()),
    "plugins": sorted(plugin().manifest.name for plugin in CLI_PLUGINS.items()),
    "chat_tools": sorted(spec.name for spec in chat_tools.all_specs()),
    "connection_tools": sorted(spec.name for spec in listing.all_specs()),
    "records_connections": connection_records() is not None,
}}
print("REPORT" + json.dumps(report))
"""


def _run(target: str, home: Path) -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(_PROBE.format(target=target))],
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


@pytest.fixture(scope="module", params=["console_main", "main"])
def open_entry(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> dict[str, Any]:
    target: str = request.param
    return _run(target, tmp_path_factory.mktemp(target))


def test_the_open_entry_installs_the_generic_sql_connector(open_entry: dict[str, Any]) -> None:
    assert "alkera.generic-sql" in open_entry["installed"]
    assert open_entry["plugins"] == ["generic_sql"]


def test_the_open_entry_lights_up_the_sql_tools(open_entry: dict[str, Any]) -> None:
    assert {"sql.connections", "sql.query", "sql.schema"} <= set(open_entry["connection_tools"])


def test_a_chat_on_the_open_entry_is_served_the_notebook_tools(open_entry: dict[str, Any]) -> None:
    assert {"notebook.create", "notebook.edit", "notebook.read", "notebook.run"} <= set(
        open_entry["chat_tools"]
    )


def test_the_open_entry_records_connection_state(open_entry: dict[str, Any]) -> None:
    assert open_entry["records_connections"] is True


_OPEN_CLI = """
import sys

sys.argv = ["alkera", *{argv!r}]
from alkera_cli import entry

entry.main()
"""


def _run_open_cli(argv: list[str], home: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(_OPEN_CLI.format(argv=argv))],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        env={**os.environ, "ALKERA_HOME": str(home), "NO_COLOR": "1"},
        cwd=home,
    )


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("chat", id="terminal-chat"),
        pytest.param("report", id="crash-reports"),
        pytest.param("__manifest__", id="release-manifest"),
        pytest.param("daemon-selftest", id="release-selftest"),
    ],
)
def test_the_open_cli_has_none_of_the_product_s_commands(command: str, tmp_path: Path) -> None:
    result = _run_open_cli([command, "--help"], tmp_path)
    assert result.returncode == 2, result.stdout[-4000:] + result.stderr[-4000:]
    assert f"No such command '{command}'" in result.stderr


def test_the_open_serve_has_no_selftest(tmp_path: Path) -> None:
    result = _run_open_cli(["serve", "--selftest"], tmp_path)
    assert result.returncode == 2, result.stdout[-4000:] + result.stderr[-4000:]
    assert "No such option: --selftest" in result.stderr


def test_bare_open_alkera_shows_the_help(tmp_path: Path) -> None:
    """With no default command registered, bare `alkera` is the help screen."""
    result = _run_open_cli([], tmp_path)
    assert result.returncode == 0, result.stderr[-4000:]
    assert "Usage" in result.stdout
    assert "login" in result.stdout
