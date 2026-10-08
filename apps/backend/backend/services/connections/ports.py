"""What a connection record asks of the parts a distribution may add.

The open platform keeps team and personal connections, their encrypted secrets
and their leases. Two things around them belong to a richer distribution and
register here, each at most once:

- :data:`CONNECTION_OAUTH`: browser sign-in for a connector. It says whether a
  provider obliges the org to register its own OAuth client, and leases a
  member's live access token. With none registered no connector signs in through
  the browser, so a save that asks for it is refused and a per-user grant has
  nothing to lease.
- :data:`CONNECTION_CHECKS`: a server-side check that a connection works before
  it is saved, and again on a rotated secret. With none registered a save is
  admitted as built and a rotation is not re-checked; the machine that opens the
  connection is the one that finds out.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Protocol, TypeVar
from uuid import UUID

from alkera_core.extensions import ExtensionError, ExtensionPoint

T = TypeVar("T")

if TYPE_CHECKING:
    from alkera_core.connections.models import TeamConnection
    from alkera_core.connections.schemas import TeamConnectionUpsertRequest
    from sqlalchemy.ext.asyncio import AsyncSession


class ReauthRequiredError(Exception):
    """The member must sign in to the provider again; user-facing."""


class ProviderUnreachableError(Exception):
    """The provider could not be reached. NOT a refusal: it has said nothing
    about this grant, so nothing about the grant changes and the caller is told
    to try again rather than to sign in again."""


class GrantOnDeviceError(Exception):
    """The member's grant lives on their own machine (a public client), so the
    server has no token to lease."""


PROVIDER_UNREACHABLE_CODE = "provider_unreachable"
PROVIDER_UNREACHABLE_MESSAGE = "The provider could not be reached; try again."


class ConnectionOAuth(Protocol):
    """Browser sign-in for connections."""

    def org_client_rule(self, provider: str) -> bool | None:
        """Whether ``provider`` obliges the org to register its own client, or
        ``None`` when no sign-in is wired for it."""
        ...

    async def lease(
        self, db: AsyncSession, conn: TeamConnection, *, user_id: UUID
    ) -> tuple[str, datetime | None, str]:
        """A live access token, its expiry and scope. Raises
        :class:`ReauthRequiredError`, :class:`ProviderUnreachableError` or
        :class:`GrantOnDeviceError`."""
        ...


class NoConnectionOAuth:
    """No browser sign-in: no provider is wired, and no grant is held here."""

    def org_client_rule(self, provider: str) -> bool | None:
        return None

    async def lease(
        self, db: AsyncSession, conn: TeamConnection, *, user_id: UUID
    ) -> tuple[str, datetime | None, str]:
        raise GrantOnDeviceError(conn.handle)


#: The browser sign-in a distribution registers. At most one registers.
CONNECTION_OAUTH: ExtensionPoint[ConnectionOAuth] = ExtensionPoint("connections.oauth")


def connection_oauth(
    point: ExtensionPoint[ConnectionOAuth] = CONNECTION_OAUTH,
) -> ConnectionOAuth:
    """The registered sign-in, or :class:`NoConnectionOAuth`. Freezes the point."""
    return _one(point, NoConnectionOAuth(), "browser sign-ins")


class SaveCheckRequiredError(Exception):
    """A save refused until a check answers for this exact configuration."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


#: Run once the request's transaction has committed (a check handed to a worker
#: must not race the row it reads).
AfterCommit = Callable[[], None]


def _nothing() -> None:
    return None


class ConnectionChecks(Protocol):
    """A server-side check that a connection works."""

    #: Whether a save must consume a settled check (the form starts one first).
    checks_before_save: bool

    async def admit_save(
        self,
        db: AsyncSession,
        *,
        team_id: UUID,
        requester: UUID,
        payload: TeamConnectionUpsertRequest,
    ) -> object | None:
        """The settled check this save may consume, or ``None`` when the save
        needs none. Raises :class:`SaveCheckRequiredError`."""
        ...

    async def saved(self, db: AsyncSession, conn: TeamConnection, admitted: object | None) -> None:
        """Carry the admitted check onto the row just saved."""
        ...

    async def recheck(
        self, db: AsyncSession, conn: TeamConnection, *, requested_by: UUID, reason: str
    ) -> AfterCommit:
        """Ask for the stored row to be checked again; the returned callable
        starts it after the commit."""
        ...


class NoConnectionChecks:
    """No server-side check: a save is admitted as built."""

    checks_before_save = False

    async def admit_save(
        self,
        db: AsyncSession,
        *,
        team_id: UUID,
        requester: UUID,
        payload: TeamConnectionUpsertRequest,
    ) -> object | None:
        return None

    async def saved(self, db: AsyncSession, conn: TeamConnection, admitted: object | None) -> None:
        return None

    async def recheck(
        self, db: AsyncSession, conn: TeamConnection, *, requested_by: UUID, reason: str
    ) -> AfterCommit:
        return _nothing


#: The server-side check a distribution registers. At most one registers.
CONNECTION_CHECKS: ExtensionPoint[ConnectionChecks] = ExtensionPoint("connections.checks")


def connection_checks(
    point: ExtensionPoint[ConnectionChecks] = CONNECTION_CHECKS,
) -> ConnectionChecks:
    """The registered check, or :class:`NoConnectionChecks`. Freezes the point."""
    return _one(point, NoConnectionChecks(), "connection checks")


def _one(point: ExtensionPoint[T], default: T, what: str) -> T:
    registered = point.items()
    if len(registered) > 1:
        raise ExtensionError(f"{len(registered)} {what} registered on {point.name!r}; one at most")
    return registered[0] if registered else default


__all__ = [
    "CONNECTION_CHECKS",
    "CONNECTION_OAUTH",
    "PROVIDER_UNREACHABLE_CODE",
    "PROVIDER_UNREACHABLE_MESSAGE",
    "AfterCommit",
    "ConnectionChecks",
    "ConnectionOAuth",
    "GrantOnDeviceError",
    "NoConnectionChecks",
    "NoConnectionOAuth",
    "ProviderUnreachableError",
    "ReauthRequiredError",
    "SaveCheckRequiredError",
    "connection_checks",
    "connection_oauth",
]
