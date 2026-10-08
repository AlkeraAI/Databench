"""Signed entitlement tokens: mint/parse round-trip, every rejection class,
expiry/grace boundaries, the fail-closed shell, and the boot-status log line.

The parse is cached per process (`get_entitlements`), so an autouse fixture
clears the cache around every test; expiry is evaluated per call, driven ACROSS
day boundaries with freezegun.
"""

from __future__ import annotations

import sys
from datetime import UTC, date, datetime
from typing import Any

import pytest
from alkera_core import entitlements as ent
from alkera_core.config import settings
from alkera_core.entitlements import (
    EntitlementError,
    Entitlements,
    Feature,
    SignedLicense,
    entitled_feature_names,
    generate_keypair,
    get_entitlements,
    has_feature,
    is_licensed,
    license_authority,
    mint_entitlement_token,
    parse_entitlement_token,
)
from freezegun import freeze_time

PRIV, PUB = generate_keypair()
KEYS = {"alk1": PUB}


def mint(
    *,
    customer: str = "acme-corp",
    features: Feature = Feature.BYOK,
    expires_on: date = date(2099, 1, 1),
    serial: int = 42,
    signing_key: str = PRIV,
) -> str:
    return mint_entitlement_token(
        customer=customer,
        features=features,
        expires_on=expires_on,
        serial=serial,
        signing_key_b64=signing_key,
    )


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> Any:
    get_entitlements.cache_clear()
    monkeypatch.setattr(settings, "alkera_entitlements", None)
    monkeypatch.setattr(settings, "alkera_entitlements_public_key", None)
    monkeypatch.setattr(settings, "app_env", "local")
    yield
    get_entitlements.cache_clear()


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def info(self, event: str, **kw: Any) -> None:
        self.calls.append(("info", event, kw))

    def warning(self, event: str, **kw: Any) -> None:
        self.calls.append(("warning", event, kw))

    def error(self, event: str, **kw: Any) -> None:
        self.calls.append(("error", event, kw))


@pytest.fixture
def rec(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    recorder = _Recorder()
    monkeypatch.setattr(ent, "log", recorder)
    return recorder


# --- round trip ----------------------------------------------------------------


def test_mint_parse_round_trip() -> None:
    token = mint(customer="acme-corp", expires_on=date(2027, 6, 30), serial=7)
    parsed = parse_entitlement_token(token, public_keys=KEYS)
    assert parsed.parse_status == "ok"
    assert parsed.customer == "acme-corp"
    assert parsed.features == Feature.BYOK
    assert parsed.expires_on == date(2027, 6, 30)
    assert parsed.serial == 7
    assert parsed.feature_names() == ["byok"]


def test_token_shape_is_prefixed_and_three_segments() -> None:
    token = mint()
    assert token.startswith("alk1.")
    assert token.count(".") == 2


def test_mint_rejects_malformed_signing_key() -> None:
    with pytest.raises(EntitlementError):
        mint(signing_key="too-short")


# --- rejection classes ----------------------------------------------------------

_VALID = mint()
_P0, _P1, _P2 = _VALID.split(".")
_OTHER_PRIV, _OTHER_PUB = generate_keypair()


def _payload_b64(payload: bytes) -> str:
    return ent._b64url_encode(payload)


_TAMPERED_PAYLOAD = _payload_b64(b'{"c":"evil","e":"2099-01-01","f":255,"n":1}')


@pytest.mark.parametrize(
    "token",
    [
        pytest.param("", id="empty"),
        pytest.param("   ", id="whitespace"),
        pytest.param(_VALID.replace("alk1", "alk0"), id="unknown-version"),
        pytest.param(_VALID.replace("alk1", "ALK1"), id="uppercased-version"),
        pytest.param(f"{_P0}.{_P1}", id="two-segments"),
        pytest.param(f"{_VALID}.extra", id="four-segments"),
        pytest.param(f"alk1.!!not-b64!!.{_P2}", id="payload-not-b64url"),
        pytest.param(f"alk1.{_P1}.!!not-b64!!", id="sig-not-b64url"),
        pytest.param(f"alk1.{_payload_b64(b'not json')}.{_P2}", id="payload-not-json"),
        pytest.param(f"alk1.{_payload_b64(b'[1,2]')}.{_P2}", id="payload-json-array"),
        pytest.param(f"alk1.{_P1}.{_P2[:-8]}", id="truncated-sig"),
        pytest.param(
            f"alk1.{_TAMPERED_PAYLOAD}.{_P2}",
            id="tampered-payload-original-sig",
        ),
        pytest.param(mint(signing_key=_OTHER_PRIV), id="sig-from-foreign-keypair"),
    ],
)
def test_structural_and_crypto_rejections(token: str) -> None:
    with pytest.raises(EntitlementError):
        parse_entitlement_token(token, public_keys=KEYS)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("c", None, id="missing-customer"),
        pytest.param("c", "", id="empty-customer"),
        pytest.param("c", 3, id="customer-not-str"),
        pytest.param("f", None, id="missing-features"),
        pytest.param("f", "1", id="features-str"),
        pytest.param("f", True, id="features-bool"),
        pytest.param("f", -1, id="features-negative"),
        pytest.param("e", None, id="missing-expiry"),
        pytest.param("e", 20270630, id="expiry-not-str"),
        pytest.param("e", "not-a-date", id="expiry-not-iso"),
        pytest.param("n", None, id="missing-serial"),
        pytest.param("n", "42", id="serial-str"),
        pytest.param("n", False, id="serial-bool"),
    ],
)
def test_payload_field_rejections(field: str, value: Any) -> None:
    """Re-sign a doctored payload with the REAL key: only field validation can
    reject it (the signature is genuine)."""
    import json

    payload: dict[str, Any] = {"c": "acme", "e": "2099-01-01", "f": 1, "n": 1}
    if value is None:
        del payload[field]
    else:
        payload[field] = value
    payload_b64 = ent._b64url_encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    from cryptography.hazmat.primitives.asymmetric import ed25519

    seed = ent._b64url_decode(PRIV)
    sig = ed25519.Ed25519PrivateKey.from_private_bytes(seed).sign(
        f"alk1.{payload_b64}".encode("ascii")
    )
    token = f"alk1.{payload_b64}.{ent._b64url_encode(sig)}"
    with pytest.raises(EntitlementError):
        parse_entitlement_token(token, public_keys=KEYS)


def test_invalid_token_yields_invalid_state_via_shell(
    monkeypatch: pytest.MonkeyPatch, rec: _Recorder
) -> None:
    monkeypatch.setattr(settings, "alkera_entitlements", "alk1.garbage.token")
    monkeypatch.setattr(settings, "alkera_entitlements_public_key", PUB)
    assert get_entitlements().state() == "invalid"
    assert has_feature(Feature.BYOK) is False
    assert entitled_feature_names() == []
    assert any(event == "entitlements.token_invalid" for _, event, _ in rec.calls)


# --- expiry / grace boundaries ---------------------------------------------------


def _entitled(monkeypatch: pytest.MonkeyPatch, token: str) -> None:
    monkeypatch.setattr(settings, "alkera_entitlements", token)
    monkeypatch.setattr(settings, "alkera_entitlements_public_key", PUB)
    get_entitlements.cache_clear()


def test_expiry_and_grace_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    _entitled(monkeypatch, mint(expires_on=date(2026, 7, 31)))
    with freeze_time("2026-07-31 23:59:59") as frozen:
        assert get_entitlements().state() == "valid"
        assert has_feature(Feature.BYOK) is True

        frozen.move_to("2026-08-01 00:00:01")  # day 1 past expiry -> grace, still ON
        assert get_entitlements().state() == "grace"
        assert has_feature(Feature.BYOK) is True
        assert entitled_feature_names() == ["byok"]

        frozen.move_to("2026-08-30 12:00:00")  # day 30 -> last grace day
        assert get_entitlements().state() == "grace"
        assert has_feature(Feature.BYOK) is True

        frozen.move_to("2026-08-31 00:00:01")  # day 31 -> expired, OFF
        assert get_entitlements().state() == "expired"
        assert has_feature(Feature.BYOK) is False
        assert entitled_feature_names() == []


def test_state_flips_without_reparse(monkeypatch: pytest.MonkeyPatch) -> None:
    """The parse is cached but the clock is live — the same cached object
    reports different states across time."""
    _entitled(monkeypatch, mint(expires_on=date(2026, 7, 31)))
    with freeze_time("2026-07-01"):
        first = get_entitlements()
        assert first.state() == "valid"
    with freeze_time("2026-12-01"):
        assert get_entitlements() is first  # cached — no reparse
        assert first.state() == "expired"


def test_grace_until() -> None:
    parsed = parse_entitlement_token(mint(expires_on=date(2026, 7, 31)), public_keys=KEYS)
    assert parsed.grace_until() == date(2026, 8, 30)


# --- bitmask semantics -----------------------------------------------------------


def test_multi_bit_mask_with_unknown_bits_still_grants_byok() -> None:
    token = mint(features=Feature(0b101))  # BYOK + an unknown future bit
    parsed = parse_entitlement_token(token, public_keys=KEYS)
    assert parsed.has(Feature.BYOK, now=datetime(2026, 1, 1, tzinfo=UTC)) is True
    assert parsed.feature_names() == ["byok"]  # unknown bit carried but inert


@pytest.mark.parametrize(
    ("mask", "expected"),
    [
        pytest.param(0, False, id="zero-mask"),
        pytest.param(0b10, False, id="only-unknown-bit"),
        pytest.param(0b1, True, id="byok-bit"),
    ],
)
def test_mask_grants_exactly_its_bits(mask: int, expected: bool) -> None:
    parsed = parse_entitlement_token(mint(features=Feature(mask)), public_keys=KEYS)
    assert parsed.has(Feature.BYOK, now=datetime(2026, 1, 1, tzinfo=UTC)) is expected


# --- the fail-closed shell -------------------------------------------------------


def test_absent_token_is_unentitled(rec: _Recorder) -> None:
    assert get_entitlements() == Entitlements.absent()
    assert get_entitlements().state() == "absent"
    assert has_feature(Feature.BYOK) is False


def test_parse_is_cached_until_cache_clear(monkeypatch: pytest.MonkeyPatch) -> None:
    _entitled(monkeypatch, mint())
    assert has_feature(Feature.BYOK) is True
    monkeypatch.setattr(settings, "alkera_entitlements", "alk1.now.garbage")
    assert has_feature(Feature.BYOK) is True  # still the cached parse
    get_entitlements.cache_clear()
    assert has_feature(Feature.BYOK) is False


def test_pubkey_override_honored_outside_production(monkeypatch: pytest.MonkeyPatch) -> None:
    _entitled(monkeypatch, mint())
    monkeypatch.setattr(settings, "app_env", "staging")
    get_entitlements.cache_clear()
    assert has_feature(Feature.BYOK) is True


def test_pubkey_override_ignored_in_production(
    monkeypatch: pytest.MonkeyPatch, rec: _Recorder
) -> None:
    """In production the override is dead config: verification falls back to the
    shipped constant (a placeholder here), so the token reads invalid — and one
    warning names the ignored override."""
    _entitled(monkeypatch, mint())
    monkeypatch.setattr(settings, "app_env", "production")
    get_entitlements.cache_clear()
    assert has_feature(Feature.BYOK) is False
    assert get_entitlements().state() == "invalid"
    assert any(event == "entitlements.pubkey_override_ignored" for _, event, _ in rec.calls)


def test_pubkey_override_dead_in_a_running_deployment(
    monkeypatch: pytest.MonkeyPatch, rec: _Recorder
) -> None:
    """The override is a TEST-ONLY seam. A running backend/gateway/worker never has
    pytest imported, so even outside production the override is ignored — closing the
    revenue bypass where a customer flips APP_ENV=staging, supplies their own public
    key, and self-signs a BYOK token. Simulate a deployment by dropping ``pytest``
    from ``sys.modules``."""
    _entitled(monkeypatch, mint())
    monkeypatch.setattr(settings, "app_env", "staging")  # non-production, still dead
    monkeypatch.delitem(sys.modules, "pytest", raising=False)
    assert ent._dev_override_honored() is False
    get_entitlements.cache_clear()
    assert has_feature(Feature.BYOK) is False
    assert get_entitlements().state() == "invalid"
    assert any(event == "entitlements.pubkey_override_ignored" for _, event, _ in rec.calls)


def test_byok_active_requires_self_hosted_direct_and_entitled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _entitled(monkeypatch, mint())
    monkeypatch.setattr(settings, "self_hosted", True)
    monkeypatch.setattr(settings, "gateway_upstream", "direct")
    assert ent.byok_active() is True
    monkeypatch.setattr(settings, "gateway_upstream", "proxy")
    assert ent.byok_active() is False  # entitlement dormant under proxy upstream
    monkeypatch.setattr(settings, "gateway_upstream", "direct")
    monkeypatch.setattr(settings, "self_hosted", False)
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_x")
    monkeypatch.setattr(settings, "stripe_webhook_secret", "whsec_x")
    assert ent.byok_active() is False  # SaaS never


# --- the boot log line -----------------------------------------------------------


@pytest.mark.parametrize(
    ("setup", "expected_level"),
    [
        pytest.param("absent", "info", id="absent-info"),
        pytest.param("valid", "info", id="valid-info"),
        pytest.param("invalid", "warning", id="invalid-warning"),
        pytest.param("grace", "warning", id="grace-warning"),
        pytest.param("expired", "error", id="expired-error"),
    ],
)
def test_status_log_level_per_state(
    setup: str, expected_level: str, monkeypatch: pytest.MonkeyPatch, rec: _Recorder
) -> None:
    if setup == "valid":
        _entitled(monkeypatch, mint())
    elif setup == "invalid":
        monkeypatch.setattr(settings, "alkera_entitlements", "not-a-token")
    elif setup in ("grace", "expired"):
        _entitled(monkeypatch, mint(expires_on=date(2026, 1, 31)))

    at = {"grace": "2026-02-10", "expired": "2026-06-01"}.get(setup, "2026-01-01")
    with freeze_time(at):
        ent.log_entitlements_status("gateway")

    status_calls = [(lvl, kw) for lvl, event, kw in rec.calls if event == "entitlements.status"]
    assert len(status_calls) == 1
    level, kw = status_calls[0]
    assert level == expected_level
    assert kw["component"] == "gateway"
    assert kw["state"] == setup
    if setup == "grace":
        assert kw["grace_until"] == "2026-03-02"


def test_the_product_licenses_its_features_through_the_signed_token() -> None:
    # The suites run the product, which installs the signed license: with no
    # token, a licensed feature is off.
    assert is_licensed()
    assert isinstance(license_authority(), SignedLicense)
    assert has_feature(Feature.BYOK) is False
    assert entitled_feature_names() == []
