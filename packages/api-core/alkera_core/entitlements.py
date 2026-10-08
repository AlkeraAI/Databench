"""Signed, offline-verified feature entitlements.

Some capabilities (the first: LLM BYOK on a self-hosted deployment) are enabled
per-customer by a compact Ed25519-signed token Alkera hands the customer 1:1,
set in the deliberately-undocumented ``ALKERA_ENTITLEMENTS`` env var:

    alk1.<base64url(payload)>.<base64url(signature)>

    payload = {"c": "acme-corp", "f": 1, "e": "2027-06-30", "n": 42}
      c  customer slug (attribution if a token ever leaks)
      f  feature bitmask (bit 0 = BYOK; bits are assigned once and never reused)
      e  expiry DATE (inclusive; valid through 23:59:59 UTC of that day)
      n  issuance serial (from the SaaS-side ledger)

The signature covers the ASCII bytes of ``"<version>." + base64url(payload)``
so a token can never be replayed under a different scheme version. Verification
is fully offline against a public key that ships in this module — air-gapped
deployments need no egress and there is no phone-home. This is a commercial
honesty gate, not DRM: a customer who patches their own binary can bypass it;
the signature's job is to make values non-guessable, attributable, and
expiring. The contract does the rest.

Failure posture: **fail closed and quiet.** A missing / malformed / tampered /
foreign token never crashes a customer boot — the deployment simply runs
unentitled, with one structured log line saying so. Expiry gets a grace window
(feature stays ON, warnings get loud) so a lapsed renewal never hard-drops a
production workflow mid-quarter.
"""

from __future__ import annotations

import base64
import binascii
import json
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import IntFlag
from functools import lru_cache
from typing import Literal, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import ed25519

from alkera_core.config import settings
from alkera_core.extensions import Extension, ExtensionError, ExtensionPoint
from alkera_core.logging import get_logger

log = get_logger(__name__)


class Feature(IntFlag):
    """Entitlement feature bits. Bits are append-only: NEVER reorder or reuse a
    bit — issued tokens embed the raw mask and verify offline forever."""

    BYOK = 1  # bit 0 — self-hosted direct-to-provider keys; disables Alkera token billing


# Days past ``expires_on`` during which the feature stays ON (with loud daily
# warnings) so a slow renewal never hard-drops a production deployment.
ENTITLEMENT_GRACE = timedelta(days=30)

# version prefix -> base64url raw 32-byte Ed25519 public key. THE production key
# ships here, in code, reviewed like code. Until the one-time key ceremony runs
# this placeholder makes every token invalid — fail-closed, safe to ship.
# Rotation: add "alk2" + its key in a release and mint new tokens as alk2; old
# alk1 tokens keep verifying until the entry is deleted in a later major.
_PUBLIC_KEYS: dict[str, str] = {
    "alk1": "4I0mr-qmjA8wRoDl7n-Y-6rA7zaip76bIVHLr7r503U",
}

EntitlementState = Literal["valid", "grace", "expired", "invalid", "absent"]


class EntitlementError(ValueError):
    """A token failed structural or cryptographic validation.

    Raised only by the pure parse/mint functions — the settings-facing shell
    (:func:`get_entitlements`) converts every failure into an unentitled state
    instead of raising.
    """


@dataclass(frozen=True)
class Entitlements:
    """The parsed (or absent/invalid) entitlement grant for this process."""

    parse_status: Literal["ok", "absent", "invalid"]
    customer: str | None
    features: Feature
    expires_on: date | None
    serial: int | None

    @classmethod
    def absent(cls) -> Entitlements:
        return cls("absent", None, Feature(0), None, None)

    @classmethod
    def invalid(cls) -> Entitlements:
        return cls("invalid", None, Feature(0), None, None)

    def state(self, now: datetime | None = None) -> EntitlementState:
        """Expiry/grace are evaluated at CALL time (the parse is cached, the
        clock is not) — a long-running process flips valid → grace → expired
        without a restart."""
        if self.parse_status == "absent":
            return "absent"
        if self.parse_status == "invalid" or self.expires_on is None:
            return "invalid"
        today = (now or datetime.now(UTC)).astimezone(UTC).date()
        if today <= self.expires_on:
            return "valid"
        if today <= self.expires_on + ENTITLEMENT_GRACE:
            return "grace"
        return "expired"

    def grace_until(self) -> date | None:
        if self.expires_on is None:
            return None
        return self.expires_on + ENTITLEMENT_GRACE

    def has(self, feature: Feature, now: datetime | None = None) -> bool:
        """True only while the grant is ``valid`` or in ``grace`` — fail-closed
        in every other state."""
        if self.state(now) not in ("valid", "grace"):
            return False
        return (self.features & feature) == feature

    def feature_names(self) -> list[str]:
        """The KNOWN feature names in the mask (unknown bits from a newer
        issuer are carried but inert)."""
        return [f.name.lower() for f in Feature if f.name and f in self.features]


# --- pure core (no settings, no I/O) -----------------------------------------

_NAME_TO_FEATURE: dict[str, Feature] = {f.name.lower(): f for f in Feature if f.name}


def feature_mask_from_names(names: Iterable[str]) -> Feature:
    """Feature names (``["byok"]``) → the bitmask to mint. Raises ``ValueError``
    on an unknown name — the mint surface must never guess a bit."""
    mask = Feature(0)
    for name in names:
        feature = _NAME_TO_FEATURE.get(name)
        if feature is None:
            raise ValueError(f"unknown feature name {name!r}")
        mask |= feature
    return mask


def feature_names_for_mask(mask: int) -> list[str]:
    """The KNOWN feature names in a raw mask (unknown bits are inert)."""
    flags = Feature(mask)
    return [f.name.lower() for f in Feature if f.name and f in flags]


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    padded = text + "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _decode_key(b64: str, *, label: str) -> bytes:
    try:
        raw = _b64url_decode(b64)
    except (ValueError, binascii.Error) as exc:
        raise EntitlementError(f"{label} is not valid base64url") from exc
    if len(raw) != 32:
        raise EntitlementError(f"{label} must decode to exactly 32 bytes")
    return raw


def generate_keypair() -> tuple[str, str]:
    """A fresh Ed25519 keypair as ``(private_seed_b64url, public_key_b64url)``
    — both the raw 32-byte forms, base64url-encoded without padding."""
    private = ed25519.Ed25519PrivateKey.generate()
    seed = private.private_bytes_raw()
    public = private.public_key().public_bytes_raw()
    return _b64url_encode(seed), _b64url_encode(public)


def mint_entitlement_token(
    *,
    customer: str,
    features: Feature,
    expires_on: date,
    serial: int,
    signing_key_b64: str,
    version: str = "alk1",
) -> str:
    """Sign a grant. SaaS-side only — the private seed never ships to customers."""
    seed = _decode_key(signing_key_b64, label="signing key")
    payload = {"c": customer, "e": expires_on.isoformat(), "f": int(features), "n": serial}
    payload_b64 = _b64url_encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    message = f"{version}.{payload_b64}".encode("ascii")
    signature = ed25519.Ed25519PrivateKey.from_private_bytes(seed).sign(message)
    return f"{version}.{payload_b64}.{_b64url_encode(signature)}"


def parse_entitlement_token(token: str, *, public_keys: dict[str, str]) -> Entitlements:
    """Strict parse + verify: raises :class:`EntitlementError` on ANY problem.

    ``public_keys`` maps version prefix -> base64url raw public key (the
    rotation table). The signed message includes the version prefix, so a
    signature can't be replayed across scheme versions.
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise EntitlementError("token must have exactly 3 dot-separated segments")
    version, payload_b64, sig_b64 = parts
    key_b64 = public_keys.get(version)
    if key_b64 is None:
        raise EntitlementError(f"unknown entitlement token version {version!r}")
    public_raw = _decode_key(key_b64, label=f"public key for {version!r}")
    try:
        signature = _b64url_decode(sig_b64)
        payload_raw = _b64url_decode(payload_b64)
    except (ValueError, binascii.Error) as exc:
        raise EntitlementError("token segments are not valid base64url") from exc
    try:
        ed25519.Ed25519PublicKey.from_public_bytes(public_raw).verify(
            signature, f"{version}.{payload_b64}".encode("ascii")
        )
    except (InvalidSignature, ValueError, TypeError) as exc:
        # InvalidSignature for a wrong signature; ValueError/TypeError for
        # structurally-impossible input (e.g. a truncated signature) — all the
        # same outcome: not a token we minted.
        raise EntitlementError("signature verification failed") from exc

    try:
        payload = json.loads(payload_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EntitlementError("payload is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise EntitlementError("payload must be a JSON object")

    customer = payload.get("c")
    features = payload.get("f")
    expires = payload.get("e")
    serial = payload.get("n")
    if not isinstance(customer, str) or not customer:
        raise EntitlementError("payload field 'c' (customer) must be a non-empty string")
    if not isinstance(features, int) or isinstance(features, bool) or features < 0:
        raise EntitlementError("payload field 'f' (features) must be a non-negative integer")
    if not isinstance(serial, int) or isinstance(serial, bool):
        raise EntitlementError("payload field 'n' (serial) must be an integer")
    if not isinstance(expires, str):
        raise EntitlementError("payload field 'e' (expiry) must be an ISO date string")
    try:
        expires_on = date.fromisoformat(expires)
    except ValueError as exc:
        raise EntitlementError("payload field 'e' (expiry) is not a valid ISO date") from exc

    return Entitlements(
        parse_status="ok",
        customer=customer,
        # IntFlag keeps unknown bits (a newer issuer may mint bit N this build
        # doesn't know); known-feature checks are unaffected.
        features=Feature(features),
        expires_on=expires_on,
        serial=serial,
    )


# --- settings-facing shell (what the codebase calls) --------------------------


def _dev_override_honored() -> bool:
    """Whether the ``ALKERA_ENTITLEMENTS_PUBLIC_KEY`` dev/test override may replace
    the shipped verification key.

    It is honored ONLY under pytest (and never in production). ``APP_ENV`` is
    customer-controlled, so gating on ``not is_production`` alone let a self-hosted
    customer set ``APP_ENV=staging``, supply their own public key, and self-sign a
    token with the module's own mint helper — a revenue bypass with no binary patch.
    A running backend / gateway / worker never has ``pytest`` imported, so this
    makes the override a true test seam while the customer-facing gate stays pinned
    to the shipped ``_PUBLIC_KEYS`` constant. (Local manual BYOK testing edits that
    constant directly — the same source-edit bar a customer would face.)
    """
    return "pytest" in sys.modules and not settings.is_production


def _public_keys() -> dict[str, str]:
    """The rotation table, with the dev/test override applied ONLY under pytest."""
    keys = dict(_PUBLIC_KEYS)
    if settings.alkera_entitlements_public_key and _dev_override_honored():
        keys["alk1"] = settings.alkera_entitlements_public_key
    return keys


@lru_cache(maxsize=1)
def get_entitlements() -> Entitlements:
    """Parse ``ALKERA_ENTITLEMENTS`` once per process. NEVER raises: absent →
    unentitled, any parse/verify failure → unentitled (one WARNING log).

    The cache holds the *parse*; expiry/grace evaluate against the clock at
    call time. Tests reset with ``get_entitlements.cache_clear()``.
    """
    token = (settings.alkera_entitlements or "").strip()
    if settings.alkera_entitlements_public_key and not _dev_override_honored():
        log.warning(
            "entitlements.pubkey_override_ignored",
            detail="ALKERA_ENTITLEMENTS_PUBLIC_KEY is a test-only seam and has no "
            "effect on a running deployment",
        )
    if not token:
        return Entitlements.absent()
    try:
        return parse_entitlement_token(token, public_keys=_public_keys())
    except EntitlementError as exc:
        log.warning("entitlements.token_invalid", reason=str(exc))
        return Entitlements.invalid()


class LicenseAuthority(Protocol):
    """Who decides which licensed features a deployment has."""

    def has(self, feature: Feature, now: datetime | None = None) -> bool: ...

    def feature_names(self, now: datetime | None = None) -> list[str]: ...


class SignedLicense:
    """The signed ``ALKERA_ENTITLEMENTS`` token: a feature is available only
    while a valid (or in-grace) token grants it, and only on a self-hosted
    install. The product installs it (:data:`SIGNED_LICENSE`)."""

    def has(self, feature: Feature, now: datetime | None = None) -> bool:
        return get_entitlements().has(feature, now)

    def feature_names(self, now: datetime | None = None) -> list[str]:
        ent = get_entitlements()
        if ent.state(now) not in ("valid", "grace"):
            return []
        return ent.feature_names()


#: The deployment's license authority. With none registered (an open deployment,
#: its operator's own install, which nobody licenses) every feature is
#: available. At most one registers.
LICENSE_AUTHORITY: ExtensionPoint[LicenseAuthority] = ExtensionPoint("license_authority")

SIGNED_LICENSE = Extension(
    name="alkera.signed_license", install=lambda: LICENSE_AUTHORITY.register(SignedLicense())
)


def license_authority() -> LicenseAuthority | None:
    registered = LICENSE_AUTHORITY.items()
    if len(registered) > 1:
        raise ExtensionError("more than one license authority is registered")
    return registered[0] if registered else None


def is_licensed() -> bool:
    """Whether anything licenses this deployment's features (the product)."""
    return license_authority() is not None


def has_feature(feature: Feature, now: datetime | None = None) -> bool:
    """THE entitlement check the rest of the codebase calls. Under a license
    authority it is that authority's answer (cheap, never raises, fail-closed
    on any doubt); with none, every feature is available."""
    authority = license_authority()
    return True if authority is None else authority.has(feature, now)


def entitled_feature_names(now: datetime | None = None) -> list[str]:
    """Currently-available feature names (``["byok"]``), for API exposure."""
    authority = license_authority()
    if authority is None:
        return [f.name.lower() for f in Feature if f.name]
    return authority.feature_names(now)


def byok_active() -> bool:
    """BYOK is IN EFFECT: self-hosted, direct upstream, and entitled.

    The entitlement is a *capability*, not a mode switch — a BYOK-entitled
    deployment running ``GATEWAY_UPSTREAM=proxy`` is billed-through-Alkera and
    the entitlement stays dormant.
    """
    return settings.is_self_hosted and not settings.gateway_proxy_mode and has_feature(Feature.BYOK)


BillingMode = Literal["stripe", "managed", "byok"]


def billing_mode() -> BillingMode:
    """The deployment's commercial posture, for client branching:

    - ``stripe``  — Alkera SaaS: plans/top-ups via Stripe, obfuscated usage bar.
    - ``managed`` — self-hosted billed-through-Alkera: covered plan + postpaid
      additional; the org admin allocates, no Stripe surfaces.
    - ``byok``    — self-hosted on customer keys: raw local usage at provider
      list cost, spend controlled by limits, no plans/top-ups anywhere.
    """
    if byok_active():
        return "byok"
    if settings.stripe_configured:
        return "stripe"
    return "managed"


def log_entitlements_status(component: str) -> None:
    """The single structured boot/status line every long-lived component emits.

    Level by state: valid/absent → INFO, grace/invalid → WARNING (grace adds
    ``grace_until``), expired → ERROR.
    """
    ent = get_entitlements()
    state = ent.state()
    fields = {
        "component": component,
        "state": state,
        "customer": ent.customer,
        "features": ent.feature_names(),
        "expires_on": ent.expires_on.isoformat() if ent.expires_on else None,
        "serial": ent.serial,
    }
    if state in ("valid", "absent"):
        log.info("entitlements.status", **fields)
    elif state == "grace":
        grace_until = ent.grace_until()
        log.warning(
            "entitlements.status",
            grace_until=grace_until.isoformat() if grace_until else None,
            **fields,
        )
    elif state == "expired":
        log.error("entitlements.status", **fields)
    else:  # invalid
        log.warning("entitlements.status", **fields)


__all__ = [
    "ENTITLEMENT_GRACE",
    "LICENSE_AUTHORITY",
    "SIGNED_LICENSE",
    "EntitlementError",
    "EntitlementState",
    "Entitlements",
    "Feature",
    "LicenseAuthority",
    "SignedLicense",
    "byok_active",
    "entitled_feature_names",
    "feature_mask_from_names",
    "feature_names_for_mask",
    "generate_keypair",
    "get_entitlements",
    "has_feature",
    "is_licensed",
    "license_authority",
    "log_entitlements_status",
    "mint_entitlement_token",
    "parse_entitlement_token",
]
