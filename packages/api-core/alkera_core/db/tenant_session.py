"""Stamp a session with the orgs its statements may see.

The content tables (``alkera_core.db.row_security.CONTENT_TABLES``) carry a
``FORCE``d row-level policy, ``tenant_isolation``, that binds the role
``alkera_tenant_app``: under it a statement sees and writes only rows whose org
is in ``alkera_org_ids()``, the comma-separated ``alkera.org_ids`` setting. The
login the backend connects as bypasses row security, so a session that never
assumes the role behaves exactly as before.

:func:`bind_tenant` records the orgs on a session. From then on every
transaction the session begins opens with the role assumed and the setting
stamped, both ``SET LOCAL`` (transaction-scoped: a pooled connection carries
neither into the next borrower's transaction). :func:`apply_now` stamps the
transaction already open, for a context that resolves part way through one.

When exactly one org is in scope, ``alkera.org_id`` (the Files policy's setting)
is stamped beside it, so a Files read inside the same request still binds.

Code that enters a narrower role (the Files role) for a few statements and
hands control back to its caller puts back the role that was in force
(:func:`swap_role` / :func:`restore_role`), never ``NONE``: on a bound session
that role is the tenant role (or, inside a cross-tenant window, the login), and
``SET ROLE NONE`` would quietly lift the policy for the rest of the request.
Code inside such a narrower role that needs a few statements outside it (a
table the Files role holds no grant on) steps out through :func:`stepped_out`,
which goes to :func:`outer_role` and puts the narrower role back afterwards.

A cross-tenant window (:func:`alkera_core.db.cross_tenant.cross_tenant_write`)
is recorded on the session too (:func:`open_window` / :func:`close_window`).
While one is open, every transaction the session begins is stamped with its
orgs but stepped out to the login, so a block that commits part way through
(an admin handler that commits before it reads back) keeps the window it was
written to run in rather than dropping into its caller's org.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
from typing import Any
from uuid import UUID

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from alkera_core.db.unwind import then_restore

#: The role every bound transaction assumes. Spelled again in revision 0188
#: and in :mod:`alkera_core.db.row_security`; a test pins that they agree.
TENANT_ROLE = "alkera_tenant_app"
#: The setting ``alkera_org_ids()`` reads.
ORG_IDS_SETTING = "alkera.org_ids"
#: The Files policy's single-org setting.
FILES_ORG_SETTING = "alkera.org_id"
#: Where a bound session keeps its orgs (``Session.info``).
INFO_KEY = "alkera_org_ids"
#: How many cross-tenant windows are open on a bound session (``Session.info``).
WINDOW_KEY = "alkera_cross_tenant_windows"
#: The role value that means "the login": no role assumed.
LOGIN_ROLE = "none"

#: One round trip: the role, the org list and the single-org setting together,
#: so no statement can ever run under the role without its orgs beside it.
_STAMP = (
    "SELECT set_config('role', :role, true), "
    "set_config(:ids_name, :ids, true), "
    "set_config(:one_name, :one, true)"
)


class TenantBindingError(RuntimeError):
    """A session was asked to bind to no org, or rebound to different ones."""


def bind_tenant(session: AsyncSession, org_ids: Sequence[UUID]) -> None:
    """Record ``org_ids`` as the only orgs ``session`` may see from its next
    transaction on. Call :func:`apply_now` as well when a transaction is open.

    Binding the same orgs again is a no-op; binding different ones is refused,
    because a session whose scope moved mid-request is a bug, never a feature.
    """
    ids = tuple(dict.fromkeys(UUID(str(org)) for org in org_ids))
    if not ids:
        raise TenantBindingError("a tenant binding needs at least one org")
    current = session.info.get(INFO_KEY)
    if current is not None and frozenset(current) != frozenset(ids):
        raise TenantBindingError("this session is already bound to other orgs")
    session.info[INFO_KEY] = ids


def bound_org_ids(session: AsyncSession | Session) -> tuple[UUID, ...] | None:
    """The orgs ``session`` is bound to, or ``None`` when it is not bound."""
    ids = session.info.get(INFO_KEY)
    return None if ids is None else tuple(ids)


def in_window(session: AsyncSession | Session) -> bool:
    """Whether a cross-tenant window is open on ``session``."""
    return bool(session.info.get(WINDOW_KEY, 0))


def open_window(session: AsyncSession) -> None:
    """Record that a cross-tenant window opened on ``session``: until the
    matching :func:`close_window`, every transaction it begins starts as the
    login. Windows nest; only the outermost close ends the effect."""
    session.info[WINDOW_KEY] = session.info.get(WINDOW_KEY, 0) + 1


def close_window(session: AsyncSession) -> None:
    """Record that the innermost cross-tenant window on ``session`` closed."""
    depth = session.info.get(WINDOW_KEY, 0) - 1
    if depth > 0:
        session.info[WINDOW_KEY] = depth
    else:
        session.info.pop(WINDOW_KEY, None)


def outer_role(session: AsyncSession | Session) -> str:
    """The role a narrower role steps out to on ``session``: the tenant role
    on a bound session, the login on one nobody bound (a sweep) and inside a
    cross-tenant window, which runs as the login by design."""
    if bound_org_ids(session) and not in_window(session):
        return TENANT_ROLE
    return LOGIN_ROLE


def _stamp_params(session: AsyncSession | Session, ids: Sequence[UUID]) -> dict[str, str]:
    return {
        "role": TENANT_ROLE if not in_window(session) else LOGIN_ROLE,
        "ids_name": ORG_IDS_SETTING,
        "ids": ",".join(str(org) for org in ids),
        "one_name": FILES_ORG_SETTING,
        "one": str(ids[0]) if len(ids) == 1 else "",
    }


@event.listens_for(Session, "after_begin")
def _stamp_on_begin(session: Session, transaction: Any, connection: Any) -> None:
    # A SAVEPOINT is not a new transaction: its statements already run under
    # whatever the enclosing one stamped (or a window it opened since), and
    # stamping again would quietly close such a window.
    if transaction.nested:
        return
    ids = session.info.get(INFO_KEY)
    if ids:
        connection.execute(text(_STAMP), _stamp_params(session, ids))


async def apply_now(session: AsyncSession) -> None:
    """Stamp the transaction ``session`` already has open. A session with no
    open transaction needs nothing: the next one it begins is stamped."""
    ids = session.info.get(INFO_KEY)
    if ids and session.in_transaction():
        await session.execute(text(_STAMP), _stamp_params(session, ids))


#: Read the role in force and assume another in one statement; the CTE is
#: materialized, so the read happens before the change.
_SWAP = (
    "WITH prior AS MATERIALIZED (SELECT current_setting('role') AS role) "
    "SELECT role, set_config('role', :role, true) FROM prior"
)


async def swap_role(session: AsyncSession, role: str) -> str:
    """Assume ``role`` for the rest of the transaction (``SET LOCAL``) and
    answer the role that was in force, for :func:`restore_role`."""
    prior = (await session.execute(text(_SWAP), {"role": role})).one()[0]
    return str(prior)


async def restore_role(session: AsyncSession, role: str) -> None:
    """Put back a role :func:`swap_role` answered."""
    await session.execute(text("SELECT set_config('role', :role, true)"), {"role": role})


@asynccontextmanager
async def stepped_out(session: AsyncSession) -> AsyncIterator[None]:
    """Run the block under :func:`outer_role` and put back the role that was
    in force when it ends.

    For code holding a narrower role (the Files role) that needs a few
    statements on tables that role holds no grant on. On a bound request the
    block runs as the tenant role, so the content tables' policy still binds
    it; the login only where the session already ran as the login. The role
    is put back without letting the restore replace an error the block raised
    (:func:`alkera_core.db.unwind.then_restore`).
    """
    prior = await swap_role(session, outer_role(session))
    async with then_restore(lambda: restore_role(session, prior)):
        yield


@contextmanager
def stepped_out_at_commit(session: Session) -> Iterator[None]:
    """:func:`stepped_out` for a hook that runs as the transaction commits (a
    ``before_commit`` listener), where only the synchronous session is in
    hand: the commit may come while a narrower role is still in force. The
    role is put back after a block that did not raise; one that raised fails
    the commit, and the role ends with the transaction."""
    prior = str(session.execute(text(_SWAP), {"role": outer_role(session)}).one()[0])
    yield
    session.execute(text("SELECT set_config('role', :role, true)"), {"role": prior})


__all__ = [
    "FILES_ORG_SETTING",
    "INFO_KEY",
    "LOGIN_ROLE",
    "ORG_IDS_SETTING",
    "TENANT_ROLE",
    "WINDOW_KEY",
    "TenantBindingError",
    "apply_now",
    "bind_tenant",
    "bound_org_ids",
    "close_window",
    "in_window",
    "open_window",
    "outer_role",
    "restore_role",
    "stepped_out",
    "stepped_out_at_commit",
    "swap_role",
]
