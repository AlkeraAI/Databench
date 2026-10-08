"""Giving a box's folders back.

The sleep's hand-back (push what the chat wrote, then release the lease), the
release with nothing pushed of a folder taken for a mirror that never ran, the
retry of a hand-back that did not land, and the settling of a folder an
earlier life of this box left held. Each runs under the folder's
custody lock, the one the beats take, so no beat asserts a lease this box is
in the middle of giving up; none of them raises, since a chat that cannot give
its folder back must still leave its slot.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from typing import Any

import httpx
from alkera_sdk.client import AlkeraHTTPError

from alkera_cli.cloud.folder import ChatFolders, FolderBusyError, FolderHandBackError
from alkera_cli.cloud.release_log import log_released
from alkera_cli.cloud.workspace_host import WorkspaceHost
from alkera_cli.cloud.workspace_seat import is_workspace_key
from alkera_cli.host.backoff import doubling_wait

logger = logging.getLogger(__name__)

#: How long a folder an earlier life left held must stay unserved before it is
#: settled. A restart in place keeps its leases for the next process, which
#: takes back the folders of the chats it resumes in its first passes;
#: settling one of those first would push, release and wipe a tree the chat is
#: about to pull straight back down.
LEFT_GRACE_SECONDS = 60.0
#: The first wait after a settle that did not land, doubled per failure.
LEFT_RETRY_FIRST_SECONDS = 15.0
#: The ceiling on that wait.
LEFT_RETRY_CAP_SECONDS = 300.0


@dataclass(slots=True)
class _Left:
    """What this box knows of one folder an earlier life of it left held."""

    #: When it was first found unserved, on the box's monotonic clock.
    seen_at: float
    #: Settles that did not land, in a row.
    failures: int = 0
    #: Not tried again before this reading of the clock.
    retry_at: float = 0.0


class FolderReturns:
    """See the module docstring. ``serving`` answers the custody keys the
    box serves right now (a chat's own, and each workspace a member is in);
    ``report`` puts a verdict on a chat."""

    def __init__(
        self,
        *,
        folders: ChatFolders,
        lock: Callable[[str], asyncio.Lock],
        report: Callable[..., Awaitable[Any]],
        serving: Callable[[], Collection[str]],
        workspaces: WorkspaceHost,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._folders = folders
        self._lock = lock
        self._report = report
        self._serving = serving
        self._workspaces = workspaces
        self._clock = clock
        self._left: dict[str, _Left] = {}

    async def hand_back(
        self,
        chat_id: str,
        *,
        recover: bool = True,
        ending: str | None = None,
        gone: bool = False,
        if_owed: bool = False,
    ) -> None:
        """Push what the chat wrote, then release its lease. Never raises: a
        chat that cannot hand its folder back must still leave its slot.

        Under the chat's custody lock: the beats run on their own schedule, so
        without it a beat in flight would go on asserting a lease the box is in
        the middle of giving up.

        ``recover`` carries the caller's knowledge of whether the chat is still
        there, and only matters for a folder that has gone from the drive: see
        :meth:`alkera_cli.cloud.folder.ChatFolders.hand_back`.
        """
        if not self._folders.enabled:
            return
        async with self._lock(chat_id):
            if self._folders.held(chat_id) is None:
                return
            hand_back = functools.partial(
                self._folders.hand_back, recover=recover, ending=ending, gone=gone
            )
            if if_owed:  # a retry: nothing once a take has served the folder again
                hand_back = functools.partial(hand_back, if_owed=True)
            try:
                released = await asyncio.to_thread(hand_back, chat_id)
            except FolderHandBackError as refused:
                await self.not_handed_back(refused)
                return
            except (httpx.HTTPError, OSError, RuntimeError) as exc:  # pragma: no cover - belt
                logger.warning(
                    "chat %s: its folder was not handed back (%s); the lease lapses on its own",
                    chat_id,
                    exc,
                )
                return
        if released is None:
            return
        log_released(chat_id, released)
        if released.recovered:
            # The chat's own folder is gone, so nobody will find the work by
            # opening the chat. Said on the chat, not only in this box's log.
            await self._report(
                chat_id,
                "refused",
                f"this chat's folder is no longer in the drive; its files were saved to "
                f"{released.shown_path or released.org_path}",
                kind="folder_gone",
            )

    async def not_handed_back(self, refused: FolderHandBackError) -> None:
        """A hand-back that did not land: wait it out, or stop and say why.

        A transient failure — the API restarting, a timeout — is waited out for
        as long as it takes; the folder stays held and the next pass tries
        again. A refusal the server spelled out is tried a bounded number of
        times, and when that is spent the reason goes on the chat, because a
        box that retries forever is a box whose work never arrives and never
        tells anybody why.
        """
        if not refused.exhausted:
            logger.info(
                "chat %s: its folder was not handed back (%s); it is still held and the next "
                "pass tries again",
                refused.chat_id,
                refused.reason,
            )
            return
        logger.warning(
            "chat %s: its folder was refused %d time(s) (%s); this box has stopped trying and "
            "the lease lapses on its own",
            refused.chat_id,
            refused.attempts,
            refused.reason,
        )
        if not is_workspace_key(refused.chat_id):
            await self._report(
                refused.chat_id,
                "refused",
                f"this chat's files could not be saved to its folder: {refused.reason}",
                kind="files_unsaved",
            )

    async def retry_owed(self) -> None:
        """Try again for every folder a refused hand-back left held.

        The sleep is where a folder goes back, and a sleep that failed leaves
        nobody watching: the mirror is gone, so without this pass the work sits
        on this disk until the box is re-provisioned.
        """
        if not self._folders.enabled:
            return
        # A folder served again is not owed: its chat (or a member) runs in it.
        # A workspace the host still keeps is retried by the host, under its lock.
        serving = set(self._serving())
        for chat_id in await asyncio.to_thread(self._folders.owed):
            if chat_id not in serving:
                await self.hand_back(chat_id, if_owed=True)
        await self._workspaces.retry_owed()

    async def settle_left(self) -> list[str]:
        """Hand back every folder an earlier life of this box left held and
        nothing on this one serves. Returns the keys handed back.

        The previous process may have ended mid-turn (a restart in place keeps
        its leases for its successor), or been killed. Either way its folders'
        records are on this disk, and so is any work it wrote and never sent.
        A folder no chat here takes back is taken and handed back the way a
        sleep does: everything on this disk is pushed, then the lease goes.
        Left alone, the lease lapses and the only copy of that work stays on a
        box that has stopped telling the drive it exists.

        A settle that does not land is tried again on a later pass, after a
        doubling wait. Nothing is ever deleted here except by a hand-back that
        has landed every file.
        """
        # A custody built without the records (a test double, an older one)
        # has nothing an earlier life left: the pass is a no-op there.
        left_behind = getattr(self._folders, "left_behind", None)
        if not self._folders.enabled or left_behind is None:
            return []
        now = self._clock()
        left = await asyncio.to_thread(left_behind)
        serving = set(self._serving())
        self._left = {key: self._left.get(key) or _Left(seen_at=now) for key in left}
        settled: list[str] = []
        for key, record in left.items():
            state = self._left[key]
            if key in serving or now - state.seen_at < LEFT_GRACE_SECONDS:
                continue
            if now < state.retry_at:
                continue
            async with self._lock(key):
                try:
                    released = await asyncio.to_thread(self._folders.settle, key, record)
                except (
                    FolderBusyError,
                    FolderHandBackError,
                    AlkeraHTTPError,
                    httpx.HTTPError,
                    OSError,
                ) as exc:
                    self._settle_failed(key, state, exc)
                    continue
            if released is not None:
                log_released(key, released)
                settled.append(key)
                self._left.pop(key, None)
        return settled

    def _settle_failed(self, key: str, state: _Left, exc: BaseException) -> None:
        state.failures += 1
        wait = doubling_wait(
            state.failures, base=LEFT_RETRY_FIRST_SECONDS, cap=LEFT_RETRY_CAP_SECONDS
        )
        state.retry_at = self._clock() + wait
        logger.warning(
            "%s: a folder an earlier life of this box held was not handed back (%s); its copy "
            "stays on this disk and it is tried again in %.0f s",
            key,
            exc,
            wait,
        )

    async def release(self, chat_id: str) -> None:
        """Give the chat's folder back with nothing pushed: it was taken for a
        mirror that never ran here. Never raises: a folder that could not be
        released lapses on its own, and the wait before the next try stands.

        Under the chat's custody lock, for the reason the hand-back is: a beat
        in flight would otherwise go on asserting a lease this box is handing
        straight back."""
        if not self._folders.enabled:
            return
        async with self._lock(chat_id):
            if self._folders.held(chat_id) is None:
                return
            try:
                released = await asyncio.to_thread(self._folders.release, chat_id)
            except (httpx.HTTPError, OSError, RuntimeError) as exc:
                logger.warning(
                    "chat %s: its folder was not released (%s); the lease lapses on its own",
                    chat_id,
                    exc,
                )
                return
        if released:
            logger.info(
                "chat %s: its folder was released with nothing pushed; nothing ran here", chat_id
            )


__all__ = ["FolderReturns"]
