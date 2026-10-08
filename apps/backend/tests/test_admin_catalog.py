"""Admin catalog CRUD — configure models / routes / prices the gateway serves."""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import EventOutbox
from httpx import AsyncClient
from sqlalchemy import func, select
from tests.conftest import OrgWithAdmin, app_client, login


def _model_id() -> str:
    return f"test-model-{secrets.token_hex(4)}"


async def _staff(client: AsyncClient, staff: OrgWithAdmin) -> None:
    await login(client, staff.admin_email, staff.admin_password)


@pytest.mark.asyncio
async def test_catalog_requires_staff(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    assert (await client.get("/admin/v1/catalog/models")).status_code == 401
    await login(client, org_admin.admin_email, org_admin.admin_password)
    assert (await client.get("/admin/v1/catalog/models")).status_code == 403


@pytest.mark.asyncio
async def test_create_model_appears_in_list(
    client: AsyncClient, platform_support: OrgWithAdmin
) -> None:
    await _staff(client, platform_support)
    mid = _model_id()
    resp = await client.post(
        "/admin/v1/catalog/models",
        json={"id": mid, "display_name": "Test", "family": "test", "enabled": True},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] == mid
    assert body["routes"] == [] and body["sell_prices"] == []

    listing = await client.get("/admin/v1/catalog/models")
    assert any(m["id"] == mid for m in listing.json())


@pytest.mark.asyncio
async def test_create_and_update_token_limits(
    client: AsyncClient, platform_support: OrgWithAdmin
) -> None:
    # context_window + max_output_tokens flow through create (persisted + read back)
    # and PATCH (partial update touching only the limit).
    await _staff(client, platform_support)
    mid = _model_id()
    created = await client.post(
        "/admin/v1/catalog/models",
        json={
            "id": mid,
            "display_name": "Big",
            "family": "test",
            "context_window": 1_000_000,
            "max_output_tokens": 64_000,
        },
    )
    assert created.status_code == 201
    body = created.json()
    assert body["context_window"] == 1_000_000
    assert body["max_output_tokens"] == 64_000

    # Defaults to 0 (unknown) when omitted.
    other = await client.post(
        "/admin/v1/catalog/models",
        json={"id": _model_id(), "display_name": "D", "family": "test"},
    )
    assert other.json()["max_output_tokens"] == 0

    patched = await client.patch(
        f"/admin/v1/catalog/models/{mid}", json={"max_output_tokens": 128_000}
    )
    assert patched.status_code == 200
    assert patched.json()["max_output_tokens"] == 128_000
    # The untouched limit is unchanged by the partial PATCH.
    assert patched.json()["context_window"] == 1_000_000


@pytest.mark.asyncio
async def test_negative_token_limit_rejected(
    client: AsyncClient, platform_support: OrgWithAdmin
) -> None:
    await _staff(client, platform_support)
    resp = await client.post(
        "/admin/v1/catalog/models",
        json={"id": _model_id(), "display_name": "N", "family": "test", "max_output_tokens": -1},
    )
    assert resp.status_code == 422  # Field(ge=0)


@pytest.mark.asyncio
async def test_duplicate_model_409(client: AsyncClient, platform_support: OrgWithAdmin) -> None:
    await _staff(client, platform_support)
    mid = _model_id()
    body = {"id": mid, "display_name": "T", "family": "test"}
    assert (await client.post("/admin/v1/catalog/models", json=body)).status_code == 201
    assert (await client.post("/admin/v1/catalog/models", json=body)).status_code == 409


@pytest.mark.asyncio
async def test_full_model_configuration_flow(
    client: AsyncClient, platform_admin: OrgWithAdmin
) -> None:
    await _staff(client, platform_admin)
    mid = _model_id()
    await client.post(
        "/admin/v1/catalog/models",
        json={"id": mid, "display_name": "Claude", "family": "claude", "enabled": True},
    )
    # Add a Bedrock route.
    route_resp = await client.post(
        f"/admin/v1/catalog/models/{mid}/routes",
        json={"provider": "bedrock", "upstream_model_id": "anthropic.claude-x", "priority": 0},
    )
    assert route_resp.status_code == 201
    route_id = route_resp.json()["id"]

    # Set input + output sell prices.
    for kind, price in (("input", "3.00"), ("output", "15.00")):
        r = await client.post(
            f"/admin/v1/catalog/models/{mid}/sell-prices",
            json={"rate_kind": kind, "per_million_usd": price},
        )
        assert r.status_code == 201

    # Set the provider input cost.
    cost = await client.post(
        "/admin/v1/catalog/provider-costs",
        json={
            "provider": "bedrock",
            "upstream_model_id": "anthropic.claude-x",
            "rate_kind": "input",
            "per_million_usd": "0.30",
        },
    )
    assert cost.status_code == 201
    # Responses serve nano-USD per token only; $0.30/1M = 300 nano/token.
    assert cost.json()["per_token_nanos"] == 300

    # The model detail now reflects the full config.
    detail = (await client.get(f"/admin/v1/catalog/models/{mid}")).json()
    assert len(detail["routes"]) == 1
    sells = {p["rate_kind"]: p["per_token_nanos"] for p in detail["sell_prices"]}
    assert sells == {"input": 3000, "output": 15000}
    costs = {p["rate_kind"]: p["per_token_nanos"] for p in detail["provider_costs"]}
    assert costs["input"] == 300
    assert route_id  # created


@pytest.mark.asyncio
async def test_route_update_and_delete(client: AsyncClient, platform_support: OrgWithAdmin) -> None:
    await _staff(client, platform_support)
    mid = _model_id()
    await client.post(
        "/admin/v1/catalog/models", json={"id": mid, "display_name": "M", "family": "t"}
    )
    route_id = (
        await client.post(
            f"/admin/v1/catalog/models/{mid}/routes",
            json={"provider": "anthropic", "upstream_model_id": "up", "enabled": True},
        )
    ).json()["id"]

    disabled = await client.patch(f"/admin/v1/catalog/routes/{route_id}", json={"enabled": False})
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False

    assert (await client.delete(f"/admin/v1/catalog/routes/{route_id}")).status_code == 204
    detail = (await client.get(f"/admin/v1/catalog/models/{mid}")).json()
    assert detail["routes"] == []


@pytest.mark.asyncio
async def test_update_model_enabled(client: AsyncClient, platform_support: OrgWithAdmin) -> None:
    await _staff(client, platform_support)
    mid = _model_id()
    await client.post(
        "/admin/v1/catalog/models",
        json={"id": mid, "display_name": "M", "family": "t", "enabled": False},
    )
    resp = await client.patch(f"/admin/v1/catalog/models/{mid}", json={"enabled": True})
    assert resp.status_code == 200
    assert resp.json()["enabled"] is True


@pytest.mark.asyncio
async def test_bad_price_400(client: AsyncClient, platform_admin: OrgWithAdmin) -> None:
    await _staff(client, platform_admin)
    mid = _model_id()
    await client.post(
        "/admin/v1/catalog/models", json={"id": mid, "display_name": "M", "family": "t"}
    )
    resp = await client.post(
        f"/admin/v1/catalog/models/{mid}/sell-prices",
        json={"rate_kind": "input", "per_million_usd": "not-a-number"},
    )
    assert resp.status_code == 400
    # A NEGATIVE price would write negative billed_cost_nanos that silently
    # REDUCE invoice rollups — a typo'd "-3.00" must never become a discount.
    resp = await client.post(
        f"/admin/v1/catalog/models/{mid}/sell-prices",
        json={"rate_kind": "input", "per_million_usd": "-3.00"},
    )
    assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# Reasoning-effort variants
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_create_model_with_reasoning_efforts(
    client: AsyncClient, platform_support: OrgWithAdmin
) -> None:
    await _staff(client, platform_support)
    mid = _model_id()
    resp = await client.post(
        "/admin/v1/catalog/models",
        json={
            "id": mid,
            "display_name": "Claude",
            "family": "claude",
            "reasoning_efforts": ["low", "medium", "high"],
            "default_effort": "medium",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["reasoning_efforts"] == ["low", "medium", "high"]
    assert body["default_effort"] == "medium"

    # And it round-trips through the read endpoints.
    detail = (await client.get(f"/admin/v1/catalog/models/{mid}")).json()
    assert detail["reasoning_efforts"] == ["low", "medium", "high"]
    assert detail["default_effort"] == "medium"


@pytest.mark.asyncio
async def test_create_model_defaults_efforts_to_empty(
    client: AsyncClient, platform_support: OrgWithAdmin
) -> None:
    await _staff(client, platform_support)
    mid = _model_id()
    body = (
        await client.post(
            "/admin/v1/catalog/models",
            json={"id": mid, "display_name": "M", "family": "t"},
        )
    ).json()
    assert body["reasoning_efforts"] == []
    assert body["default_effort"] is None


@pytest.mark.asyncio
async def test_create_model_rejects_default_outside_efforts(
    client: AsyncClient, platform_support: OrgWithAdmin
) -> None:
    await _staff(client, platform_support)
    resp = await client.post(
        "/admin/v1/catalog/models",
        json={
            "id": _model_id(),
            "display_name": "M",
            "family": "t",
            "reasoning_efforts": ["low", "high"],
            "default_effort": "medium",  # not in the list
        },
    )
    assert resp.status_code == 422  # schema-level model_validator


@pytest.mark.asyncio
async def test_update_model_efforts(client: AsyncClient, platform_support: OrgWithAdmin) -> None:
    await _staff(client, platform_support)
    mid = _model_id()
    await client.post(
        "/admin/v1/catalog/models", json={"id": mid, "display_name": "M", "family": "t"}
    )
    resp = await client.patch(
        f"/admin/v1/catalog/models/{mid}",
        json={"reasoning_efforts": ["minimal", "low", "medium", "high"], "default_effort": "high"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["reasoning_efforts"] == ["minimal", "low", "medium", "high"]
    assert body["default_effort"] == "high"


@pytest.mark.asyncio
async def test_update_model_rejects_default_outside_efforts(
    client: AsyncClient, platform_support: OrgWithAdmin
) -> None:
    await _staff(client, platform_support)
    mid = _model_id()
    await client.post(
        "/admin/v1/catalog/models",
        json={
            "id": mid,
            "display_name": "M",
            "family": "t",
            "reasoning_efforts": ["low", "high"],
            "default_effort": "low",
        },
    )
    # Setting only default_effort to a value not in the (unchanged) efforts → 400.
    resp = await client.patch(f"/admin/v1/catalog/models/{mid}", json={"default_effort": "medium"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_create_model_with_thinking_mode(
    client: AsyncClient, platform_support: OrgWithAdmin
) -> None:
    await _staff(client, platform_support)
    mid = _model_id()
    resp = await client.post(
        "/admin/v1/catalog/models",
        json={
            "id": mid,
            "display_name": "Claude",
            "family": "claude",
            "reasoning_efforts": ["low", "high"],
            "default_effort": "low",
            "thinking_mode": "adaptive",
        },
    )
    assert resp.status_code == 201
    assert resp.json()["thinking_mode"] == "adaptive"
    detail = (await client.get(f"/admin/v1/catalog/models/{mid}")).json()
    assert detail["thinking_mode"] == "adaptive"


@pytest.mark.asyncio
async def test_create_model_rejects_bad_thinking_mode(
    client: AsyncClient, platform_support: OrgWithAdmin
) -> None:
    await _staff(client, platform_support)
    resp = await client.post(
        "/admin/v1/catalog/models",
        json={
            "id": _model_id(),
            "display_name": "M",
            "family": "t",
            "thinking_mode": "bogus",  # not in the Literal
        },
    )
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# Capability tier
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_create_model_with_tier(client: AsyncClient, platform_support: OrgWithAdmin) -> None:
    await _staff(client, platform_support)
    mid = _model_id()
    resp = await client.post(
        "/admin/v1/catalog/models",
        json={"id": mid, "display_name": "M", "family": "t", "tier": "frontier"},
    )
    assert resp.status_code == 201
    assert resp.json()["tier"] == "frontier"
    detail = (await client.get(f"/admin/v1/catalog/models/{mid}")).json()
    assert detail["tier"] == "frontier"


@pytest.mark.asyncio
async def test_create_model_defaults_tier_to_standard(
    client: AsyncClient, platform_support: OrgWithAdmin
) -> None:
    await _staff(client, platform_support)
    mid = _model_id()
    body = (
        await client.post(
            "/admin/v1/catalog/models",
            json={"id": mid, "display_name": "M", "family": "t"},
        )
    ).json()
    assert body["tier"] == "standard"


@pytest.mark.asyncio
async def test_update_model_tier(client: AsyncClient, platform_support: OrgWithAdmin) -> None:
    await _staff(client, platform_support)
    mid = _model_id()
    await client.post(
        "/admin/v1/catalog/models", json={"id": mid, "display_name": "M", "family": "t"}
    )
    resp = await client.patch(f"/admin/v1/catalog/models/{mid}", json={"tier": "cheap"})
    assert resp.status_code == 200
    assert resp.json()["tier"] == "cheap"
    detail = (await client.get(f"/admin/v1/catalog/models/{mid}")).json()
    assert detail["tier"] == "cheap"


@pytest.mark.asyncio
async def test_create_model_rejects_bad_tier(
    client: AsyncClient, platform_support: OrgWithAdmin
) -> None:
    await _staff(client, platform_support)
    resp = await client.post(
        "/admin/v1/catalog/models",
        json={"id": _model_id(), "display_name": "M", "family": "t", "tier": "bogus"},
    )
    assert resp.status_code == 422  # not a ModelTier value


# --------------------------------------------------------------------------- #
# Model flags
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_create_model_with_flags(client: AsyncClient, platform_support: OrgWithAdmin) -> None:
    await _staff(client, platform_support)
    mid = _model_id()
    resp = await client.post(
        "/admin/v1/catalog/models",
        json={
            "id": mid,
            "display_name": "M",
            "family": "t",
            "flags": ["anthropic_haiku_style_thinking"],
        },
    )
    assert resp.status_code == 201
    assert resp.json()["flags"] == ["anthropic_haiku_style_thinking"]
    detail = (await client.get(f"/admin/v1/catalog/models/{mid}")).json()
    assert detail["flags"] == ["anthropic_haiku_style_thinking"]


@pytest.mark.asyncio
async def test_create_model_defaults_flags_to_empty(
    client: AsyncClient, platform_support: OrgWithAdmin
) -> None:
    await _staff(client, platform_support)
    mid = _model_id()
    body = (
        await client.post(
            "/admin/v1/catalog/models",
            json={"id": mid, "display_name": "M", "family": "t"},
        )
    ).json()
    assert body["flags"] == []


@pytest.mark.asyncio
async def test_update_model_flags(client: AsyncClient, platform_support: OrgWithAdmin) -> None:
    await _staff(client, platform_support)
    mid = _model_id()
    await client.post(
        "/admin/v1/catalog/models", json={"id": mid, "display_name": "M", "family": "t"}
    )
    set_resp = await client.patch(
        f"/admin/v1/catalog/models/{mid}", json={"flags": ["anthropic_haiku_style_thinking"]}
    )
    assert set_resp.status_code == 200
    assert set_resp.json()["flags"] == ["anthropic_haiku_style_thinking"]
    # Clearing back to [] works (PATCH with an explicit empty list).
    clear_resp = await client.patch(f"/admin/v1/catalog/models/{mid}", json={"flags": []})
    assert clear_resp.status_code == 200
    assert clear_resp.json()["flags"] == []


@pytest.mark.asyncio
async def test_create_model_rejects_bad_flag(
    client: AsyncClient, platform_support: OrgWithAdmin
) -> None:
    await _staff(client, platform_support)
    resp = await client.post(
        "/admin/v1/catalog/models",
        json={"id": _model_id(), "display_name": "M", "family": "t", "flags": ["bogus"]},
    )
    assert resp.status_code == 422  # not a ModelFlag value


@pytest.mark.asyncio
async def test_create_model_rejects_haiku_flag_with_unmapped_effort(
    client: AsyncClient, platform_support: OrgWithAdmin
) -> None:
    """A Haiku-style-thinking model may only expose efforts the gateway maps to a
    budget (none/low/medium/high). An unmapped effort like 'max' would route + charge
    but silently run with NO thinking, so the create is rejected at config time (400)."""
    await _staff(client, platform_support)
    resp = await client.post(
        "/admin/v1/catalog/models",
        json={
            "id": _model_id(),
            "display_name": "M",
            "family": "t",
            "reasoning_efforts": ["low", "max"],
            "default_effort": "low",
            "flags": ["anthropic_haiku_style_thinking"],
        },
    )
    assert resp.status_code == 400  # 'max' not in HAIKU_THINKING_EFFORTS


@pytest.mark.asyncio
async def test_create_model_accepts_haiku_flag_with_mapped_efforts(
    client: AsyncClient, platform_support: OrgWithAdmin
) -> None:
    """The full none/low/medium/high set is accepted for a Haiku-flagged model."""
    await _staff(client, platform_support)
    resp = await client.post(
        "/admin/v1/catalog/models",
        json={
            "id": _model_id(),
            "display_name": "M",
            "family": "t",
            "reasoning_efforts": ["none", "low", "medium", "high"],
            "default_effort": "none",
            "flags": ["anthropic_haiku_style_thinking"],
        },
    )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_update_model_rejects_adding_haiku_flag_with_unmapped_effort(
    client: AsyncClient, platform_support: OrgWithAdmin
) -> None:
    """The EFFECTIVE config is validated on PATCH: adding the flag to a model that
    already exposes an unmapped effort ('max') is rejected (400)."""
    await _staff(client, platform_support)
    mid = _model_id()
    create = await client.post(
        "/admin/v1/catalog/models",
        json={
            "id": mid,
            "display_name": "M",
            "family": "t",
            "reasoning_efforts": ["low", "max"],
            "default_effort": "low",
        },
    )
    assert create.status_code == 201
    resp = await client.patch(
        f"/admin/v1/catalog/models/{mid}",
        json={"flags": ["anthropic_haiku_style_thinking"]},
    )
    assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# Tiered prices (tier_min_tokens) + DELETE endpoints
# --------------------------------------------------------------------------- #


async def _make_model(client: AsyncClient) -> str:
    mid = _model_id()
    await client.post(
        "/admin/v1/catalog/models",
        json={"id": mid, "display_name": "M", "family": "t", "enabled": True},
    )
    return mid


@pytest.mark.asyncio
async def test_sell_price_tiers_round_trip(
    client: AsyncClient, platform_admin: OrgWithAdmin
) -> None:
    await _staff(client, platform_admin)
    mid = await _make_model(client)

    # Base tier (defaults tier_min_tokens=0) and a 200k surcharge tier — both INPUT.
    base = await client.post(
        f"/admin/v1/catalog/models/{mid}/sell-prices",
        json={"rate_kind": "input", "per_million_usd": "3.00"},
    )
    assert base.status_code == 201
    surcharge = await client.post(
        f"/admin/v1/catalog/models/{mid}/sell-prices",
        json={"rate_kind": "input", "per_million_usd": "6.00", "tier_min_tokens": 200000},
    )
    assert surcharge.status_code == 201

    detail = (await client.get(f"/admin/v1/catalog/models/{mid}")).json()
    by_tier = {
        (p["rate_kind"], p["tier_min_tokens"]): p["per_token_nanos"] for p in detail["sell_prices"]
    }
    # Two distinct rows survive — the 200k tier is NOT collapsed into the base.
    # $3.00/1M = 3000 nano/token, $6.00/1M = 6000.
    assert by_tier[("input", 0)] == 3000
    assert by_tier[("input", 200000)] == 6000


@pytest.mark.asyncio
async def test_provider_cost_tiers_round_trip(
    client: AsyncClient, platform_admin: OrgWithAdmin
) -> None:
    await _staff(client, platform_admin)
    mid = await _make_model(client)
    await client.post(
        f"/admin/v1/catalog/models/{mid}/routes",
        json={"provider": "bedrock", "upstream_model_id": "anthropic.claude-x", "priority": 0},
    )

    for tier, price in ((0, "0.30"), (200000, "0.60")):
        r = await client.post(
            "/admin/v1/catalog/provider-costs",
            json={
                "provider": "bedrock",
                "upstream_model_id": "anthropic.claude-x",
                "rate_kind": "input",
                "per_million_usd": price,
                "tier_min_tokens": tier,
            },
        )
        assert r.status_code == 201

    detail = (await client.get(f"/admin/v1/catalog/models/{mid}")).json()
    by_tier = {(p["rate_kind"], p["tier_min_tokens"]): p for p in detail["provider_costs"]}
    # $0.30/1M = 300 nano/token, $0.60/1M = 600.
    assert by_tier[("input", 0)]["per_token_nanos"] == 300
    assert by_tier[("input", 200000)]["per_token_nanos"] == 600
    # ProviderCostRead carries the route identity on every row.
    for tier in (0, 200000):
        assert by_tier[("input", tier)]["provider"] == "bedrock"
        assert by_tier[("input", tier)]["upstream_model_id"] == "anthropic.claude-x"


@pytest.mark.asyncio
async def test_provider_cost_create_echoes_tier(
    client: AsyncClient, platform_admin: OrgWithAdmin
) -> None:
    await _staff(client, platform_admin)
    mid = await _make_model(client)
    await client.post(
        f"/admin/v1/catalog/models/{mid}/routes",
        json={"provider": "bedrock", "upstream_model_id": "anthropic.claude-x", "priority": 0},
    )
    resp = await client.post(
        "/admin/v1/catalog/provider-costs",
        json={
            "provider": "bedrock",
            "upstream_model_id": "anthropic.claude-x",
            "rate_kind": "input",
            "per_million_usd": "0.60",
            "tier_min_tokens": 200000,
        },
    )
    assert resp.status_code == 201
    assert resp.json()["tier_min_tokens"] == 200000


@pytest.mark.asyncio
async def test_delete_sell_price_tier(client: AsyncClient, platform_admin: OrgWithAdmin) -> None:
    await _staff(client, platform_admin)
    mid = await _make_model(client)
    for tier, price in ((0, "3.00"), (200000, "6.00")):
        await client.post(
            f"/admin/v1/catalog/models/{mid}/sell-prices",
            json={"rate_kind": "input", "per_million_usd": price, "tier_min_tokens": tier},
        )

    deleted = await client.delete(
        f"/admin/v1/catalog/models/{mid}/sell-prices",
        params={"rate_kind": "input", "tier_min_tokens": 200000},
    )
    assert deleted.status_code == 200

    detail = (await client.get(f"/admin/v1/catalog/models/{mid}")).json()
    tiers = {(p["rate_kind"], p["tier_min_tokens"]) for p in detail["sell_prices"]}
    assert tiers == {("input", 0)}


@pytest.mark.asyncio
async def test_delete_sell_price_404_when_absent(
    client: AsyncClient, platform_admin: OrgWithAdmin
) -> None:
    await _staff(client, platform_admin)
    mid = await _make_model(client)
    resp = await client.delete(
        f"/admin/v1/catalog/models/{mid}/sell-prices",
        params={"rate_kind": "input", "tier_min_tokens": 200000},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_provider_cost_tier(client: AsyncClient, platform_admin: OrgWithAdmin) -> None:
    await _staff(client, platform_admin)
    mid = await _make_model(client)
    await client.post(
        f"/admin/v1/catalog/models/{mid}/routes",
        json={"provider": "bedrock", "upstream_model_id": "anthropic.claude-x", "priority": 0},
    )
    for tier, price in ((0, "0.30"), (200000, "0.60")):
        await client.post(
            "/admin/v1/catalog/provider-costs",
            json={
                "provider": "bedrock",
                "upstream_model_id": "anthropic.claude-x",
                "rate_kind": "input",
                "per_million_usd": price,
                "tier_min_tokens": tier,
            },
        )

    deleted = await client.delete(
        "/admin/v1/catalog/provider-costs",
        params={
            "provider": "bedrock",
            "upstream_model_id": "anthropic.claude-x",
            "rate_kind": "input",
            "tier_min_tokens": 200000,
        },
    )
    assert deleted.status_code == 204

    detail = (await client.get(f"/admin/v1/catalog/models/{mid}")).json()
    tiers = {(p["rate_kind"], p["tier_min_tokens"]) for p in detail["provider_costs"]}
    assert tiers == {("input", 0)}


@pytest.mark.asyncio
async def test_delete_provider_cost_404_when_absent(
    client: AsyncClient, platform_admin: OrgWithAdmin
) -> None:
    await _staff(client, platform_admin)
    resp = await client.delete(
        "/admin/v1/catalog/provider-costs",
        params={
            "provider": "bedrock",
            "upstream_model_id": "anthropic.claude-never-seeded",
            "rate_kind": "input",
            "tier_min_tokens": 200000,
        },
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Prices are money: the platform admin's. The catalog's shape stays with staff.
# --------------------------------------------------------------------------- #

AUTHZ_TYPE = "authz.decision"
MONEY_POLICY = "platform.billing"
ADMIN_REQUIRED = "Platform admin role required"


async def _mark() -> int:
    """The newest outbox row now, so every decision read is scoped to what
    the step after the mark wrote."""
    async with AsyncSessionLocal() as s:
        return int((await s.execute(select(func.max(EventOutbox.id)))).scalar() or 0)


async def _money_decisions(since: int) -> list[EventOutbox]:
    async with AsyncSessionLocal() as s:
        rows = await s.execute(
            select(EventOutbox)
            .where(EventOutbox.type == AUTHZ_TYPE, EventOutbox.id > since)
            .order_by(EventOutbox.id)
        )
        return [r for r in rows.scalars().all() if r.payload.get("policy") == MONEY_POLICY]


def _effects(rows: list[EventOutbox]) -> list[tuple[str, str]]:
    return [(r.payload["effect"], r.payload["reason"]) for r in rows]


@dataclass(frozen=True)
class _PriceWrite:
    operation: str
    ok: int
    stage: Callable[[AsyncClient], Awaitable[dict[str, Any]]]
    attempt: Callable[[AsyncClient, dict[str, Any]], Awaitable[httpx.Response]]
    state: Callable[[AsyncClient, dict[str, Any]], Awaitable[object]]
    entity_id: Callable[[dict[str, Any]], str]


_UPSTREAM = "anthropic.claude-priced"


async def _stage_model(admin: AsyncClient) -> dict[str, Any]:
    mid = await _make_model(admin)
    upstream = f"{_UPSTREAM}-{secrets.token_hex(4)}"
    routed = await admin.post(
        f"/admin/v1/catalog/models/{mid}/routes",
        json={"provider": "bedrock", "upstream_model_id": upstream, "priority": 0},
    )
    assert routed.status_code == 201, routed.text
    return {"mid": mid, "upstream": upstream}


async def _stage_priced_model(admin: AsyncClient) -> dict[str, Any]:
    ctx = await _stage_model(admin)
    sold = await admin.post(
        f"/admin/v1/catalog/models/{ctx['mid']}/sell-prices",
        json={"rate_kind": "input", "per_million_usd": "3.00"},
    )
    assert sold.status_code == 201, sold.text
    return ctx


async def _stage_costed_model(admin: AsyncClient) -> dict[str, Any]:
    ctx = await _stage_model(admin)
    costed = await admin.post("/admin/v1/catalog/provider-costs", json=_cost_body(ctx))
    assert costed.status_code == 201, costed.text
    return ctx


def _cost_body(ctx: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider": "bedrock",
        "upstream_model_id": ctx["upstream"],
        "rate_kind": "input",
        "per_million_usd": "0.30",
    }


def _cost_params(ctx: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider": "bedrock",
        "upstream_model_id": ctx["upstream"],
        "rate_kind": "input",
        "tier_min_tokens": 0,
    }


async def _sell_prices(admin: AsyncClient, ctx: dict[str, Any]) -> list[tuple[str, int, int]]:
    detail = (await admin.get(f"/admin/v1/catalog/models/{ctx['mid']}")).json()
    return sorted(
        (p["rate_kind"], p["tier_min_tokens"], p["per_token_nanos"]) for p in detail["sell_prices"]
    )


async def _provider_costs(admin: AsyncClient, ctx: dict[str, Any]) -> list[tuple[str, int, int]]:
    detail = (await admin.get(f"/admin/v1/catalog/models/{ctx['mid']}")).json()
    return sorted(
        (p["rate_kind"], p["tier_min_tokens"], p["per_token_nanos"])
        for p in detail["provider_costs"]
    )


PRICE_WRITES = [
    pytest.param(
        _PriceWrite(
            operation="sell_price_set",
            ok=201,
            stage=_stage_model,
            attempt=lambda who, ctx: who.post(
                f"/admin/v1/catalog/models/{ctx['mid']}/sell-prices",
                json={"rate_kind": "input", "per_million_usd": "3.00"},
            ),
            state=_sell_prices,
            entity_id=lambda ctx: ctx["mid"],
        ),
        id="sell-price-set",
    ),
    pytest.param(
        _PriceWrite(
            operation="sell_price_delete",
            ok=200,
            stage=_stage_priced_model,
            attempt=lambda who, ctx: who.delete(
                f"/admin/v1/catalog/models/{ctx['mid']}/sell-prices",
                params={"rate_kind": "input", "tier_min_tokens": 0},
            ),
            state=_sell_prices,
            entity_id=lambda ctx: ctx["mid"],
        ),
        id="sell-price-delete",
    ),
    pytest.param(
        _PriceWrite(
            operation="provider_cost_set",
            ok=201,
            stage=_stage_model,
            attempt=lambda who, ctx: who.post(
                "/admin/v1/catalog/provider-costs", json=_cost_body(ctx)
            ),
            state=_provider_costs,
            entity_id=lambda ctx: f"bedrock:{ctx['upstream']}",
        ),
        id="provider-cost-set",
    ),
    pytest.param(
        _PriceWrite(
            operation="provider_cost_delete",
            ok=204,
            stage=_stage_costed_model,
            attempt=lambda who, ctx: who.delete(
                "/admin/v1/catalog/provider-costs", params=_cost_params(ctx)
            ),
            state=_provider_costs,
            entity_id=lambda ctx: f"bedrock:{ctx['upstream']}",
        ),
        id="provider-cost-delete",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("write", PRICE_WRITES)
async def test_a_price_write_is_the_platform_admins_and_the_decision_is_on_record(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    platform_support: OrgWithAdmin,
    write: _PriceWrite,
) -> None:
    """Support reaches the choke point and is refused on record: 403, the price
    table untouched, a deny row naming the operation and the priced thing. The
    platform admin's identical request lands, and the probe that read
    "untouched" is shown to see the change."""
    await _staff(client, platform_admin)
    ctx = await write.stage(client)
    before = await write.state(client, ctx)

    support = app_client()
    await login(support, platform_support.admin_email, platform_support.admin_password)
    try:
        mark = await _mark()
        refused = await write.attempt(support, ctx)
    finally:
        await support.aclose()
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["message"] == ADMIN_REQUIRED
    assert await write.state(client, ctx) == before

    rows = await _money_decisions(mark)
    assert _effects(rows) == [("deny", "platform_admin_required")]
    (deny,) = rows
    assert deny.org_id == platform_support.org_id
    assert deny.entity == "platform_billing"
    assert deny.entity_id == write.entity_id(ctx)
    assert deny.visibility == "platform"
    assert deny.payload["action"] == "admin"
    assert deny.payload["attrs"] == {
        "operation": write.operation,
        "platform_admin": False,
        "platform_staff": True,
    }

    mark = await _mark()
    allowed = await write.attempt(client, ctx)
    assert allowed.status_code == write.ok, allowed.text
    assert await write.state(client, ctx) != before
    rows = await _money_decisions(mark)
    assert _effects(rows) == [("allow", "admin_moves_money")]
    assert rows[0].org_id == platform_admin.org_id
    assert rows[0].entity_id == write.entity_id(ctx)
    assert rows[0].payload["attrs"]["operation"] == write.operation


@pytest.mark.asyncio
async def test_a_price_is_decided_before_the_model_is_looked_up(
    client: AsyncClient, platform_support: OrgWithAdmin
) -> None:
    """A support member pricing a model that does not exist learns nothing about
    the catalog: the refusal is the money decision, not a 404."""
    await _staff(client, platform_support)
    resp = await client.post(
        f"/admin/v1/catalog/models/{_model_id()}/sell-prices",
        json={"rate_kind": "input", "per_million_usd": "3.00"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["message"] == ADMIN_REQUIRED


@pytest.mark.asyncio
async def test_support_staff_still_shape_the_catalog_and_no_money_decision_is_taken(
    client: AsyncClient, platform_support: OrgWithAdmin
) -> None:
    """The asymmetry: which models exist, their limits and the routes that serve
    them are staff configuration, so the same support member who may not set a
    price does all of it — and none of it reaches the money policy."""
    await _staff(client, platform_support)
    mark = await _mark()
    mid = _model_id()
    created = await client.post(
        "/admin/v1/catalog/models",
        json={"id": mid, "display_name": "Shaped", "family": "t", "enabled": False},
    )
    assert created.status_code == 201, created.text
    route = await client.post(
        f"/admin/v1/catalog/models/{mid}/routes",
        json={"provider": "anthropic", "upstream_model_id": "up", "enabled": True},
    )
    assert route.status_code == 201, route.text
    route_id = route.json()["id"]
    assert (
        await client.patch(f"/admin/v1/catalog/models/{mid}", json={"enabled": True})
    ).status_code == 200
    assert (
        await client.patch(f"/admin/v1/catalog/routes/{route_id}", json={"enabled": False})
    ).status_code == 200
    assert (await client.delete(f"/admin/v1/catalog/routes/{route_id}")).status_code == 204
    detail = await client.get(f"/admin/v1/catalog/models/{mid}")
    assert detail.status_code == 200
    assert detail.json()["enabled"] is True
    assert detail.json()["routes"] == []
    assert await _money_decisions(mark) == []
