"""How long a chat creation waits on the gateway's model catalog.

A chat is never created without a model, so this wait is on the critical path of
the create itself: past it the person is told the model catalog is unavailable
and no chat exists. How long "too long" is depends on where the gateway sits
relative to the backend — same pod, same region, or an ocean away — which is a
property of the deployment and not of Alkera, so it is a setting rather than a
figure compiled into a release.

The cases drive a real socket that accepts a connection and then never answers,
which is the shape of a gateway that is up and wedged: the wait is what ends it,
so what a case measures is the wait actually in force.
"""

from __future__ import annotations

import socket
import time
from collections.abc import Iterator

import pytest
from alkera_core.config import settings
from alkera_core.http import async_client
from alkera_core.models import User
from backend.services.chats import catalog as chat_catalog
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin

pytestmark = pytest.mark.asyncio


@pytest.fixture
def wedged_gateway() -> Iterator[str]:
    """A listener that completes the handshake into its backlog and then never
    reads or answers. A client connects fine and hangs on the response, so only
    the read timeout ends the call."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    finally:
        listener.close()


@pytest.fixture
def real_outbound(monkeypatch: pytest.MonkeyPatch) -> None:
    """Undo the suite-wide catalog stub. Every other backend test wants a
    scripted catalog; these want the client the service builds for itself,
    because the timeout it is built with is the whole subject."""
    monkeypatch.setattr(chat_catalog, "async_client", async_client)


@pytest.fixture
async def caller(real_session: AsyncSession, org_admin: OrgWithAdmin) -> tuple[AsyncSession, User]:
    """A member the hop token can be minted for (the hop names their membership
    in the org); the catalog call is the only thing under test."""
    user = await real_session.get(User, org_admin.admin_id)
    assert user is not None
    return real_session, user


async def test_a_wedged_gateway_is_given_up_on_at_the_configured_wait(
    monkeypatch: pytest.MonkeyPatch,
    wedged_gateway: str,
    real_outbound: None,
    caller: tuple[AsyncSession, User],
) -> None:
    """Shipped, the wait is ten seconds; configured down it is a twentieth of a
    second. A create that still held the old constant would sit on this socket
    for the whole ten, which is the difference this asserts — and the reason the
    figure moved: a deployment whose gateway is a region away raises it, and one
    that would rather fail the create fast lowers it."""
    monkeypatch.setattr(settings, "chat_model_catalog_timeout_seconds", 0.05)

    started = time.monotonic()
    with pytest.raises(chat_catalog.CatalogUnavailableError) as excinfo:
        session, user = caller
        await chat_catalog.fetch_catalog(
            session, user, user.home_org_team_id, base_url=wedged_gateway
        )
    waited = time.monotonic() - started

    assert "could not be reached" in str(excinfo.value)
    assert waited < 5.0, (
        f"waited {waited:.1f}s on a wedged gateway: the configured 0.05s was not the wait in force"
    )


async def test_an_injected_client_still_owns_its_own_wait(
    monkeypatch: pytest.MonkeyPatch,
    wedged_gateway: str,
    real_outbound: None,
    caller: tuple[AsyncSession, User],
) -> None:
    """The setting decides the client the SERVICE builds, never one a caller
    hands in. A test rig and the money path both pass their own client, and a
    deployment tightening its catalog wait must not reach into those."""
    monkeypatch.setattr(settings, "chat_model_catalog_timeout_seconds", 600.0)

    client = async_client(timeout=0.05)
    started = time.monotonic()
    try:
        with pytest.raises(chat_catalog.CatalogUnavailableError):
            session, user = caller
            await chat_catalog.fetch_catalog(
                session, user, user.home_org_team_id, client=client, base_url=wedged_gateway
            )
    finally:
        await client.aclose()

    assert time.monotonic() - started < 5.0, "the injected client's own timeout was overridden"
