"""A cloud box's chat uses the per-user connections of the workspace's owner.

Sharing a workspace shares every connection its owner may use, per-user ones
included, which run on the OWNER's own grant (leased for that chat or
workspace alone; see ``cloud_sync/test_box_owner_connections_nbsqw.py``). A box
chat's registry view admits exactly the records the server answered for that
chat, whatever their credential mode; a person's own daemon, which is
unscoped, keeps every connection it holds.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alkera_cli.contracts.tool_types import CredentialMode
from alkera_cli.plugins.plugin_base.connection import Connection
from alkera_cli.plugins.plugin_base.tool import ToolError, ToolRegistry
from alkera_core.project.directory import ProjectDirectory


def _registry(tmp_path: Path) -> ToolRegistry:
    reg = ToolRegistry(ProjectDirectory(tmp_path / ".alkera").blobs())
    for handle, mode in (
        ("shared_wh", CredentialMode.SHARED),
        ("owners_wh", CredentialMode.PER_USER),
        ("someone_elses_wh", CredentialMode.PER_USER),
    ):
        conn = Connection(handle=handle, plugin="postgres", credential_mode=mode)
        conn._runtime_bindings["team_record_id"] = f"rec-{handle}"
        reg.register_connection(conn, capabilities=None)
    return reg


def _box_view(reg: ToolRegistry) -> ToolRegistry:
    """The records the server answered for this chat: its owner's."""
    return reg.restricted(
        frozenset(),
        connection_ids=frozenset({"rec-shared_wh", "rec-owners_wh"}),
        knowledge_owner="u",
    )


def test_a_box_chat_sees_the_owners_shared_and_per_user_connections(tmp_path: Path) -> None:
    view = _box_view(_registry(tmp_path))
    assert sorted(c.handle for c in view.connections()) == ["owners_wh", "shared_wh"]
    assert view.connection_for("owners_wh") is not None


def test_a_box_chat_never_sees_a_record_the_server_did_not_answer(tmp_path: Path) -> None:
    ctx = _box_view(_registry(tmp_path)).build_context(fence=object())
    assert ctx.resolve_connection("owners_wh").handle == "owners_wh"
    with pytest.raises(ToolError, match="unknown connection handle 'someone_elses_wh'"):
        ctx.resolve_connection("someone_elses_wh")


def test_a_persons_own_daemon_keeps_every_connection(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    assert sorted(c.handle for c in reg.connections()) == [
        "owners_wh",
        "shared_wh",
        "someone_elses_wh",
    ]
