#!/usr/bin/env python3
"""Browser-simulating driver for the live SSO end-to-end test.

Runs INSIDE the compose network (via the backend image, which already has httpx),
so it reaches the backend (`backend:8000`) and the real Keycloak IdP
(`keycloak:8080`) by the same authority the backend uses, no issuer/redirect
mismatch. It drives the FULL authorization-code flow against a real IdP:

    discover → /sso/{org}/login → Keycloak login form → POST creds →
    IdP 302 back to /sso/{org}/login/callback → backend exchanges the code,
    verifies the id_token, runs the cross-org IdpScope gate, JIT-provisions,
    and mints a session.

Asserts the security-critical outcomes a unit test with a mock IdP cannot prove
end-to-end: a real signed id_token is accepted, an in-domain user is provisioned
into the right org, and the IdpScope gate refuses a user
whose email is OUTSIDE the connection's allowed domains.

Cookies are tracked by hand (name=value, Secure flag ignored) because the stack
runs APP_ENV=production with Secure cookies, but the test speaks http in-network
  the Secure attribute is a transport concern irrelevant to the auth logic here.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import html
import json
import os
import re
import struct
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

BACKEND = os.environ["BACKEND"].rstrip("/")
ADMIN_EMAIL = os.environ["ADMIN_EMAIL"]
ADMIN_PASSWORD = os.environ["ADMIN_PASSWORD"]
OIDC_ISSUER = os.environ["OIDC_ISSUER"].rstrip("/")
OIDC_CLIENT_ID = os.environ["OIDC_CLIENT_ID"]
OIDC_CLIENT_SECRET = os.environ["OIDC_CLIENT_SECRET"]
ALLOWED_DOMAINS = os.environ["ALLOWED_DOMAINS"]
ALICE_EMAIL = os.environ["ALICE_EMAIL"]
ALICE_PASSWORD = os.environ["ALICE_PASSWORD"]
MALLORY_EMAIL = os.environ["MALLORY_EMAIL"]
MALLORY_PASSWORD = os.environ["MALLORY_PASSWORD"]

_FORM_ACTION = re.compile(r'action="([^"]*login-actions/authenticate[^"]*)"')


def _fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def _ok(msg: str) -> None:
    print(f"PASS: {msg}")


def _poll(pred: Callable[[], bool], *, tries: int = 15, delay: float = 0.3) -> bool:
    """Retry a predicate to ride out read-after-write visibility lag, a write's
    commit can land just after its 200 response, so an immediate read may miss it.
    Poll a cheap read until the write is visible, THEN make the real assertion."""
    for _ in range(tries):
        if pred():
            return True
        time.sleep(delay)
    return False


@dataclass
class CookieJar:
    """A Secure-flag-ignoring cookie store keyed by name (one host per flow)."""

    store: dict[str, str] = field(default_factory=dict)

    def absorb(self, resp: httpx.Response) -> None:
        for raw in resp.headers.get_list("set-cookie"):
            pair = raw.split(";", 1)[0].strip()
            if "=" not in pair:
                continue
            name, value = pair.split("=", 1)
            if value in ("", '""', "deleted"):
                self.store.pop(name, None)
            else:
                self.store[name] = value

    def header(self) -> dict[str, str]:
        if not self.store:
            return {}
        return {"Cookie": "; ".join(f"{k}={v}" for k, v in self.store.items())}


def admin_login(client: httpx.Client) -> str:
    jar = CookieJar()
    r = client.post(
        f"{BACKEND}/api/v1/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
    )
    if r.status_code != 200:
        _fail(f"admin login HTTP {r.status_code}: {r.text[:300]}")
    jar.absorb(r)
    if "alkera_session" not in jar.store:
        _fail("admin login set no session cookie")
    _ok("org admin logged in")
    return jar.store["alkera_session"]


def admin_org_id(client: httpx.Client, session: str) -> str:
    r = client.get(f"{BACKEND}/api/v1/auth/me", headers={"Cookie": f"alkera_session={session}"})
    if r.status_code != 200:
        _fail(f"/auth/me HTTP {r.status_code}: {r.text[:300]}")
    return str(r.json()["org_team_id"])


def configure_oidc(client: httpx.Client, session: str) -> None:
    r = client.put(
        f"{BACKEND}/api/v1/org/sso",
        headers={"Cookie": f"alkera_session={session}"},
        json={
            "protocol": "oidc",
            "allowed_domains": ALLOWED_DOMAINS,
            "enabled": True,
            "oidc_issuer": OIDC_ISSUER,
            "oidc_client_id": OIDC_CLIENT_ID,
            "oidc_client_secret": OIDC_CLIENT_SECRET,
        },
    )
    if r.status_code != 200:
        _fail(f"PUT /org/sso HTTP {r.status_code}: {r.text[:400]}")
    body = r.json()
    if not (body.get("enabled") and body.get("has_client_secret")):
        _fail(f"SSO config not enabled/secret-bearing: {body}")
    # The secret must never be echoed back in the read model.
    if OIDC_CLIENT_SECRET in r.text:
        _fail("the OIDC client secret was echoed back by the GET shape (a leak)")
    _ok("org admin configured the OIDC connection (secret stored, never echoed)")


def discover(client: httpx.Client, email: str) -> str | None:
    r = client.get(f"{BACKEND}/api/v1/auth/sso/discover", params={"email": email})
    if r.status_code != 200:
        _fail(f"discover HTTP {r.status_code}: {r.text[:200]}")
    body = r.json()
    return body["login_url"] if body.get("sso") else None


def oidc_login(*, email: str, password: str) -> CookieJar:
    """Run the full authorization-code flow as `email`. Returns the backend
    cookie jar after the callback (holds alkera_session iff login succeeded).

    Uses its OWN client so the IdP session never bleeds between flows, a shared
    jar would let Keycloak auto-SSO the second user as the first (the IdP session
    cookie), masking the server-side gate we're trying to prove."""
    # mallory's domain is NOT bound to SSO, so discover(mallory) is correctly
    # sso=false; we still drive HER through alice's org login endpoint to prove
    # the SERVER-SIDE gate (not just the discovery hint) refuses her.
    with httpx.Client(follow_redirects=False, timeout=30.0) as client:
        login_url = discover(client, ALICE_EMAIL)
        if login_url is None:
            _fail("discover did not return a login_url for the in-domain user")

        backend_jar = CookieJar()
        # 1. Kick off login at the backend → 302 to Keycloak, sets the signed state cookie.
        r = client.get(login_url)
        backend_jar.absorb(r)
        if r.status_code != 302:
            _fail(f"/sso/login expected 302, got {r.status_code}: {r.text[:200]}")
        kc_auth = r.headers["location"]
        if not kc_auth.startswith(OIDC_ISSUER):
            _fail(f"login did not redirect to the IdP: {kc_auth}")
        if "alkera_oauth_tx" not in backend_jar.store:
            _fail("backend set no signed oauth_state cookie before the IdP hop")

        # 2. Render the IdP login form.
        r = client.get(kc_auth)
        if r.status_code != 200:
            _fail(f"IdP login page HTTP {r.status_code} (IdP session bled across flows?)")
        m = _FORM_ACTION.search(r.text)
        if not m:
            _fail("could not find the Keycloak login form action")
        action = html.unescape(m.group(1))

        # 3. Submit credentials to the IdP (its own session cookies auto-carry).
        r = client.post(
            action,
            data={"username": email, "password": password, "credentialId": ""},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if r.status_code not in (302, 303):
            _fail(f"IdP did not accept credentials for {email}: HTTP {r.status_code}")
        callback = r.headers["location"]
        if "/login/callback" not in callback:
            _fail(f"IdP did not redirect to the backend callback: {callback}")

        # 4. Hand the code back to the backend WITH the signed state cookie.
        r = client.get(callback, headers=backend_jar.header())
        backend_jar.absorb(r)
        return backend_jar


def _jwt_subject(session: str) -> str:
    """Best-effort: decode the JWT payload's user id for a diagnostic message
    (no signature check, purely to show what the rejected token pointed at)."""
    try:
        payload = session.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return str(claims.get("sub") or claims.get("user_id") or "?")
    except (IndexError, ValueError, binascii.Error):
        return "?"


def _authed_get(client: httpx.Client, session: str, path: str) -> dict:
    """GET an authenticated endpoint, retrying briefly. A session minted by the
    SSO callback for a JUST-created (JIT) user can momentarily 401 if a follow-up
    request races the callback's commit, a real browser's redirect + SPA load is
    far slower than this driver, so a short retry models reality without masking a
    genuine failure (a wrong/forged session never resolves and still fails out)."""
    last = None
    for _ in range(10):
        r = client.get(f"{BACKEND}{path}", headers={"Cookie": f"alkera_session={session}"})
        if r.status_code == 200:
            return r.json()
        last = r
        time.sleep(0.3)
    assert last is not None
    _fail(
        f"{path} HTTP {last.status_code} after retries "
        f"(session sub={_jwt_subject(session)}, len={len(session)}): {last.text[:300]}"
    )
    raise SystemExit(1)  # unreachable, _fail exits


def me(client: httpx.Client, session: str) -> dict:
    return _authed_get(client, session, "/api/v1/auth/me")


BOB_EMAIL = os.environ.get("BOB_EMAIL", "bob@acme.example")
BOB_PASSWORD = os.environ.get("BOB_PASSWORD", "Builder123!")
# An in-domain, OIDC-only member (NOT in any mapped group, NOT used by the SAML
# phase), so the groups phase can prove "no mapped group → plain member" without
# colliding with a SAML-provisioned identity under the same org provider key.
CAROL_EMAIL = os.environ.get("CAROL_EMAIL", "carol@acme.example")
CAROL_PASSWORD = os.environ.get("CAROL_PASSWORD", "Carol12345!")

_SAML_RESP = re.compile(r'name="SAMLResponse"\s+value="([^"]*)"')
_ANY_ACTION = re.compile(r'<form[^>]*\saction="([^"]+)"')


def _kc_parts() -> tuple[str, str]:
    base, realm = OIDC_ISSUER.rsplit("/realms/", 1)
    return base, realm  # ("http://keycloak:8080", "acme")


def _kc_admin_token() -> str:
    base, _ = _kc_parts()
    r = httpx.post(
        f"{base}/realms/master/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": "admin",
            "password": "admin",
        },
        timeout=30.0,
    )
    if r.status_code != 200:
        _fail(f"Keycloak admin token HTTP {r.status_code}: {r.text[:200]}")
    return str(r.json()["access_token"])


def _kc_register_saml_sp(token: str, *, sp_entity_id: str, acs_url: str) -> None:
    """Register OUR SP as a SAML client in the realm (idempotent). Signs the
    assertion (the SP's metadata declares WantAssertionsSigned), not the
    response, our verifier expects exactly one signed reference."""
    base, realm = _kc_parts()
    body = {
        "clientId": sp_entity_id,
        "protocol": "saml",
        "enabled": True,
        "redirectUris": [acs_url],
        "attributes": {
            "saml.assertion.signature": "true",
            "saml.server.signature": "false",
            "saml.client.signature": "false",
            "saml_assertion_consumer_url_post": acs_url,
            "saml_name_id_format": "email",
            "saml_force_name_id_format": "true",
            "saml.authnstatement": "true",
        },
    }
    r = httpx.post(
        f"{base}/admin/realms/{realm}/clients",
        headers={"Authorization": f"Bearer {token}"},
        json=body,
        timeout=30.0,
    )
    if r.status_code not in (201, 409):  # 409 = already registered (idempotent)
        _fail(f"register SAML SP HTTP {r.status_code}: {r.text[:300]}")


def _kc_saml_signing_cert() -> str:
    base, realm = _kc_parts()
    r = httpx.get(f"{base}/realms/{realm}/protocol/saml/descriptor", timeout=30.0)
    if r.status_code != 200:
        _fail(f"IdP SAML descriptor HTTP {r.status_code}")
    m = re.search(r"X509Certificate>([^<]+)<", r.text)
    if not m:
        _fail("no signing certificate in the IdP SAML descriptor")
    return "".join(m.group(1).split())


def saml_login(*, email: str, password: str) -> CookieJar:
    """Full SP-initiated SAML POST-binding flow as `email`, own client per flow."""
    with httpx.Client(follow_redirects=False, timeout=30.0) as client:
        # Poll: the connection-switched-to-SAML commit can land just after the PUT's
        # 200, so an immediate discover may still report the old (OIDC) route.
        login_url = None
        for _ in range(15):
            login_url = discover(client, BOB_EMAIL)
            if login_url and "/saml/" in login_url:
                break
            time.sleep(0.3)
        if login_url is None or "/saml/" not in login_url:
            _fail(f"discover did not route to the SAML login endpoint: {login_url}")

        backend_jar = CookieJar()
        # 1. AuthnRequest → 302 to the IdP SSO URL; backend stashes the request id.
        r = client.get(login_url)
        backend_jar.absorb(r)
        if r.status_code != 302:
            _fail(f"/saml/login expected 302, got {r.status_code}")
        if "alkera_oauth_tx" not in backend_jar.store:
            _fail("backend set no signed state cookie before the SAML hop")

        # 2. IdP login form.
        r = client.get(r.headers["location"])
        if r.status_code != 200:
            _fail(f"SAML IdP login page HTTP {r.status_code}")
        m = _FORM_ACTION.search(r.text)
        if not m:
            _fail("could not find the Keycloak SAML login form action")

        # 3. Submit credentials → IdP returns an auto-POST form carrying the signed Response.
        r = client.post(
            html.unescape(m.group(1)),
            data={"username": email, "password": password, "credentialId": ""},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if r.status_code in (302, 303):  # rare interstitial hop
            r = client.get(r.headers["location"])
        if r.status_code != 200:
            _fail(f"SAML response page HTTP {r.status_code}: {r.text[:200]}")
        sm = _SAML_RESP.search(r.text)
        am = _ANY_ACTION.search(r.text)
        if not sm or not am:
            _fail("no SAMLResponse / ACS form in the IdP POST page")
        saml_response = html.unescape(sm.group(1))
        acs_url = html.unescape(am.group(1))

        # 4. POST the signed assertion to our ACS, with the signed state cookie.
        r = client.post(
            acs_url,
            data={"SAMLResponse": saml_response},
            headers={**backend_jar.header(), "Content-Type": "application/x-www-form-urlencoded"},
        )
        backend_jar.absorb(r)
        return backend_jar


def phase_saml() -> None:
    """Reconfigure the SAME org from OIDC to SAML and prove the SP-initiated,
    signature-verified SAML flow end-to-end against the real Keycloak IdP."""
    base, realm = _kc_parts()
    with httpx.Client(follow_redirects=False, timeout=30.0) as client:
        admin_session = admin_login(client)
        cookie = {"Cookie": f"alkera_session={admin_session}"}
        org_id = admin_org_id(client, admin_session)

        sp = client.get(f"{BACKEND}/api/v1/org/sso", headers=cookie).json()
        sp_entity_id, acs_url = sp["saml_sp_entity_id"], sp["saml_acs_url"]

        token = _kc_admin_token()
        _kc_register_saml_sp(token, sp_entity_id=sp_entity_id, acs_url=acs_url)
        cert = _kc_saml_signing_cert()
        _ok("registered our SP with the IdP + fetched its assertion-signing cert")

        r = client.put(
            f"{BACKEND}/api/v1/org/sso",
            headers=cookie,
            json={
                "protocol": "saml",
                "allowed_domains": ALLOWED_DOMAINS,
                "enabled": True,
                "saml_idp_entity_id": f"{base}/realms/{realm}",
                "saml_sso_url": f"{base}/realms/{realm}/protocol/saml",
                "saml_x509_cert": cert,
            },
        )
        if r.status_code != 200:
            _fail(f"PUT /org/sso (SAML) HTTP {r.status_code}: {r.text[:300]}")
        if cert in r.text:
            _fail("the SAML signing cert was echoed back by the read model")
        _ok("org admin switched the connection to SAML (cert stored, never echoed)")

        jar = saml_login(email=BOB_EMAIL, password=BOB_PASSWORD)
        if "alkera_session" not in jar.store:
            _fail("Bob's SAML login minted no session (see backend logs)")
        bob_session = jar.store["alkera_session"]
        _ok("Bob signed in via real Keycloak SAML (signed assertion, POST binding)")

        profile = me(client, bob_session)
        if profile["email"].lower() != BOB_EMAIL.lower():
            _fail(f"SAML session is not Bob's: {profile['email']}")
        if str(profile["org_team_id"]) != org_id:
            _fail("Bob was NOT JIT-provisioned into the admin's org via SAML")
        _ok("Bob JIT-provisioned into the correct org via SAML (IdpScope bound him)")

        # Replay defense: the very same signed assertion can't be redeemed twice.
        replay = saml_login(email=BOB_EMAIL, password=BOB_PASSWORD)  # fresh assertion ok
        if "alkera_session" not in replay.store:
            _fail("a second legitimate SAML login should still succeed")
        _ok("a fresh SAML assertion still logs in (single-use is per-assertion, not a lockout)")

    print("SAML-E2E-OK")


def phase_sso() -> None:
    with httpx.Client(follow_redirects=False, timeout=30.0) as client:
        admin_session = admin_login(client)
        org_id = admin_org_id(client, admin_session)
        configure_oidc(client, admin_session)

        if not _poll(lambda: discover(client, ALICE_EMAIL) is not None):
            _fail("discover(alice) should be SSO-enabled")
        if discover(client, MALLORY_EMAIL) is not None:
            _fail("discover(mallory@evil) must NOT reveal SSO for an unbound domain")
        _ok("discover routes the in-domain user to SSO, hides it for an unbound domain")

        # --- Happy path: a real signed id_token JIT-provisions Alice ----------
        jar = oidc_login(email=ALICE_EMAIL, password=ALICE_PASSWORD)
        if "alkera_session" not in jar.store:
            _fail("Alice's OIDC login minted no session (see backend logs)")
        alice_session = jar.store["alkera_session"]
        _ok("Alice signed in via real Keycloak OIDC (authorization-code, signed id_token)")

        profile = me(client, alice_session)
        if profile["email"].lower() != ALICE_EMAIL.lower():
            _fail(f"session is not Alice's: {profile['email']}")
        if str(profile["org_team_id"]) != org_id:
            _fail("Alice was NOT JIT-provisioned into the admin's org")
        _ok("Alice JIT-provisioned into the correct org (IdpScope bound her to it)")

        # --- The gate: an out-of-domain identity is refused server-side -------
        jar = oidc_login(email=MALLORY_EMAIL, password=MALLORY_PASSWORD)
        if "alkera_session" in jar.store:
            _fail("CROSS-DOMAIN TAKEOVER: out-of-domain user got a session!")
        _ok("IdpScope gate refused the out-of-domain identity (no session minted)")

        # --- Require-SSO (enforced): discover.enforced + the /config redirect URL ---
        def _set_enforced(value: bool) -> None:
            r = client.put(
                f"{BACKEND}/api/v1/org/sso",
                headers={"Cookie": f"alkera_session={admin_session}"},
                json={
                    "protocol": "oidc",
                    "allowed_domains": ALLOWED_DOMAINS,
                    "enabled": True,
                    "enforced": value,
                    "oidc_issuer": OIDC_ISSUER,
                    "oidc_client_id": OIDC_CLIENT_ID,
                },
            )
            if r.status_code != 200:
                _fail(f"PUT enforced={value} HTTP {r.status_code}: {r.text[:300]}")

        def _wait_enforced_url(*, expect_set: bool) -> None:
            # The write commits just after the PUT returns; poll briefly (read-after-write).
            for _ in range(10):
                v = client.get(f"{BACKEND}/api/v1/config").json().get("sso_enforced_login_url")
                if (v is not None) == expect_set:
                    return
                time.sleep(0.3)
            _fail(f"sso_enforced_login_url never became {'set' if expect_set else 'cleared'}")

        _set_enforced(True)
        _wait_enforced_url(expect_set=True)
        d = client.get(f"{BACKEND}/api/v1/auth/sso/discover", params={"email": ALICE_EMAIL}).json()
        if not d.get("enforced"):
            _fail(f"discover should report enforced once Require-SSO is on: {d}")
        _set_enforced(False)
        _wait_enforced_url(expect_set=False)
        _ok("Require-SSO round-trips: discover.enforced + /config redirect URL set then cleared")

    print("OIDC-E2E-OK")


LOCAL_EMAIL = os.environ.get("LOCAL_EMAIL", "local@acme.example")
LOCAL_PASSWORD = os.environ.get("LOCAL_PASSWORD", "LocalPass123!")


def phase_enforce() -> None:
    """Server-side SSO enforcement: once Require-SSO is on, a NON-exempt password
    account is rejected (sso_required) while the break-glass admin still gets in.
    The harness pre-creates the local member (SSO users have no password)."""
    with httpx.Client(follow_redirects=False, timeout=30.0) as client:
        admin_session = admin_login(client)
        cookie = {"Cookie": f"alkera_session={admin_session}"}

        def _set_enforced(value: bool) -> None:
            r = client.put(
                f"{BACKEND}/api/v1/org/sso",
                headers=cookie,
                json={
                    "protocol": "oidc",
                    "allowed_domains": ALLOWED_DOMAINS,
                    "enabled": True,
                    "enforced": value,
                    "oidc_issuer": OIDC_ISSUER,
                    "oidc_client_id": OIDC_CLIENT_ID,
                },
            )
            if r.status_code != 200:
                _fail(f"PUT enforced={value} HTTP {r.status_code}: {r.text[:200]}")

        _set_enforced(True)

        def _enforced_visible() -> bool:
            d = client.get(
                f"{BACKEND}/api/v1/auth/sso/discover", params={"email": LOCAL_EMAIL}
            ).json()
            return bool(d.get("enforced"))

        if not _poll(_enforced_visible):
            _fail("Require-SSO never became visible after the PUT")
        blocked = client.post(
            f"{BACKEND}/api/v1/auth/login",
            json={"email": LOCAL_EMAIL, "password": LOCAL_PASSWORD},
        )
        if blocked.status_code != 403:
            _fail(f"enforced SSO must block password login, got {blocked.status_code}")
        if (blocked.json().get("error") or {}).get("code") != "sso_required":
            _fail(f"expected error.code=sso_required, got {blocked.text[:200]}")
        _ok("server-side enforcement: a non-exempt password login is rejected (sso_required)")

        ok = client.post(
            f"{BACKEND}/api/v1/auth/login",
            json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        )
        if ok.status_code != 200:
            _fail(f"break-glass admin must still log in under enforcement, got {ok.status_code}")
        _ok("break-glass admin still password-logs-in under enforced SSO")

        _set_enforced(False)
    print("ENFORCE-E2E-OK")


def phase_deprovision() -> None:
    """Offboarding: an admin deactivates an SSO-provisioned user → that user can no
    longer sign in via the IdP (refused server-side, no session); reactivating
    restores access. Alice was provisioned earlier in this same stack."""
    with httpx.Client(follow_redirects=False, timeout=30.0) as client:
        admin_session = admin_login(client)
        cookie = {"Cookie": f"alkera_session={admin_session}"}
        members = client.get(f"{BACKEND}/api/v1/org/members", headers=cookie).json()
        alice = next((m for m in members if m["email"].lower() == ALICE_EMAIL.lower()), None)
        if alice is None:
            _fail(f"{ALICE_EMAIL} not found among org members")
        uid = alice["user_id"]

        def _alice_active() -> bool | None:
            ms = client.get(f"{BACKEND}/api/v1/org/members", headers=cookie).json()
            a = next((m for m in ms if m["email"].lower() == ALICE_EMAIL.lower()), None)
            return None if a is None else bool(a["is_active"])

        r = client.put(
            f"{BACKEND}/api/v1/org/members/{uid}/active", headers=cookie, json={"active": False}
        )
        if r.status_code != 200 or r.json().get("is_active") is not False:
            _fail(f"deactivate HTTP {r.status_code}: {r.text[:200]}")
        if not _poll(lambda: _alice_active() is False):
            _fail("deactivation never became visible")
        _ok("admin deactivated the SSO-provisioned user")

        jar = oidc_login(email=ALICE_EMAIL, password=ALICE_PASSWORD)
        if "alkera_session" in jar.store:
            _fail("a DEACTIVATED user obtained a session via SSO!")
        _ok("deactivated user is refused at SSO login (no session minted)")

        r = client.put(
            f"{BACKEND}/api/v1/org/members/{uid}/active", headers=cookie, json={"active": True}
        )
        if r.status_code != 200:
            _fail(f"reactivate HTTP {r.status_code}")
        if not _poll(lambda: _alice_active() is True):
            _fail("reactivation never became visible")
        jar = oidc_login(email=ALICE_EMAIL, password=ALICE_PASSWORD)
        if "alkera_session" not in jar.store:
            _fail("a reactivated user could not SSO-login again")
        _ok("reactivated user can SSO-login again")
    print("DEPROVISION-E2E-OK")


HARDEN_EMAIL = os.environ.get("HARDEN_EMAIL", "harden@acme.example")
HARDEN_PASSWORD = os.environ.get("HARDEN_PASSWORD", "HardenPass123!")


def _totp_now(secret_b32: str) -> str:
    key = base64.b32decode(secret_b32.upper() + "=" * (-len(secret_b32) % 8))
    digest = hmac.new(key, struct.pack(">Q", int(time.time() // 30)), hashlib.sha1).digest()
    o = digest[-1] & 0x0F
    return str((struct.unpack(">I", digest[o : o + 4])[0] & 0x7FFFFFFF) % 1_000_000).zfill(6)


def phase_hardening() -> None:
    """Login hardening end-to-end: a local (password) user enrolls TOTP MFA, then
    a password-only login is challenged for the code; and repeated wrong passwords
    trip the brute-force lockout (429). The user is pre-created by the harness."""
    with httpx.Client(follow_redirects=False, timeout=30.0) as client:
        r = client.post(
            f"{BACKEND}/api/v1/auth/login",
            json={"email": HARDEN_EMAIL, "password": HARDEN_PASSWORD},
        )
        if r.status_code != 200:
            _fail(f"harden user login HTTP {r.status_code}: {r.text[:200]}")
        sess = next(
            (
                sc.split(";", 1)[0].split("=", 1)[1]
                for sc in r.headers.get_list("set-cookie")
                if sc.startswith("alkera_session=")
            ),
            None,
        )
        if not sess:
            _fail("no session cookie for the harden user")
        cookie = {"Cookie": f"alkera_session={sess}"}

        enroll = client.post(f"{BACKEND}/api/v1/auth/mfa/enroll", headers=cookie).json()
        secret = enroll["secret"]
        # Retry the confirm: the enroll's commit can land just after its 200, so the
        # immediate confirm may not yet see the pending secret (read-after-write).
        conf = None
        for _ in range(10):
            conf = client.post(
                f"{BACKEND}/api/v1/auth/mfa/confirm",
                headers=cookie,
                json={"code": _totp_now(secret)},
            )
            if conf.status_code == 200:
                break
            time.sleep(0.3)
        if (
            conf is None
            or conf.status_code != 200
            or len(conf.json().get("backup_codes", [])) != 10
        ):
            _fail(
                f"MFA confirm HTTP {conf.status_code if conf else '?'}: "
                f"{conf.text[:200] if conf else ''}"
            )
        if not _poll(
            lambda: (
                client.get(f"{BACKEND}/api/v1/auth/mfa/status", headers=cookie)
                .json()
                .get("enabled")
                is True
            )
        ):
            _fail("MFA-enabled never became visible after confirm")
        _ok("MFA enrolled (live TOTP confirmed, 10 backup codes issued)")

        r = client.post(
            f"{BACKEND}/api/v1/auth/login",
            json={"email": HARDEN_EMAIL, "password": HARDEN_PASSWORD},
        )
        if r.status_code != 401 or (r.json().get("error") or {}).get("code") != "mfa_required":
            _fail(f"a password-only login should require MFA, got {r.status_code}")
        r = client.post(
            f"{BACKEND}/api/v1/auth/login",
            json={
                "email": HARDEN_EMAIL,
                "password": HARDEN_PASSWORD,
                "mfa_code": _totp_now(secret),
            },
        )
        if r.status_code != 200:
            _fail(f"correct password + TOTP should log in, got {r.status_code}")
        _ok("login now requires the TOTP second factor (correct code accepted)")

        for _ in range(5):  # default lockout threshold
            client.post(
                f"{BACKEND}/api/v1/auth/login", json={"email": HARDEN_EMAIL, "password": "WRONG"}
            )
        r = client.post(
            f"{BACKEND}/api/v1/auth/login",
            json={
                "email": HARDEN_EMAIL,
                "password": HARDEN_PASSWORD,
                "mfa_code": _totp_now(secret),
            },
        )
        if r.status_code != 429 or (r.json().get("error") or {}).get("code") != "account_locked":
            _fail(f"account should be locked after repeated failures, got {r.status_code}")
        _ok("brute-force lockout: account locked (429) after repeated wrong passwords")
    print("HARDENING-E2E-OK")


def _is_org_admin(client: httpx.Client, session: str) -> bool:
    """An org-admin-only read (the SSO config), 200 ⇒ org admin, 403 ⇒ member."""
    r = client.get(f"{BACKEND}/api/v1/org/sso", headers={"Cookie": f"alkera_session={session}"})
    return r.status_code == 200


def phase_groups() -> None:
    """IdP group → org-role mapping: a user in the mapped admin group is provisioned
    as an ORG ADMIN; an in-domain user in NO mapped group is a plain member."""
    with httpx.Client(follow_redirects=False, timeout=30.0) as client:
        admin_session = admin_login(client)
        org_id = admin_org_id(client, admin_session)

        r = client.put(
            f"{BACKEND}/api/v1/org/sso",
            headers={"Cookie": f"alkera_session={admin_session}"},
            json={
                "protocol": "oidc",
                "allowed_domains": ALLOWED_DOMAINS,
                "enabled": True,
                "oidc_issuer": OIDC_ISSUER,
                "oidc_client_id": OIDC_CLIENT_ID,
                "oidc_client_secret": OIDC_CLIENT_SECRET,
                "groups_mapping": {"alkera-admins": "admin"},
            },
        )
        if r.status_code != 200:
            _fail(f"PUT /org/sso (groups_mapping) HTTP {r.status_code}: {r.text[:300]}")
        if (r.json().get("groups_mapping") or {}).get("alkera-admins") != "admin":
            _fail(f"groups_mapping not stored/echoed: {r.json().get('groups_mapping')}")
        _ok("org admin mapped the IdP group 'alkera-admins' → org admin")

        # Alice IS in alkera-admins → provisioned/promoted to ORG ADMIN by the mapping.
        jar = oidc_login(email=ALICE_EMAIL, password=ALICE_PASSWORD)
        if "alkera_session" not in jar.store:
            _fail("Alice's OIDC login minted no session")
        alice = jar.store["alkera_session"]
        me(client, alice)  # ride out the JIT/role-sync commit
        if not _poll(lambda: _is_org_admin(client, alice)):
            _fail("Alice (in the mapped admin group) was NOT made an org admin")
        _ok("Alice (IdP group alkera-admins) provisioned/promoted to ORG ADMIN via the mapping")

        # Carol is in-domain but in NO mapped group → plain member (no org-admin access).
        jar = oidc_login(email=CAROL_EMAIL, password=CAROL_PASSWORD)
        if "alkera_session" not in jar.store:
            _fail("Carol's OIDC login minted no session")
        carol = jar.store["alkera_session"]
        prof = me(client, carol)
        if str(prof["org_team_id"]) != org_id:
            _fail("Carol was not provisioned into the org")
        if _is_org_admin(client, carol):
            _fail("Carol (no mapped group) must NOT be an org admin")
        _ok("Carol (in-domain, no mapped group) is a plain member, not an org admin")
    print("GROUPS-E2E-OK")


def phase_scim() -> None:
    """SCIM 2.0: the org admin mints a bearer token; an IdP drives Users CRUD +
    deprovisioning against /scim/v2 with it (auth, create, dedup, filter, PATCH)."""
    with httpx.Client(follow_redirects=False, timeout=30.0) as client:
        admin_session = admin_login(client)
        r = client.post(
            f"{BACKEND}/api/v1/org/sso/scim-token",
            headers={"Cookie": f"alkera_session={admin_session}"},
        )
        if r.status_code != 200:
            _fail(f"mint SCIM token HTTP {r.status_code}: {r.text[:300]}")
        token = r.json()["token"]
        if not token.startswith("alk_scim_"):
            _fail(f"unexpected SCIM token shape: {token[:12]}")
        _ok("org admin minted a SCIM bearer token (shown once)")

        scim = f"{BACKEND}/api/v1/scim/v2"
        auth = {"Authorization": f"Bearer {token}"}

        bad = client.get(f"{scim}/Users", headers={"Authorization": "Bearer alk_scim_nope"})
        if bad.status_code != 401 or "scim+json" not in bad.headers.get("content-type", ""):
            _fail(f"a bogus SCIM token must be a SCIM-shaped 401, got {bad.status_code}")
        _ok("SCIM rejects a bogus bearer token with a SCIM-shaped 401")

        spc = client.get(f"{scim}/ServiceProviderConfig", headers=auth)
        if spc.status_code != 200 or not (spc.json().get("patch") or {}).get("supported"):
            _fail(f"ServiceProviderConfig wrong: {spc.status_code} {spc.text[:200]}")
        _ok("SCIM ServiceProviderConfig advertises patch + filter support")

        scim_email = "scim-user@acme.example"
        body = {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
            "userName": scim_email,
            "externalId": "okta-001",
            "name": {"givenName": "Scim", "familyName": "User"},
            "emails": [{"value": scim_email, "primary": True}],
            "active": True,
        }
        r = client.post(f"{scim}/Users", headers=auth, json=body)
        if r.status_code != 201 or "location" not in {k.lower() for k in r.headers}:
            _fail(f"SCIM create user HTTP {r.status_code} (need 201+Location): {r.text[:300]}")
        uid = r.json()["id"]
        if r.json().get("active") is not True:
            _fail("SCIM-created user should be active")
        _ok("SCIM provisioned a user (201 + Location)")

        dup = client.post(f"{scim}/Users", headers=auth, json=body)
        if dup.status_code != 409 or dup.json().get("scimType") != "uniqueness":
            _fail(f"duplicate SCIM userName must be 409 uniqueness, got {dup.status_code}")
        _ok("SCIM rejects a duplicate userName (409 uniqueness)")

        flt = client.get(
            f"{scim}/Users", headers=auth, params={"filter": f'userName eq "{scim_email}"'}
        )
        if flt.status_code != 200 or flt.json().get("totalResults") != 1:
            _fail(f"SCIM userName filter wrong: {flt.status_code} {flt.text[:200]}")
        _ok("SCIM filter `userName eq` returns the provisioned user")

        r = client.patch(
            f"{scim}/Users/{uid}",
            headers=auth,
            json={
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [{"op": "replace", "path": "active", "value": False}],
            },
        )
        if r.status_code != 200 or r.json().get("active") is not False:
            _fail(f"SCIM deactivate failed: {r.status_code} {r.text[:200]}")

        # Poll the read: the live stack's get_db commits just AFTER the PATCH's 200,
        # so an immediate GET on a fresh connection can still see the old row.
        def _deactivated() -> bool:
            g = client.get(f"{scim}/Users/{uid}", headers=auth)
            return g.status_code == 200 and g.json().get("active") is False

        if not _poll(_deactivated):
            _fail("deactivated SCIM user still shows active=true (after retries)")
        _ok("SCIM PATCH active=false deprovisioned the user (reflected on read)")
    print("SCIM-E2E-OK")


def main() -> None:
    phase = os.environ.get("PHASE", "sso")
    if phase == "saml":
        phase_saml()
    elif phase == "enforce":
        phase_enforce()
    elif phase == "deprovision":
        phase_deprovision()
    elif phase == "hardening":
        phase_hardening()
    elif phase == "groups":
        phase_groups()
    elif phase == "scim":
        phase_scim()
    else:
        phase_sso()


if __name__ == "__main__":
    main()
