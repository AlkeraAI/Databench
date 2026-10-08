"""Finishing a sign-in and reading the saved one, for every surface that signs in.

``alkera login`` and the editor's device login (the daemon's ``auth.*`` methods)
both end in :func:`complete_login`, so a token is saved only when it passes the
same three checks wherever it was minted. The API must accept it, the account
must have verified its email, and the model gateway must not reject it. A
gateway that cannot be reached does not block the save: an outage is not an
auth failure, and the surface says the token was checked against the API only.

:func:`auth_status` is the saved sign-in as both surfaces read it.

Each surface keeps its own words; this module returns outcomes, never messages.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from alkera_cli.account import auth_file, session
from alkera_cli.account.auth_file import Profile, ProfileSource
from alkera_cli.account.jwt_decode import jwt_expires_at
from alkera_cli.gateway import client as gateway_client
from alkera_cli.host.config import get_settings

if TYPE_CHECKING:
    from alkera_core.project import ProjectDirectory

LoginRefusal = Literal[
    "api_rejected", "email_verification_required", "gateway_rejected", "cancelled"
]
"""Why a minted token was not saved. ``cancelled`` is the caller's own ``should_stop``."""

SignedOutReason = Literal["missing", "invalid", "email_verification_required", "refused"]
"""Why the saved sign-in does not hold. ``refused``: a profile exists but may
not be used here (the project is pinned to another org, or the asked-for org
has no sign-in)."""


@dataclass(frozen=True, slots=True)
class GatewayCheck:
    """The gateway's answer to a token.

    ``accepted`` is True when the gateway served the token, False when it
    rejected it (``detail`` carries its reason), and None when it could not be
    reached (``detail`` says why)."""

    accepted: bool | None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class LoginOutcome:
    """What :func:`complete_login` did with a token.

    ``refusal`` is None exactly when the token was saved. ``email`` is the
    account's whenever the API resolved one. ``gateway`` is None when the check
    never ran (the API or the email rule refused first)."""

    refusal: LoginRefusal | None
    email: str | None = None
    expires_at: datetime | None = None
    gateway: GatewayCheck | None = None
    profile: Profile | None = None

    @property
    def saved(self) -> bool:
        return self.refusal is None


@dataclass(frozen=True, slots=True)
class AuthStatus:
    """The resolved sign-in, checked against the API.

    ``reason`` is None exactly when it holds. ``stored`` is the profile
    whenever one resolved, so a caller can probe further with its token.
    ``detail`` is the one-line refusal when ``reason`` is ``refused`` (a pinned
    project, or an asked-for org with no sign-in)."""

    reason: SignedOutReason | None
    stored: Profile | None = None
    email: str | None = None
    source: ProfileSource | None = None
    detail: str | None = None

    @property
    def authenticated(self) -> bool:
        return self.reason is None


def check_gateway(token: str) -> GatewayCheck:
    """Ask the gateway the CLI's chats talk to whether it serves ``token``.

    Runs its own event loop, so call it from a thread, never from a running loop."""
    settings = get_settings()

    async def _probe() -> None:
        await gateway_client.fetch_models(
            gateway_url=settings.alkera_gateway_url,
            token=token,
            timeout_seconds=settings.request_timeout_seconds,
        )

    try:
        asyncio.run(_probe())
    except gateway_client.GatewayAuthError as exc:
        return GatewayCheck(False, exc.detail)
    except gateway_client.GatewayUnavailableError as exc:
        return GatewayCheck(None, str(exc))
    return GatewayCheck(True)


def complete_login(
    api_url: str,
    token: str,
    *,
    should_stop: Callable[[], bool] | None = None,
    make_current: bool = True,
) -> LoginOutcome:
    """Check a freshly minted token and save it when every check passes.

    The token is stored as the profile its own claims name (person and org),
    never the org the caller asked for: an approver may pick another org on
    the page, and the token is what the server will act as. A sign-in to a new
    org or as another person is simply another profile.

    The email rule is read from the API's identity, before the gateway is
    asked, so an unverified account is refused the same way whether or not the
    gateway is up. ``should_stop`` is asked last, just before the save, so a
    caller that stopped wanting the sign-in during the checks (a cancel, a
    logout, a newer sign-in) is answered ``cancelled`` and nothing is written."""
    user = session.resolve_user(api_url, token)
    if user is None:
        return LoginOutcome(refusal="api_rejected")
    if user.email_verification_required:
        return LoginOutcome(refusal="email_verification_required", email=user.email)
    gateway = check_gateway(token)
    if gateway.accepted is False:
        return LoginOutcome(refusal="gateway_rejected", email=user.email, gateway=gateway)
    if should_stop is not None and should_stop():
        return LoginOutcome(refusal="cancelled", email=user.email, gateway=gateway)
    expires_at = jwt_expires_at(token)
    minted = auth_file.profile_from_token(
        api_url, token, email=user.email, org_name=user.org_name, expires_at=expires_at
    )
    if not minted.user_id or not minted.org_team_id:
        # A token whose claims cannot be read is keyed by what the API says it
        # is: /auth/me describes the token's own org.
        minted = minted.model_copy(
            update={
                "user_id": minted.user_id or user.id,
                "org_team_id": minted.org_team_id or user.org_team_id,
            }
        ).with_key()
    stored = auth_file.save_profile(minted, make_current=make_current)
    return LoginOutcome(
        refusal=None, email=user.email, expires_at=expires_at, gateway=gateway, profile=stored
    )


def auth_status(*, project: ProjectDirectory | None = None, org: str | None = None) -> AuthStatus:
    """The sign-in an operation for ``project`` would use, and whether the API
    still accepts it. A profile whose org name is missing or stale is refreshed
    from ``/auth/me``.

    An unverified account reads as signed out, since the gateway refuses it
    every model request. Its token stays saved, so the next read holds once the
    email is verified."""
    try:
        resolved = auth_file.resolve_profile_with_source(org=org, project=project)
    except auth_file.ProfileResolutionError as exc:
        return AuthStatus(reason="refused", detail=str(exc))
    if resolved is None:
        return AuthStatus(reason="missing")
    stored = resolved.profile
    user = session.resolve_user(stored.api_url, stored.token, org_id=stored.org_team_id or None)
    if user is None:
        return AuthStatus(reason="invalid", stored=stored, source=resolved.source)
    if resolved.source != "token_env" and (
        user.org_name != stored.org_name or not stored.user_id or not stored.org_team_id
    ):
        refreshed = auth_file.update_profile_identity(
            stored.key,
            user_id=user.id,
            org_team_id=user.org_team_id,
            email=user.email,
            org_name=user.org_name,
        )
        stored = refreshed or stored
    if user.email_verification_required:
        return AuthStatus(
            reason="email_verification_required",
            stored=stored,
            email=user.email,
            source=resolved.source,
        )
    return AuthStatus(reason=None, stored=stored, email=user.email, source=resolved.source)


__all__ = [
    "AuthStatus",
    "GatewayCheck",
    "LoginOutcome",
    "LoginRefusal",
    "SignedOutReason",
    "auth_status",
    "check_gateway",
    "complete_login",
]
