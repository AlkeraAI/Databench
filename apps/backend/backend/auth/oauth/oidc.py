"""Generic OpenID Connect (authorization-code) provider.

Google is just an instance of this with the Google issuer. Any future
enterprise OIDC IdP (Okta, Azure AD, Workspace, Auth0) is a new instance with a
different issuer + per-org client credentials — no new code path. The standard
OIDC claim names (`sub`, `email`, `email_verified`, `given_name`,
`family_name`) are mapped by default; pass a custom `claim_mapper` for an IdP
that names them differently.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
from alkera_core.egress import EgressPolicy
from alkera_core.http import async_client
from alkera_core.net import is_private_host
from authlib.jose import JsonWebToken
from authlib.jose.errors import JoseError

from backend.auth.oauth.base import OAuthError
from backend.auth.oauth.profile import FederatedProfile

_HTTP_TIMEOUT = 10.0


def _private_idp_allowed() -> bool:
    """Whether this deployment may dial an IdP the public internet cannot reach.

    A customer's own install legitimately runs Keycloak/ADFS inside its own
    network, and its org admin already owns that network; Alkera's SaaS must never
    dial its own VPC on a tenant's behalf, so it fails closed. Same deployment
    rule the connector probe applies to a warehouse address.
    """
    from alkera_core.config import settings

    return settings.is_self_hosted


def _idp_client() -> httpx.AsyncClient:
    """The client for every URL an issuer supplies or serves.

    :func:`_url_is_dialable` reads the NAME, which an org admin chooses; what the
    name resolves to is theirs to choose as well. So the dial itself is guarded:
    the host is resolved once, every address must be public unless this
    deployment may reach a private IdP, and the connection goes to the address
    that was checked — a record that changes after the check changes nothing.
    """
    return async_client(
        timeout=_HTTP_TIMEOUT,
        egress_policy=EgressPolicy.from_settings(allow_private=_private_idp_allowed()),
        egress_schemes=("https",),
    )


def _url_is_dialable(url: str) -> bool:
    """Shared shape + reachability rule for every URL this module fetches.

    Shape is absolute: ``https``, a host, no embedded credentials. Reachability is
    the part that depends on the deployment — see :func:`_private_idp_allowed`.
    An unparseable URL is never dialable.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        return False
    return _private_idp_allowed() or not is_private_host(url)


def issuer_is_dialable(issuer: str) -> bool:
    """Whether ``issuer`` is an OIDC issuer this deployment will contact.

    An issuer is org-admin-supplied and every request built from it — discovery,
    the token exchange (which carries the org's client secret), JWKS — is made BY
    THE SERVER, from inside its own network, and the discovery leg is reachable
    by an unauthenticated visitor hitting the org's SSO login URL. So the issuer
    must be an ``https`` origin with a host, no credentials, and no query/fragment
    (an issuer ending in ``?`` or ``#`` would otherwise swallow the
    ``/.well-known/openid-configuration`` suffix and turn discovery into a fetch
    of an arbitrary path). On Alkera's SaaS the host must additionally be publicly
    routable, so a tenant cannot aim the backend at Alkera's own network.
    """
    # The delimiters themselves, not the parsed parts: a trailing bare `?` or `#`
    # parses as an EMPTY query/fragment yet still swallows the suffix.
    if "?" in issuer or "#" in issuer:
        return False
    return _url_is_dialable(issuer)


def _endpoint_from_discovery(meta: dict[str, Any], key: str, *, what: str) -> str:
    """One endpoint out of an IdP-served discovery document, held to the same rule
    the issuer was.

    The document is untrusted input from the issuer, so the URLs it names get the
    same treatment as the issuer itself — otherwise a discovery response is a
    server-side request to any address the IdP names. A missing/blank entry is a
    refusal, not a ``KeyError``."""
    raw = meta.get(key)
    url = str(raw) if isinstance(raw, str) else ""
    if not _url_is_dialable(url):
        raise OAuthError(f"OIDC discovery returned an unusable {what}")
    return url


def normalize_groups(value: Any) -> tuple[str, ...]:
    """Coerce an IdP groups/roles claim into a tuple of names. Accepts a list of
    strings (the OIDC norm), a single string, or a space/comma-separated string
    (some IdPs flatten). Blanks are dropped; order + case are preserved."""
    # Cap the count so a misbehaving IdP can't flood the group→role lookup.
    cap = 1000
    if value is None:
        return ()
    if isinstance(value, str):
        parts = [p.strip() for p in value.replace(",", " ").split()]
        return tuple(p for p in parts if p)[:cap]
    if isinstance(value, (list, tuple)):
        return tuple(str(v).strip() for v in value if str(v).strip())[:cap]
    return ()


def _default_claim_mapper(provider: str, claims: dict[str, Any]) -> FederatedProfile:
    return FederatedProfile(
        provider=provider,
        subject=str(claims["sub"]),
        email=str(claims.get("email") or "").lower(),
        email_verified=bool(claims.get("email_verified", False)),
        first_name=str(claims.get("given_name") or ""),
        last_name=str(claims.get("family_name") or ""),
        # `groups` is the OIDC norm; `roles` is a common Azure AD / Keycloak alias.
        idp_groups=normalize_groups(claims.get("groups") or claims.get("roles")),
        raw=claims,
    )


class OidcProvider:
    """An OIDC authorization-code provider for a confidential client.

    SECURITY: a generic/per-org IdP is only authoritative for its own org's
    addresses. `oauth_service.resolve` auto-links by verified email globally —
    safe for Google, NOT for an arbitrary IdP. Before registering an instance of
    this beyond Google, org-scope or domain-restrict the auto-link (see the
    TRUST BOUNDARY note in `oauth_service.resolve`).
    """

    kind = "oidc"

    def __init__(
        self,
        *,
        key: str,
        issuer: str,
        client_id: str,
        client_secret: str,
        scopes: Sequence[str] = ("openid", "email", "profile"),
        extra_auth_params: dict[str, str] | None = None,
        accepted_issuers: Sequence[str] | None = None,
        claim_mapper: Callable[[str, dict[str, Any]], FederatedProfile] | None = None,
    ) -> None:
        self.key = key
        self._issuer = issuer.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._scopes = list(scopes)
        self._extra_auth_params = extra_auth_params or {}
        # An IdP's id_token `iss` may differ slightly from the discovery URL
        # base (e.g. Google issues both with and without scheme historically),
        # so accept an explicit allowlist when given.
        self._accepted_issuers = list(accepted_issuers or [issuer.rstrip("/")])
        self._claim_mapper = claim_mapper or _default_claim_mapper

    async def _discover(self) -> dict[str, Any]:
        # Re-checked here, not only where the connection is written: a row stored
        # before the guard existed (or by any other writer) must fail closed
        # rather than keep its reach into this network.
        if not issuer_is_dialable(self._issuer):
            raise OAuthError(f"{self.key}: issuer is not an address this deployment may dial")
        url = f"{self._issuer}/.well-known/openid-configuration"
        try:
            async with _idp_client() as client:
                resp = await client.get(url)
                resp.raise_for_status()
                return dict(resp.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise OAuthError(f"OIDC discovery failed for {self.key}: {exc}") from exc

    async def authorization_url(self, *, redirect_uri: str, state: str, nonce: str | None) -> str:
        meta = await self._discover()
        authorize = _endpoint_from_discovery(
            meta, "authorization_endpoint", what="authorization endpoint"
        )
        params = {
            "response_type": "code",
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(self._scopes),
            "state": state,
            **self._extra_auth_params,
        }
        if nonce:
            params["nonce"] = nonce
        return f"{authorize}?{urlencode(params)}"

    async def fetch_profile(
        self, *, code: str, redirect_uri: str, nonce: str | None
    ) -> FederatedProfile:
        meta = await self._discover()
        token_endpoint = _endpoint_from_discovery(meta, "token_endpoint", what="token endpoint")
        jwks_uri = _endpoint_from_discovery(meta, "jwks_uri", what="JWKS URI")
        token = await self._exchange(token_endpoint, code=code, redirect_uri=redirect_uri)
        id_token = token.get("id_token")
        if not id_token:
            raise OAuthError(f"{self.key}: token response missing id_token")
        jwks = await self._fetch_jwks(jwks_uri)
        claims = self._verify_id_token(id_token, jwks=jwks, nonce=nonce)
        return self._claim_mapper(self.key, claims)

    async def _exchange(
        self, token_endpoint: str, *, code: str, redirect_uri: str
    ) -> dict[str, Any]:
        try:
            async with _idp_client() as client:
                resp = await client.post(
                    token_endpoint,
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": redirect_uri,
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                    },
                    headers={"Accept": "application/json"},
                )
                resp.raise_for_status()
                return dict(resp.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise OAuthError(f"{self.key}: token exchange failed: {exc}") from exc

    async def _fetch_jwks(self, jwks_uri: str) -> dict[str, Any]:
        try:
            async with _idp_client() as client:
                resp = await client.get(jwks_uri)
                resp.raise_for_status()
                return dict(resp.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise OAuthError(f"{self.key}: JWKS fetch failed: {exc}") from exc

    def _verify_id_token(
        self, id_token: str, *, jwks: dict[str, Any], nonce: str | None
    ) -> dict[str, Any]:
        jws = JsonWebToken(["RS256", "ES256"])
        claims_options = {
            "iss": {"essential": True, "values": self._accepted_issuers},
            "aud": {"essential": True, "value": self._client_id},
            "exp": {"essential": True},
        }
        try:
            claims = jws.decode(id_token, key=jwks, claims_options=claims_options)
            claims.validate(leeway=60)
        except (JoseError, ValueError, KeyError) as exc:
            raise OAuthError(f"{self.key}: id_token verification failed: {exc}") from exc
        if nonce is not None and claims.get("nonce") != nonce:
            raise OAuthError(f"{self.key}: id_token nonce mismatch")
        return dict(claims)
