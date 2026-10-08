"""Which uid runs a chat's agent, as the chat tree asks it.

The tree itself lives in :mod:`alkera_cli.files.chat_fs` so the files package
(the live sync, the pull) can use it without importing the cloud package; this
adds the one thing only the daemon knows: which uid runs a chat's agent
(:func:`sandbox_identity`), installed as the tree's resolver when the daemon
starts (:func:`install_sandbox_identity`). The cloud service reads the tree
through here, so it names the two tree types it uses.
"""

from __future__ import annotations

from alkera_cli.files.chat_fs import (
    ChatTree,
    ChatTreeError,
    TreeIdentity,
    has_identity_resolver,
    set_identity_resolver,
)
from alkera_cli.harness.sandbox import chat_identity

__all__ = ["ChatTree", "ChatTreeError", "install_sandbox_identity", "sandbox_identity"]


def sandbox_identity(chat_id: str) -> TreeIdentity | None:
    """The chat's sandbox identity in the tree's shape: the one answer the
    sandbox gives to which uid the agent's commands run as, and so which uid
    every file in the chat's tree is handed to
    (:func:`alkera_cli.harness.sandbox.chat_identity`); ``None`` on a host with
    no per-chat uid (not root, no ``setpriv``, a laptop, a test), where files
    stay the daemon's, which is also the agent's.

    The user is made on first use, the same user the spawn runs the agent as,
    so a file the daemon lands before the chat has ever run is the chat's by
    the time the agent first opens it."""
    return chat_identity(chat_id)


def install_sandbox_identity() -> None:
    """Make :func:`sandbox_identity` the tree's resolver, unless another one
    (a sandbox that runs agents under a different uid) was installed first."""
    if not has_identity_resolver():
        set_identity_resolver(sandbox_identity)
