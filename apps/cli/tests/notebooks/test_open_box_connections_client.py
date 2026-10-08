"""The open build's box gives its notebooks a connections client.

A box notebook's SQL reads which connections its workspace has through the
connections client the box's data plane installs for the process. The open
platform's composition must install it on every credential a box runs on:
the platform's machine credential, and the self-hosted box's operator login
(``cloud-mirror run`` signed in as a person), where it once installed nothing
and every SQL cell was refused with "no connections client yet".

The probe runs in a fresh interpreter that installs only the open platform's
composition (the CLI suite's conftest installs the product in this one).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from alkera_core.token_prefixes import MACHINE_TOKEN_PREFIX

pytestmark = [pytest.mark.spread]

_PROBE = """
import json, sys
from pathlib import Path

from alkera_core.project.directory import ProjectDirectory

from alkera_cli.app import open_product

open_product.install()

from alkera_cli.cloud.box_data import box_data
from alkera_cli.cloud_sync.client import resolve_connections_client


class Runtime:
    project = ProjectDirectory(Path(sys.argv[1]) / ".alkera")


box_data().schema_cards(
    api_url="http://api.test",
    token=sys.argv[2],
    runtime=Runtime(),
    chats=list,
    refresh_interval=60.0,
    token_source=lambda: sys.argv[2],
)
client = resolve_connections_client()
print("REPORT" + json.dumps({
    "identity": None if client is None else client.identity_key,
    "reads_workspaces": callable(getattr(client, "records_for_workspace", None)),
}))
"""


def _probe(tmp_path: Path, token: str) -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(_PROBE), str(tmp_path), token],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        # A fresh home: no login on disk the client could fall back to.
        env={**os.environ, "ALKERA_HOME": str(tmp_path / "home"), "NO_COLOR": "1"},
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    line = next(line for line in result.stdout.splitlines() if line.startswith("REPORT"))
    report: dict[str, Any] = json.loads(line.removeprefix("REPORT"))
    return report


@pytest.mark.parametrize(
    "token",
    [
        pytest.param("person-session-jwt", id="operator-login"),
        pytest.param(MACHINE_TOKEN_PREFIX + "m" * 40, id="machine-credential"),
    ],
)
def test_the_open_box_installs_the_client_its_notebooks_read(tmp_path: Path, token: str) -> None:
    report = _probe(tmp_path, token)

    assert report == {
        "identity": hashlib.sha256(token.encode()).hexdigest()[:16],
        "reads_workspaces": True,
    }
