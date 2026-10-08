"""Which opencode session a chat resumes: a pure choice over the agent's own
session listing, the session the chat's manifest pins, and whether the store
is the chat's record or a cache of its transcript.
"""

from __future__ import annotations

import logging

from alkera_cli.harness.adapter import HarnessUnavailableError
from alkera_cli.harness.adapters.opencode_translate import _pick_latest_session

logger = logging.getLogger(__name__)


def choose_session(
    sessions: object, pinned: object, *, store_is_cache: bool, chat_id: str
) -> str | None:
    """The session to attach to, or ``None`` to create a fresh one.

    1. A pinned session the store holds is attached to.
    2. A pinned session the store does not hold is refused locally, where it
       means the store was wiped and a person should repair the chat, never
       silently re-targeted. Where the store is a cache (a cloud box, whose
       store travels with the folder and may not have landed before the last
       box let it go) the chat continues in a fresh session from the
       transcript: refusing would refuse it on every box from here on.
    3. An unpinned chat (older than the pin) takes the latest session.
    """
    listed = sessions if isinstance(sessions, list) else []
    existing = {s["id"] for s in listed if isinstance(s, dict) and isinstance(s.get("id"), str)}
    if isinstance(pinned, str) and pinned:
        if pinned in existing:
            logger.info("attaching to pinned agent session %s", pinned)
            return pinned
        if not store_is_cache:
            raise HarnessUnavailableError(
                f"agent session {pinned!r} pinned in this chat's manifest "
                "no longer exists in the harness storage (was the .harness/ "
                "directory wiped?). Start a new chat to continue."
            )
        logger.warning(
            "agent session %s pinned for %s is not in the store that came with its "
            "folder; the chat continues in a fresh session from the transcript",
            pinned,
            chat_id,
        )
        return None
    latest = _pick_latest_session(listed) if listed else None
    if latest is not None:
        logger.info("attaching to existing agent session %s", latest)
    return latest


__all__ = ["choose_session"]
