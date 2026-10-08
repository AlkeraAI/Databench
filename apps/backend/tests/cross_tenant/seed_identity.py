"""Identity seeds for the cross-tenant matrix.

Per org: the two-org person's own registered CLI token for that org's
membership (the world's ``token_a`` / ``token_b``), named by its ``jti``. It is
the person's, so ownership alone does not keep it from org B's credential: the
sessions list from B must not show A's token, and revoking it from B must find
nothing.

Per org, too: an SSO link request parked for the two-org person by that org's
IdP. The cookie naming it is held by no client the matrix drives, so every
link route from B's credential must find nothing and consume nothing.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from alkera_core.auth import decode_session_token
from alkera_core.auth.sign_in_policy import sso_provider_key
from alkera_core.models import SsoLinkRequest
from backend.services.identity.sso_link import token_hash

if TYPE_CHECKING:
    from route_matrix import Side, TwoOrgWorld
    from sqlalchemy.ext.asyncio import AsyncSession


async def seed(session: AsyncSession, world: TwoOrgWorld, side: Side) -> dict[str, str]:
    jti = decode_session_token(side.token).jti
    assert jti is not None
    parked = SsoLinkRequest(
        token_hash=token_hash(secrets.token_urlsafe(32)),
        org_team_id=side.org_id,
        provider=sso_provider_key(side.org_id),
        subject=f"sub-{secrets.token_hex(8)}",
        email=side.member.email,
        email_verified=True,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    session.add(parked)
    await session.flush()
    return {"session": jti, "sso_link_request": str(parked.id)}
