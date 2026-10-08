"""First-admin bootstrap for a fresh (production) deployment.

Unlike the dev seed (`seeds/dev.py`), this is meant to run in `production`, on
demand, exactly once — there is no other way for a self-hosted operator to get
their first `ALKERA_ADMIN` without hand-editing the database.

Safety invariant: it is a hard no-op if ANY user already exists. A populated
database is treated as "already initialized", so re-running can never mint a
second backdoor admin (and an attacker who can trigger the command can't elevate
an existing victim by pointing it at their email — the command simply refuses).

Not registered in `seeds/__init__.py::SEEDS` — those run via `make seed` and are
gated to local/staging. This one runs through `scripts.bootstrap_admin`.
"""

from __future__ import annotations

from alkera_core.models import PlatformRole, User
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.identity import email_verification as email_verification_service
from backend.services.org import membership_in
from backend.services.org import teams as team_service


async def _existing_user_count(session: AsyncSession) -> int:
    """How many users exist — the "is this instance already initialized?" probe.
    Factored out so the create / skip branches are deterministically testable
    against a shared (never-empty) test database."""
    return await session.scalar(select(func.count()).select_from(User)) or 0


async def bootstrap_first_admin(
    session: AsyncSession,
    *,
    org_name: str,
    admin_email: str,
    admin_first_name: str,
    admin_last_name: str,
    admin_password: str,
) -> str:
    """Create the first org + `ALKERA_ADMIN` user, or no-op if already initialized.

    Returns a one-line summary (`"created: …"` or `"skipped: …"`). Does NOT commit
    — the caller owns the transaction (mirrors the seed convention).
    """
    user_count = await _existing_user_count(session)
    if user_count:
        return (
            f"skipped: instance already initialized ({user_count} user(s) exist); "
            "refusing to create a bootstrap admin"
        )

    org, admin = await team_service.create_org_with_admin(
        session,
        org_name=org_name,
        admin_email=admin_email,
        admin_first_name=admin_first_name,
        admin_last_name=admin_last_name,
        admin_password=admin_password,
        admin_platform_role=PlatformRole.ALKERA_ADMIN,
    )
    # The first admin has no inbox round-trip available; mark verified so they are
    # not gated by the email-verification wall on first login.
    await email_verification_service.mark_verified(session, admin)
    # Break-glass: the first admin can always password-login even if the org later
    # enforces SSO — a misconfigured or unreachable IdP must never lock everyone out.
    # The flag is the org's, on the admin's membership in it.
    membership = await membership_in(session, user_id=admin.id, org_team_id=org.id)
    if membership is None:  # pragma: no cover - the users trigger writes it with the row
        raise RuntimeError("the bootstrap admin was created without their membership")
    membership.sso_exempt = True
    await session.flush()
    return (
        f"created: org={org.name!r} (id={org.id}); admin={admin.email} (platform_role=alkera_admin)"
    )
