"""Gateway readiness endpoint + startup provider-config validation."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from alkera_core.config import settings
from alkera_core.db import session as db_session
from alkera_core.llm_provider import Provider
from alkera_core.model_catalog import ModelTier
from alkera_core.model_catalog_defaults import DefaultModel, add_model
from alkera_core.models.model_catalog import Model, ModelRoute
from alkera_core.readiness import probe_latch
from httpx import AsyncClient
from model_gateway.app_factory import _assert_provider_config
from model_gateway.routability import NO_ROUTABLE_MODEL
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine


async def test_health_live_ok(gateway_client: AsyncClient) -> None:
    resp = await gateway_client.get("/health/live")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_health_ready_ok(gateway_client: AsyncClient) -> None:
    # The test Postgres is up, so readiness is 200 with db ok.
    resp = await gateway_client.get("/health/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"


@pytest.mark.parametrize(
    ("deadline", "expected"),
    [
        pytest.param(None, 200, id="default-outlasts-a-slow-answer"),
        pytest.param(0.2, 503, id="a-tightened-deadline-is-obeyed"),
    ],
)
async def test_health_ready_gives_the_database_the_configured_deadline(
    gateway_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    deadline: float | None,
    expected: int,
) -> None:
    """The gateway's readiness deadline is the backend's, off one setting.

    It used to be two seconds compiled in on both sides, so a Postgres that was
    slow rather than gone made every gateway task report itself unready and the
    balancer drained them all at once. The probe runs a real query, so the two
    cases here are the real wait: past the old two seconds it still answers ok,
    and a deployment that tightens the setting gets the refusal it asked for.
    """
    original_execute = AsyncSession.execute

    async def _slow(self: AsyncSession, *args: Any, **kwargs: Any) -> Any:
        return await original_execute(self, text("SELECT pg_sleep(2.5)"))

    monkeypatch.setattr(AsyncSession, "execute", _slow)
    if deadline is not None:
        monkeypatch.setattr(settings, "health_ready_timeout_seconds", deadline)

    resp = await gateway_client.get("/health/ready")

    assert resp.status_code == expected


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


async def test_a_database_stall_after_a_ready_probe_keeps_the_gateway_in_rotation(
    gateway_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Sep 29 2026 staging's gateway task failed readiness with the backend's
    in the same minute — one database, every task — and ECS stopped it. A task
    that has been ready keeps answering 200 (body ``degraded``) inside the grace
    window, and is refused once the failure has outlasted it."""
    clock = _Clock()
    monkeypatch.setattr(probe_latch, "clock", clock)
    monkeypatch.setattr(settings, "health_ready_grace_seconds", 900.0)
    assert (await gateway_client.get("/health/ready")).status_code == 200

    async def _stalled(self: AsyncSession, *args: Any, **kwargs: Any) -> Any:
        raise TimeoutError("the database did not answer inside the deadline")

    monkeypatch.setattr(AsyncSession, "execute", _stalled)

    clock.now += 899.0
    graced = await gateway_client.get("/health/ready")
    assert graced.status_code == 200
    assert graced.json()["status"] == "degraded"

    clock.now += 1.0
    assert (await gateway_client.get("/health/ready")).status_code == 503


async def test_a_gateway_that_was_never_ready_is_refused_at_once(
    gateway_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new revision that cannot reach its database fails its first checks, so
    the deployment circuit breaker can roll it back."""
    monkeypatch.setattr(settings, "health_ready_grace_seconds", 900.0)

    async def _stalled(self: AsyncSession, *args: Any, **kwargs: Any) -> Any:
        raise TimeoutError("the database did not answer inside the deadline")

    monkeypatch.setattr(AsyncSession, "execute", _stalled)
    assert (await gateway_client.get("/health/ready")).status_code == 503


async def test_health_ready_answers_while_the_request_pool_is_drained(
    gateway_client: AsyncClient,
) -> None:
    """The gateway meters on the tenants' pool and probes on one of its own.

    A probe that queued behind the requests already stuck made a busy task read
    as a dead one: the balancer pulled it, and its traffic saturated the task
    next in line. The tenants' factory here is pointed at a pool of one
    connection and that connection is held, which is what a drained pool is.
    """
    drained = create_async_engine(
        settings.database_url, pool_size=1, max_overflow=0, pool_timeout=0.3
    )
    db_session.AsyncSessionLocal.configure(bind=drained)
    try:
        async with drained.connect():
            resp = await asyncio.wait_for(gateway_client.get("/health/ready"), timeout=15.0)
    finally:
        db_session.AsyncSessionLocal.configure(bind=db_session.engine)
        await drained.dispose()

    assert resp.status_code == 200
    assert resp.json()["db"] == "ok"


def test_provider_config_skipped_when_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "app_env", "local")
    _assert_provider_config()  # local never requires provider creds


def test_provider_config_requires_a_provider_in_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "anthropic_api_key", None)
    monkeypatch.setattr(settings, "openai_api_key", None)
    monkeypatch.setattr(settings, "aws_bearer_token_bedrock", None)
    monkeypatch.setattr(settings, "aws_access_key_id", None)
    monkeypatch.setattr(settings, "gateway_assume_bedrock_iam", False)
    with pytest.raises(RuntimeError, match="no provider credentials"):
        _assert_provider_config()


def test_provider_config_ok_with_one_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-test")
    monkeypatch.setattr(settings, "openai_api_key", None)
    _assert_provider_config()  # no raise — one provider is enough


def test_provider_config_bedrock_iam_optout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "anthropic_api_key", None)
    monkeypatch.setattr(settings, "openai_api_key", None)
    monkeypatch.setattr(settings, "aws_bearer_token_bedrock", None)
    monkeypatch.setattr(settings, "aws_access_key_id", None)
    monkeypatch.setattr(settings, "gateway_assume_bedrock_iam", True)
    _assert_provider_config()  # no raise — relying on a Bedrock IAM role


# --- BYOK boot decision table ---------------------------------------------------


def _byok_entitled(monkeypatch: pytest.MonkeyPatch) -> None:
    """A production-shaped BYOK install: patch the shipped verification constant
    (standing in for the real key-ceremony value — the env override is ignored in
    production by design) and mint a valid grant."""
    from datetime import UTC, datetime, timedelta

    from alkera_core import entitlements as ent

    priv, pub = ent.generate_keypair()
    token = ent.mint_entitlement_token(
        customer="acme-corp",
        features=ent.Feature.BYOK,
        expires_on=(datetime.now(UTC) + timedelta(days=30)).date(),
        serial=1,
        signing_key_b64=priv,
    )
    monkeypatch.setitem(ent._PUBLIC_KEYS, "alk1", pub)
    monkeypatch.setattr(settings, "alkera_entitlements", token)
    monkeypatch.setattr(settings, "alkera_entitlements_public_key", None)
    monkeypatch.setattr(settings, "self_hosted", True)
    ent.get_entitlements.cache_clear()


def _no_env_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "anthropic_api_key", None)
    monkeypatch.setattr(settings, "openai_api_key", None)
    monkeypatch.setattr(settings, "aws_bearer_token_bedrock", None)
    monkeypatch.setattr(settings, "aws_access_key_id", None)
    monkeypatch.setattr(settings, "gateway_assume_bedrock_iam", False)


def test_provider_config_byok_boots_with_no_env_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """BYOK credentials live per-org in the DB — unknowable at boot, so an
    entitled direct-mode install with zero env keys must boot."""
    from alkera_core import entitlements as ent

    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "gateway_upstream", "direct")
    _no_env_keys(monkeypatch)
    _byok_entitled(monkeypatch)
    try:
        _assert_provider_config()  # no raise
    finally:
        ent.get_entitlements.cache_clear()


def test_provider_config_unentitled_still_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dev-key token is worthless against the production constant: without a
    genuine entitlement the keyless boot refusal stands."""
    from alkera_core import entitlements as ent

    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "gateway_upstream", "direct")
    monkeypatch.setattr(settings, "self_hosted", True)
    monkeypatch.setattr(settings, "alkera_entitlements", "alk1.someones.token")
    _no_env_keys(monkeypatch)
    ent.get_entitlements.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="no provider credentials"):
            _assert_provider_config()
    finally:
        ent.get_entitlements.cache_clear()


def test_provider_config_byok_dormant_under_proxy_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Entitlement + proxy upstream boots (billed-through-Alkera keeps working)
    but warns that the entitlement is dormant."""
    import model_gateway.app_factory as gw_main
    from alkera_core import entitlements as ent

    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "gateway_upstream", "proxy")
    monkeypatch.setattr(settings, "alkera_proxy_token", "alk_proxy_x")
    _byok_entitled(monkeypatch)

    events: list[tuple[str, str]] = []

    class _Rec:
        def info(self, event: str, **kw: Any) -> None:
            events.append(("info", event))

        def warning(self, event: str, **kw: Any) -> None:
            events.append(("warning", event))

    monkeypatch.setattr(gw_main, "log", _Rec())
    try:
        _assert_provider_config()  # no raise
    finally:
        ent.get_entitlements.cache_clear()
    assert ("warning", "gateway.byok.entitled_but_proxy_upstream") in events


def _no_instance_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("anthropic_api_key", "openai_api_key", "aws_bearer_token_bedrock"):
        monkeypatch.setattr(settings, name, None)
    monkeypatch.setattr(settings, "aws_access_key_id", None)
    monkeypatch.setattr(settings, "gateway_assume_bedrock_iam", False)
    monkeypatch.setattr(settings, "gateway_upstream", "direct")


async def test_health_ready_warns_when_no_model_can_be_routed(
    gateway_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gateway holding no provider key routes nothing; it stays ready and
    says why no chat can start."""
    _no_instance_keys(monkeypatch)

    resp = await gateway_client.get("/health/ready")

    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert resp.json()["detail"] == NO_ROUTABLE_MODEL


async def test_health_ready_is_quiet_once_a_keyed_provider_routes_a_model(
    gateway_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_instance_keys(monkeypatch)
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-test")
    slug = f"probe-routable-{uuid.uuid4().hex[:8]}"
    async with db_session.AsyncSessionLocal() as db:
        await add_model(
            db,
            DefaultModel(
                slug=slug,
                display_name="Probe",
                family="claude",
                provider=Provider.ANTHROPIC,
                upstream_model_id=slug,
                tier=ModelTier.CHEAP,
                efforts=[],
                default_effort=None,
                thinking_mode=None,
                supports_thinking=False,
            ),
        )
        await db.commit()
    try:
        resp = await gateway_client.get("/health/ready")
    finally:
        async with db_session.AsyncSessionLocal() as db:
            await db.execute(delete(ModelRoute).where(ModelRoute.model_id == slug))
            await db.execute(delete(Model).where(Model.id == slug))
            await db.commit()

    assert resp.status_code == 200
    assert resp.json()["detail"] is None


async def test_gateway_ready_strict_is_503_inside_the_grace_window(
    gateway_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``?strict=1`` on the gateway drops the grace the load balancer gets."""
    clock = _Clock()
    monkeypatch.setattr(probe_latch, "clock", clock)
    monkeypatch.setattr(settings, "health_ready_grace_seconds", 900.0)
    assert (await gateway_client.get("/health/ready")).status_code == 200

    async def _stalled(self: AsyncSession, *args: Any, **kwargs: Any) -> Any:
        raise TimeoutError("the database did not answer inside the deadline")

    monkeypatch.setattr(AsyncSession, "execute", _stalled)
    clock.now += 1.0

    assert (await gateway_client.get("/health/ready")).status_code == 200
    strict = await gateway_client.get("/health/ready?strict=1")
    assert strict.status_code == 503
    assert strict.json()["status"] == "degraded"
