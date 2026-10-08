"""Proxy tokens and extra routes reach the gateway only through its extension points.

The open gateway accepts no proxy token and serves no proxy heartbeat: both
belong to whoever bills for a hosted gateway and are registered at composition.
Each case swaps in fresh points, so the product's registrations (which the
composed suite installs) are not on them.
"""

from __future__ import annotations

import pytest
from alkera_core.extensions import ExtensionError, ExtensionPoint
from alkera_core.models import ProxyToken, User
from fastapi import APIRouter, FastAPI, HTTPException
from model_gateway import app_factory, auth, extension_points
from model_gateway.extension_points import ProxyIdentity
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture
def identities(monkeypatch: pytest.MonkeyPatch) -> ExtensionPoint[ProxyIdentity]:
    fresh: ExtensionPoint[ProxyIdentity] = ExtensionPoint("gateway_proxy_identity")
    monkeypatch.setattr(extension_points, "PROXY_IDENTITIES", fresh)
    return fresh


@pytest.fixture
def routers(monkeypatch: pytest.MonkeyPatch) -> ExtensionPoint[APIRouter]:
    fresh: ExtensionPoint[APIRouter] = ExtensionPoint("gateway_routers")
    monkeypatch.setattr(app_factory, "GATEWAY_ROUTERS", fresh)
    return fresh


async def _identity(db: AsyncSession, token: ProxyToken) -> User:
    raise AssertionError("not called")


@pytest.mark.asyncio
async def test_with_no_identity_registered_a_proxy_token_is_refused(
    identities: ExtensionPoint[ProxyIdentity],
) -> None:
    with pytest.raises(HTTPException) as refused:
        await auth._authenticate_proxy("alk_proxy_anything")
    assert refused.value.status_code == 401


def test_one_registered_identity_is_the_resolver(
    identities: ExtensionPoint[ProxyIdentity],
) -> None:
    identities.register(_identity)
    assert extension_points.proxy_identity() is _identity


def test_two_registered_identities_are_refused(
    identities: ExtensionPoint[ProxyIdentity],
) -> None:
    async def another(db: AsyncSession, token: ProxyToken) -> User:
        raise AssertionError("not called")

    identities.register(_identity)
    identities.register(another)
    with pytest.raises(ExtensionError, match="more than one proxy identity"):
        extension_points.proxy_identity()


def _paths(application: FastAPI) -> set[str]:
    return {getattr(route, "path", "") for route in application.routes}


def test_the_gateway_serves_a_registered_router_and_no_heartbeat_without_one(
    routers: ExtensionPoint[APIRouter], monkeypatch: pytest.MonkeyPatch
) -> None:
    bare = _paths(app_factory.create_app())
    assert "/proxy/heartbeat" not in bare
    assert "/v1/models" in bare

    extra = APIRouter()

    @extra.get("/extension/ping")
    async def _ping() -> dict[str, bool]:
        return {"ok": True}

    with_one: ExtensionPoint[APIRouter] = ExtensionPoint("gateway_routers")
    with_one.register(extra)
    monkeypatch.setattr(app_factory, "GATEWAY_ROUTERS", with_one)
    assert _paths(app_factory.create_app()) - bare == {"/extension/ping"}
