"""SAML 2.0 Service Provider — SP-initiated Web Browser SSO.

Security model (every check fails CLOSED; read before touching):

- **Signature** is verified by ``signxml`` (delegated XML-DSig: exclusive C14N +
  RSA verify via ``cryptography``) against the org's configured IdP certificate.
  We never hand-roll canonicalization or crypto.
- **XSW (XML Signature Wrapping) defense:** identity is read ONLY from the subtree
  ``signxml`` returns as cryptographically verified (``signed_xml``) — never from a
  re-query of the document. An attacker who wraps a forged assertion next to a
  legitimately-signed one cannot get the forged identity read, because we operate
  exclusively on the verified element. We additionally require the identity to come
  from the (single) Assertion inside that verified subtree.
- **Conditions** are all enforced against the VERIFIED assertion: Issuer, the
  audience (must name our SP), the validity window (NotBefore/NotOnOrAfter + a small
  clock skew), the SubjectConfirmation Recipient (must equal our ACS — token
  redirection defense) and NotOnOrAfter, and **InResponseTo** (must equal the
  AuthnRequest id we minted and stashed in a signed cookie — replay / CSRF /
  unsolicited-response defense).
- **XXE / entity-expansion** is disabled at parse time (no DTD, no network, no
  entity resolution).

The provider only ever VERIFIES. Signing (for tests / SP metadata) lives elsewhere.
"""

from __future__ import annotations

import base64
import binascii
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from lxml import etree
from signxml.exceptions import SignXMLException
from signxml.verifier import SignatureConfiguration, XMLVerifier

from backend.auth.oauth.base import OAuthError
from backend.auth.oauth.profile import FederatedProfile

_SAMLP = "urn:oasis:names:tc:SAML:2.0:protocol"
_SAML = "urn:oasis:names:tc:SAML:2.0:assertion"
_NS = {"samlp": _SAMLP, "saml": _SAML}
_STATUS_SUCCESS = "urn:oasis:names:tc:SAML:2.0:status:Success"
_BEARER = "urn:oasis:names:tc:SAML:2.0:cm:bearer"

# Tolerate small clock drift between SP and IdP on the validity-window checks.
_CLOCK_SKEW = timedelta(seconds=60)

# Require an x509-backed enveloped signature with exactly one reference (the
# signed element), searched anywhere in the document.
_EXPECT = SignatureConfiguration(require_x509=True, location=".//", expect_references=1)


class SamlError(OAuthError):
    """A SAML response failed validation. Subclasses OAuthError so the SSO route
    maps every failure to one generic, info-leak-free login error."""


@dataclass(frozen=True, slots=True)
class SamlSpConfig:
    """This SP's identity, as the IdP must be configured to see it."""

    entity_id: str  # the Audience the IdP must assert
    acs_url: str  # the Recipient / where the IdP POSTs the response


@dataclass(frozen=True, slots=True)
class SamlIdpConfig:
    entity_id: str  # the expected assertion Issuer
    sso_url: str  # where we send the AuthnRequest (HTTP-Redirect)
    x509_cert: str  # PEM or bare base64 DER


def _normalize_cert(raw: str) -> str:
    """Accept a PEM block or a bare base64 DER cert; return PEM."""
    raw = raw.strip()
    if "BEGIN CERTIFICATE" in raw:
        return raw
    body = "".join(raw.split())
    lines = "\n".join(body[i : i + 64] for i in range(0, len(body), 64))
    return f"-----BEGIN CERTIFICATE-----\n{lines}\n-----END CERTIFICATE-----\n"


# A legitimate SAML response is a few KB; cap the decoded size so a giant payload
# can't burn CPU/memory in parsing before the structural checks run.
_MAX_RESPONSE_BYTES = 1_000_000


def _hardened_parser() -> etree.XMLParser:
    # No DTD load, no network, no entity resolution → XXE + billion-laughs safe.
    # remove_comments closes the comment-injection / C14N-vs-extraction class
    # (Duo 2018): a comment splitting a NameID is stripped before we read text, so
    # what we extract always equals what the signature digested.
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        dtd_validation=False,
        huge_tree=False,
        remove_comments=True,
    )


def _text(el: etree._Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


def _attr(attrs: dict[str, str], *names: str) -> str:
    """First non-empty attribute among ``names``, matched CASE-INSENSITIVELY (real
    IdPs vary the casing — Okta 'emailaddress', etc.)."""
    lower = {k.lower(): v for k, v in attrs.items()}
    for name in names:
        value = lower.get(name.lower())
        if value:
            return value.strip()
    return ""


def _parse_instant(value: str) -> datetime | None:
    if not value:
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


class SamlProvider:
    kind = "saml"

    def __init__(self, *, key: str, idp: SamlIdpConfig, sp: SamlSpConfig) -> None:
        self.key = key
        self._idp = idp
        self._sp = sp
        self._cert_pem = _normalize_cert(idp.x509_cert)

    # --- SP-initiated request (HTTP-Redirect binding) ----------------------- #

    def authn_request_redirect_url(self, *, request_id: str, relay_state: str | None = None) -> str:
        """Build the AuthnRequest and the IdP redirect URL. ``request_id`` is the
        unique id we'll later require the response's InResponseTo to echo."""
        instant = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        xml = (
            f'<samlp:AuthnRequest xmlns:samlp="{_SAMLP}" xmlns:saml="{_SAML}" '
            f'ID="{request_id}" Version="2.0" IssueInstant="{instant}" '
            f'Destination="{_xml_attr(self._idp.sso_url)}" '
            f'AssertionConsumerServiceURL="{_xml_attr(self._sp.acs_url)}" '
            f'ProtocolBinding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST">'
            f"<saml:Issuer>{_xml_text(self._sp.entity_id)}</saml:Issuer>"
            f'<samlp:NameIDPolicy AllowCreate="true" '
            f'Format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress"/>'
            f"</samlp:AuthnRequest>"
        )
        # HTTP-Redirect binding: raw DEFLATE (no zlib header) → base64 → urlencode.
        deflated = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
        body = deflated.compress(xml.encode("utf-8")) + deflated.flush()
        params = {"SAMLRequest": base64.b64encode(body).decode("ascii")}
        if relay_state:
            params["RelayState"] = relay_state
        sep = "&" if "?" in self._idp.sso_url else "?"
        return f"{self._idp.sso_url}{sep}{urlencode(params)}"

    # --- ACS response validation -------------------------------------------- #

    def parse_response(
        self, saml_response_b64: str, *, expected_in_response_to: str, now: datetime | None = None
    ) -> FederatedProfile:
        now = now or datetime.now(UTC)

        # The id we'll require InResponseTo to echo must itself be well-formed —
        # an empty/None expectation would let an IdP that OMITS InResponseTo match.
        if not expected_in_response_to or not expected_in_response_to.startswith("_"):
            raise SamlError("invalid AuthnRequest correlation id")
        if len(saml_response_b64) > _MAX_RESPONSE_BYTES * 2:  # base64 ~ 1.33x raw
            raise SamlError("SAMLResponse too large")

        try:
            xml_bytes = base64.b64decode(saml_response_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise SamlError("malformed SAMLResponse encoding") from exc
        if len(xml_bytes) > _MAX_RESPONSE_BYTES:
            raise SamlError("SAMLResponse too large")
        try:
            root = etree.fromstring(xml_bytes, parser=_hardened_parser())
        except etree.XMLSyntaxError as exc:
            raise SamlError("malformed SAMLResponse XML") from exc
        # A DOCTYPE is never legitimate here and is the entity-expansion vector.
        if root.getroottree().docinfo.doctype:
            raise SamlError("SAMLResponse must not carry a DOCTYPE")

        # 1) Signature — the only trust anchor. Returns the verified subtree(s).
        try:
            results = XMLVerifier().verify(root, x509_cert=self._cert_pem, expect_config=_EXPECT)
        except (SignXMLException, ValueError) as exc:
            # Don't leak the specific verification failure into the error chain.
            raise SamlError("signature verification failed") from exc
        verified_list = results if isinstance(results, list) else [results]

        # 2) The Assertion MUST come from inside a verified subtree (XSW defense):
        #    either the verified element IS the Assertion, or it's an ancestor
        #    (e.g. a signed Response) that contains exactly one Assertion.
        assertion = _verified_assertion(verified_list)
        if assertion is None:
            raise SamlError("no signed Assertion in the response")

        # 3) Everything below is read from the VERIFIED assertion only.
        self._check_issuer(assertion)
        not_on_or_after = self._check_conditions(assertion, now=now)
        self._check_subject_confirmation(
            assertion, expected_in_response_to=expected_in_response_to, now=now
        )

        # A best-effort, non-authoritative Status sanity check on the response root
        # (security rests entirely on the verified assertion above).
        status = root.find("samlp:Status/samlp:StatusCode", _NS)
        if status is not None and status.get("Value") not in (None, _STATUS_SUCCESS):
            raise SamlError("SAML status is not Success")

        name_id = _text(assertion.find("saml:Subject/saml:NameID", _NS))
        attrs = _attributes(assertion)
        # Real IdPs differ: Okta/Azure 'email' or the WS-* URN (→ 'emailaddress'),
        # Google Workspace 'mail'. Match case-insensitively across all of them.
        email = _attr(attrs, "email", "emailAddress", "mail")
        if not email:
            # Fall back to the NameID only when it's actually an email — never a
            # persistent/opaque NameID, which would become a bogus "email".
            if "@" in name_id:
                email = name_id
            else:
                raise SamlError("assertion carries no email attribute or email NameID")
        email = email.strip().lower()

        return FederatedProfile(
            provider=self.key,
            # Normalize the stable subject so case variance from the IdP can't mint
            # a second identity row for the same person.
            subject=(name_id or email).strip().lower(),
            email=email,
            email_verified=True,  # the IdP (within its allowed domains) vouches
            first_name=_attr(attrs, "firstName", "givenName", "given_name"),
            last_name=_attr(attrs, "lastName", "surname", "sn", "family_name"),
            idp_groups=_group_values(assertion),
            raw={
                "attributes": attrs,
                "name_id": name_id,
                "assertion_id": assertion.get("ID") or "",
                "not_on_or_after": not_on_or_after.isoformat()
                if not_on_or_after is not None
                else "",
            },
        )

    def _check_issuer(self, assertion: etree._Element) -> None:
        issuer = _text(assertion.find("saml:Issuer", _NS))
        if issuer != self._idp.entity_id:
            raise SamlError("assertion Issuer does not match the configured IdP")

    def _check_conditions(self, assertion: etree._Element, *, now: datetime) -> datetime:
        conditions = assertion.find("saml:Conditions", _NS)
        if conditions is None:
            raise SamlError("assertion has no Conditions")
        not_before = _parse_instant(conditions.get("NotBefore", ""))
        not_on_or_after = _parse_instant(conditions.get("NotOnOrAfter", ""))
        # Require BOTH bounds — a missing one would otherwise mean "valid forever"
        # / "valid since epoch" (fail-open replay window).
        if not_before is None or not_on_or_after is None:
            raise SamlError("Conditions must carry NotBefore and NotOnOrAfter")
        if now + _CLOCK_SKEW < not_before:
            raise SamlError("assertion not yet valid")
        if now - _CLOCK_SKEW >= not_on_or_after:
            raise SamlError("assertion expired")
        # Audience: our SP entity id MUST be one of the restricted audiences.
        audiences = {
            _text(a) for a in conditions.findall("saml:AudienceRestriction/saml:Audience", _NS)
        }
        if not audiences:
            raise SamlError("assertion has no AudienceRestriction")
        if self._sp.entity_id not in audiences:
            raise SamlError("assertion audience does not include this SP")
        return not_on_or_after

    def _check_subject_confirmation(
        self, assertion: etree._Element, *, expected_in_response_to: str, now: datetime
    ) -> etree._Element:
        # Require a bearer SubjectConfirmation whose data binds this exact request
        # + recipient + freshness. ALL must hold on ONE confirmation.
        for confirmation in assertion.findall("saml:Subject/saml:SubjectConfirmation", _NS):
            if confirmation.get("Method") != _BEARER:
                continue
            data = confirmation.find("saml:SubjectConfirmationData", _NS)
            if data is None:
                continue
            if data.get("InResponseTo") != expected_in_response_to:
                continue  # replay / unsolicited / wrong flow
            if data.get("Recipient") != self._sp.acs_url:
                continue  # token redirection
            noa = _parse_instant(data.get("NotOnOrAfter", ""))
            if noa is None or now - _CLOCK_SKEW >= noa:
                continue
            return confirmation
        raise SamlError("no valid bearer SubjectConfirmation (InResponseTo/Recipient/expiry)")


def _verified_assertion(verified: list) -> etree._Element | None:  # type: ignore[type-arg]
    """The single Assertion living inside a cryptographically-verified subtree.
    Returns None if there isn't exactly one. This is the XSW defense — we only
    ever look at elements signxml proved were signed."""
    for result in verified:
        el = result.signed_xml
        if el is None:
            continue
        tag = etree.QName(el)
        if tag.namespace == _SAML and tag.localname == "Assertion":
            return el
        # A signed ancestor (e.g. the Response) — accept the one Assertion it covers.
        found = el.findall(f".//{{{_SAML}}}Assertion")
        if len(found) == 1:
            return found[0]
    return None


def _attributes(assertion: etree._Element) -> dict[str, str]:
    """Map every Attribute by its full Name, its FriendlyName, and the short tail
    of a URN/OID Name — so a case-insensitive lookup finds it however the IdP
    names it (Okta URN, Azure claim, Google short name, …)."""
    out: dict[str, str] = {}
    for attr in assertion.findall("saml:AttributeStatement/saml:Attribute", _NS):
        name = attr.get("Name")
        friendly = attr.get("FriendlyName")
        if not (name or friendly):
            continue
        value = _text(attr.find("saml:AttributeValue", _NS))
        if name:
            out.setdefault(name, value)
            short = name.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
            out.setdefault(short, value)
        if friendly:
            out.setdefault(friendly, value)
    return out


# IdP group/role attribute names (full Name, FriendlyName, or short URN/OID tail),
# matched case-insensitively. Group attrs are MULTI-valued, so they need their own
# extractor (`_attributes` keeps only the first value per attribute).
_GROUP_ATTR_NAMES = frozenset(
    {
        "groups",
        "group",
        "memberof",
        "roles",
        "role",
        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/groups",
    }
)


def _group_values(assertion: etree._Element) -> tuple[str, ...]:
    """Every value of the assertion's group/role attribute(s), across IdP naming."""
    out: list[str] = []
    for attr in assertion.findall("saml:AttributeStatement/saml:Attribute", _NS):
        name = (attr.get("Name") or "").strip()
        friendly = (attr.get("FriendlyName") or "").strip()
        short = name.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
        if {name.lower(), friendly.lower(), short.lower()} & _GROUP_ATTR_NAMES:
            for value in attr.findall("saml:AttributeValue", _NS):
                text = (value.text or "").strip()
                if text:
                    out.append(text)
    return tuple(out[:1000])  # cap so a flooded assertion can't blow up the role lookup


def _xml_text(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _xml_attr(value: str) -> str:
    return _xml_text(value).replace('"', "&quot;")
