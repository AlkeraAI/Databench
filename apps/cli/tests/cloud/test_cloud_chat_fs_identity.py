"""Who owns what the box daemon writes into a chat's tree: the chat's sandbox
identity, the same answer the sandbox gives for the uid the agent's commands
run as, and nobody else.

``chat_fs`` asks one resolver; the box daemon installs the sandbox's when it
starts, so the uid a command is dropped to and the uid a written file is
handed to can never be two answers.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from alkera_cli.cloud import chat_fs as cloud_chat_fs
from alkera_cli.files import chat_fs
from alkera_cli.harness import sandbox_probe, sandbox_uid
from alkera_cli.harness.sandbox_probe import SandboxCapability

WITH_UID = SandboxCapability(
    platform="linux",
    root=True,
    setpriv="/usr/bin/setpriv",
    setfacl="/usr/bin/setfacl",
    runsc=None,
    cgroup="systemd",
    reason="injected: uid",
)
WITHOUT_UID = SandboxCapability(
    platform="linux", root=False, setpriv=None, runsc=None, cgroup="none", reason="injected"
)


@pytest.fixture(autouse=True)
def _default_resolver() -> Iterator[None]:
    previous = chat_fs.set_identity_resolver(None)
    yield
    chat_fs.set_identity_resolver(previous)


def test_the_sandbox_identity_is_the_chats_uid_and_group_on_a_box_with_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    looked_up: list[str] = []

    def ensure(chat_id: str) -> int:
        looked_up.append(chat_id)
        return 20005

    monkeypatch.setattr(sandbox_probe, "current_capability", lambda: WITH_UID)
    monkeypatch.setattr(sandbox_uid, "ensure_chat_uid", ensure)
    assert cloud_chat_fs.sandbox_identity("chat-1") == chat_fs.TreeIdentity(uid=20005, gid=20005)
    assert looked_up == ["chat-1"]


def test_a_host_with_no_per_chat_uid_hands_files_to_nobody_and_makes_no_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def never(chat_id: str) -> int:
        raise AssertionError("no user may be made on a host with no per-chat uid")

    monkeypatch.setattr(sandbox_probe, "current_capability", lambda: WITHOUT_UID)
    monkeypatch.setattr(sandbox_uid, "ensure_chat_uid", never)
    assert cloud_chat_fs.sandbox_identity("chat-1") is None


def test_installing_makes_it_the_trees_resolver_unless_one_is_there(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox_probe, "current_capability", lambda: WITH_UID)
    monkeypatch.setattr(sandbox_uid, "ensure_chat_uid", lambda chat_id: 20077)
    cloud_chat_fs.install_sandbox_identity()
    assert chat_fs.has_identity_resolver()
    assert chat_fs._resolver is cloud_chat_fs.sandbox_identity
    # The tree asks it, and gets the sandbox's answer.
    assert chat_fs.identity_for("chat-2") == chat_fs.TreeIdentity(uid=20077, gid=20077)

    other = chat_fs.TreeIdentity(uid=1, gid=1)
    chat_fs.set_identity_resolver(lambda _chat: other)
    cloud_chat_fs.install_sandbox_identity()
    assert chat_fs.identity_for("any") == other
