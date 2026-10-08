"""The chat's uid on the box: its user, how it is made, and the identity the
chat's working tree is handed to.

Every sandboxed chat runs as a system user of its own, in a range reserved for
chats (:data:`UID_MIN` to :data:`UID_MAX`) that the box's firewall keys its
metadata rule on. The user is made on first use (:func:`ensure_chat_uid`) and
is what the agent server, the agent's commands and the environment steps run
as. :func:`chat_identity` is the one answer to "who must own a file in the
chat's working tree": the mirror's writes from the drive, the tools' files, the
environment the agent installs into are all handed to it, so the agent's
commands can read and write every one of them.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from alkera_cli.files.chat_fs import TreeIdentity
from alkera_cli.harness.sandbox_layout import chat_user
from alkera_cli.harness.sandbox_scope import scope_of
from alkera_cli.harness.sandbox_steps import Runner, SandboxRefusedError, run_argv
from alkera_cli.harness.sandbox_uid_ledger import ledger_path, ledger_uid

if TYPE_CHECKING:
    from alkera_cli.harness.sandbox_probe import SandboxCapability

UID_MIN = 20000
UID_MAX = 59999


def useradd_argv(chat_id: str, useradd: str = "useradd") -> tuple[str, ...]:
    """Create the chat's system user in the reserved range, no home, no shell.

    A system account takes its uid from ``SYS_UID_MIN..SYS_UID_MAX``, not from
    ``UID_MIN..UID_MAX``, and the distros leave those at or below 999 (or
    unset, which means ``UID_MIN - 1``), so the system pair is the one that
    must be overridden. The account stays a system one on purpose: a regular
    account is also handed a subordinate uid/gid range in ``/etc/subuid``,
    which a chat must not hold, and password ageing it never uses."""
    return (
        useradd,
        "--system",
        "--no-create-home",
        "--shell",
        "/usr/sbin/nologin",
        "--key",
        f"SYS_UID_MIN={UID_MIN}",
        "--key",
        f"SYS_UID_MAX={UID_MAX}",
        "--key",
        f"SYS_GID_MIN={UID_MIN}",
        "--key",
        f"SYS_GID_MAX={UID_MAX}",
        "--user-group",
        chat_user(chat_id),
    )


Lookup = Callable[[str], int | None]
"""The uid of a user name, or ``None`` when there is no such user."""


def _default_lookup(name: str) -> int | None:
    try:
        import pwd
    except ImportError:  # pragma: no cover - Windows
        return None
    try:
        return pwd.getpwnam(name).pw_uid
    except KeyError:
        return None


def ensure_chat_uid(
    chat_id: str,
    *,
    run: Runner = run_argv,
    lookup: Lookup = _default_lookup,
) -> int:
    """The chat's uid, creating its user on first use.

    A uid outside the reserved range is refused: it would be a real account's,
    and the firewall rule that keeps chats away from the metadata service would
    not match it.

    An org worker records its chats' uids in its own root instead
    (:mod:`~alkera_cli.harness.sandbox_uid_ledger`): it has no host user table
    to write, and its ids are its org's alone."""
    name = chat_user(chat_id)
    ledger = ledger_path()
    if ledger is not None:
        return ledger_uid(ledger, name, low=UID_MIN, high=UID_MAX)
    uid = lookup(name)
    if uid is None:
        status = run(useradd_argv(chat_id))
        if status != 0:
            raise SandboxRefusedError(
                f"could not create the chat's user {name} (useradd exited {status})"
            )
        uid = lookup(name)
        if uid is None:
            raise SandboxRefusedError(f"the chat's user {name} was created but cannot be resolved")
    if not UID_MIN <= uid <= UID_MAX:
        raise SandboxRefusedError(
            f"the chat's user {name} has uid {uid}, outside {UID_MIN}-{UID_MAX}"
        )
    return uid


def owner_session(session_id: str, parent_session_id: str | None = None) -> str:
    """The session whose identity owns the working tree ``session_id`` runs in:
    a subagent child shares its parent's root, so it runs as the parent and its
    files are the parent's; every other session is its own owner. The one
    answer the plan, the command sandbox, the tree's resolver and the tools'
    writes all take, so a child never hands the parent's tree to itself."""
    return parent_session_id or session_id


def workspace_files_gid(tree: str, *, ensure: Callable[[str], int]) -> int:
    """The group every process that shares the workspace's tree writes through:
    the tree identity's own (``tree`` is the workspace's custody key, and an
    identity's gid is its uid). The agents under gVisor run as it; a member on
    a ``none`` box carries it as its one supplementary group
    (:func:`member_identity`); a notebook kernel and the environment build uid
    have it as their primary group. One answer, so all of them write files
    the others can change."""
    return ensure(tree)


def member_identity(
    tree_uid: int,
    owner: str,
    *,
    member: bool,
    mode: str,
    ensure: Callable[[str], int],
) -> tuple[int, int | None]:
    """The uid a session's processes run as and, for a workspace member on a
    ``none`` box, the group it shares the workspace's tree through:
    ``(uid, share_gid)``.

    Under gVisor every member runs as the tree's identity (``tree_uid``) in a
    container of its own, whose binds keep one member out of another's
    trees. With no container (``none``) the uid is the only boundary on the
    host, so a member runs as its own chat's uid (``owner``'s: the session,
    or the parent a subagent child shares its root with), and reaches the
    shared tree through the tree identity's group. A chat on its own runs as
    the tree's identity, which is its own. The group is
    :func:`workspace_files_gid`'s: ``tree_uid`` is the tree identity's uid,
    and its gid is the same number."""
    if not member or mode != "none":
        return tree_uid, None
    return ensure(owner), tree_uid


def chat_identity(
    chat_id: str,
    *,
    cap: SandboxCapability | None = None,
    ensure: Callable[[str], int] | None = None,
) -> TreeIdentity | None:
    """The identity files in the chat's working tree are handed to, or ``None``
    on a host with no per-chat uid (a box that is not root, a laptop, a test),
    where the daemon's own files stay the daemon's and the agent runs as the
    daemon anyway. ``cap`` is the host's probe (read here when not given) and
    ``ensure`` the uid lookup (:func:`ensure_chat_uid` when not given).

    Today the agent server and the agent's commands share the chat's one uid,
    and its group is that uid's. When the server's uid is split from the
    commands', this stays the commands' side and the chat's shared group, and
    every writer into the tree (the mirror landing a file from the drive, a
    tool spilling a result, the environment steps) follows without a change:
    the tree is theirs together, never the server's alone."""
    if cap is None:
        from alkera_cli.harness.sandbox_probe import current_capability

        cap = current_capability()
    if not cap.controls:
        return None
    # A workspace member's tree is the workspace's: its uid owns it.
    uid = (ensure or ensure_chat_uid)(scope_of(chat_id).tree)
    return TreeIdentity(uid=uid, gid=uid)


__all__ = [
    "UID_MAX",
    "UID_MIN",
    "Lookup",
    "TreeIdentity",
    "chat_identity",
    "ensure_chat_uid",
    "member_identity",
    "owner_session",
    "useradd_argv",
    "workspace_files_gid",
]
