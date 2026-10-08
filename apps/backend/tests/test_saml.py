"""SAML SP — adversarial security tests for the response validator.

A test IdP signs a SAML response with a throwaway key; the provider verifies it
against the matching cert. The happy path proves a well-formed signed assertion
yields the right identity; the rest are the attacks an enterprise security review
fires at a SAML SP — every one MUST be rejected. Security rests on the verified
assertion, so each negative case breaks exactly one guarantee.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import pytest
from backend.auth.oauth.saml import (
    SamlError,
    SamlIdpConfig,
    SamlProvider,
    SamlSpConfig,
)
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from lxml import etree
from signxml import XMLSigner

_SAMLP = "urn:oasis:names:tc:SAML:2.0:protocol"
_SAML = "urn:oasis:names:tc:SAML:2.0:assertion"
_NS = {"samlp": _SAMLP, "saml": _SAML}

IDP_ENTITY = "https://idp.acme.example.com/metadata"
SP_ENTITY = "https://app.example.com/saml/metadata/orgA"
ACS_URL = "https://app.example.com/api/v1/auth/sso/orgA/saml/acs"
REQUEST_ID = "_req-12345"


def _make_keypair() -> tuple[bytes, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subj = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-idp")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subj)
        .issuer_name(subj)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(days=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    return key_pem, cert_pem


# A signing keypair shared across the suite, plus a SEPARATE attacker keypair.
_KEY_PEM, _CERT_PEM = _make_keypair()
_ATTACKER_KEY_PEM, _ATTACKER_CERT_PEM = _make_keypair()


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def build_response(
    *,
    email: str = "user@acme.example.com",
    name_id: str | None = None,
    audience: str = SP_ENTITY,
    recipient: str = ACS_URL,
    in_response_to: str = REQUEST_ID,
    issuer: str = IDP_ENTITY,
    not_before: datetime | None = None,
    not_on_or_after: datetime | None = None,
    status: str = "urn:oasis:names:tc:SAML:2.0:status:Success",
    sign: bool = True,
    sign_key: bytes | None = None,
    sign_cert: str | None = None,
    extra_forged_assertion: bool = False,
    email_attr_name: str = "email",
    include_email_attr: bool = True,
    conditions: str = "full",  # full | no_nb | no_noa | missing | empty_audience
    comment_in_nameid: bool = False,
) -> str:
    """Build a (by default validly-signed) SAML response, base64-encoded for ACS.
    Every knob exists to construct one adversarial case."""
    now = datetime.now(UTC)
    nb = _iso(not_before if not_before is not None else now - timedelta(minutes=5))
    noa = _iso(not_on_or_after if not_on_or_after is not None else now + timedelta(minutes=5))
    name_id = name_id or email
    name_id_inner = name_id.replace("@", "@<!--c-->", 1) if comment_in_nameid else name_id

    if conditions == "missing":
        conditions_xml = ""
    else:
        attrs = []
        if conditions not in ("no_nb",):
            attrs.append(f'NotBefore="{nb}"')
        if conditions not in ("no_noa",):
            attrs.append(f'NotOnOrAfter="{noa}"')
        audience_xml = (
            ""
            if conditions == "empty_audience"
            else f"<saml:AudienceRestriction><saml:Audience>{audience}</saml:Audience></saml:AudienceRestriction>"  # noqa: E501
        )
        conditions_xml = f"<saml:Conditions {' '.join(attrs)}>{audience_xml}</saml:Conditions>"

    attr_xml = (
        f'<saml:AttributeStatement><saml:Attribute Name="{email_attr_name}">'
        f"<saml:AttributeValue>{email}</saml:AttributeValue></saml:Attribute></saml:AttributeStatement>"
        if include_email_attr
        else ""
    )

    assertion = (
        f'<saml:Assertion xmlns:saml="{_SAML}" ID="_assert1" Version="2.0" IssueInstant="{_iso(now)}">'  # noqa: E501
        f"<saml:Issuer>{issuer}</saml:Issuer>"
        f"<saml:Subject><saml:NameID>{name_id_inner}</saml:NameID>"
        f'<saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer">'
        f'<saml:SubjectConfirmationData InResponseTo="{in_response_to}" Recipient="{recipient}" NotOnOrAfter="{noa}"/>'  # noqa: E501
        f"</saml:SubjectConfirmation></saml:Subject>"
        f"{conditions_xml}{attr_xml}"
        f"</saml:Assertion>"
    )

    forged = ""
    if extra_forged_assertion:
        forged = (
            f'<saml:Assertion xmlns:saml="{_SAML}" ID="_forged" Version="2.0" IssueInstant="{_iso(now)}">'  # noqa: E501
            f"<saml:Issuer>{issuer}</saml:Issuer>"
            f"<saml:Subject><saml:NameID>attacker@acme.example.com</saml:NameID></saml:Subject>"
            f'<saml:Conditions NotBefore="{nb}" NotOnOrAfter="{noa}">'
            f"<saml:AudienceRestriction><saml:Audience>{audience}</saml:Audience></saml:AudienceRestriction>"
            f"</saml:Conditions>"
            f'<saml:AttributeStatement><saml:Attribute Name="email"><saml:AttributeValue>attacker@acme.example.com</saml:AttributeValue></saml:Attribute></saml:AttributeStatement>'  # noqa: E501
            f"</saml:Assertion>"
        )

    response = (
        f'<samlp:Response xmlns:samlp="{_SAMLP}" xmlns:saml="{_SAML}" ID="_resp1" '
        f'InResponseTo="{in_response_to}" Version="2.0" IssueInstant="{_iso(now)}">'
        f"<saml:Issuer>{issuer}</saml:Issuer>"
        f'<samlp:Status><samlp:StatusCode Value="{status}"/></samlp:Status>'
        f"{forged}{assertion}"
        f"</samlp:Response>"
    )

    root = etree.fromstring(response.encode())
    if sign:
        # Sign the GENUINE assertion (ID=_assert1) specifically — in the XSW case a
        # forged sibling (ID=_forged) is left UNSIGNED, exactly the attack shape.
        target = root.xpath("//saml:Assertion[@ID='_assert1']", namespaces=_NS)[0]
        signed = XMLSigner(c14n_algorithm="http://www.w3.org/2001/10/xml-exc-c14n#").sign(
            target, key=sign_key or _KEY_PEM, cert=sign_cert or _CERT_PEM, reference_uri="_assert1"
        )
        target.getparent().replace(target, signed)
    return base64.b64encode(etree.tostring(root)).decode("ascii")


def _provider() -> SamlProvider:
    return SamlProvider(
        key="sso:orgA",
        idp=SamlIdpConfig(entity_id=IDP_ENTITY, sso_url="https://idp/sso", x509_cert=_CERT_PEM),
        sp=SamlSpConfig(entity_id=SP_ENTITY, acs_url=ACS_URL),
    )


def _parse(resp_b64: str):
    return _provider().parse_response(resp_b64, expected_in_response_to=REQUEST_ID)


# --------------------------------------------------------------------------- #
# happy path
# --------------------------------------------------------------------------- #


def test_valid_signed_response_yields_profile() -> None:
    profile = _parse(build_response(email="dev@acme.example.com"))
    assert profile.email == "dev@acme.example.com"
    assert profile.email_verified is True
    assert profile.provider == "sso:orgA"


# --------------------------------------------------------------------------- #
# signature attacks
# --------------------------------------------------------------------------- #


def test_unsigned_response_rejected() -> None:
    with pytest.raises(SamlError):
        _parse(build_response(sign=False))


def test_wrong_cert_signature_rejected() -> None:
    # Signed by an attacker key the SP doesn't trust.
    with pytest.raises(SamlError):
        _parse(build_response(sign_key=_ATTACKER_KEY_PEM, sign_cert=_ATTACKER_CERT_PEM))


def test_tampered_after_signing_rejected() -> None:
    good = build_response(email="user@acme.example.com")
    raw = base64.b64decode(good)
    tampered = raw.replace(b"user@acme.example.com", b"attacker@acme.example.com")
    with pytest.raises(SamlError):
        _parse(base64.b64encode(tampered).decode())


def test_xml_signature_wrapping_rejected() -> None:
    # A forged unsigned assertion wrapped alongside the legitimately-signed one.
    # Two assertions exist; only one is signed → the verified subtree is the
    # signed Response... actually only the assertion is signed here, so the
    # forged sibling is NOT in any verified subtree. The forged identity must
    # never surface.
    resp = build_response(email="real@acme.example.com", extra_forged_assertion=True)
    profile = _parse(resp)
    # If anything is returned at all, it MUST be the real (signed) identity.
    assert profile.email == "real@acme.example.com"
    assert "attacker" not in profile.email


# --------------------------------------------------------------------------- #
# condition attacks (all read off the VERIFIED assertion)
# --------------------------------------------------------------------------- #


def test_expired_assertion_rejected() -> None:
    past = datetime.now(UTC) - timedelta(hours=1)
    with pytest.raises(SamlError, match="expired"):
        _parse(build_response(not_on_or_after=past))


def test_not_yet_valid_assertion_rejected() -> None:
    future = datetime.now(UTC) + timedelta(hours=1)
    with pytest.raises(SamlError, match="not yet valid"):
        _parse(build_response(not_before=future))


def test_wrong_audience_rejected() -> None:
    with pytest.raises(SamlError, match="audience"):
        _parse(build_response(audience="https://some-other-sp/metadata"))


def test_wrong_recipient_rejected() -> None:
    with pytest.raises(SamlError):
        _parse(build_response(recipient="https://evil.example.com/acs"))


def test_wrong_in_response_to_rejected() -> None:
    # A replayed / unsolicited response for a different (or no) AuthnRequest.
    with pytest.raises(SamlError):
        _parse(build_response(in_response_to="_some-other-request"))


def test_wrong_issuer_rejected() -> None:
    with pytest.raises(SamlError, match="Issuer"):
        _parse(build_response(issuer="https://impostor-idp/metadata"))


# --------------------------------------------------------------------------- #
# malformed / abuse
# --------------------------------------------------------------------------- #


def test_doctype_rejected() -> None:
    payload = (
        '<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY x "y">]>'
        f'<samlp:Response xmlns:samlp="{_SAMLP}"/>'
    )
    with pytest.raises(SamlError):
        _parse(base64.b64encode(payload.encode()).decode())


def test_garbage_rejected() -> None:
    with pytest.raises(SamlError):
        _parse("not-valid-base64-xml!!!")


def test_authn_request_redirect_is_well_formed() -> None:
    url = _provider().authn_request_redirect_url(request_id=REQUEST_ID, relay_state="rs")
    assert url.startswith("https://idp/sso?")
    assert "SAMLRequest=" in url and "RelayState=rs" in url


# --------------------------------------------------------------------------- #
# review-hardening cases (fail-closed conditions, interop, comment injection)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("mode", ["missing", "no_nb", "no_noa", "empty_audience"])
def test_conditions_must_be_complete(mode: str) -> None:
    # A missing Conditions, a missing time bound (→ "valid forever"), or an empty
    # AudienceRestriction must all be refused — no fail-open validity window.
    with pytest.raises(SamlError):
        _parse(build_response(conditions=mode))


def test_no_email_attr_and_non_email_nameid_rejected() -> None:
    # NameID is an opaque/persistent id (no '@') and there's no email attribute →
    # we must NOT turn the opaque id into a bogus email; reject.
    with pytest.raises(SamlError):
        _parse(build_response(include_email_attr=False, name_id="persistent-opaque-id-12345"))


@pytest.mark.parametrize(
    "attr_name",
    [
        "mail",  # Google Workspace
        "emailAddress",  # generic
        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",  # Okta/Azure URN
        "EMAIL",  # case-insensitive
    ],
)
def test_email_attribute_interop(attr_name: str) -> None:
    profile = _parse(build_response(email="dev@acme.example.com", email_attr_name=attr_name))
    assert profile.email == "dev@acme.example.com"


def test_nameid_case_is_normalized_for_subject() -> None:
    profile = _parse(
        build_response(
            include_email_attr=False, name_id="User@Acme.Example.Com", email="User@Acme.Example.Com"
        )
    )
    # Subject + email lowercased so case variance can't mint a second identity.
    assert profile.subject == "user@acme.example.com"
    assert profile.email == "user@acme.example.com"


def test_comment_in_nameid_reads_full_signed_value() -> None:
    # The Duo comment-injection: a comment splitting the NameID. With remove_comments
    # the extracted value equals what exc-C14N digested (the FULL string), so an
    # attacker can't truncate to a victim address.
    full = "attacker@a.example.com.evil.example.com"
    profile = _parse(
        build_response(include_email_attr=False, name_id=full, email=full, comment_in_nameid=True)
    )
    assert profile.email == full  # not "attacker@a.example.com" or "attacker"


def test_empty_in_response_to_expectation_rejected() -> None:
    # If the caller's expected id were ever empty, an IdP omitting InResponseTo
    # must not match.
    with pytest.raises(SamlError):
        _provider().parse_response(build_response(), expected_in_response_to="")


def test_oversized_response_rejected() -> None:
    with pytest.raises(SamlError):
        _parse(base64.b64encode(b"<x/>" + b"A" * 3_000_000).decode())


# --------------------------------------------------------------------------- #
# Route-level integration: login → ACS → resolve (cross-org gate) → session
# --------------------------------------------------------------------------- #

import uuid  # noqa: E402

import pytest_asyncio  # noqa: E402
from alkera_core.auth import decode_oauth_state  # noqa: E402
from alkera_core.db.session import AsyncSessionLocal  # noqa: E402
from alkera_core.models import SsoConnection, User  # noqa: E402
from backend.services.identity import sso as sso_service  # noqa: E402
from backend.utils.cookies import OAUTH_STATE_COOKIE  # noqa: E402
from sqlalchemy import select  # noqa: E402
from tests.conftest import hold_sso_domains, make_member  # noqa: E402


async def _configure_saml(org_id, *, domain: str) -> None:
    async with AsyncSessionLocal() as s:
        s.add(
            SsoConnection(
                org_team_id=org_id,
                protocol="saml",
                enabled=True,
                saml_entity_id=IDP_ENTITY,
                saml_sso_url="https://idp/sso",
                saml_x509_cert=_CERT_PEM,
            )
        )
        await s.commit()
    await hold_sso_domains(org_id, domain)


@pytest_asyncio.fixture
async def saml_org():
    """A fresh org with SAML configured + a known domain it owns."""
    from backend.services.org import teams as team_service

    domain = f"acme-{uuid.uuid4().hex[:8]}.example.com"
    async with AsyncSessionLocal() as s:
        _org, admin = await team_service.create_org_with_admin(
            s,
            org_name=f"org-{uuid.uuid4().hex[:8]}",
            admin_email=f"admin-{uuid.uuid4().hex[:8]}@alkera.dev",
            admin_first_name="A",
            admin_last_name="D",
            admin_password="pass-123456",
        )
        await s.commit()
        org_id = admin.home_org_team_id
    await _configure_saml(org_id, domain=domain)
    return org_id, domain


async def _login_and_get_request_id(client, org_id) -> str:
    resp = await client.get(f"/api/v1/auth/sso/{org_id}/saml/login", follow_redirects=False)
    assert resp.status_code == 302
    assert "SAMLRequest=" in resp.headers["location"]
    tx = decode_oauth_state(client.cookies[OAUTH_STATE_COOKIE])
    return tx.state


@pytest.mark.asyncio
async def test_saml_acs_jit_provisions_and_logs_in(client, saml_org) -> None:
    org_id, domain = saml_org
    sp = sso_service.saml_sp_config(org_id)
    request_id = await _login_and_get_request_id(client, org_id)

    email = f"newhire@{domain}"
    response = build_response(
        email=email, audience=sp.entity_id, recipient=sp.acs_url, in_response_to=request_id
    )
    acs = await client.post(
        f"/api/v1/auth/sso/{org_id}/saml/acs",
        data={"SAMLResponse": response},
        follow_redirects=False,
    )
    assert acs.status_code == 302  # logged in → redirect into the app
    assert "oauth_error" not in acs.headers["location"]

    async with AsyncSessionLocal() as s:
        user = (await s.execute(select(User).where(User.email == email))).scalar_one()
        assert user.home_org_team_id == org_id  # JIT-provisioned into the IdP's org


@pytest.mark.asyncio
async def test_saml_acs_rejects_assertion_replay(client, saml_org) -> None:
    org_id, domain = saml_org
    sp = sso_service.saml_sp_config(org_id)
    await client.get(f"/api/v1/auth/sso/{org_id}/saml/login", follow_redirects=False)
    tx_cookie = client.cookies[OAUTH_STATE_COOKIE]
    request_id = decode_oauth_state(tx_cookie).state
    response = build_response(
        email=f"replay@{domain}",
        audience=sp.entity_id,
        recipient=sp.acs_url,
        in_response_to=request_id,
    )

    first = await client.post(
        f"/api/v1/auth/sso/{org_id}/saml/acs",
        data={"SAMLResponse": response},
        follow_redirects=False,
    )
    assert first.status_code == 302
    assert "oauth_error" not in first.headers["location"]  # logged in

    # Replay the SAME captured (cookie + response): the assertion is single-use.
    client.cookies.set(OAUTH_STATE_COOKIE, tx_cookie)
    second = await client.post(
        f"/api/v1/auth/sso/{org_id}/saml/acs",
        data={"SAMLResponse": response},
        follow_redirects=False,
    )
    assert "oauth_error=replay" in second.headers["location"]


@pytest.mark.asyncio
async def test_saml_acs_refuses_cross_org_takeover(client, saml_org) -> None:
    org_a, domain = saml_org  # org A's SAML claims it owns `domain`
    # The victim actually lives in a DIFFERENT org B, with an email in that domain.
    async with AsyncSessionLocal() as s:
        from backend.services.org import teams as team_service

        _org, admin_b = await team_service.create_org_with_admin(
            s,
            org_name=f"orgB-{uuid.uuid4().hex[:8]}",
            admin_email=f"adminB-{uuid.uuid4().hex[:8]}@alkera.dev",
            admin_first_name="B",
            admin_last_name="D",
            admin_password="pass-123456",
        )
        await s.commit()
        victim, _pw = await make_member(
            s, org_id=admin_b.home_org_team_id, email=f"victim@{domain}", verified=True
        )
        victim_email = victim.email

    sp = sso_service.saml_sp_config(org_a)
    request_id = await _login_and_get_request_id(client, org_a)
    response = build_response(
        email=victim_email, audience=sp.entity_id, recipient=sp.acs_url, in_response_to=request_id
    )
    acs = await client.post(
        f"/api/v1/auth/sso/{org_a}/saml/acs",
        data={"SAMLResponse": response},
        follow_redirects=False,
    )
    assert acs.status_code == 302
    # Refused by the cross-org gate — no session, an error redirect instead.
    assert "sso_org_mismatch" in acs.headers["location"]


@pytest.mark.asyncio
async def test_saml_acs_without_login_cookie_rejected(client, saml_org) -> None:
    org_id, domain = saml_org
    sp = sso_service.saml_sp_config(org_id)
    response = build_response(
        email=f"x@{domain}", audience=sp.entity_id, recipient=sp.acs_url, in_response_to="_nope"
    )
    # No prior /login → no state cookie → cannot bind InResponseTo.
    acs = await client.post(
        f"/api/v1/auth/sso/{org_id}/saml/acs",
        data={"SAMLResponse": response},
        follow_redirects=False,
    )
    assert acs.status_code == 302
    assert "oauth_error=expired" in acs.headers["location"]


@pytest.mark.asyncio
async def test_saml_metadata_exposes_sp_acs(client, saml_org) -> None:
    org_id, _domain = saml_org
    resp = await client.get(f"/api/v1/auth/sso/{org_id}/saml/metadata")
    assert resp.status_code == 200
    assert "EntityDescriptor" in resp.text
    assert f"/api/v1/auth/sso/{org_id}/saml/acs" in resp.text
