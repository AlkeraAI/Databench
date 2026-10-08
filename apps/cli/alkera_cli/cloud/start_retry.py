"""What a cloud box does with a chat whose mirror would not start: a
gateway refusal or a failed start, each remembered with a doubling wait and
said once on the chat. A mixin over ``CloudMirrorService``."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol

from alkera_core.chat_refusals import ChatRefusalKind

from alkera_cli.cloud.faults import refusal_kind
from alkera_cli.cloud.start_failures import (
    START_FAILURE_BACKOFF_CAP_SECONDS,
    StartFailure,
    gateway_refusal,
    next_failure,
    start_failure_kind,
    start_failure_sentence,
    start_key,
)
from alkera_cli.host.backoff import doubling_wait

if TYPE_CHECKING:
    from alkera_cli.cloud.folder_returns import FolderReturns
    from alkera_cli.cloud.mirror import ChatMirror
    from alkera_cli.cloud.sleep_policy import SleepPolicy
    from alkera_cli.cloud.workspace_host import WorkspaceHost

#: These lines are the mirror service's, and read under its logger.
logger = logging.getLogger("alkera_cli.cloud.service")


class _PollSettings(Protocol):
    @property
    def poll_interval(self) -> float: ...


class StartRetry:
    """The service's side of a mirror that would not start."""

    if TYPE_CHECKING:
        _settings: _PollSettings
        _clock: Callable[[], float]
        _mirrors: dict[str, ChatMirror]
        _refused: dict[str, str]
        _refusal_waits: dict[str, StartFailure]
        _start_failures: dict[str, StartFailure]
        _woke_at: dict[str, str]
        _policy: SleepPolicy
        _returns: FolderReturns
        _workspaces: WorkspaceHost

        async def report_publisher_state(
            self,
            chat_id: str,
            state: str,
            reason: str = "",
            *,
            ending: str | None = None,
            kind: ChatRefusalKind | None = None,
        ) -> None: ...

    def _may_retry_start(self, chat_id: str, chat: dict[str, Any]) -> bool:
        return all(
            self._wait_is_over(table, chat_id, chat)
            for table in (self._start_failures, self._refusal_waits)
        )

    def _wait_is_over(
        self, table: dict[str, StartFailure], chat_id: str, chat: dict[str, Any]
    ) -> bool:
        """Whether ``chat_id`` may be tried again: nothing remembered, news on
        its row since (which forgets the record), or its wait is up."""
        failure = table.get(chat_id)
        if failure is None:
            return True
        if failure.seen != start_key(chat):
            del table[chat_id]
            return True
        return self._clock() >= failure.retry_at

    async def _start_refused(
        self, chat_id: str, chat: dict[str, Any], mirror: ChatMirror, code: str
    ) -> None:
        """The gateway would not let this box publish the chat: no session was
        opened and nothing was spent.

        Said once per chat and reason, and put on the chat so the reader's
        banner says it rather than a composer that never answers. The chat is
        then left alone for a wait that doubles with each refusal in a row (news
        on its row ends it early), and a refusal that is a verdict — no credit,
        another publisher — hands the folder back unpushed, so the lease is not
        held by a box that will not serve the chat. A transient one (the
        document not there yet, a timeout) keeps it: the next try is likely to
        succeed, and here.
        """
        previous = self._refusal_waits.get(chat_id)
        count = (previous.count if previous is not None and previous.reason == code else 0) + 1
        wait = doubling_wait(
            count, base=self._settings.poll_interval, cap=START_FAILURE_BACKOFF_CAP_SECONDS
        )
        verdict = refusal_kind(code) == "verdict"
        if self._refused.get(chat_id) != code:
            logger.warning(
                "chat %s is bound to this machine but cannot be published by it (%s); "
                "it is tried again in %.0f s%s",
                chat_id,
                code,
                wait,
                " and its folder is handed back" if verdict else "",
            )
        self._refused[chat_id] = code
        self._mirrors.pop(chat_id, None)
        self._policy.forget(chat_id)
        self._woke_at.pop(chat_id, None)
        with contextlib.suppress(Exception):
            await mirror.stop()
        if verdict:
            # After the mirror is down, never before: a session that got as
            # far as opening could otherwise write behind a lease given back.
            await self._returns.release(chat_id)
        await self._workspaces.leave(chat_id)  # nothing of it runs in its workspace
        self._refusal_waits[chat_id], say = next_failure(
            previous, count, self._clock() + wait, start_key(chat), code, refusal_kind(code)
        )
        if say:
            # Said once (a transient one once it has outlasted its tries), and
            # AFTER the folder is back: a reader who sees the reason may
            # move the chat elsewhere at once, and that box must find the
            # lease free.
            sentence, kind = gateway_refusal(code)
            await self.report_publisher_state(chat_id, "refused", sentence, kind=kind)
        self._workspaces.forget_report(chat_id)

    async def _start_failed(
        self, chat_id: str, chat: dict[str, Any], mirror: ChatMirror, exc: BaseException
    ) -> None:
        """The mirror would not start, so nothing here serves the chat — and
        nothing here may hold it.

        The mirror is stopped (whatever the start got as far as spawning goes
        with it), the folder goes back at once and UNPUSHED — a mirror that
        never ran wrote nothing, and this disk may carry an earlier life's
        residue — and the failure is remembered so the box waits, twice the
        poll interval doubling to a cap, rather than taking the folder and
        failing on it every tick. The reader is told why on the chat itself,
        once per reason, instead of watching a composer that never answers.
        """
        previous = self._start_failures.get(chat_id)
        count = (previous.count if previous is not None else 0) + 1
        wait = doubling_wait(
            count, base=self._settings.poll_interval, cap=START_FAILURE_BACKOFF_CAP_SECONDS
        )
        never_ran = chat.get("session_state") == "starting"
        reason = start_failure_sentence(exc, never_ran=never_ran)
        if previous is None or previous.reason != reason:
            logger.exception(
                "mirror for chat %s failed to start; it is tried again in %.0f s", chat_id, wait
            )
        else:
            logger.warning(
                "mirror for chat %s failed to start again (%d in a row: %s); it is tried again "
                "in %.0f s",
                chat_id,
                count,
                exc,
                wait,
            )
        self._mirrors.pop(chat_id, None)
        self._policy.forget(chat_id)
        self._woke_at.pop(chat_id, None)
        with contextlib.suppress(Exception):
            await mirror.stop()
        # After the mirror is down, never before: a session that got as far as
        # opening could otherwise write behind a lease already given back.
        await self._returns.release(chat_id)
        await self._workspaces.leave(chat_id)  # nothing of it runs in its workspace
        self._start_failures[chat_id], say = next_failure(
            previous, count, self._clock() + wait, start_key(chat), reason, refusal_kind(exc)
        )
        if say:
            # Said AFTER the folder is back: a reader who sees the reason may
            # wake the chat elsewhere at once, and that box must find the lease
            # free. A passing fault is not said until it outlasts its tries.
            await self.report_publisher_state(
                chat_id, "refused", reason, kind=start_failure_kind(exc, never_ran=never_ran)
            )
        self._workspaces.forget_report(chat_id)
