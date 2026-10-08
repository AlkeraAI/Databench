"""What rides every turn after the mode steering and the root guidance.

A turn brief is asked on every turn and answers the text that joins the
turn's system block, or ``None`` when it has nothing to say. The chat's list,
in order: its live task list, the environment's restore notice, and what others
did in the notebooks this chat has touched. A new per-turn brief is one more
entry here, not another argument threaded through the prompt composer.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING

from alkera_cli.notebooks.session import notebook_digest

if TYPE_CHECKING:
    from alkera_cli.harness.runtime import ChatSession

TurnBriefProvider = Callable[[], Awaitable[str | None]]

logger = logging.getLogger(__name__)


def turn_briefs(session: ChatSession) -> list[TurnBriefProvider]:
    """The chat's per-turn briefs, in the order they join the system block."""

    async def wake_notice() -> str | None:
        return session._runtime.environment.notice(session.session_id)

    async def notebooks() -> str | None:
        # A subagent reports through its parent, which keeps the cursors.
        binding = None if session.is_subagent else session._tool_binding
        return await notebook_digest(binding, session._permission_mode)

    return [session._task_reminder, wake_notice, notebooks]


async def brief_texts(providers: Sequence[TurnBriefProvider], chat_id: str) -> list[str | None]:
    """Every brief's text for the turn about to start, in order.

    A brief is context the model reads beside the person's words, never the
    words themselves, so one that cannot be read (a notebook host that is
    still reconnecting to the workspace's files, a task store that cannot be
    loaded) is left out of this turn and logged. Raising here failed the whole
    message before the agent saw it."""
    texts: list[str | None] = []
    for provider in providers:
        try:
            texts.append(await provider())
        except Exception:
            logger.warning(
                "chat %s: a turn brief could not be read; the turn runs without it",
                chat_id,
                exc_info=True,
                extra={"chat_id": chat_id},
            )
            texts.append(None)
    return texts


__all__ = ["TurnBriefProvider", "brief_texts", "turn_briefs"]
