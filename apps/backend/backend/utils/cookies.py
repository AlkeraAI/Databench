"""Centralized cookie helpers for the session + OAuth-handshake cookies.

One place to set/clear so flags stay consistent across login/logout/refresh
and the OAuth flow.
"""

from __future__ import annotations

from alkera_core.auth import COOKIE_NAME, OAUTH_STATE_TTL_SECONDS, REFRESH_ROUTE_PATH
from alkera_core.auth.tokens import GITHUB_INSTALL_CLAIM_TTL_SECONDS
from alkera_core.config import settings
from fastapi import Response

# Transient cookie holding the signed OAuth handshake state (CSRF + nonce +
# carried params). Set at /start, read-and-cleared at /callback. SameSite=lax
# so it survives the top-level GET redirect back from the provider.
OAUTH_STATE_COOKIE = "alkera_oauth_tx"

# Transient cookie holding the GitHub install nonce. Planted when the install URL
# is minted, matched against the signed state's nonce hash when GitHub's callback
# comes back -- the double-submit that binds the STATE (not the code or
# installation id, which stay independent inputs) to ONE browser session, and,
# reused as the PKCE verifier + the confirmation's session key, ties the whole
# flow to this browser. SameSite=lax for the same reason as the OAuth cookie: it
# has to survive the top-level GET redirect back from GitHub.
INSTALL_NONCE_COOKIE = "alkera_gh_install"

# Transient cookie holding the Slack link confirmation nonce. Planted when the
# clicked link renders its confirmation page, matched against the form's hidden
# field when that page is submitted -- the double-submit that proves the binding
# was confirmed in THIS browser, on a page that named the Slack identity, rather
# than performed by a cross-site navigation carrying only the SameSite=lax
# session cookie.
SLACK_LINK_COOKIE = "alkera_slack_link"


def _set_transient_cookie(response: Response, *, key: str, value: str, max_age: int) -> None:
    """One home for the shared flags: HttpOnly, SameSite=lax, Secure per
    settings, path=/. Every cookie this module sets carries exactly these."""
    response.set_cookie(
        key=key,
        value=value,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=settings.auth_cookie_secure,
        path="/",
    )


def _clear_transient_cookie(response: Response, *, key: str) -> None:
    """Delete with the same flags the cookie was set with, so the browser
    matches (and actually drops) it."""
    response.delete_cookie(
        key=key,
        path="/",
        httponly=True,
        samesite="lax",
        secure=settings.auth_cookie_secure,
    )


def set_session_cookie(response: Response, token: str) -> None:
    _set_transient_cookie(
        response, key=COOKIE_NAME, value=token, max_age=settings.auth_token_ttl_seconds
    )


def clear_session_cookie(response: Response) -> None:
    _clear_transient_cookie(response, key=COOKIE_NAME)


def set_refresh_cookie(response: Response, token: str) -> None:
    """The refresh token rides ONLY to the refresh route: ``Path`` keeps the
    long-lived credential off every ordinary API request (and off the logout
    route, which ends the family through the access token's ``jti`` instead).
    ``SameSite=strict`` because the only sender is the app's own fetch."""
    response.set_cookie(
        key=settings.auth_refresh_cookie_name,
        value=token,
        max_age=settings.auth_refresh_absolute_seconds,
        httponly=True,
        samesite="strict",
        secure=settings.auth_cookie_secure,
        path=REFRESH_ROUTE_PATH,
    )


def clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        key=settings.auth_refresh_cookie_name,
        path=REFRESH_ROUTE_PATH,
        httponly=True,
        samesite="strict",
        secure=settings.auth_cookie_secure,
    )


def set_oauth_state_cookie(response: Response, token: str) -> None:
    _set_transient_cookie(
        response, key=OAUTH_STATE_COOKIE, value=token, max_age=OAUTH_STATE_TTL_SECONDS
    )


def clear_oauth_state_cookie(response: Response) -> None:
    _clear_transient_cookie(response, key=OAUTH_STATE_COOKIE)


def set_install_nonce_cookie(response: Response, nonce: str) -> None:
    _set_transient_cookie(
        response, key=INSTALL_NONCE_COOKIE, value=nonce, max_age=GITHUB_INSTALL_CLAIM_TTL_SECONDS
    )


def clear_install_nonce_cookie(response: Response) -> None:
    _clear_transient_cookie(response, key=INSTALL_NONCE_COOKIE)


def set_slack_link_cookie(response: Response, nonce: str, *, max_age: int) -> None:
    _set_transient_cookie(response, key=SLACK_LINK_COOKIE, value=nonce, max_age=max_age)


def clear_slack_link_cookie(response: Response) -> None:
    _clear_transient_cookie(response, key=SLACK_LINK_COOKIE)
