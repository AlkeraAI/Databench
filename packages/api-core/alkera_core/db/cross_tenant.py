"""The one way a platform job reads a tenant table across every tenant.

The Files tables carry a FORCE'd row-level policy keyed on ``alkera.org_id``.
A statement with no tenant context is not refused by that policy -- it is
*filtered*, to nothing -- unless the login bypasses row security. So a platform
job that lists "every org with a drive" gets the right answer under a superuser
and an empty one under an ordinary login, with no error either way, and a job
that reads an empty answer as "nothing exists" then acts on every tenant.

This seam makes that read explicit, audited and loud:

* the role is proven first -- a login that neither is a superuser nor holds
  ``BYPASSRLS`` is refused before it reads a row;
* the statements inside run with ``row_security = off``, under which Postgres
  RAISES when a policy would have applied instead of filtering, so a silently
  filtered answer is impossible even if the role check were wrong;
* every use writes one ``db.cross_tenant_read`` line naming why.

Read-only by construction: the transaction is rolled back on the way out.

:func:`cross_tenant_write` is the same proof for platform work that must
write (or read inside a unit of work it is about to commit) across tenants from
inside a request whose session is bound to its caller's orgs
(:mod:`alkera_core.db.tenant_session`): it steps out of the tenant role to the
login for its block, proves the login bypasses row security, logs
``db.cross_tenant_write`` and puts the role back afterwards. The caller's
transaction commits normally.

On a session bound to a tenant both seams first step out of the tenant role:
it is the login, not the role, whose privileges are being proven.

Nothing else in the codebase sets ``row_security``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from alkera_core.db.errors import sqlstate_of
from alkera_core.db.tenant_session import (
    LOGIN_ROLE,
    apply_now,
    bound_org_ids,
    close_window,
    in_window,
    open_window,
    restore_role,
    swap_role,
)
from alkera_core.db.unwind import restore_while_unwinding
from alkera_core.logging import get_logger

log = get_logger(__name__)

_INSUFFICIENT_PRIVILEGE = "42501"


_WHO = (
    "SELECT current_user, r.rolsuper OR r.rolbypassrls "
    "FROM pg_roles r WHERE r.rolname = current_user"
)


class CrossTenantReadRefused(RuntimeError):  # noqa: N818 - named for what happened
    """The database login cannot be trusted to see every tenant's rows."""

    code = "cross_tenant_read_refused"


class CrossTenantWriteRefused(RuntimeError):  # noqa: N818 - named for what happened
    """The database login cannot be trusted to reach every tenant's rows."""

    code = "cross_tenant_write_refused"


@asynccontextmanager
async def cross_tenant_read(session: AsyncSession, *, reason: str) -> AsyncIterator[AsyncSession]:
    """Open a read that spans tenants, or raise :class:`CrossTenantReadRefused`.

    ``session`` must not already be inside a transaction: the window is this
    seam's own, and ending it must not discard a caller's pending writes.
    """
    if session.in_transaction():
        raise RuntimeError("cross_tenant_read needs a session with no open transaction")
    try:
        await session.execute(text("SET LOCAL row_security = off"))
        if bound_org_ids(session):
            await swap_role(session, LOGIN_ROLE)
        row = (await session.execute(text(_WHO))).first()
        role = None if row is None else str(row[0])
        if row is None or not row[1]:
            log.error("db.cross_tenant_read.refused", reason=reason, role=role)
            raise CrossTenantReadRefused(
                f"role {role!r} neither is a superuser nor holds BYPASSRLS; a cross-tenant "
                "read under it would be silently filtered to nothing"
            )
        log.info("db.cross_tenant_read", reason=reason, role=role)
        try:
            yield session
        except DBAPIError as exc:
            if sqlstate_of(exc) == _INSUFFICIENT_PRIVILEGE:
                log.error("db.cross_tenant_read.refused", reason=reason, role=role)
                raise CrossTenantReadRefused(
                    "a row-level policy applies to this role; refusing a filtered answer"
                ) from exc
            raise
    finally:
        await session.rollback()


@asynccontextmanager
async def cross_tenant_write(session: AsyncSession, *, reason: str) -> AsyncIterator[AsyncSession]:
    """Run the block as the login, across tenants, inside the caller's unit of
    work; or raise :class:`CrossTenantWriteRefused` before it runs.

    For platform work inside a request that must reach every org's rows and
    stay in the request's transaction: a write, or a read whose answer the
    same unit of work acts on. On a session bound to its orgs the block steps
    out of the tenant role to the login (``SET LOCAL``) and the role that was
    in force is put back when it ends, so a window opened inside another (a
    machine-wide helper called from the platform admin surface) leaves the
    outer one standing.

    The window lasts as long as the block, not as long as the transaction it
    opened in: it is recorded on the session
    (:func:`alkera_core.db.tenant_session.open_window`), so a block that
    commits part way through begins its next transaction as the login too.
    When it ends in a transaction other than the one it opened in, that
    transaction is stamped from the session's binding again.

    The block's pending writes are flushed before the role is handed back:
    rows of another org left dirty would otherwise flush at commit under the
    tenant role, and be refused by its policy.

    Unlike :func:`cross_tenant_read` the block does not run with
    ``row_security = off``: it spans arbitrary unit-of-work code, which may
    itself enter the Files role, under which the Files policy must bind. The
    proof that the login bypasses row security is what makes its answers
    whole.
    """
    bound = bool(bound_org_ids(session))
    prior = await swap_role(session, LOGIN_ROLE) if bound else None
    opened_in = session.sync_session.get_transaction()
    row = (await session.execute(text(_WHO))).first()
    role = None if row is None else str(row[0])

    async def _hand_back() -> None:
        if prior is None or not session.in_transaction():
            return
        if session.sync_session.get_transaction() is opened_in:
            await restore_role(session, prior)
        elif not in_window(session):
            await apply_now(session)

    if row is None or not row[1]:
        log.error("db.cross_tenant_write.refused", reason=reason, role=role)
        await _hand_back()
        raise CrossTenantWriteRefused(
            f"role {role!r} neither is a superuser nor holds BYPASSRLS; a cross-tenant "
            "write under it would be silently filtered"
        )
    log.info("db.cross_tenant_write", reason=reason, role=role)
    if bound:
        open_window(session)
    try:
        yield session
        await session.flush()
    except BaseException as error:
        if bound:
            close_window(session)
        await restore_while_unwinding(error, _hand_back)
        raise
    if bound:
        close_window(session)
    await _hand_back()


__all__ = [
    "CrossTenantReadRefused",
    "CrossTenantWriteRefused",
    "cross_tenant_read",
    "cross_tenant_write",
]
