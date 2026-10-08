"""In-memory representation of the current CLI user.

The CLI never persists user profile data to disk — only the bearer token
in `~/.alkera/auth.yml`. This module's job is to turn a token into a
fresh `CurrentUser` by hitting `GET /auth/me`, with results held only
for the lifetime of the running process.

Use `resolve_user(api_url, token)` from any command that needs identity.
"""

from __future__ import annotations

import httpx
from alkera_sdk import AlkeraClient
from pydantic import BaseModel


class CurrentUser(BaseModel):
    """Snapshot of the authenticated user, fetched from `/auth/me`."""

    id: str
    email: str
    display_name: str
    # True while the account still owes a verified email. The gateway refuses
    # model requests outright for such accounts, so the chat surfaces (TUI,
    # VS Code webview) block their composers on this instead of letting a send
    # bounce off a 403.
    email_verification_required: bool = False
    #: The org the token is bound to (``/auth/me`` describes the token's org).
    org_team_id: str = ""
    org_name: str = ""


def resolve_user(
    api_url: str, token: str, *, org_id: str | None = None, timeout: float = 5.0
) -> CurrentUser | None:
    """Fetch the user the token authenticates as. Returns None on auth failure
    or any transport error — callers decide how to react. ``org_id`` is the org
    the caller believes the token is for: the server refuses a different one
    (409 ``org_changed``), which reads as an auth failure here too.
    """
    try:
        with AlkeraClient(base_url=api_url, token=token, org_id=org_id, timeout=timeout) as api:
            result = api.auth.me()
    except (httpx.HTTPError, RuntimeError):
        return None
    org_name = result.org_name if isinstance(result.org_name, str) else ""
    return CurrentUser(
        id=str(result.id),
        email=result.email,
        display_name=result.display_name,
        email_verification_required=result.email_verification_required is True,
        org_team_id=str(result.org_team_id),
        org_name=org_name,
    )


def revoke_session(
    api_url: str, token: str, *, org_id: str | None = None, timeout: float = 5.0
) -> bool:
    """Best-effort: ask the backend to revoke this token server-side (logout).

    Returns True on success, False on any transport/API error. Callers clear
    local state regardless — logout must succeed even offline / with a dead
    token.
    """
    try:
        with AlkeraClient(base_url=api_url, token=token, org_id=org_id, timeout=timeout) as api:
            api.auth.logout()
    except (httpx.HTTPError, RuntimeError):
        return False
    return True


__all__ = ["CurrentUser", "resolve_user", "revoke_session"]
