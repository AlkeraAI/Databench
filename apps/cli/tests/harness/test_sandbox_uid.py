"""The chat's identity on the box: who the working tree is handed to.

``chat_identity`` is the one answer every writer into a chat's tree reads (the
mirror's writes from the drive, the tools' spilled results and payloads, the
environment steps), so the agent's commands can read and write all of it.
These pin what it answers on each kind of host, and that it never makes a
user on a host that has no per-chat uid to make one for.
"""

from __future__ import annotations

import pytest
from alkera_cli.harness import sandbox as sb
from alkera_cli.harness.sandbox_probe import SandboxCapability
from alkera_cli.harness.sandbox_uid import TreeIdentity, chat_identity

CHAT = "chat_0123456789abcdef"

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
    platform="linux",
    root=False,
    setpriv=None,
    runsc=None,
    cgroup="none",
    reason="injected: no uid",
)


def test_the_identity_is_the_chats_uid_and_its_own_group() -> None:
    """The agent server and the agent's commands share this one uid today, and
    its group is that uid's; the day they split, this is the commands' side and
    the chat's group, and this test changes with it on purpose."""
    looked_up: list[str] = []

    def ensure(chat_id: str) -> int:
        looked_up.append(chat_id)
        return 20031

    assert chat_identity(CHAT, cap=WITH_UID, ensure=ensure) == TreeIdentity(uid=20031, gid=20031)
    assert looked_up == [CHAT]


def test_a_host_with_no_per_chat_uid_names_no_identity_and_makes_no_user() -> None:
    """A laptop, a test, a box that is not root: the daemon's files stay the
    daemon's, and no user is created for a uid nothing would run as."""

    def never(chat_id: str) -> int:
        raise AssertionError("no user may be made on a host with no per-chat uid")

    assert chat_identity(CHAT, cap=WITHOUT_UID, ensure=never) is None


def test_the_identity_reads_the_hosts_probe_when_none_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alkera_cli.harness import sandbox_probe

    monkeypatch.setattr(sandbox_probe, "current_capability", lambda: WITH_UID)
    assert chat_identity(CHAT, ensure=lambda _c: 20099) == TreeIdentity(uid=20099, gid=20099)
    monkeypatch.setattr(sandbox_probe, "current_capability", lambda: WITHOUT_UID)
    assert chat_identity(CHAT, ensure=lambda _c: 20099) is None


def test_the_identity_is_exported_where_the_launch_is() -> None:
    """The mirror and the tools import it from ``sandbox`` beside the launch."""
    assert sb.chat_identity is chat_identity
    assert sb.TreeIdentity is TreeIdentity
    assert "chat_identity" in sb.__all__ and "TreeIdentity" in sb.__all__
