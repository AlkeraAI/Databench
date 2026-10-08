"""The org a browser tab rendered, held to the org its session is in.

A browser holds one session, so a switch in one window moves every window of
that browser into the new org. A tab still showing the old org names the org it
rendered on every request (``X-Alkera-Org``); the server refuses the request
before the route runs when that is not the credential's org. That guarantee is
only as good as its coverage: a write that names no org at all could land in an
org the tab never showed. So a write that rides the browser's session cookie
must name its org: logged while open tabs may still run a portal that did not,
refused once ``ORG_ASSERTION_MODE`` is ``enforce``. Every credential a tab
never holds (a Bearer JWT from the CLI or the editor, a box's machine or worker
credential, a personal access token, a CI or proxy token) is held to the header
only when it sends one.

Reads are never refused for a missing header: a stale tab must still be able to
render, and its reads are how it learns its session moved.
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

from alkera_core.auth.tenancy import ORG_HEADER
from alkera_core.authz import ActingContext
from alkera_core.config import settings
from alkera_core.logging import get_logger
from fastapi import HTTPException, Request

from backend.auth.refusals import ORG_ASSERTION_REQUIRED, ORG_CHANGED, Reader

log = get_logger(__name__)

#: The client asserted an org (``X-Alkera-Org``) that is not its credential's.
ORG_CHANGED_CODE: Final = ORG_CHANGED.code
#: A browser write that named no org: the tab is running a portal from before
#: every write named its org, or the caller is not the portal at all.
ORG_ASSERTION_REQUIRED_CODE: Final = ORG_ASSERTION_REQUIRED.code

#: The methods that change something. Every other method reads.
UNSAFE_METHODS: Final = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: The routes a browser writes through with its session cookie before or across
#: an org choice, where the tab has no rendered org to name: signing in or up
#: (no org yet), signing out and renewing the session (the session itself, not
#: an org's data), and switching, creating or joining an org from the switcher
#: or the no-organization landing (the org is what the request chooses). None of
#: them resolves its caller through the credential door today, so none meets the
#: requirement; naming them here keeps it that way if one ever does. Keyed by
#: method and the route's path template.
ORG_LESS_WRITES: Final = frozenset(
    {
        ("POST", "/api/v1/auth/login"),
        ("POST", "/api/v1/auth/signup"),
        ("POST", "/api/v1/auth/oauth/register"),
        ("POST", "/api/v1/auth/logout"),
        ("POST", "/api/v1/auth/refresh"),
        ("POST", "/api/v1/auth/refresh/org"),
        ("POST", "/api/v1/auth/refresh/org/new"),
        ("POST", "/api/v1/auth/refresh/org/join"),
    }
)


def org_changed(reader: Reader) -> HTTPException:
    return ORG_CHANGED.to(reader)


def org_assertion_required() -> HTTPException:
    """Only a browser session is held to naming its org, so only a tab reads it."""
    return ORG_ASSERTION_REQUIRED.to(Reader.BROWSER)


def _route_path(request: Request) -> str | None:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else None


def _must_name_its_org(request: Request) -> bool:
    """Whether this browser request is a write that must name its org."""
    if request.scope.get("type") != "http" or request.method not in UNSAFE_METHODS:
        return False
    return (request.method, _route_path(request)) not in ORG_LESS_WRITES


def _unnamed_browser_write(request: Request, ctx: ActingContext) -> None:
    """A browser write that names no org: logged, and refused when enforced."""
    log.warning(
        "org_assertion.missing",
        route=_route_path(request) or "",
        method=request.method,
        org_id=str(ctx.org_id),
        enforced=settings.org_assertion_mode == "enforce",
    )
    if settings.org_assertion_mode == "enforce":
        raise org_assertion_required()


def refuse_a_stale_org_assertion(
    request: Request, ctx: ActingContext, *, browser_session: bool
) -> None:
    """Hold the client's org assertion to its credential's org.

    The header is an assertion, never a selector: a mismatch (or an unreadable
    value) is a 409 ``org_changed`` before the route runs, whatever the
    credential. ``browser_session`` says the credential is the browser's session
    cookie; such a request that writes without naming its org, other than one of
    :data:`ORG_LESS_WRITES`, logs ``org_assertion.missing`` and, when
    ``ORG_ASSERTION_MODE`` is ``enforce``, is a 428 ``org_assertion_required``.
    Every other credential without the header is unaffected.
    """
    asserted = request.headers.get(ORG_HEADER)
    if asserted is None:
        if browser_session and _must_name_its_org(request):
            _unnamed_browser_write(request, ctx)
        return
    try:
        matches = UUID(asserted.strip()) == ctx.org_id
    except ValueError:
        matches = False
    if not matches:
        raise org_changed(Reader.of(browser_session=browser_session))
