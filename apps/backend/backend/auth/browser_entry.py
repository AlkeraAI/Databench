"""API routes a person reaches by clicking a link, and what a signed-out click gets.

A few API routes are not called by the SPA at all: a person lands on them from
outside -- a link in a Slack message, the redirect back from Slack's consent
screen -- and the server answers with a page of its own. For those, a missing
session is not a client bug to report as JSON: it is a person on a phone or in
a browser that has not signed in yet. A top-level navigation with no usable
session is sent to the sign-in page with this exact URL as its ``return_to``,
so the query string (the link code, the install code and state) survives the
round trip and the route runs once the person is back, signed in.

Anything that is not a page navigation -- a ``fetch``, the CLI, a test client
asking for JSON -- still gets the 401 the rest of the API answers, so no API
client ever follows a redirect to an HTML login page.

The SPA's post-sign-in navigation only honours an ``/api/...`` return target it
knows to be one of these pages (``SERVER_PAGES`` in ``auth-actions.tsx``); a
route added here must be added there too.
"""

from __future__ import annotations

from urllib.parse import quote

from alkera_core.config import settings
from alkera_core.models import User
from fastapi import HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth.dependencies import current_user


def is_page_navigation(request: Request) -> bool:
    """True when a browser is loading this URL as the page itself.

    ``Sec-Fetch-Mode: navigate`` is the browser's own statement and wins when
    present; it cannot be set by page script. A client that sends no fetch
    metadata (an older browser) is read from what it asks to receive: a page
    load accepts ``text/html``; an API client asks for JSON or anything.
    """
    if request.method != "GET":
        return False
    mode = request.headers.get("sec-fetch-mode")
    if mode is not None:
        return mode.strip().lower() == "navigate"
    return "text/html" in request.headers.get("accept", "").lower()


def sign_in_redirect(request: Request) -> RedirectResponse:
    """Send the browser to sign in, returning to exactly this URL afterwards.

    The return target is the request's own path and query -- relative, so the
    SPA's open-redirect guard accepts it and it resolves on the portal's origin,
    which serves ``/api`` in every deployment shape.
    """
    target = request.url.path
    if request.url.query:
        target = f"{target}?{request.url.query}"
    location = f"{settings.frontend_base_url.rstrip('/')}/login?return_to={quote(target, safe='')}"
    # The target carries a single-use code; nothing on the way may keep a copy.
    return RedirectResponse(
        location,
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


async def user_or_sign_in(request: Request, db: AsyncSession) -> User | RedirectResponse:
    """The signed-in user, or -- for a page navigation with no usable session --
    the redirect to sign in. Every other refusal is raised unchanged."""
    try:
        return await current_user(request, db)
    except HTTPException as exc:
        if exc.status_code == status.HTTP_401_UNAUTHORIZED and is_page_navigation(request):
            return sign_in_redirect(request)
        raise


__all__ = ["is_page_navigation", "sign_in_redirect", "user_or_sign_in"]
