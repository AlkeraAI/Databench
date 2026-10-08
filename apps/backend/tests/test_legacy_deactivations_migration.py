"""Revision 0186 never enables an identity the previous release disabled.

Before this release an org offboarded a person by setting ``users.is_active``
to false, and the previous release reads nothing else when it decides whether
someone may sign in. Its tasks keep serving while the services roll, and for
good after an app-only rollback, so an identity this revision enabled would let
a person their org offboarded sign in again. The revision moves the org's
decision onto the home membership and leaves the identity exactly as it was;
the org lifts it when it reactivates the membership.

Seeded at 0185 with the writes the previous release makes, upgraded, and run a
second time over its own work.
"""

from __future__ import annotations

import secrets
import uuid
from typing import Any

from alembic import command
from sqlalchemy import text
from tests.migration_harness import ScratchDatabase, migration_scratch

_PARENT = "0185"
_REVISION = "0186"


async def _person(scratch: ScratchDatabase, org: uuid.UUID, *, disabled: bool) -> uuid.UUID:
    """A member written as the previous release writes one; ``disabled`` is the
    org's deactivation, which the 0185 mirror carried onto the home membership."""
    user_id = uuid.uuid4()
    async with scratch.session() as session:
        await session.execute(
            text("INSERT INTO users (id, org_team_id, email) VALUES (:id, :org, :email)"),
            {"id": user_id, "org": org, "email": f"legacy-{secrets.token_hex(5)}@alkera.dev"},
        )
        await session.commit()
        if disabled:
            await session.execute(
                text("UPDATE users SET is_active = false WHERE id = :id"), {"id": user_id}
            )
            await session.commit()
    return user_id


async def _disabled_identities(scratch: ScratchDatabase) -> set[uuid.UUID]:
    async with scratch.session() as session:
        rows = await session.execute(text("SELECT id FROM users WHERE NOT is_active"))
        return {row[0] for row in rows}


async def _home(scratch: ScratchDatabase, user_id: uuid.UUID) -> dict[str, Any]:
    async with scratch.session() as session:
        row = (
            await session.execute(
                text(
                    "SELECT u.is_active, om.status FROM users u JOIN org_memberships om "
                    "ON om.user_id = u.id AND om.org_team_id = u.org_team_id WHERE u.id = :u"
                ),
                {"u": user_id},
            )
        ).one()
        return {"is_active": row[0], "membership": row[1]}


async def test_the_revision_never_enables_an_identity_the_previous_release_disabled() -> None:
    async with migration_scratch() as scratch:
        await scratch.downgrade(_PARENT)
        async with scratch.session() as session:
            org = await session.scalar(
                text(
                    "INSERT INTO teams (id, name, is_root) VALUES (gen_random_uuid(), :n, true) "
                    "RETURNING id"
                ),
                {"n": f"legacy-{secrets.token_hex(4)}"},
            )
            await session.commit()
        assert isinstance(org, uuid.UUID)
        offboarded = await _person(scratch, org, disabled=True)
        staying = await _person(scratch, org, disabled=False)
        disabled_before = await _disabled_identities(scratch)
        assert offboarded in disabled_before

        await scratch.upgrade(_REVISION)

        assert await _disabled_identities(scratch) == disabled_before
        assert await _home(scratch, offboarded) == {
            "is_active": False,
            "membership": "deactivated",
        }
        assert await _home(scratch, staying) == {"is_active": True, "membership": "active"}

        # Run again over its own work, as the repair for a database stamped
        # under an earlier numbering does: still nobody enabled.
        command.stamp(scratch.config, _PARENT)
        await scratch.upgrade(_REVISION)
        assert await _disabled_identities(scratch) == disabled_before
