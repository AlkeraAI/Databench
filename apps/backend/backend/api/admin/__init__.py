"""Admin router (`/admin/v1/...`).

Strict separation from tenant routes — different prefix, different package,
different module namespace. Router-level dep enforces ALKERA_SUPPORT floor;
specific routes inside override to require ALKERA_ADMIN where dangerous.

The shape of the router itself is the safety net: any endpoint mounted by
`build_admin_router`, an extension's included, runs `require_platform_staff`
at minimum. Skipping that requires editing this file to remove the
dependency, which is loud in code review.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Annotated

from alkera_core.db.cross_tenant import cross_tenant_write
from alkera_core.models import User
from fastapi import APIRouter, Depends

from backend.api.admin import (
    audit,
    bans,
    compute,
    machines,
    offerings,
    ops,
    org_insight,
    orgs,
    sso_domains,
    users,
)
from backend.auth.dependencies import DbSession, require_platform_staff


async def platform_window(
    db: DbSession, _staff: Annotated[User, Depends(require_platform_staff)]
) -> AsyncIterator[None]:
    """Hold every admin request in one cross-tenant window.

    Staff act across every org by design (which staff may do what is the
    platform policies' decision, already made by ``require_platform_staff`` and
    each route's own checks), so the request's session steps out of the
    content tables' row-level policy for the length of the handler. Function
    scope: the window closes before the session commits."""
    async with cross_tenant_write(db, reason="admin.platform_surface"):
        yield


def build_admin_router(extension_routers: Sequence[APIRouter] = ()) -> APIRouter:
    """The ``/admin/v1`` router: every core admin surface, plus the routers
    extensions registered for it, mounted among the per-org surfaces.

    Each sub-router sets ``route_class=AuditedRoute`` so successful writes are
    recorded to the audit log. FastAPI's include_router preserves each route's
    own class, so setting it here would NOT cover the children; it must live on
    every sub-router, an extension's included. A completeness test enforces this.
    """
    admin_router = APIRouter(
        prefix="/admin/v1",
        tags=["admin"],
        dependencies=[
            Depends(require_platform_staff),
            Depends(platform_window, scope="function"),
        ],
    )
    for child in (
        audit,
        bans,
        compute,
        machines,
        offerings,
        ops,
        org_insight,
    ):
        admin_router.include_router(child.router)
    for router in extension_routers:
        admin_router.include_router(router)
    for child in (orgs, sso_domains, users):
        admin_router.include_router(child.router)
    return admin_router


__all__ = ["build_admin_router"]
