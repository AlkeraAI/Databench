"""Where a web chat is placed: the container sandbox first, the org's box after.

A browser chat runs on the platform's container service and on nothing else, so
``resolve_machine_for`` prefers the org's live machine of
``settings.compute_web_chat_provider`` (``container``) and falls back to the
org's other live workspace machine. Both branches matter:

* the fallback is what EVERY deployment takes today — nothing provisions a
  container yet — so a regression there strands every chat on every org;
* the preference is the rule the container service is being built under, and it
  has to beat the "most recently heartbeated wins" ordering it sits on top of,
  or a busier RunPod box would quietly keep taking the chats.

The org isolation the old rule had must survive the new one, so a container
machine in another org is checked too: it is the exact shape ("prefer this kind
anywhere") that a careless join would leak across tenants.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from alkera_core.authz import ActingContext
from alkera_core.compute.provider import CONTAINER, RUNPOD
from alkera_core.config import settings
from alkera_core.models.compute import ComputeAllocation, ComputeMachineType
from backend.services.compute.placement import resolve_machine_for
from backend.services.org import teams as team_service
from sqlalchemy.ext.asyncio import AsyncSession
from tests._compute_helpers import make_machine_type
from tests.conftest import OrgWithAdmin, _unique_email, _unique_org_name

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows]

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)


async def _machine(
    session: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
    provider: str,
    name: str,
    heartbeat: datetime,
    mt: ComputeMachineType | None = None,
) -> ComputeAllocation:
    """A live workspace machine of ``provider``, last heard from at ``heartbeat``."""
    machine_type = mt or await make_machine_type(session, provider=provider)
    alloc = ComputeAllocation(
        user_id=user_id,
        org_team_id=org_id,
        machine_type_id=machine_type.id,
        lifecycle="workspace",
        name=name,
        state="ready",
        provider_machine_id=f"pod-{name}",
        last_heartbeat_at=heartbeat,
    )
    session.add(alloc)
    await session.commit()
    return alloc


def _ctx(org: OrgWithAdmin) -> ActingContext:
    return ActingContext.for_user(user_id=org.admin_id, org_id=org.org_id, email=org.admin_email)


async def _resolve(session: AsyncSession, org: OrgWithAdmin) -> str | None:
    binding = await resolve_machine_for(
        session, ctx=_ctx(org), org_team_id=org.org_id, purpose="chat"
    )
    return None if binding is None else binding.name


async def test_with_no_container_machine_the_orgs_workspace_box_still_serves_the_chat(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The path every deployment is on today: the preference finds nothing and
    placement answers exactly what it answered before."""
    await _machine(
        real_session,
        org_id=org_admin.org_id,
        user_id=org_admin.admin_id,
        provider=RUNPOD,
        name="the-box",
        heartbeat=NOW,
    )

    assert await _resolve(real_session, org_admin) == "the-box"


async def test_an_org_with_no_machine_at_all_still_resolves_to_nothing(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    assert await _resolve(real_session, org_admin) is None


async def test_a_live_container_machine_is_preferred_over_the_orgs_box(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """And it wins even though the RunPod box heartbeated more recently — the
    ordering the fallback uses must not decide this."""
    await _machine(
        real_session,
        org_id=org_admin.org_id,
        user_id=org_admin.admin_id,
        provider=CONTAINER,
        name="sandbox",
        heartbeat=NOW - timedelta(minutes=5),
    )
    await _machine(
        real_session,
        org_id=org_admin.org_id,
        user_id=org_admin.admin_id,
        provider=RUNPOD,
        name="the-box",
        heartbeat=NOW,
    )

    assert await _resolve(real_session, org_admin) == "sandbox"


async def test_a_released_container_machine_does_not_win_the_placement(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The preference narrows the LIVE set; it does not widen it. A dead
    sandbox must not beat a live box, or the chat binds to nothing."""
    dead = await _machine(
        real_session,
        org_id=org_admin.org_id,
        user_id=org_admin.admin_id,
        provider=CONTAINER,
        name="sandbox",
        heartbeat=NOW,
    )
    dead.state = "released"
    await real_session.commit()
    await _machine(
        real_session,
        org_id=org_admin.org_id,
        user_id=org_admin.admin_id,
        provider=RUNPOD,
        name="the-box",
        heartbeat=NOW - timedelta(minutes=5),
    )

    assert await _resolve(real_session, org_admin) == "the-box"


async def test_the_preferred_provider_comes_from_settings_not_a_constant(
    real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deployment that runs its chats on the org's own box says so, and the
    container machine stops winning."""
    await _machine(
        real_session,
        org_id=org_admin.org_id,
        user_id=org_admin.admin_id,
        provider=CONTAINER,
        name="sandbox",
        heartbeat=NOW,
    )
    await _machine(
        real_session,
        org_id=org_admin.org_id,
        user_id=org_admin.admin_id,
        provider=RUNPOD,
        name="the-box",
        heartbeat=NOW - timedelta(minutes=5),
    )
    assert await _resolve(real_session, org_admin) == "sandbox"

    monkeypatch.setattr(settings, "compute_web_chat_provider", RUNPOD)

    assert await _resolve(real_session, org_admin) == "the-box"


async def test_the_default_preference_is_the_container_service() -> None:
    assert settings.compute_web_chat_provider == CONTAINER


async def test_another_orgs_container_machine_is_never_bound(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    other_org, other_admin = await team_service.create_org_with_admin(
        real_session,
        org_name=_unique_org_name(),
        admin_email=_unique_email("neighbour"),
        admin_first_name="N",
        admin_last_name="B",
        admin_password="pw-1234567890",
    )
    await real_session.commit()
    await _machine(
        real_session,
        org_id=other_org.id,
        user_id=other_admin.id,
        provider=CONTAINER,
        name="their-sandbox",
        heartbeat=NOW,
    )
    await _machine(
        real_session,
        org_id=org_admin.org_id,
        user_id=org_admin.admin_id,
        provider=RUNPOD,
        name="our-box",
        heartbeat=NOW - timedelta(minutes=5),
    )

    assert await _resolve(real_session, org_admin) == "our-box"
