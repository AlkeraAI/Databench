"""What a message's turn needs on the box before it starts.

A chat's attachments are Files nodes, fetched into the chat's directory; the
files its message links were uploaded into the chat's working folder and
arrive on the live plane, so the turn first takes what the drive holds for
that folder and waits for what it still owes. :class:`TurnInputs` does
both for the mirror service, which hands it the folders it holds and the drain
that takes the drive's changes.
"""

from __future__ import annotations

import asyncio
import logging
import stat as stat_module
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from alkera_cli.cloud.attachments import (
    ContentFetcher,
    LinkedFile,
    MaterializedAttachments,
    attachments_in,
    linked_files_in,
    materialize_attachments,
)
from alkera_cli.cloud.chat_fs import ChatTree, ChatTreeError
from alkera_cli.files.live_sync import byte_deadline
from alkera_cli.host.backoff import doubled

if TYPE_CHECKING:
    from alkera_cli.cloud.folder import HeldFolder

logger = logging.getLogger(__name__)

#: How long a turn waits for a linked file that is not moving. It is a floor,
#: not a limit: the wait is :func:`byte_deadline` over the bytes already landed
#: and it starts again from every byte that arrives, so a 1 TB file crossing a
#: slow link is never called missing while it is still coming. Only silence
#: this long ends the wait.
INBOUND_STALL_FLOOR_SECONDS = 60.0

#: The first gap between asks while a linked file is still owed. The upload and
#: the message are two requests, so a file whose inbound record lands a beat
#: after the words is the ordinary case and the second ask comes quickly.
INBOUND_POLL_FIRST_SECONDS = 1.0

#: What that gap grows to. It doubles per ask, so a transfer that will take
#: hours is asked after a few seconds, not a few thousand times.
INBOUND_POLL_MAX_SECONDS = 5.0


class HeldLookup(Protocol):
    """The folders a box holds, by custody key."""

    def held(self, chat_id: str) -> HeldFolder | None: ...


class TurnInputs:
    """A turn's inputs on disk: what the drive holds for the chat's folder,
    the files its message links, and its attachments."""

    def __init__(
        self,
        *,
        folders: HeldLookup,
        live_key: Callable[[str], str],
        drive_named: Callable[[str], str | None],
        drain: Callable[[str], Awaitable[object]],
        reader_for: Callable[[str | None], Awaitable[ContentFetcher]],
        project_path: Path,
        clock: Callable[[], float],
        sleep: Callable[[float], Awaitable[None]],
    ) -> None:
        self._folders = folders
        #: The custody key whose live sync covers a chat's working directory.
        self._live_key = live_key
        #: The drive a chat's own record names, if the box opened it.
        self._drive_named = drive_named
        self._drain = drain
        self._reader_for = reader_for
        self._project_path = project_path
        self._clock = clock
        self._sleep = sleep
        #: The readers for Files content, one per drive, each built on the
        #: first chat of its drive that has an attachment and kept. Keyed by
        #: the drive id; the empty key is the caller's own drive, for a chat
        #: whose record names none.
        self.readers: dict[str, ContentFetcher] = {}

    async def prepare(self, chat_id: str, message: Mapping[str, Any]) -> MaterializedAttachments:
        """Put this message's attachments on disk before its turn starts.

        A chat attachment is a Files node, so the bytes are not in the message:
        the parts name nodes and this fetches them into the chat's own
        directory. It never raises — a node the box cannot read comes back as a
        NOTICE naming the file, and the turn runs with what did arrive, because
        a reader who attached three files is better served by an answer plus a
        visible gap than by a turn that refuses to start.

        What the drive is holding for this chat's folder is taken first. A file
        dropped into the folder on the web is not an attachment on the message
        — the agent reaches it by name — so a turn that started before it
        landed would read a path the person can already see and find nothing.
        """
        await self._drain_before_turn(chat_id, message)
        linked = await self._missing_linked_files(chat_id, message)
        found = attachments_in(message)
        if not found:
            return MaterializedAttachments(notices=linked)
        try:
            fetcher = await self._content_fetcher(chat_id)
        except Exception as unreachable:
            logger.warning("chat %s: no Files reader for its attachments: %s", chat_id, unreachable)
            return MaterializedAttachments(
                notices=linked
                + tuple(
                    f"{one.display_name}: could not be fetched ({unreachable})" for one in found
                )
            )
        materialized = await materialize_attachments(
            found,
            project_path=self._project_path,
            chat_id=chat_id,
            fetcher=fetcher,
        )
        if not linked:
            return materialized
        return MaterializedAttachments(
            files=materialized.files, notices=linked + materialized.notices
        )

    async def _missing_linked_files(
        self, chat_id: str, message: Mapping[str, Any]
    ) -> tuple[str, ...]:
        """What the message links that is still not in the chat's folder.

        A file the reader attached in the composer is not a ``file`` part: it
        was uploaded straight into the chat's working folder and the message
        carries a chat-RELATIVE link to it. So the bytes arrive on the live
        plane, and the only thing standing between the reader's link and the
        agent's read is a drain that has not caught up.

        The wait itself happened in :meth:`_drain_before_turn`, which asks the
        drive again for as long as bytes keep landing. What is still absent
        once that returns is named, because an agent handed a link to bytes
        that are nowhere has no way to tell a slow drive from a broken product
        -- and said so. A file that arrived in part is named with what did
        arrive, so a stalled transfer reads as a stall rather than as a file
        that was never sent.
        """
        linked = linked_files_in(message)
        if not linked:
            return ()
        root = self._chat_folder_root(chat_id)
        if root is None:
            # No folder is held for this chat, so nothing was ever promised to
            # be there and the link is not this box's to answer for.
            return ()
        still = self._absent(root, linked)
        notices: list[str] = []
        for one in still:
            landed = self._landed_bytes(chat_id, root, (one,))
            logger.warning(
                "chat %s: the message links %s but only %d byte(s) for it are on disk",
                chat_id,
                one.path,
                landed,
                extra={"chat_id": chat_id, "path": one.path, "landed_bytes": landed},
            )
            arrived = f" ({landed} of its bytes did)" if landed else ""
            notices.append(f"{one.display_name}: never arrived on this machine{arrived}")
        return tuple(notices)

    def _chat_folder_root(self, chat_id: str) -> Path | None:
        """The working directory the chat's links are relative to, if held."""
        held = self._folders.held(self._live_key(chat_id))
        if held is None or held.live is None:
            return None
        return held.live.root

    @staticmethod
    def _absent(root: Path, linked: Sequence[LinkedFile]) -> tuple[LinkedFile, ...]:
        """Which of ``linked`` is not a file under ``root`` right now.

        Looked up through the chat tree like every other touch of the folder: a
        link the transcript wrote is untrusted text, so a path that would leave
        the folder is refused rather than stat'ed, and a symlink standing at the
        name is not the file.
        """
        tree = ChatTree(root)
        absent: list[LinkedFile] = []
        for one in linked:
            try:
                present = tree.is_file(one.path)
            except ChatTreeError:
                continue
            if not present:
                absent.append(one)
        return tuple(absent)

    async def _drain_before_turn(self, chat_id: str, message: Mapping[str, Any]) -> None:
        """Take what the drive holds for this chat, and wait for what it owes.

        Bounded by the FILES the message names, never by a clock: a turn whose
        reader linked a 200 GB export waits for that export, because a fixed
        window is a promise about bandwidth the box cannot keep. What ends the
        wait is silence — no byte landing for :func:`byte_deadline` over what
        has landed so far, floored at :data:`INBOUND_STALL_FLOOR_SECONDS` —
        and every byte that arrives starts that window again. A message that
        links nothing takes one drain and starts, so an ordinary turn pays
        nothing for this.
        """
        if self._folders.held(chat_id) is None:
            return
        await self._ask_the_drive(chat_id)
        linked = linked_files_in(message)
        if not linked:
            return
        root = self._chat_folder_root(chat_id)
        if root is None:
            return
        missing = self._absent(root, linked)
        landed = self._landed_bytes(chat_id, root, missing)
        moved_at = self._clock()
        wait = INBOUND_POLL_FIRST_SECONDS
        while missing:
            deadline = byte_deadline(landed, floor=INBOUND_STALL_FLOOR_SECONDS)
            if self._clock() - moved_at >= deadline:
                logger.warning(
                    "chat %s: %s stopped arriving (%d byte(s), nothing for %.0fs); its turn "
                    "starts on what did arrive",
                    chat_id,
                    ", ".join(one.path for one in missing),
                    landed,
                    deadline,
                    extra={"chat_id": chat_id, "landed_bytes": landed},
                )
                return
            await self._sleep(wait)
            wait = doubled(wait, floor=INBOUND_POLL_FIRST_SECONDS, cap=INBOUND_POLL_MAX_SECONDS)
            await self._ask_the_drive(chat_id)
            missing = self._absent(root, missing)
            if not missing:
                return
            now = self._landed_bytes(chat_id, root, missing)
            if now > landed:
                # Bytes are still coming for a file that is not whole yet, so
                # the wait is not a stall and starts again from here.
                landed = now
                moved_at = self._clock()

    async def _ask_the_drive(self, chat_id: str) -> None:
        """One drain, given up on after the stall floor — never cancelled.

        The download runs off the loop, so giving up on the ASK does not give
        up on the bytes: they go on landing and the next ask settles them. What
        the bound buys is a turn that keeps looking at the disk while a very
        slow hand-over is still in flight, instead of standing inside one
        request for the length of the transfer.
        """
        try:
            await asyncio.wait_for(self._drain(chat_id), INBOUND_STALL_FLOOR_SECONDS)
        except TimeoutError:
            logger.info(
                "chat %s: the drive has not answered within %.0fs; its bytes are still coming",
                chat_id,
                INBOUND_STALL_FLOOR_SECONDS,
            )

    def _landed_bytes(self, chat_id: str, root: Path, linked: Sequence[LinkedFile]) -> int:
        """How many bytes for ``linked`` are on the box right now.

        A file that has landed counts whole; one still coming counts what its
        download has streamed so far into the live sync's spool (outside the
        chat's tree), the only evidence the box has that a transfer is
        progressing rather than hung. Nothing here follows a link in the tree.
        """
        held = self._folders.held(chat_id)
        live = held.live if held is not None else None
        tree = live.tree if live is not None and live.tree is not None else ChatTree(root)
        total = 0
        for one in linked:
            try:
                info = tree.stat(one.path)
            except ChatTreeError:
                continue
            if info is not None and stat_module.S_ISREG(info.st_mode):
                total += info.st_size
            if live is not None and live.spool is not None:
                total += live.spool.landing_bytes(one.path)
        return total

    async def _content_fetcher(self, chat_id: str) -> ContentFetcher:
        """The reader for ``chat_id``'s attachments, bound to the chat's drive.

        The drive is the chat's, off its record — a pool box serves chats from
        many orgs, each in its own drive, and a box on its machine credential
        is a member of no org, so "the caller's drive" names nothing for it —
        with the held folder's lease naming the same drive for a mirror that
        predates the record's field. Only a chat that names none is read
        through the caller's own drive, which an org box on its operator's
        session still has. One reader per drive, kept for the life of the box.
        """
        drive = self._drive_of(chat_id)
        key = drive or ""
        fetcher = self.readers.get(key)
        if fetcher is None:
            fetcher = await self._reader_for(drive)
            self.readers[key] = fetcher
        return fetcher

    def _drive_of(self, chat_id: str) -> str | None:
        """The drive ``chat_id``'s folder is on, as this box knows it: the chat
        record the mirror was opened from, else the lease the folder is held
        under, else nothing."""
        named = self._drive_named(chat_id)
        if named:
            return named
        held = self._folders.held(chat_id)
        if held is not None and held.record.drive_id:
            return held.record.drive_id
        return None


__all__ = [
    "INBOUND_POLL_FIRST_SECONDS",
    "INBOUND_POLL_MAX_SECONDS",
    "INBOUND_STALL_FLOOR_SECONDS",
    "HeldLookup",
    "TurnInputs",
]
