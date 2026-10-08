"""The two rules every door that re-parents or copies a subtree runs.

Both are spelled once because each was first written into one route and then
found missing from the next door over. A move that would hand the mover a rung
they did not hold lived in the single move and the batch, and not in the
restore that lands a trashed tree in a folder of the caller's choosing. A copy
of a folder decided the folder and nothing beneath it, so a chat folder the
caller could not open walked out inside its parent — through the duplicate,
the tracked copy, the batch and the archive alike.

Each rule takes a ``decide`` callable rather than a request: the doors bind the
platform engine differently (the batch keeps the request transaction and
commits each refusal on its own), and the rule must not know. What it does
know is which node to decide and which action, so the refusal is the policy's
own — the decision row and the opaque-or-visible shape included.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from alkera_core.authz.policies.files import COPY_REFUSED
from alkera_core.authz.principal import ActingContext
from alkera_core.files.authz.actions import FilesAction
from alkera_core.files.authz.authorize import Authorized, Denied
from alkera_core.files.authz.decider import CHAT_SUBTYPE, AccessFacts, rung_within
from alkera_core.files.authz.readable import access_by_id
from alkera_core.files.errors import NotFound
from alkera_core.files.ids import NodeId
from alkera_core.files.repo import ID_BATCH, FilesRepo
from alkera_core.models.files.tree import FileNode

#: Decide ``action`` on one node through the door's own binding of the engine,
#: returning the handle or raising the policy's refusal.
Decide = Callable[[uuid.UUID, FilesAction], Awaitable[Authorized[Any]]]

#: What a copy refused over a hidden conversation says. It names no node: the
#: caller learns the folder holds something they cannot copy, not which chat.
HIDDEN_CHAT_MESSAGE = "This folder holds a conversation you cannot copy"

#: The same refusal over a node that is not part of a conversation.
HIDDEN_ITEM_MESSAGE = "This folder holds an item you cannot copy"


async def refuse_a_move_that_raises_the_mover(
    source: Authorized[Any], destination: Authorized[Any], *, decide: Decide
) -> None:
    """A move may not hand the mover a rung on the node they did not hold.

    A move re-derives the subtree's inherited grants from its new parent, so a
    "Can edit" holder who dragged a colleague's folder into their own home
    became its owner by the home's default — purge, hold, share — and everyone
    the old folder admitted, the owner included, was left with nothing. When
    the mover's rung on the destination is above their rung on the source, the
    source is decided again as the owner's verb on it, ``DELETE``: an owner
    moves their node anywhere, and anybody else is refused on record with the
    reason that names what they lack. A move between folders where the mover
    holds the same rung, or a lower one, is the ordinary write it always was.

    Every door that changes a node's parent calls this — the single move, the
    batch move and a restore aimed at a named folder — so a door cannot decide
    the two ends and forget what moving between them does.
    """
    if rung_within(source.access, destination.access.role or ""):
        return
    await decide(uuid.UUID(str(source.node.id)), FilesAction.DELETE)


async def refuse_a_copy_over_hidden_chats(
    repo: FilesRepo,
    ctx: ActingContext,
    root: FileNode,
    *,
    facts: AccessFacts,
    decide: Decide,
    action: FilesAction,
    rows: Sequence[FileNode] | None = None,
) -> None:
    """A copy of a folder may not carry out anything the copier cannot open.

    A folder's grant says everything about what is beneath it, with one
    exception today: a chat folder is decided by the chat's own rule, so a
    member's private chat sits in a ``Chats`` folder an org admin may read
    while the chat itself is not theirs. A copy decided on the parent alone
    walked the subtree with no further question and — through the duplicate,
    the tracked copy, the batch and the archive — copied the transcript row for
    row into a drive the copier owns.

    Every live node beneath ``root`` is therefore decided on its own, by the
    decider the by-id door uses, and the first one the caller may not read
    refuses the whole operation: a copy of a ``Chats`` folder with one
    conversation quietly missing is a surprise, and refusing is the answer
    that cannot be misread. Deciding every node rather than only chat folders
    keeps the rule true for whatever next stops inheriting its folder's grant.
    The refusal is filed on the node that caused it — the decision row an
    audit reads names the node and the caller — and answered on the parent as
    the visible copy refusal, since the caller could read the folder they asked
    about and a not-found there would be a lie. The message names no node.

    ``rows`` is the subtree the route has already read, when it has: a copy
    scans it for its quota anyway, and a second scan would be paid for nothing.
    Without it the subtree is read a page at a time, so an archive of a large
    folder costs a page of rows and not the tree. ``action`` is the verb the
    door decides — ``COPY`` for a copy, ``EXPORT`` for an archive — so the row
    says what was attempted.
    """
    if rows is not None:
        await _refuse_unreadable(repo, ctx, root, rows, facts=facts, decide=decide, action=action)
        return
    after: tuple[int, uuid.UUID] | None = None
    while True:
        page = await repo.subtree_page(root, limit=ID_BATCH, after=after)
        if not page:
            return
        await _refuse_unreadable(repo, ctx, root, page, facts=facts, decide=decide, action=action)
        if len(page) < ID_BATCH:
            return
        last = page[-1]
        after = (int(last.depth), uuid.UUID(str(last.id)))


async def _refuse_unreadable(
    repo: FilesRepo,
    ctx: ActingContext,
    root: FileNode,
    rows: Sequence[FileNode],
    *,
    facts: AccessFacts,
    decide: Decide,
    action: FilesAction,
) -> None:
    """Refuse on the first of ``rows`` (the root and the trashed aside) the
    caller may not read, shallowest first, so a chat folder is the node named
    rather than a file inside it."""
    live = [row for row in rows if row.id != root.id and row.trashed_at is None]
    if not live:
        return
    decided = await access_by_id(repo, ctx, [NodeId(row.id) for row in live], facts=facts)
    for row in live:
        access = decided.get(row.id)
        if access is not None and access.allows(FilesAction.READ):
            continue
        in_chat = row.subtype == CHAT_SUBTYPE or (access is not None and access.in_chat_subtree)
        try:
            await decide(uuid.UUID(str(row.id)), action)
        except NotFound:
            raise Denied(
                COPY_REFUSED, HIDDEN_CHAT_MESSAGE if in_chat else HIDDEN_ITEM_MESSAGE
            ) from None
        # The engine admitted what the batched read did not (a grant made
        # between the two reads): the door's own decision stands.


__all__ = [
    "HIDDEN_CHAT_MESSAGE",
    "HIDDEN_ITEM_MESSAGE",
    "Decide",
    "refuse_a_copy_over_hidden_chats",
    "refuse_a_move_that_raises_the_mover",
]
