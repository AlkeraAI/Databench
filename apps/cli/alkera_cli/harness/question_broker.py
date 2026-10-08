"""Routes `QuestionRequest` events to a registered resolver.

Parallel to `PermissionBroker`. The harness emits a `QuestionRequest`
when it wants a clarification from the user mid-turn (opencode's
`question` tool, ACP's similar clarifier, …). The orchestrator routes
to:

- CLI REPL: prompts on stdin with numbered options + free-form fallback.
- Daemon: server→client `harness.question_required` JSON-RPC request
  the editor renders in the chat webview.

The resolver returns a `QuestionResolution`:

- `("answer", [[label, ...], ...])` — answers parallel to
  `QuestionRequest.questions`. Each inner list is the chosen labels
  for that question (1 element for single-select, N for multi-select).
- `("reject", reason)` — the user dismissed.

On timeout or resolver exception, the broker auto-rejects. The
harness sees this as a hard rejection — its question-tool call errors
out.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Literal, TypeAlias

if TYPE_CHECKING:
    from alkera_core.schemas.chat import QuestionRequest

logger = logging.getLogger(__name__)


# Resolver result — discriminated by the first element.
QuestionResolution: TypeAlias = (
    tuple[Literal["answer"], list[list[str]]] | tuple[Literal["reject"], str | None]
)


# A resolver takes a question request and returns a `QuestionResolution`.
QuestionResolver = Callable[["QuestionRequest"], Awaitable[QuestionResolution]]


class QuestionBroker:
    """Holds the resolver + timeout policy.

    The default is NO timeout: a clarifier question waits for the human
    indefinitely (they may have walked away — an auto-reject under them breaks
    the turn). A timeout is opt-in for non-interactive resolvers (tests,
    automation) that must not deadlock.
    """

    def __init__(
        self,
        resolver: QuestionResolver,
        *,
        default_timeout_seconds: float | None = None,
    ) -> None:
        self._resolver = resolver
        self._timeout = default_timeout_seconds
        # request_id → (session_id, ask kind) for every question currently
        # parked on a human — what chat lists surface as the pending-ask mark.
        # Keyed by session because subagent sessions share their parent's
        # broker instance.
        self._pending: dict[str, tuple[str, Literal["question", "plan"]]] = {}

    @property
    def default_timeout_seconds(self) -> float | None:
        return self._timeout

    def pending_kind_for(self, session_id: str) -> Literal["question", "plan"] | None:
        """The ask kind ``session_id`` is waiting on the human for, or ``None``."""
        for sid, kind in self._pending.values():
            if sid == session_id:
                return kind
        return None

    async def resolve(self, request: QuestionRequest) -> QuestionResolution:
        """Ask the resolver; return their answer-or-rejection. On
        timeout or resolver exception, auto-rejects."""
        kind: Literal["question", "plan"] = (
            "plan" if request.kind == "plan_approval" else "question"
        )
        self._pending[request.request_id] = (request.session_id, kind)
        try:
            return await self._resolve(request)
        finally:
            self._pending.pop(request.request_id, None)

    async def _resolve(self, request: QuestionRequest) -> QuestionResolution:
        if self._timeout is None:
            try:
                return await self._resolver(request)
            except Exception:
                logger.exception(
                    "QuestionBroker resolver raised; auto-rejecting %s",
                    request.request_id,
                )
                return ("reject", "resolver-error")
        try:
            return await asyncio.wait_for(self._resolver(request), timeout=self._timeout)
        except TimeoutError:
            logger.warning(
                "QuestionBroker timeout (%ss) on request %s — auto-rejecting",
                self._timeout,
                request.request_id,
            )
            return ("reject", "timeout")
        except Exception:
            logger.exception(
                "QuestionBroker resolver raised; auto-rejecting %s",
                request.request_id,
            )
            return ("reject", "resolver-error")


__all__ = ["QuestionBroker", "QuestionResolution", "QuestionResolver"]
