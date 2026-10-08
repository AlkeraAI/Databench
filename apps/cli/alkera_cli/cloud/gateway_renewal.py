"""How a cloud chat's mirror keeps its agent on a gateway token that has not
lapsed: checked before every turn, renewed between turns by opening the agent
again on a fresh token. A mixin over ``ChatMirror``."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx
from alkera_core.auth.tokens import GATEWAY_TOKEN_REFRESH_BEFORE_SECONDS

from alkera_cli.cloud.rest import CloudApiError

if TYPE_CHECKING:
    import asyncio

    from alkera_cli.harness import ChatSession

logger = logging.getLogger(__name__)


class GatewayTokenRenewal:
    """The mirror's side of a gateway token's lifetime."""

    if TYPE_CHECKING:
        _chat_id: str
        _session: ChatSession | None
        _broker: Any
        _idle: asyncio.Event
        _gateway_token_expires_at: datetime | None

        async def _mint_gateway_token(self) -> str: ...

        async def _reopen_on(self, gateway_token: str) -> None: ...

    def _gateway_token_due(self) -> bool:
        """Whether the agent's gateway token is close enough to lapsing that
        the next turn must not start on it. A token whose expiry the backend
        did not state is left alone: there is nothing to judge it by."""
        expires = self._gateway_token_expires_at
        if expires is None:
            return False
        left = expires - datetime.now(UTC)
        return left < timedelta(seconds=GATEWAY_TOKEN_REFRESH_BEFORE_SECONDS)

    async def _refresh_gateway_token(self) -> None:
        """Put the agent on a fresh gateway token before a turn starts, when
        the one it holds is about to lapse.

        The token reaches the agent only at spawn — in the inline config
        opencode reads once, or in the Claude subprocess's environment — so it
        cannot be swapped under a running agent: the session is closed and
        opened again on the new token, which resumes the same conversation the
        way a sleep and a wake do. That only ever happens between turns; a turn
        still running keeps the token it started on.

        The new token is minted BEFORE the old session is closed, so a mint
        that fails leaves the agent as it was. A refusal (the box's session is
        gone, or the box is no longer the publisher) is final and the turn is
        not started on a token that would be refused too; a mint that merely
        did not arrive leaves the turn to run on the old token while it is
        still good, and the next turn asks again.
        """
        if not self._gateway_token_due() or not self._idle.is_set():
            return
        if self._session is None or self._broker is None:
            return
        previous = self._gateway_token_expires_at
        try:
            token = await self._mint_gateway_token()
        except CloudApiError as exc:
            if exc.status in (401, 403, 404) or not _still_good(previous):
                raise
            logger.warning(
                "mirror %s: the gateway token could not be renewed (%s); this turn runs on "
                "the current one",
                self._chat_id,
                exc,
            )
            return
        except (httpx.HTTPError, OSError) as exc:
            if not _still_good(previous):
                raise
            logger.warning(
                "mirror %s: the gateway token could not be renewed (%s); this turn runs on "
                "the current one",
                self._chat_id,
                exc,
            )
            return
        await self._reopen_on(token)


def _still_good(expires: datetime | None) -> bool:
    """Whether a gateway token expiring at ``expires`` is still accepted now."""
    return expires is None or expires > datetime.now(UTC)


__all__ = ["GatewayTokenRenewal"]
