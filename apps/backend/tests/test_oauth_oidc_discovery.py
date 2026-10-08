"""OIDC discovery is a server-side fetch of an org-admin-supplied URL, and every
endpoint it returns is a second one. Both legs are pinned here: the issuer must be
an https origin this deployment may dial, and so must each endpoint the discovery
document names — otherwise a per-org IdP config turns Alkera's SaaS backend into a
request generator aimed at Alkera's own network (the SSO login route that triggers
discovery carries no auth).

"May dial" is deployment-shaped: a customer's own install reaches its own internal
IdP, the hosted service reaches public addresses only. Both shapes are pinned, in
both directions.

The wire is scripted through the module's `async_client` seam; nothing dials out.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from alkera_core.config import settings
from backend.auth.oauth import oidc
from backend.auth.oauth.base import OAuthError
from backend.auth.oauth.oidc import OidcProvider, issuer_is_dialable

# No module-level `asyncio` mark: the suite runs in asyncio AUTO mode, and this
# module mixes sync (classification) with async (wire) cases.

ISSUER = "https://idp.acme.example.com"
DISCOVERY = f"{ISSUER}/.well-known/openid-configuration"
INTERNAL_ISSUER = "https://sso.corp.internal/realms/acme"
INTERNAL_DISCOVERY = f"{INTERNAL_ISSUER}/.well-known/openid-configuration"


@pytest.fixture(autouse=True)
def _saas_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """These cases are about what Alkera's HOSTED service may dial, so pin that
    shape explicitly instead of inheriting it. The self-hosted carve-out is opted
    into per test (a later setattr wins)."""
    monkeypatch.setattr(settings, "self_hosted", False)


def _script(
    monkeypatch: pytest.MonkeyPatch, doc: dict[str, Any], *, at: str = DISCOVERY
) -> list[str]:
    """Serve `doc` at the discovery URL; record every URL the provider fetches."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if str(request.url) == at:
            return httpx.Response(200, json=doc)
        return httpx.Response(200, json={})

    def factory(**_kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(oidc, "async_client", factory)
    return seen


def _provider(issuer: str = ISSUER) -> OidcProvider:
    return OidcProvider(key="sso:test", issuer=issuer, client_id="cid", client_secret="s")


@pytest.mark.parametrize(
    ("issuer", "on_saas", "on_prem"),
    [
        pytest.param("https://accounts.google.com", True, True, id="public-origin"),
        pytest.param("https://login.microsoftonline.com/t/v2.0", True, True, id="public-with-path"),
        # Reachability — and ONLY reachability — depends on the deployment.
        pytest.param("https://127.0.0.1/idp", False, True, id="loopback"),
        pytest.param("https://10.0.3.17:8080/x", False, True, id="rfc1918"),
        pytest.param("https://169.254.170.2/creds", False, True, id="link-local"),
        pytest.param("https://sso.corp.internal", False, True, id="internal-suffix"),
        pytest.param("https://intranet", False, True, id="bare-host"),
        # Shape stays refused on BOTH — the carve-out is not a general relaxation.
        pytest.param("http://idp.acme.example.com", False, False, id="http"),
        pytest.param("http://sso.corp.internal", False, False, id="http-internal"),
        pytest.param("https://idp.acme.example.com?", False, False, id="trailing-query"),
        pytest.param("https://idp.acme.example.com#", False, False, id="trailing-fragment"),
        pytest.param("https://u:p@idp.acme.example.com", False, False, id="userinfo"),
        pytest.param("https://u:p@sso.corp.internal", False, False, id="userinfo-internal"),
        pytest.param("https://", False, False, id="no-host"),
        pytest.param("https://[::1/idp", False, False, id="unparseable"),
        pytest.param("", False, False, id="empty"),
    ],
)
def test_issuer_is_dialable_classification(
    monkeypatch: pytest.MonkeyPatch, issuer: str, on_saas: bool, on_prem: bool
) -> None:
    """Both deployment shapes in one case, so the carve-out cannot invert silently:
    a private IdP is configurable on a customer's own install and refused on the
    hosted service, while every SHAPE rule holds on both."""
    monkeypatch.setattr(settings, "self_hosted", False)
    assert issuer_is_dialable(issuer) is on_saas
    monkeypatch.setattr(settings, "self_hosted", True)
    assert issuer_is_dialable(issuer) is on_prem


async def test_a_private_issuer_is_refused_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _script(monkeypatch, {"authorization_endpoint": f"{ISSUER}/authorize"})
    with pytest.raises(OAuthError):
        await _provider("http://169.254.170.2/v2/credentials").authorization_url(
            redirect_uri="https://app.example/cb", state="st", nonce="n"
        )
    assert seen == []  # never dialed


@pytest.mark.parametrize(
    "endpoint",
    [
        pytest.param("http://idp.acme.example.com/authorize", id="http"),
        pytest.param("https://127.0.0.1/authorize", id="loopback"),
        pytest.param("https://vault.internal/authorize", id="internal"),
        pytest.param("", id="blank"),
        pytest.param(None, id="missing"),
    ],
)
async def test_a_discovery_document_naming_a_private_authorize_endpoint_is_refused(
    monkeypatch: pytest.MonkeyPatch, endpoint: str | None
) -> None:
    """The document is untrusted input from the issuer, so its URLs get the same
    treatment the issuer got. A missing key is a refusal, not a KeyError → 500."""
    doc = {} if endpoint is None else {"authorization_endpoint": endpoint}
    _script(monkeypatch, doc)
    with pytest.raises(OAuthError):
        await _provider().authorization_url(
            redirect_uri="https://app.example/cb", state="st", nonce="n"
        )


async def test_a_private_token_endpoint_is_never_sent_the_client_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _script(
        monkeypatch,
        {
            "authorization_endpoint": f"{ISSUER}/authorize",
            "token_endpoint": "http://10.0.3.17:8080/token",
            "jwks_uri": f"{ISSUER}/jwks",
        },
    )
    with pytest.raises(OAuthError):
        await _provider().fetch_profile(code="c", redirect_uri="https://app.example/cb", nonce="n")
    assert seen == [DISCOVERY]  # the exchange never left the building


async def test_a_private_jwks_uri_is_refused_too(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _script(
        monkeypatch,
        {
            "authorization_endpoint": f"{ISSUER}/authorize",
            "token_endpoint": f"{ISSUER}/token",
            "jwks_uri": "https://metadata.internal/jwks",
        },
    )
    with pytest.raises(OAuthError):
        await _provider().fetch_profile(code="c", redirect_uri="https://app.example/cb", nonce="n")
    # Refused up front — not after POSTing the code + secret to the token endpoint.
    assert seen == [DISCOVERY]


async def test_a_self_hosted_install_reaches_its_own_internal_idp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The carve-out end to end: on a customer's own install both legs — the issuer
    AND every endpoint its discovery document names — may sit on an internal
    address. An internal Keycloak/ADFS is the normal case there, and the org admin
    already owns that network."""
    monkeypatch.setattr(settings, "self_hosted", True)
    seen = _script(
        monkeypatch,
        {
            "authorization_endpoint": "https://sso.corp.internal/realms/acme/auth",
            "token_endpoint": "https://sso.corp.internal/realms/acme/token",
            "jwks_uri": "https://10.0.1.5/realms/acme/certs",
        },
        at=INTERNAL_DISCOVERY,
    )
    url = await _provider(INTERNAL_ISSUER).authorization_url(
        redirect_uri="https://app.example/cb", state="st", nonce="n"
    )
    assert url.startswith("https://sso.corp.internal/realms/acme/auth?")
    assert seen == [INTERNAL_DISCOVERY]


async def test_the_same_internal_idp_is_refused_on_the_hosted_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of the pair: identical config, hosted deployment — refused
    before a single request, so a tenant can never make Alkera's backend dial
    Alkera's own network."""
    seen = _script(
        monkeypatch,
        {"authorization_endpoint": "https://sso.corp.internal/realms/acme/auth"},
        at=INTERNAL_DISCOVERY,
    )
    with pytest.raises(OAuthError):
        await _provider(INTERNAL_ISSUER).authorization_url(
            redirect_uri="https://app.example/cb", state="st", nonce="n"
        )
    assert seen == []


async def test_a_public_issuer_naming_an_internal_endpoint_is_refused_on_saas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hosted service still refuses a PUBLIC issuer that redirects the token
    leg inward — the deployment rule is applied to every URL, not only the issuer,
    so a compromised/hostile IdP can't name an internal address for us to POST the
    client secret to."""
    seen = _script(
        monkeypatch,
        {
            "authorization_endpoint": f"{ISSUER}/authorize",
            "token_endpoint": "https://sso.corp.internal/token",
            "jwks_uri": f"{ISSUER}/jwks",
        },
    )
    with pytest.raises(OAuthError):
        await _provider().fetch_profile(code="c", redirect_uri="https://app.example/cb", nonce="n")
    assert seen == [DISCOVERY]


async def test_a_public_cross_host_discovery_document_still_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The asymmetric case: real IdPs (Google) serve authorize/token/JWKS on
    DIFFERENT public hosts than the issuer, so same-origin is not the rule —
    publicly-routable https is."""
    _script(
        monkeypatch,
        {
            "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_endpoint": "https://oauth2.googleapis.com/token",
            "jwks_uri": "https://www.googleapis.com/oauth2/v3/certs",
        },
    )
    url = await _provider().authorization_url(
        redirect_uri="https://app.example/cb", state="st", nonce="n"
    )
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "state=st" in url and "nonce=n" in url


# ---------------------------------------------------------------------------
# What the NAME resolves to: the dial is guarded, not just the string
# ---------------------------------------------------------------------------


def _wire(
    monkeypatch: pytest.MonkeyPatch, answers: dict[str, list[list[str]]], doc: dict[str, Any]
) -> tuple[list[httpx.Request], list[str]]:
    """The REAL guarded client, with the network and the resolver replaced: every
    request that would leave the process is recorded, as is every lookup."""
    sent: list[httpx.Request] = []
    lookups: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=doc)

    def resolver(host: str, port: int) -> list[str]:
        lookups.append(host)
        queue = answers[host]
        return queue.pop(0) if len(queue) > 1 else queue[0]

    real = oidc.async_client

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        return real(
            transport=httpx.MockTransport(handler),
            egress_resolver=resolver,
            trust_env=False,
            **kwargs,
        )

    monkeypatch.setattr(oidc, "async_client", factory)
    return sent, lookups


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(["10.0.4.20"], id="private"),
        pytest.param(["169.254.169.254"], id="metadata"),
        pytest.param(["127.0.0.1"], id="loopback"),
        pytest.param(["93.184.216.34", "10.0.4.20"], id="public-and-private"),
        pytest.param(["::ffff:10.0.4.20"], id="mapped-private"),
    ],
)
async def test_a_public_looking_issuer_resolving_inward_is_never_dialed_on_saas(
    monkeypatch: pytest.MonkeyPatch, answer: list[str]
) -> None:
    sent, lookups = _wire(
        monkeypatch,
        {"idp.acme.example.com": [answer]},
        {"authorization_endpoint": f"{ISSUER}/authorize"},
    )
    assert issuer_is_dialable(ISSUER)  # the string alone gives nothing away
    with pytest.raises(OAuthError, match="discovery failed"):
        await _provider().authorization_url(
            redirect_uri="https://app.example/cb", state="st", nonce="n"
        )
    assert lookups == ["idp.acme.example.com"]
    assert sent == []


async def test_discovery_is_sent_to_the_address_that_was_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A record that flips inward after the check changes nothing: the request goes
    to the vetted address, as the issuer's own host name."""
    sent, lookups = _wire(
        monkeypatch,
        {"idp.acme.example.com": [["93.184.216.34"], ["10.0.4.20"]]},
        {"authorization_endpoint": f"{ISSUER}/authorize"},
    )
    url = await _provider().authorization_url(
        redirect_uri="https://app.example/cb", state="st", nonce="n"
    )
    assert url.startswith(f"{ISSUER}/authorize?")
    assert lookups == ["idp.acme.example.com"]
    (request,) = sent
    assert request.url.host == "93.184.216.34"
    assert request.headers["host"] == "idp.acme.example.com"
    assert request.extensions["sni_hostname"] == "idp.acme.example.com"


async def test_a_self_hosted_install_may_resolve_inward_but_never_to_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "self_hosted", True)
    sent, _ = _wire(
        monkeypatch,
        {"idp.acme.example.com": [["10.0.4.20"]], "meta.acme.example.com": [["169.254.169.254"]]},
        {"authorization_endpoint": f"{ISSUER}/authorize"},
    )
    await _provider().authorization_url(redirect_uri="https://app.example/cb", state="s", nonce="n")
    assert [r.url.host for r in sent] == ["10.0.4.20"]
    with pytest.raises(OAuthError, match="discovery failed"):
        await _provider("https://meta.acme.example.com").authorization_url(
            redirect_uri="https://app.example/cb", state="s", nonce="n"
        )
    assert len(sent) == 1
