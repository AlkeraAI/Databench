"""Where a held folder lands on a box, what it is leased as, and what moves.

The folder custody (:class:`~alkera_cli.cloud.folder.ChatFolders`) holds two
kinds of folder under one set of verbs. A chat's own folder, keyed by the chat
id: leased as ``chat``, landed under ``.alkera/chats/<id>``, pulled and pushed
whole (its records travel with it). A workspace's folder, keyed by
:func:`~alkera_cli.cloud.workspace_seat.workspace_key`: leased as
``workspace``, landed under ``.alkera/workspaces/<id>``, and only its shared
``files/`` tree comes down and goes up; each member chat's records under
``.chats/`` move under that chat's own nested lease, from its own directory,
never twice from two places. This module is that difference, spelled once.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from alkera_core.files.objects_bridge import CHAT_TYPE, working_folder_name

from alkera_cli.cloud.workspace_seat import CHATS_DIR, FILES_DIR, workspace_key, workspace_of_key
from alkera_cli.files.pull import pull as pull_tree
from alkera_cli.files.push import AgreedBase, PushSummary
from alkera_cli.files.push import push as push_tree
from alkera_cli.files.walk import LIVE_SYNC_DEFAULT_PRESETS

#: The lease purposes a box takes a chat's folder and a workspace's under. The
#: server records them and shows them to whoever is refused.
CHAT_FOLDER_PURPOSE = "chat"
WORKSPACE_FOLDER_PURPOSE = "workspace"

#: The chat folder travels, so neither half of a transfer carries the state a
#: box keeps about its own hold on it — the per-chat write lock and the
#: forensic rotations the reclaim leaves beside it. Excluded from the PUSH
#: because they mean nothing anywhere else, and from the PULL because a folder
#: pushed before this rule existed still holds them and a lock stamped with
#: another host's name can never be reclaimed on this one. The names live in
#: `alkera_core.project.local_state`, spelled once for both directions.
#
#: The working directory inside it leaves behind what the live sync never
#: streamed either: the virtual environments, ``node_modules``, ``__pycache__``
#: and tool caches a working directory grows at any depth when it is also the
#: agent's home. Scoped to the working directory so the records beside it
#: keep their own rules.
_CHAT_WORKING_DIR = working_folder_name(CHAT_TYPE)
_WORKING_DIR_PRESETS: dict[str, Any] = (
    {"exclude_presets_within": {_CHAT_WORKING_DIR.decode("utf-8"): LIVE_SYNC_DEFAULT_PRESETS}}
    if _CHAT_WORKING_DIR is not None
    else {"exclude_presets": LIVE_SYNC_DEFAULT_PRESETS}
)
PUSH_LOCAL = partial(push_tree, skip_local_state=True, **_WORKING_DIR_PRESETS)
PULL_LOCAL = partial(pull_tree, skip_local_state=True)
#: A workspace's shared tree leaves behind the same caches a chat's working
#: directory does, inside ``files/``.
PUSH_WORKSPACE = partial(
    push_tree,
    skip_local_state=True,
    exclude_presets_within={FILES_DIR: LIVE_SYNC_DEFAULT_PRESETS},
)
PULL_WORKSPACE = partial(pull_tree, skip_local_state=True, only=FILES_DIR.encode("utf-8"))


@dataclass(frozen=True, slots=True)
class CustodyLayout:
    """The answers that differ between a chat's folder and a workspace's,
    by custody key."""

    chats_root: Path
    workspaces_root: Path

    @classmethod
    def beside(cls, chats_root: Path, workspaces_root: Path | None = None) -> CustodyLayout:
        """The layout with the workspaces landing beside the chats
        (``.alkera/workspaces`` next to ``.alkera/chats``) unless named."""
        return cls(chats_root, workspaces_root or chats_root.parent / "workspaces")

    def root(self, key: str) -> Path:
        """Where the folder held under ``key`` lands on this box."""
        workspace_id = workspace_of_key(key)
        if workspace_id is not None:
            return self.workspaces_root / workspace_id
        return self.chats_root / key

    def key_of(self, root: Path) -> str | None:
        """The custody key whose folder lands at ``root``, the inverse of
        :meth:`root`; ``None`` for a directory this layout does not place."""
        parent, name = root.resolve().parent, root.name
        if parent == self.workspaces_root.resolve():
            return workspace_key(name)
        return name if parent == self.chats_root.resolve() else None

    def bound(self, key: str) -> Path:
        """The directory a wipe of ``key``'s copy must stay inside."""
        return self.workspaces_root if workspace_of_key(key) is not None else self.chats_root

    def purpose(self, key: str) -> str:
        return WORKSPACE_FOLDER_PURPOSE if workspace_of_key(key) else CHAT_FOLDER_PURPOSE

    def pull(self, key: str, chat_pull: Callable[..., Any]) -> Callable[..., Any]:
        """The take's pull for ``key``, writing as the tree's own identity.
        ``chat_pull`` is the pull a chat's folder takes (the caller's, so the
        one place it is spelled is the one a test replaces)."""
        return partial(PULL_WORKSPACE if workspace_of_key(key) else chat_pull, chat_id=key)

    def push(
        self,
        key: str,
        tombstones: Sequence[str],
        chat_push: Callable[..., PushSummary],
        *,
        bases: Mapping[str, AgreedBase] | None = None,
    ) -> Callable[..., PushSummary]:
        """The checkpoint push for ``key``, blind to what it already trashed:
        a workspace's never carries ``.chats/``. ``bases`` are the agreed
        bases its writes are fenced on (the live sync's watched tree, the
        shared ``files/`` of a workspace), so a write over a head somebody
        else moved keeps their version as a conflicted copy rather than
        replacing it."""
        agreed = dict(bases or {})
        if workspace_of_key(key) is not None:
            return partial(PUSH_WORKSPACE, skip=[*tombstones, CHATS_DIR], bases=agreed)
        return partial(chat_push, skip=tombstones, bases=agreed)


__all__ = [
    "CHAT_FOLDER_PURPOSE",
    "PULL_LOCAL",
    "PUSH_LOCAL",
    "WORKSPACE_FOLDER_PURPOSE",
    "CustodyLayout",
]
