"""Org-admin BYOK provider credentials: entitlement invisibility, RBAC,
write-only secrets (encrypted at rest, never echoed), the anti-exfiltration
rule, Bedrock auth modes, test-connection classification, and audit hygiene."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from alkera_core import entitlements as ent
from alkera_core.auth.secret_box import decrypt_secret, encrypt_secret
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.llm_provider import Provider
from alkera_core.model_providers import ProviderProbeResult
from alkera_core.models import ModelProviderConfig, OrgAuditEvent
from httpx import AsyncClient
from sqlalchemy import select
from tests.conftest import OrgWithAdmin, login, make_member

PRIV, PUB = ent.generate_keypair()
BASE = "/api/v1/org/model-providers"


@pytest.fixture(autouse=True)
def _byok_entitled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Baseline: a BYOK-entitled self-hosted install."""
    token = ent.mint_entitlement_token(
        customer="acme-corp",
        features=ent.Feature.BYOK,
        expires_on=(datetime.now(UTC) + timedelta(days=90)).date(),
        serial=1,
        signing_key_b64=PRIV,
    )
    monkeypatch.setattr(settings, "self_hosted", True)
    monkeypatch.setattr(settings, "alkera_entitlements", token)
    monkeypatch.setattr(settings, "alkera_entitlements_public_key", PUB)
    ent.get_entitlements.cache_clear()
    yield
    ent.get_entitlements.cache_clear()


async def _row(org_id: Any, provider: str) -> ModelProviderConfig | None:
    async with AsyncSessionLocal() as s:
        return (
            await s.execute(
                select(ModelProviderConfig).where(
                    ModelProviderConfig.org_team_id == org_id,
                    ModelProviderConfig.provider == provider,
                )
            )
        ).scalar_one_or_none()


# --- entitlement invisibility -------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"),
    [
        pytest.param("GET", BASE, id="list"),
        pytest.param("PUT", f"{BASE}/anthropic", id="upsert"),
        pytest.param("DELETE", f"{BASE}/anthropic", id="remove"),
        pytest.param("POST", f"{BASE}/anthropic/test", id="test"),
    ],
)
async def test_unentitled_surface_is_a_bare_404(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
) -> None:
    monkeypatch.setattr(settings, "alkera_entitlements", None)
    ent.get_entitlements.cache_clear()
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.request(method, path, json={} if method == "PUT" else None)
    ghost = await client.get("/api/v1/org/route-that-never-existed")
    assert resp.status_code == ghost.status_code == 404
    # Indistinguishable from a route that never existed (same envelope, same
    # code/message — only the trace id differs).
    assert resp.json()["error"]["code"] == ghost.json()["error"]["code"]
    assert resp.json()["error"]["message"] == ghost.json()["error"]["message"]


@pytest.mark.asyncio
async def test_saas_is_unentitled_even_with_a_token(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "self_hosted", False)
    ent.get_entitlements.cache_clear()
    await login(client, org_admin.admin_email, org_admin.admin_password)
    assert (await client.get(BASE)).status_code == 404


# --- RBAC ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_member_cannot_read_or_mutate(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    member, password = await make_member(
        real_session, org_id=org_admin.org_id, password="member-pass-123", verified=True
    )
    await real_session.commit()
    await login(client, member.email, password or "")
    assert (await client.get(BASE)).status_code == 403
    assert (
        await client.put(f"{BASE}/anthropic", json={"api_key": "sk-ant-x-12345678"})
    ).status_code == 403


# --- write-only secrets + anti-exfiltration ------------------------------------------


@pytest.mark.asyncio
async def test_key_is_write_only_encrypted_at_rest_and_hinted(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    key = "sk-ant-api03-verysecret-9zqX"
    resp = await client.put(f"{BASE}/anthropic", json={"api_key": key})
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is True
    assert body["has_credentials"] is True
    assert body["secret_hint"] == "…9zqX"
    assert key not in resp.text  # the plaintext never appears in ANY response

    listing = await client.get(BASE)
    assert key not in listing.text

    row = await _row(org_admin.org_id, "anthropic")
    assert row is not None
    assert row.api_key_encrypted is not None
    assert key not in row.api_key_encrypted  # ciphertext at rest
    assert decrypt_secret(row.api_key_encrypted) == key


@pytest.mark.asyncio
async def test_blank_key_keeps_the_stored_secret(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    key = "sk-ant-api03-original-11aa"
    await client.put(f"{BASE}/anthropic", json={"api_key": key})
    resp = await client.put(f"{BASE}/anthropic", json={"enabled": False})
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False
    row = await _row(org_admin.org_id, "anthropic")
    assert row is not None and row.api_key_encrypted is not None
    assert decrypt_secret(row.api_key_encrypted) == key  # unchanged


@pytest.mark.asyncio
async def test_first_configure_requires_a_key(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.put(f"{BASE}/openai", json={"base_url": "https://llm.acme.dev/v1"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_changing_base_url_requires_reentering_the_key(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The anti-exfiltration rule: a stored valid key must never be silently
    repointed at a new endpoint."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    await client.put(f"{BASE}/anthropic", json={"api_key": "sk-ant-api03-abcd1234"})

    hijack = await client.put(f"{BASE}/anthropic", json={"base_url": "https://evil.example"})
    assert hijack.status_code == 400
    assert "re-entering" in hijack.json()["error"]["message"]

    # Same URL (even with a trailing slash) is NOT an endpoint change.
    same = await client.put(f"{BASE}/anthropic", json={"enabled": True, "base_url": None})
    assert same.status_code == 200

    # Re-entering the key legitimizes the move.
    moved = await client.put(
        f"{BASE}/anthropic",
        json={"base_url": "https://gw.acme.internal", "api_key": "sk-ant-api03-abcd1234"},
    )
    assert moved.status_code == 200
    assert moved.json()["base_url"] == "https://gw.acme.internal"


@pytest.mark.asyncio
async def test_credential_change_clears_verification(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    await client.put(f"{BASE}/anthropic", json={"api_key": "sk-ant-api03-abcd1234"})

    async def fake_probe(creds: Any, **kwargs: Any) -> ProviderProbeResult:
        return ProviderProbeResult(classification="ok", detail="credentials accepted", latency_ms=5)

    from backend.services import model_providers as model_provider_service

    monkeypatch.setattr(model_provider_service, "probe_provider_credentials", fake_probe)
    tested = await client.post(f"{BASE}/anthropic/test")
    assert tested.json()["status"] == "ok"
    assert (await _row(org_admin.org_id, "anthropic")).last_verified_status == "ok"  # type: ignore[union-attr]

    # Rotating the key invalidates the proof.
    await client.put(f"{BASE}/anthropic", json={"api_key": "sk-ant-api03-rotated99"})
    row = await _row(org_admin.org_id, "anthropic")
    assert row is not None
    assert row.last_verified_at is None
    assert row.last_verified_status is None


# --- Bedrock auth modes ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_bedrock_iam_mode_is_region_only(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    ok = await client.put(
        f"{BASE}/bedrock", json={"bedrock_region": "us-east-1", "bedrock_auth_mode": "iam"}
    )
    assert ok.status_code == 200
    body = ok.json()
    assert body["bedrock_auth_mode"] == "iam"
    assert body["has_credentials"] is True  # the ambient IAM role IS the credential (no stored key)

    with_keys = await client.put(
        f"{BASE}/bedrock",
        json={
            "bedrock_region": "us-east-1",
            "bedrock_auth_mode": "iam",
            "aws_access_key_id": "AKIAEXAMPLE12345",
            "aws_secret_access_key": "shhh-secret-value",
        },
    )
    assert with_keys.status_code == 400  # keys forbidden in iam mode


@pytest.mark.asyncio
async def test_bedrock_access_key_mode_requires_both_halves_and_wipes_on_switch(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    half = await client.put(
        f"{BASE}/bedrock",
        json={
            "bedrock_region": "us-east-1",
            "bedrock_auth_mode": "access_key",
            "aws_access_key_id": "AKIAEXAMPLE12345",
        },
    )
    assert half.status_code == 400

    both = await client.put(
        f"{BASE}/bedrock",
        json={
            "bedrock_region": "us-east-1",
            "bedrock_auth_mode": "access_key",
            "aws_access_key_id": "AKIAEXAMPLE12345",
            "aws_secret_access_key": "wJalrXUtnFEMI/K7MDENG",
        },
    )
    assert both.status_code == 200
    assert both.json()["secret_hint"] == "…2345"

    # Switching to iam wipes the stored pair — no orphaned secrets.
    switched = await client.put(
        f"{BASE}/bedrock", json={"bedrock_region": "us-east-1", "bedrock_auth_mode": "iam"}
    )
    assert switched.status_code == 200
    row = await _row(org_admin.org_id, "bedrock")
    assert row is not None
    assert row.aws_access_key_id_encrypted is None
    assert row.aws_secret_access_key_encrypted is None
    assert row.secret_hint is None


# --- test-connection -------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("classification", "ok"),
    [
        pytest.param("ok", True, id="ok"),
        pytest.param("invalid_key", False, id="invalid-key"),
        pytest.param("permission", False, id="permission"),
        pytest.param("network", False, id="network"),
    ],
)
async def test_test_connection_classifies_and_persists(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
    classification: str,
    ok: bool,
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    await client.put(f"{BASE}/openai", json={"api_key": "sk-proj-test-77zz"})

    async def fake_probe(creds: Any, **kwargs: Any) -> ProviderProbeResult:
        return ProviderProbeResult(
            classification=classification, detail="detail text", latency_ms=7
        )  # type: ignore[arg-type]

    from backend.services import model_providers as model_provider_service

    monkeypatch.setattr(model_provider_service, "probe_provider_credentials", fake_probe)
    resp = await client.post(f"{BASE}/openai/test")
    assert resp.status_code == 200  # a failed test is a RESULT, not an error
    body = resp.json()
    assert body["ok"] is ok
    assert body["status"] == classification
    row = await _row(org_admin.org_id, "openai")
    assert row is not None
    assert row.last_verified_status == classification
    assert row.last_verified_at is not None


@pytest.mark.asyncio
async def test_test_connection_unconfigured(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.post(f"{BASE}/openai/test")
    assert resp.status_code == 200
    assert resp.json()["status"] == "not_configured"


# --- delete + isolation + audit ---------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_removes_row(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    await client.put(f"{BASE}/anthropic", json={"api_key": "sk-ant-api03-abcd1234"})
    resp = await client.delete(f"{BASE}/anthropic")
    assert resp.status_code == 200
    assert resp.json()["configured"] is False
    assert await _row(org_admin.org_id, "anthropic") is None
    assert (await client.delete(f"{BASE}/anthropic")).status_code == 404


@pytest.mark.asyncio
async def test_unknown_provider_404s(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    assert (await client.put(f"{BASE}/vertex", json={"api_key": "x"})).status_code in (404, 422)


@pytest.mark.asyncio
async def test_mutations_are_audited_without_key_material(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    key = "sk-ant-api03-super-secret-4242"
    await client.put(f"{BASE}/anthropic", json={"api_key": key})

    async def fake_probe(creds: Any, **kwargs: Any) -> ProviderProbeResult:
        return ProviderProbeResult(classification="ok", detail="credentials accepted", latency_ms=3)

    from backend.services import model_providers as model_provider_service

    monkeypatch.setattr(model_provider_service, "probe_provider_credentials", fake_probe)
    await client.post(f"{BASE}/anthropic/test")
    await client.delete(f"{BASE}/anthropic")

    async with AsyncSessionLocal() as s:
        events = (
            (
                await s.execute(
                    select(OrgAuditEvent).where(OrgAuditEvent.org_team_id == org_admin.org_id)
                )
            )
            .scalars()
            .all()
        )
    actions = {e.action for e in events}
    assert {
        "model_provider.configured",
        "model_provider.tested",
        "model_provider.removed",
    } <= actions
    serialized = "".join(str(e.detail) for e in events)
    assert key not in serialized
    assert "super-secret" not in serialized


# --- the connection test dials the same string the gateway dials --------------


def _guarded_probe_client(
    monkeypatch: pytest.MonkeyPatch, *, answers: list[str]
) -> list[httpx.Request]:
    """Keep the service's own egress policy and its guard, fake only the socket.

    The client is still built by ``model_provider_service`` with whatever policy
    it chooses — this only scripts the name lookup and swaps the transport under
    the guard, so a refusal here is the shipped guard's refusal and a request in
    the returned list is one that genuinely left for a vetted address.
    """
    from backend.services import model_providers as model_provider_service

    sent: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"data": []})

    real = model_provider_service.async_client

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        return real(**kwargs, transport=httpx.MockTransport(record))

    # Scripted at the resolver the guard reaches for, not through a guard-only
    # keyword: a client built WITHOUT the guard has to reach the transport and
    # answer "ok" here, or the refusals below prove nothing.
    monkeypatch.setattr("alkera_core.egress.system_resolver", lambda host, port: answers)
    monkeypatch.setattr(model_provider_service, "async_client", factory)
    return sent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "reached_the_wire"),
    [
        pytest.param("169.254.169.254", False, id="metadata"),
        pytest.param("169.254.170.2", False, id="ecs-task-role-credentials"),
        pytest.param("64:ff9b::a9fe:a9fe", False, id="metadata-as-nat64"),
        pytest.param("::ffff:169.254.169.254", False, id="metadata-as-ipv4-mapped"),
        # A customer's own install legitimately fronts an in-VPC inference proxy,
        # and this console only exists on one — so a private answer is fine here.
        # The metadata service never is, whatever else the deployment allows.
        pytest.param("10.0.0.5", True, id="in-vpc-proxy"),
        pytest.param("93.184.216.34", True, id="public"),
    ],
)
async def test_the_connection_test_is_vetted_and_pinned_like_the_gateway(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
    answer: str,
    reached_the_wire: bool,
) -> None:
    """The admin-clicked test dials the same org-supplied ``base_url`` the model
    gateway does, so it answers to the same judge.

    Unguarded it was two things at once: a host-and-port oracle for the
    deployment's own network, reachable by an org admin from the console; and a
    second opinion that said "connection ok" for an endpoint the gateway refuses
    at request time.
    """
    await login(client, org_admin.admin_email, org_admin.admin_password)
    # An ordinary name — the request schema admits it, and what it RESOLVES to is
    # the whole question.
    await client.put(
        f"{BASE}/anthropic",
        json={"api_key": "sk-ant-test-77zz", "base_url": "https://llm.acme.example"},
    )
    sent = _guarded_probe_client(monkeypatch, answers=[answer])

    resp = await client.post(f"{BASE}/anthropic/test")
    assert resp.status_code == 200
    body = resp.json()

    if reached_the_wire:
        assert body["status"] == "ok"
        assert len(sent) == 1
        # Pinned: the connection goes to the vetted address while the Host header
        # stays the configured name, so the certificate is still checked against it.
        assert sent[0].url.host == answer
        assert sent[0].headers["Host"] == "llm.acme.example"
    else:
        assert body["status"] == "network"
        # Opaque: the caller learns only that it was unreachable, never which
        # address it resolved to — otherwise the refusal is the oracle.
        assert answer not in str(body["detail"])
        assert sent == []

    row = await _row(org_admin.org_id, "anthropic")
    assert row is not None
    assert row.last_verified_status == ("ok" if reached_the_wire else "network")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("self_hosted", "reached_the_wire"),
    [
        pytest.param(True, True, id="self-hosted-reaches-its-own-network"),
        pytest.param(False, False, id="alkera-operated-does-not"),
    ],
)
async def test_the_probe_reads_private_the_way_the_deployment_does(
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
    self_hosted: bool,
    reached_the_wire: bool,
) -> None:
    """The stance, driven at the service rather than the route.

    The console itself is BYOK-gated and BYOK is self-hosted-only, so the route
    404s on SaaS and the Alkera-operated half of this rule is unreachable through
    it today. It is still the half that matters if BYOK is ever offered on SaaS:
    pinned here so the probe cannot quietly become a VPC oracle the day that
    gate moves.
    """
    from backend.services import model_providers as model_provider_service

    monkeypatch.setattr(settings, "self_hosted", self_hosted)
    sent = _guarded_probe_client(monkeypatch, answers=["10.0.0.5"])
    async with AsyncSessionLocal() as db:
        db.add(
            ModelProviderConfig(
                org_team_id=org_admin.org_id,
                provider="anthropic",
                enabled=True,
                api_key_encrypted=encrypt_secret("sk-ant-test-77zz"),
                secret_hint="…7zz",
                base_url="https://llm.acme.example",
            )
        )
        await db.commit()
    async with AsyncSessionLocal() as db:
        result, _row_out = await model_provider_service.test_connection(
            db, org_team_id=org_admin.org_id, provider=Provider.ANTHROPIC
        )
        await db.commit()

    assert result is not None
    assert result.classification == ("ok" if reached_the_wire else "network")
    assert bool(sent) is reached_the_wire
