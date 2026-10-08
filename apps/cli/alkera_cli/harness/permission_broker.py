"""Routes `PermissionRequest` events to a registered resolver.

The harness emits `PermissionRequest` when it wants to run a tool that needs
approval. The orchestrator routes the request to the CLI REPL (a numeric prompt
on stdin) or, in the daemon, to a server-to-client
`harness.permission_required` request the editor renders in the chat. Either
way the broker takes a `PermissionRequest` and returns a `PermissionOptionId`.

There is no timeout by default. A prompt blocks the tool call it gates and the
user may walk away; auto-rejecting under them would break the turn, so every
interactive call site passes `default_timeout_seconds=None`. A timeout is
opt-in, for non-interactive resolvers (tests, automation) that must not
deadlock.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from alkera_cli.plugins.plugin_base.permissions.resolve import AskReconsideredError

if TYPE_CHECKING:
    from alkera_core.schemas.chat import (
        PermissionOptionId,
        PermissionRequest,
    )

logger = logging.getLogger(__name__)


# A resolver takes a permission request and returns the chosen option.
Resolver = Callable[["PermissionRequest"], Awaitable["PermissionOptionId"]]


class ResolverRefusedError(Exception):
    """A resolver refused the ask on its OWN grounds, before any person saw it.

    A resolver is the seam a person answers through, so a bare option it
    returns is recorded as that person's choice. A resolver that also holds a
    bound of its own — a cloud mirror's write fence, which turns a write outside
    the chat's folder away rather than parking it on a reader — raises this
    instead: the option and the provenance it names go on the record as the
    policy's, and ``reason`` reaches the model, which is what keeps the turn
    going (a reason-less reject is a person's, and ends it).
    """

    def __init__(
        self,
        reason: str,
        *,
        decided_by: str = "fence",
        option: PermissionOptionId = "reject_once",
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.decided_by = decided_by
        self.option = option


@dataclass(frozen=True, slots=True)
class BrokerDecision:
    """How one ask was answered: the option, the reason the model is told (a
    person's answer carries none), and who decided — ``human`` for a person's
    answer, ``timeout`` when the prompt ran out, ``broker`` when the resolver
    failed, or whatever a ``ResolverRefusedError`` named."""

    option: PermissionOptionId
    reason: str | None
    decided_by: str


class PermissionBroker:
    """Holds the resolver + timeout policy.

    The default is NO timeout: a permission prompt waits for the human
    indefinitely (they may have walked away — an auto-reject under them breaks
    the turn). A timeout is opt-in for non-interactive resolvers (tests,
    automation) that must not deadlock.
    """

    def __init__(
        self,
        resolver: Resolver,
        *,
        default_timeout_seconds: float | None = None,
    ) -> None:
        self._resolver = resolver
        self._timeout = default_timeout_seconds
        # request_id → session_id for every ask currently parked on a human.
        # Auto-decisions never reach the broker, so membership here is exactly
        # "this session is waiting on a permission answer" — what chat lists
        # surface as the pending-ask mark. Keyed by session because subagent
        # sessions share their parent's broker instance.
        self._pending: dict[str, str] = {}
        # request_id → the prompt a person is being shown. It outlives a waiter
        # that was told to reconsider, so the next ``decide`` for the same ask
        # waits on the prompt already up rather than raising a second one.
        self._prompts: dict[str, asyncio.Task[BrokerDecision]] = {}
        # request_id → what wakes that ask's waiter to reconsider.
        self._wakes: dict[str, asyncio.Event] = {}

    @property
    def default_timeout_seconds(self) -> float | None:
        return self._timeout

    def has_pending_for(self, session_id: str) -> bool:
        """Whether ``session_id`` has a permission ask awaiting the human."""
        return session_id in self._pending.values()

    async def resolve(self, request: PermissionRequest) -> PermissionOptionId:
        """Ask the resolver; return their choice. On timeout, auto-rejects
        with `reject_once` and logs. ``decide`` is the same ask with the
        provenance kept; this is for a caller that only needs the option.
        """
        return (await self.decide(request)).option

    async def decide(self, request: PermissionRequest) -> BrokerDecision:
        """Ask the resolver; return what was decided and by whom.

        A person's answer is ``human``. A prompt that ran out is ``timeout``
        and a resolver that raised is ``broker`` — both fail closed to
        ``reject_once``, and neither is a person's choice. A resolver that
        raised ``ResolverRefusedError`` decided on its own grounds: its option,
        reason and provenance are returned as it named them.

        A waiter :meth:`reconsider` wakes raises ``AskReconsideredError`` and
        leaves the prompt up: the caller decides the ask again, and a ``decide``
        for the same request waits on that same prompt. A prompt nobody comes
        back for is taken down by :meth:`withdraw`.
        """
        request_id = request.request_id
        prompt = self._prompts.get(request_id)
        if prompt is None:
            prompt = asyncio.ensure_future(self._decide(request))
            self._prompts[request_id] = prompt
        wake = asyncio.Event()
        woken = asyncio.ensure_future(wake.wait())
        self._wakes[request_id] = wake
        self._pending[request_id] = request.session_id
        keep_prompt = False
        try:
            await asyncio.wait({prompt, woken}, return_when=asyncio.FIRST_COMPLETED)
            if prompt.done():
                # An answer that landed with the reconsideration still stands:
                # the person decided the ask they were shown.
                return prompt.result()
            keep_prompt = True
            raise AskReconsideredError(request_id)
        finally:
            woken.cancel()
            self._wakes.pop(request_id, None)
            self._pending.pop(request_id, None)
            if not keep_prompt:
                # Answered, or the waiter itself was cancelled (teardown): the
                # prompt goes with it, exactly as an awaited resolver would.
                self._prompts.pop(request_id, None)
                prompt.cancel()

    def reconsider(self, request_id: str) -> bool:
        """Have the ask ``request_id`` decided again, if a person is still being
        asked it. Returns whether a waiter was woken: an ask already answered, or
        not parked on a person at all, is left alone."""
        wake = self._wakes.get(request_id)
        prompt = self._prompts.get(request_id)
        if wake is None or prompt is None or prompt.done():
            return False
        wake.set()
        return True

    async def withdraw(self, request_id: str) -> None:
        """Take down a prompt its ask was decided without: the reconsidered
        decision settled it on the stance's own authority.

        Returns once the prompt is down -- the resolver has run its own
        teardown, so no reader still holds the ask -- which is what lets the
        caller answer the agent only after the card is gone."""
        prompt = self._prompts.pop(request_id, None)
        if prompt is None or request_id in self._wakes:
            return
        prompt.cancel()
        # ``wait`` rather than ``await``: the prompt's own cancellation is the
        # expected outcome, not an error, and must not be mistaken for ours.
        await asyncio.wait({prompt})

    async def _decide(self, request: PermissionRequest) -> BrokerDecision:
        try:
            if self._timeout is None:
                option = await self._resolver(request)
            else:
                option = await asyncio.wait_for(self._resolver(request), timeout=self._timeout)
        except ResolverRefusedError as refused:
            return BrokerDecision(refused.option, refused.reason, refused.decided_by)
        except TimeoutError:
            logger.warning(
                "PermissionBroker timeout (%ss) on request %s — auto-rejecting",
                self._timeout,
                request.request_id,
            )
            return BrokerDecision("reject_once", None, "timeout")
        except Exception:
            logger.exception(
                "PermissionBroker resolver raised; auto-rejecting %s",
                request.request_id,
            )
            return BrokerDecision("reject_once", None, "broker")
        return BrokerDecision(option, None, "human")


__all__ = [
    "AskReconsideredError",
    "BrokerDecision",
    "PermissionBroker",
    "Resolver",
    "ResolverRefusedError",
]
