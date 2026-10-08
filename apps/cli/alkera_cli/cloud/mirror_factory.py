"""How the box builds the mirror that serves one chat.

A chat on its own runs in its own working directory, inside its own folder. A
member of a workspace runs in the workspace's shared ``files/`` tree instead:
its records (transcript, ledgers, the agent's session store) still live in its
own folder, and only where the agent runs, what its tools may write and what
the person sees as the chat's files move to the shared tree. That is the one
thing :class:`WorkspaceMemberMirror` changes.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from alkera_cli.account.org_id import MalformedOrgIdError, canonical_org_id
from alkera_cli.cloud.attachments import MaterializedAttachments
from alkera_cli.cloud.mirror import ChatMirror, sandbox_fields_of
from alkera_cli.cloud.publisher_identity import PublishingRefusal


class OrgBoundMirror(ChatMirror):
    """A chat mirror that holds the org its chat belongs to, as the server's
    row named it and from nowhere else: a pool box serves many orgs, and the
    chat's tenancy is what keys everything it may touch. ``None`` for a row
    from a server that does not send one."""

    def __init__(self, *, org_id: str | None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._org_id = org_id

    @property
    def org_id(self) -> str | None:
        return self._org_id


class WorkspaceMemberMirror(OrgBoundMirror):
    """A chat served as a member of a workspace: everything a chat mirror is,
    run in the workspace's shared tree."""

    def __init__(self, *, shared_tree: Path, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._shared_tree = shared_tree

    @property
    def working_dir(self) -> Path:
        """The workspace's shared tree: where every member's agent runs, what
        each may write, and what a person live-editing a file edits."""
        return self._shared_tree


def chat_org_id(chat: Mapping[str, Any]) -> str | None:
    """The org a chat row names, in the one spelling the box keys tenancy on.

    The server's row is the only source. A value that is not a UUID reads as
    no org at all, and a UUID in any other spelling (upper case, braces, no
    dashes) is rewritten to the canonical one, so one org can never land in
    two partitions because two reads spelled it differently. ``None`` for a
    row from a server that predates the field.
    """
    raw = chat.get("org_id")
    if raw is None or raw == "":
        return None
    try:
        return canonical_org_id(raw)
    except MalformedOrgIdError:
        return None


def build_mirror(
    chat_id: str,
    chat: Mapping[str, Any],
    *,
    shared_tree: Path | None,
    sandbox_bag: Mapping[str, str],
    prepare_attachments: Callable[[str, Mapping[str, Any]], Awaitable[MaterializedAttachments]],
    on_refused: Callable[[str, PublishingRefusal], Awaitable[None]],
    on_turn_end: Callable[[str], None],
    land_files: Callable[[list[str]], Awaitable[object]],
    **parts: Any,
) -> OrgBoundMirror:
    """The mirror for ``chat_id``, from its row. ``parts`` are what the service
    hands every mirror (runtime, socket, REST client, budget, identities,
    clock); ``shared_tree`` is a member's workspace tree, ``None`` for a chat
    on its own; ``sandbox_bag`` what a member's harness bag adds."""
    title = chat.get("title")
    # The chat row names its owner; every relay the mirror honours must
    # name an object that owner owns.
    owner = chat.get("owner_user_id")
    source = chat.get("source_object_id")
    kwargs: dict[str, Any] = {
        "chat_id": chat_id,
        "owner_user_id": owner if isinstance(owner, str) and owner else None,
        "org_id": chat_org_id(chat),
        "title": title if isinstance(title, str) else None,
        # The saved report/query a chat was started FROM, off its own record:
        # the box that resumes a chat after a sleep is not the box that opened
        # it, so a context held only in the running mirror would be forgotten.
        "source_object_id": source if isinstance(source, str) and source else None,
        "prepare_attachments": prepare_attachments,
        "on_refused": on_refused,
        "on_turn_end": on_turn_end,
        "land_files": land_files,
        # The row's own vCPU and memory figures — the org's plan tier, or its
        # override — go onto the manifest the session is built from. A mirror
        # opened without them ran every chat at the box floor, whatever the
        # org paid for. A member's bag also names its workspace's sandbox.
        "sandbox": {**sandbox_fields_of(chat), **sandbox_bag},
        **parts,
    }
    if shared_tree is not None:
        return WorkspaceMemberMirror(shared_tree=shared_tree, **kwargs)
    return OrgBoundMirror(**kwargs)


__all__ = ["OrgBoundMirror", "WorkspaceMemberMirror", "build_mirror", "chat_org_id"]
