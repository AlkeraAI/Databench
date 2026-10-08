"""Org-admin BYOK model-provider schemas (in-flight HTTP shapes — plain BaseModel).

Secrets are WRITE-ONLY through this surface: requests may carry them, responses
never do — reads expose only presence flags and the masked ``secret_hint``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from alkera_core.egress import (
    METADATA_HOSTNAMES,
    CanonicalUrl,
    EgressPolicy,
    EgressRefusedError,
    canonicalize_url,
    embedded_addresses,
)

ProviderName = Literal["anthropic", "openai", "bedrock"]
VerifyStatus = Literal["ok", "invalid_key", "permission", "network"]

#: What a BYOK endpoint may be. Private ranges and loopback are deliberately IN:
#: a self-hosted install legitimately points BYOK at an in-VPC or same-host
#: inference proxy. The metadata addresses are never in, whatever else is.
_BYOK_POLICY = EgressPolicy(allow_private=True)


def _refused_address(url: CanonicalUrl) -> bool:
    """Whether the literal address in ``url`` is one no LLM endpoint ever has.

    Every IPv4 address the host carries inside it is judged, not just the one it
    is spelled as: IPv4-mapped, IPv4-compatible, NAT64, 6to4 and Teredo all
    deliver to the embedded target on a network that routes them, so each is a
    second spelling of the same destination.
    """
    address = url.address
    if address is None:
        return False
    if not _BYOK_POLICY.admits(url.host, address):
        return True  # a cloud metadata service, in whatever spelling
    return any(
        candidate.is_link_local
        or candidate.is_unspecified
        or candidate.is_multicast
        or candidate.is_reserved
        for candidate in embedded_addresses(address)
    )


def validate_provider_base_url(value: str) -> str:
    """Return ``value`` if it is safe to use as an LLM upstream, else raise.

    A BYOK ``base_url`` becomes the host the gateway POSTs a chat turn to, and
    the response body is streamed back to the caller verbatim -- so an
    unvalidated one is a full-READ server-side request forgery, not a blind one.
    An org admin is an ordinary employee role, not an infrastructure operator,
    and must not be able to point the deployment's own egress at its cloud
    metadata service and read back the instance role's credentials.

    The URL is read by :mod:`alkera_core.egress` — the same parser, the same
    address table and the same metadata list that guard every other fetch of a
    URL somebody else supplied. That matters more than the individual rules: a
    validator with its own parser is a validator that can disagree with the
    client about which bytes are the host, which is how a refusal gets stepped
    around. So this function decides only the POLICY the shared judge cannot know
    — what a BYOK endpoint is allowed to be — and the reading is not its own.

    What is refused, and why each matters:

    - anything but ``http``/``https``, a missing host, embedded credentials
      (``user:pass@``), a backslash, whitespace or a control character anywhere,
      a percent-encoded authority, an invalid port -- every one of those is a
      byte two parsers read differently, or an authenticator the operator never
      configured;
    - a query or a fragment -- the transport appends ``/v1/messages`` to this
      string, so either one silently re-writes the path that gets requested;
    - a link-local, unspecified, multicast or reserved address in ANY spelling --
      the canonical dotted quad, the legacy decimal/hex/octal ``inet_aton`` forms
      the resolver also accepts, and the IPv6 encodings that carry one of those
      inside them -- plus a cloud metadata address or hostname: the
      cloud-credential escalation above.

    Deliberately NOT refused: ordinary private ranges (10/8, 172.16/12,
    192.168/16) and ``localhost``. A self-hosted install legitimately points
    BYOK at an in-VPC or same-host inference proxy, and refusing those would
    break the supported deployment.

    This validates a STRING, never a resolved address, so a name whose record
    points at a refused address -- ``169.254.169.254.nip.io``, or one that only
    answers that way after the request is accepted -- passes here. It is refused
    at the socket instead: the gateway dials a BYOK endpoint through a client
    that resolves and vets the name itself and PINS the connection to the address
    it vetted, so what this function admits is bounded by what that guard dials.
    """
    url = value.strip()
    if not url:
        raise ValueError("The base URL must not be blank")
    if "#" in url:
        # Dropped rather than refused by the shared parser (a fragment is never
        # sent), but the transport appends to this string, so silence would move
        # the path that gets requested.
        raise ValueError("The base URL must not carry a query string or fragment")
    try:
        canonical = canonicalize_url(url)
    except EgressRefusedError as exc:
        raise ValueError(f"The base URL is not usable as a provider endpoint: {exc}") from None
    if canonical.query is not None:
        raise ValueError("The base URL must not carry a query string or fragment")
    if canonical.host in METADATA_HOSTNAMES or _refused_address(canonical):
        raise ValueError("That host is not a valid provider endpoint")
    return url


class ModelProviderRead(BaseModel):
    """One provider card. NEVER carries a secret — only presence + a hint."""

    provider: ProviderName
    configured: bool
    enabled: bool
    # An encrypted secret is stored (False for a Bedrock "iam"-mode config,
    # which is credential-less by design).
    has_credentials: bool
    secret_hint: str | None
    base_url: str | None
    openai_organization_id: str | None = None
    bedrock_region: str | None = None
    bedrock_auth_mode: Literal["iam", "access_key"] | None = None
    # An INSTANCE-level env key exists for this provider — informational (on a
    # split deployment only the gateway may hold env keys; copy hedges).
    env_fallback: bool
    last_verified_at: datetime | None
    last_verified_status: VerifyStatus | None
    updated_at: datetime | None


class ModelProvidersResponse(BaseModel):
    # Always all three providers, stable order — unconfigured ones included so
    # the page renders a complete picture.
    providers: list[ModelProviderRead]


class ModelProviderUpdateRequest(BaseModel):
    """Upsert one provider's config. Secret fields omitted/blank KEEP the stored
    value; changing ``base_url`` requires re-entering the key (anti-exfiltration
    — a stored valid key must never be silently repointed at a new endpoint)."""

    enabled: bool = True
    api_key: str | None = Field(default=None, max_length=2048)
    base_url: str | None = Field(default=None, max_length=1024)
    openai_organization_id: str | None = Field(default=None, max_length=256)
    bedrock_region: str | None = Field(default=None, max_length=64)
    bedrock_auth_mode: Literal["iam", "access_key"] | None = None
    aws_access_key_id: str | None = Field(default=None, max_length=256)
    aws_secret_access_key: str | None = Field(default=None, max_length=512)

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return validate_provider_base_url(value)


class ModelProviderTestResult(BaseModel):
    ok: bool
    status: VerifyStatus | Literal["not_configured"]
    # Actionable and secret-free, e.g. "the API key was rejected (401)".
    detail: str | None
    tested_at: datetime
