"""The sweep behind the chat warmed ahead of a person's first message.

A spare — a chat the backend warmed while its owner was on the chat page, its
session opened by the box, hidden until the owner's first send claims it —
is reaped once its owner's page has stopped beating for the idle cutoff, or
once it is older than the maximum age. The row goes first (the box lists the
chat gone and stops the mirror, which hands the folder's lease back), the
folder the moment nothing holds it; a folder still leased on one pass is the
next pass's. Everything it does is in :mod:`alkera_core.objects.chat_spares`,
the same code the backend reaps with on logout and on a claim that found a
dead machine, so the two cannot disagree about what "reaped" leaves behind.
"""

from __future__ import annotations

from datetime import datetime

from alkera_core.db.session import AsyncSessionLocal
from alkera_core.logging import get_logger
from alkera_core.objects import chat_spares

log = get_logger(__name__)


async def run_reap_chat_spares(now: datetime) -> int:
    """One pass: reap every stale spare and purge every folder an earlier
    reap left leased. Returns how many spares were reaped."""
    async with AsyncSessionLocal() as session:
        reaped = await chat_spares.reap_stale(session, now=now)
        await session.commit()
    if reaped:
        log.info("chat.spares.reaped", count=reaped)
    return reaped
