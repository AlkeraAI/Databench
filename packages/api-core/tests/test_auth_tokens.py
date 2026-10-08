"""Unit tests for session-JWT encode/decode + the new `jti` claim. No DB."""

from __future__ import annotations

import json
import time
from uuid import uuid4

import jwt
import pytest
from alkera_core.auth import (
    WS_TICKET_MAX_TTL_SECONDS,
    WS_TICKET_TTL_SECONDS,
    InvalidTokenError,
    SessionClaims,
    WsMachineTicket,
    WsTicket,
    decode_session_token,
    decode_socket_ticket,
    decode_ws_machine_ticket,
    decode_ws_ticket,
    encode_cli_token,
    encode_session_token,
    encode_ws_machine_ticket,
    encode_ws_ticket,
)
from alkera_core.auth.tokens import (
    GithubInstallConfirmation,
    decode_github_install_claim,
    decode_github_install_confirmation,
    encode_github_install_claim,
    encode_github_install_confirmation,
)
from alkera_core.config import settings


def _legacy_token(**overrides: object) -> str:
    """A pre-revocation JWT: same shape minus the `jti` claim."""
    now = int(time.time())
    payload: dict[str, object] = {
        "sub": uuid4().hex,
        "email": "legacy@alkera.dev",
        "org_team_id": uuid4().hex,
        "platform_role": None,
        "iat": now,
        "exp": now + 3600,
    }
    payload.update(overrides)
    return jwt.encode(payload, settings.effective_jwt_secret, algorithm=settings.auth_jwt_alg)


def test_encode_returns_token_and_claims() -> None:
    user_id, org_id = uuid4(), uuid4()
    token, claims = encode_session_token(
        user_id=user_id, email="u@alkera.dev", org_team_id=org_id, platform_role=None
    )
    assert isinstance(token, str)
    assert claims.user_id == user_id
    assert claims.org_team_id == org_id
    assert claims.jti is not None


def test_jti_is_minted_and_round_trips() -> None:
    _token, claims = encode_session_token(
        user_id=uuid4(), email="u@alkera.dev", org_team_id=uuid4(), platform_role=None
    )
    decoded = decode_session_token(_token)
    assert claims.jti is not None
    assert decoded.jti == claims.jti


def test_each_encode_mints_a_distinct_jti() -> None:
    _, a = encode_session_token(
        user_id=uuid4(), email="u@alkera.dev", org_team_id=uuid4(), platform_role=None
    )
    _, b = encode_session_token(
        user_id=uuid4(), email="u@alkera.dev", org_team_id=uuid4(), platform_role=None
    )
    assert a.jti != b.jti


def test_cli_token_is_longer_lived_than_session_token() -> None:
    args = {
        "user_id": uuid4(),
        "email": "u@alkera.dev",
        "org_team_id": uuid4(),
        "platform_role": None,
    }
    now = int(time.time())
    _, session_claims = encode_session_token(now=now, **args)  # type: ignore[arg-type]
    _, cli_claims = encode_cli_token(now=now, **args)  # type: ignore[arg-type]
    assert cli_claims.jti is not None
    assert session_claims.jti is not None
    # CLI tokens far outlive session cookies (90d vs 1d).
    assert cli_claims.expires_at - now > session_claims.expires_at - now


def test_legacy_token_without_jti_decodes_to_none() -> None:
    """Grace path: tokens minted before revocation existed decode cleanly with
    jti=None (they remain valid until expiry, just not individually revocable)."""
    claims = decode_session_token(_legacy_token())
    assert claims.jti is None
    assert claims.email == "legacy@alkera.dev"


def test_expired_token_rejected() -> None:
    past = int(time.time()) - 10**6
    token, _ = encode_session_token(
        user_id=uuid4(), email="u@alkera.dev", org_team_id=uuid4(), platform_role=None, now=past
    )
    with pytest.raises(InvalidTokenError):
        decode_session_token(token)


def test_tampered_token_rejected() -> None:
    token, _ = encode_session_token(
        user_id=uuid4(), email="u@alkera.dev", org_team_id=uuid4(), platform_role=None
    )
    with pytest.raises(InvalidTokenError):
        decode_session_token(token[:-3] + "AAA")


# --- GitHub App install-claim state -----------------------------------------


def _install_claim_payload(**overrides: object) -> dict[str, object]:
    now = int(time.time())
    payload: dict[str, object] = {
        "typ": "github_install_claim",
        "org": uuid4().hex,
        "uid": uuid4().hex,
        "jti": uuid4().hex,
        "nch": "0" * 64,
        "iat": now,
        "exp": now + 3600,
    }
    payload.update(overrides)
    return {k: v for k, v in payload.items() if v is not None}


def test_install_claim_round_trips_its_bindings() -> None:
    org, admin = uuid4().hex, uuid4().hex
    token = encode_github_install_claim(
        org_id=org, admin_user_id=admin, nonce_hash="deadbeef", jti="abc123"
    )
    claim = decode_github_install_claim(token)
    assert claim.org_id == org
    assert claim.admin_user_id == admin
    assert claim.jti == "abc123"
    assert claim.nonce_hash == "deadbeef"


def test_install_claim_mints_distinct_jtis_by_default() -> None:
    org, admin = uuid4().hex, uuid4().hex
    a = decode_github_install_claim(
        encode_github_install_claim(org_id=org, admin_user_id=admin, nonce_hash="h")
    )
    b = decode_github_install_claim(
        encode_github_install_claim(org_id=org, admin_user_id=admin, nonce_hash="h")
    )
    assert a.jti != b.jti


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"jti": None}, id="pre-jti-state"),
        pytest.param({"jti": ""}, id="empty-jti"),
        pytest.param({"nch": None}, id="pre-browser-binding-state"),
        pytest.param({"nch": ""}, id="empty-nonce-hash"),
        pytest.param({"uid": None}, id="pre-admin-binding-state"),
        pytest.param({"uid": ""}, id="empty-admin-binding"),
        pytest.param({"org": None}, id="missing-org"),
        pytest.param({"org": ""}, id="empty-org"),
        pytest.param({"typ": "oauth_tx"}, id="wrong-typ"),
        pytest.param({"exp": int(time.time()) - 10}, id="expired"),
    ],
)
def test_install_claim_rejects_malformed_states(overrides: dict[str, object]) -> None:
    """A state minted before a binding existed -- the single-use jti, the browser
    nonce, or the initiating admin -- must read as INVALID, never as a claimable
    state with one fewer check. Same for any other malformed one."""
    token = jwt.encode(
        _install_claim_payload(**overrides),
        settings.effective_jwt_secret,
        algorithm=settings.auth_jwt_alg,
    )
    with pytest.raises(InvalidTokenError):
        decode_github_install_claim(token)


# --- GitHub App install confirmation (what the admin's click redeems) --------


def _confirmation_payload(**overrides: object) -> dict[str, object]:
    now = int(time.time())
    payload: dict[str, object] = {
        "typ": "github_install_confirm",
        "org": uuid4().hex,
        "iid": 42,
        "jti": uuid4().hex,
        "acct": "acme",
        "tgt": "Organization",
        "uid": uuid4().hex,
        "nch": "0" * 64,
        "sel": "all",
        "rps": None,
        "iat": now,
        "exp": now + 120,
    }
    payload.update(overrides)
    return {k: v for k, v in payload.items() if v is not None}


def _confirm(**overrides: object) -> str:
    now = overrides.pop("now", None)
    assert now is None or isinstance(now, int)
    fields: dict[str, object] = {
        "org_id": uuid4().hex,
        "installation_id": 99,
        "jti": "j1",
        "account_login": "acme",
        "target_type": "Organization",
        "admin_user_id": uuid4().hex,
        "nonce_hash": "deadbeef",
        "repository_selection": "all",
        "repos": None,
    }
    fields.update(overrides)
    confirmation = GithubInstallConfirmation(**fields)  # type: ignore[arg-type]
    return encode_github_install_confirmation(confirmation, now=now)


def test_confirmation_round_trips_what_the_preview_proved() -> None:
    org, admin, nch = uuid4().hex, uuid4().hex, "cafef00d"
    token = _confirm(
        org_id=org,
        admin_user_id=admin,
        nonce_hash=nch,
        repository_selection="selected",
        repos=["acme/warehouse", "acme/lakehouse"],
    )
    proof = decode_github_install_confirmation(token)
    assert proof.org_id == org
    assert proof.installation_id == 99
    assert proof.jti == "j1"
    assert proof.account_login == "acme"
    assert proof.target_type == "Organization"
    # The session binding round-trips: which admin, and which browser (nonce hash).
    assert proof.admin_user_id == admin
    assert proof.nonce_hash == nch
    # The scope the preview fetched round-trips, so the claim can seal it onto a
    # pre-webhook row instead of leaving it at the "all" default.
    assert proof.repository_selection == "selected"
    assert proof.repos == ["acme/warehouse", "acme/lakehouse"]


def test_confirmation_round_trips_an_all_scope_with_no_repo_list() -> None:
    proof = decode_github_install_confirmation(_confirm(repository_selection="all", repos=None))
    assert proof.repository_selection == "all"
    assert proof.repos is None


def test_confirmation_window_is_tight() -> None:
    """The redeem can't re-verify org ownership (the OAuth code is spent, the
    user token isn't kept), so the window between preview and click is the only
    bound on a just-removed owner still binding. Keep it short -- minutes, not
    the ten it once was."""
    from alkera_core.auth.tokens import GITHUB_INSTALL_CONFIRM_TTL_SECONDS

    assert GITHUB_INSTALL_CONFIRM_TTL_SECONDS <= 180


def test_a_confirmation_past_its_window_is_refused() -> None:
    """A stale confirmation reads as invalid, so a decision left too long can't
    bind on a click made after the window."""
    from alkera_core.auth.tokens import GITHUB_INSTALL_CONFIRM_TTL_SECONDS

    now = int(time.time())
    token = _confirm(installation_id=7, jti="j", now=now - GITHUB_INSTALL_CONFIRM_TTL_SECONDS - 5)
    with pytest.raises(InvalidTokenError):
        decode_github_install_confirmation(token)


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"org": None}, id="missing-org"),
        pytest.param({"iid": None}, id="missing-installation"),
        pytest.param({"iid": "42"}, id="installation-not-an-int"),
        pytest.param({"iid": True}, id="installation-is-a-bool"),
        pytest.param({"jti": None}, id="missing-jti"),
        pytest.param({"acct": None}, id="missing-account"),
        pytest.param({"tgt": None}, id="missing-target-type"),
        pytest.param({"uid": None}, id="missing-admin-binding"),
        pytest.param({"nch": None}, id="missing-session-binding"),
        pytest.param({"sel": None}, id="missing-repository-selection"),
        pytest.param({"typ": "github_install_claim"}, id="a-state-is-not-a-confirmation"),
        pytest.param({"exp": int(time.time()) - 10}, id="expired"),
    ],
)
def test_confirmation_rejects_anything_it_cannot_fully_state(
    overrides: dict[str, object],
) -> None:
    """The confirmation IS the proof -- the bind re-runs none of it -- so a token
    that cannot state every field it was minted with is not a proof (including the
    admin + session binding), and a state token cannot masquerade as one."""
    token = jwt.encode(
        _confirmation_payload(**overrides),
        settings.effective_jwt_secret,
        algorithm=settings.auth_jwt_alg,
    )
    with pytest.raises(InvalidTokenError):
        decode_github_install_confirmation(token)


def test_a_tampered_confirmation_is_refused() -> None:
    """Signed, so re-pointing a proof at another installation invalidates it."""
    token = _confirm()
    with pytest.raises(InvalidTokenError):
        decode_github_install_confirmation(token[:-3] + "AAA")


# ---------------------------------------------------------------------------
# Realtime socket tickets
# ---------------------------------------------------------------------------


def _session_claims(*, now: int | None = None) -> SessionClaims:
    issued = int(time.time()) if now is None else now
    return SessionClaims(
        user_id=uuid4(),
        email="u@alkera.dev",
        org_team_id=uuid4(),
        platform_role=None,
        issued_at=issued,
        expires_at=issued + 3600,
        jti=uuid4().hex,
    )


def test_ws_ticket_round_trips_its_session_binding() -> None:
    session = _session_claims()
    org_id = session.org_team_id
    now = int(time.time())
    ticket = decode_ws_ticket(
        encode_ws_ticket(user_id=session.user_id, org_id=org_id, session=session, now=now)
    )
    assert ticket.user_id == session.user_id
    assert ticket.org_id == org_id
    assert ticket.session_jti == session.jti
    assert ticket.session_issued_at == session.issued_at
    assert ticket.session_expires_at == session.expires_at
    assert ticket.issued_at == now
    assert ticket.expires_at == now + WS_TICKET_TTL_SECONDS
    assert len(ticket.jti) == 32


def test_ws_ticket_mints_a_distinct_jti_each_time_and_honours_a_given_one() -> None:
    session = _session_claims()
    org_id = session.org_team_id
    a = decode_ws_ticket(encode_ws_ticket(user_id=session.user_id, org_id=org_id, session=session))
    b = decode_ws_ticket(encode_ws_ticket(user_id=session.user_id, org_id=org_id, session=session))
    assert a.jti != b.jti
    pinned = encode_ws_ticket(
        user_id=session.user_id, org_id=org_id, session=session, jti="ticket-jti-1"
    )
    assert decode_ws_ticket(pinned).jti == "ticket-jti-1"


def test_ws_ticket_carries_no_email_or_role() -> None:
    session = _session_claims()
    raw = jwt.decode(
        encode_ws_ticket(user_id=session.user_id, org_id=session.org_team_id, session=session),
        settings.effective_jwt_secret,
        algorithms=[settings.auth_jwt_alg],
    )
    assert set(raw) == {"typ", "sub", "org", "sid", "sia", "sexp", "jti", "iat", "exp"}
    assert "@" not in json.dumps(raw)


def test_ws_ticket_expires_after_its_ttl() -> None:
    session = _session_claims()
    live = encode_ws_ticket(
        user_id=session.user_id,
        org_id=session.org_team_id,
        session=session,
        now=int(time.time()) - 20,
    )
    decode_ws_ticket(live)
    dead = encode_ws_ticket(
        user_id=session.user_id,
        org_id=session.org_team_id,
        session=session,
        now=int(time.time()) - 60,
    )
    with pytest.raises(InvalidTokenError, match="expired"):
        decode_ws_ticket(dead)


@pytest.mark.parametrize("ttl", [0, -1, 301, 10_000])
def test_ws_ticket_refuses_a_ttl_outside_the_bound(ttl: int) -> None:
    session = _session_claims()
    with pytest.raises(ValueError, match="ttl"):
        encode_ws_ticket(
            user_id=session.user_id, org_id=session.org_team_id, session=session, ttl_seconds=ttl
        )


def test_ws_ticket_accepts_the_longest_allowed_ttl() -> None:
    session = _session_claims()
    now = int(time.time())
    ticket = decode_ws_ticket(
        encode_ws_ticket(
            user_id=session.user_id,
            org_id=session.org_team_id,
            session=session,
            ttl_seconds=WS_TICKET_MAX_TTL_SECONDS,
            now=now,
        )
    )
    assert ticket.expires_at - ticket.issued_at == WS_TICKET_MAX_TTL_SECONDS


def test_ws_ticket_refuses_a_session_without_a_jti() -> None:
    """A session that cannot be revoked individually must not open sockets:
    the socket re-checks THAT session on a timer, and there would be nothing
    to re-check against."""
    legacy = SessionClaims(
        user_id=uuid4(),
        email="u@alkera.dev",
        org_team_id=uuid4(),
        platform_role=None,
        issued_at=int(time.time()),
        expires_at=int(time.time()) + 3600,
        jti=None,
    )
    with pytest.raises(ValueError, match="revoked individually"):
        encode_ws_ticket(user_id=legacy.user_id, org_id=uuid4(), session=legacy)


def test_ws_ticket_refuses_another_users_session() -> None:
    session = _session_claims()
    with pytest.raises(ValueError, match="own session"):
        encode_ws_ticket(user_id=uuid4(), org_id=session.org_team_id, session=session)


def test_ws_ticket_is_not_a_session_and_a_session_is_not_a_ticket() -> None:
    session = _session_claims()
    ticket = encode_ws_ticket(user_id=session.user_id, org_id=session.org_team_id, session=session)
    with pytest.raises(InvalidTokenError):
        decode_session_token(ticket)
    session_token, _claims = encode_session_token(
        user_id=session.user_id,
        email=session.email,
        org_team_id=session.org_team_id,
        platform_role=None,
    )
    with pytest.raises(InvalidTokenError, match="unexpected token type"):
        decode_ws_ticket(session_token)
    cli_token, _cli_claims = encode_cli_token(
        user_id=session.user_id,
        email=session.email,
        org_team_id=session.org_team_id,
        platform_role=None,
    )
    with pytest.raises(InvalidTokenError):
        decode_ws_ticket(cli_token)


def _ws_ticket_payload(**overrides: object) -> dict[str, object]:
    now = int(time.time())
    payload: dict[str, object] = {
        "typ": "ws_ticket",
        "sub": uuid4().hex,
        "org": uuid4().hex,
        "sid": uuid4().hex,
        "sia": now - 10,
        "sexp": now + 3600,
        "jti": uuid4().hex,
        "iat": now,
        "exp": now + 30,
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"sub": None}, id="missing-user"),
        pytest.param({"sub": "not-hex"}, id="user-not-a-uuid"),
        pytest.param({"org": None}, id="missing-org"),
        pytest.param({"org": ""}, id="empty-org"),
        pytest.param({"sid": None}, id="missing-session-jti"),
        pytest.param({"sid": ""}, id="empty-session-jti"),
        pytest.param({"sia": None}, id="missing-session-issued-at"),
        pytest.param({"sia": "12"}, id="session-issued-at-not-an-int"),
        pytest.param({"sia": True}, id="session-issued-at-a-bool"),
        pytest.param({"sexp": None}, id="missing-session-expiry"),
        pytest.param({"jti": None}, id="missing-jti"),
        pytest.param({"typ": "session"}, id="wrong-type"),
        pytest.param({"typ": None}, id="no-type"),
    ],
)
def test_ws_ticket_rejects_anything_it_cannot_fully_state(overrides: dict[str, object]) -> None:
    """A ticket minted before a binding existed, or with one stripped, must
    read as invalid — never as a ticket with one fewer check."""
    payload = {k: v for k, v in _ws_ticket_payload(**overrides).items() if v is not None}
    token = jwt.encode(payload, settings.effective_jwt_secret, algorithm=settings.auth_jwt_alg)
    with pytest.raises(InvalidTokenError):
        decode_ws_ticket(token)


def test_a_tampered_ws_ticket_is_refused() -> None:
    token = jwt.encode(
        _ws_ticket_payload(), settings.effective_jwt_secret, algorithm=settings.auth_jwt_alg
    )
    forged = jwt.encode(_ws_ticket_payload(), "not-the-secret", algorithm=settings.auth_jwt_alg)
    decode_ws_ticket(token)
    with pytest.raises(InvalidTokenError):
        decode_ws_ticket(forged)
    header, body, signature = token.split(".")
    with pytest.raises(InvalidTokenError):
        decode_ws_ticket(f"{header}.{body}.{signature[:-3]}xyz")


# --- the agent assertion on a socket ticket ------------------------------------

MACHINE_AGENT = "6a2c1b4d-3e1f-4b3c-8e6d-82b4d0a1c222"


def test_ws_ticket_carries_the_agent_it_was_minted_with() -> None:
    """A daemon mints its ticket with the agent headers on the request; the
    ticket carries that id so the socket speaks as the agent. A person's
    ticket carries none, and the assertion changes nothing else about it."""
    session = _session_claims()
    org_id = session.org_team_id
    person = decode_ws_ticket(
        encode_ws_ticket(user_id=session.user_id, org_id=org_id, session=session)
    )
    assert person.agent_id is None
    raw_token = encode_ws_ticket(
        user_id=session.user_id, org_id=org_id, session=session, agent_id=MACHINE_AGENT
    )
    agent = decode_ws_ticket(raw_token)
    assert agent.agent_id == MACHINE_AGENT
    assert (agent.user_id, agent.org_id, agent.session_jti) == (
        session.user_id,
        org_id,
        session.jti,
    )
    raw = jwt.decode(raw_token, settings.effective_jwt_secret, algorithms=[settings.auth_jwt_alg])
    assert raw["agt"] == MACHINE_AGENT
    assert "@" not in json.dumps(raw), "still no email and no role on the ticket"


@pytest.mark.parametrize(
    "agent_id",
    [
        pytest.param("", id="empty"),
        pytest.param("-leading-dash", id="bad-first-character"),
        pytest.param("has space", id="whitespace"),
        pytest.param("x" * 129, id="too-long"),
    ],
)
def test_ws_ticket_refuses_an_agent_id_the_assertion_grammar_does_not_admit(
    agent_id: str,
) -> None:
    session = _session_claims()
    with pytest.raises(ValueError, match="agent id"):
        encode_ws_ticket(
            user_id=session.user_id, org_id=session.org_team_id, session=session, agent_id=agent_id
        )


@pytest.mark.parametrize(
    "claim",
    [
        pytest.param("", id="empty"),
        pytest.param("has space", id="malformed"),
        pytest.param(42, id="not-a-string"),
        pytest.param(["m1"], id="a-list"),
    ],
)
def test_a_ticket_whose_agent_claim_is_unreadable_is_not_a_ticket(claim: object) -> None:
    """A forged or corrupted agent claim is refused outright — never read as a
    person's own ticket with the agent quietly dropped."""
    token = jwt.encode(
        _ws_ticket_payload(agt=claim),
        settings.effective_jwt_secret,
        algorithm=settings.auth_jwt_alg,
    )
    with pytest.raises(InvalidTokenError, match="agent id"):
        decode_ws_ticket(token)


# --------------------------------------------------------------------------- #
# The chat's gateway token: minted from a session, refused as one
# --------------------------------------------------------------------------- #


def _session(*, now: int | None = None, ttl: int | None = None) -> tuple[str, SessionClaims]:
    from alkera_core.auth import encode_cli_token

    token, claims = encode_cli_token(
        user_id=uuid4(), email="box@alkera.dev", org_team_id=uuid4(), platform_role=None, now=now
    )
    if ttl is None:
        return token, claims
    # A shorter session than the CLI's 90 days: same claims, an earlier `exp`.
    short = SessionClaims(
        user_id=claims.user_id,
        email=claims.email,
        org_team_id=claims.org_team_id,
        platform_role=None,
        issued_at=claims.issued_at,
        expires_at=claims.issued_at + ttl,
        jti=claims.jti,
    )
    return token, short


def test_gateway_token_carries_the_chat_and_the_session_it_was_minted_under() -> None:
    from alkera_core.auth import GATEWAY_TOKEN_TTL_SECONDS, decode_gateway_token, mint_gateway_token

    _, session = _session()
    chat_id = uuid4()
    now = int(time.time())
    token, minted = mint_gateway_token(
        user_id=session.user_id,
        org_team_id=session.org_team_id,
        chat_id=chat_id,
        session=session,
        now=now,
    )
    decoded = decode_gateway_token(token)
    assert decoded == minted
    assert decoded.user_id == session.user_id
    assert decoded.org_id == session.org_team_id
    assert decoded.chat_id == chat_id
    assert decoded.parent_jti == session.jti
    assert decoded.parent_issued_at == session.issued_at
    assert decoded.expires_at == now + GATEWAY_TOKEN_TTL_SECONDS
    assert decoded.jti != session.jti, "its own jti, not the session's"
    raw = jwt.decode(token, options={"verify_signature": False})
    assert raw["scope"] == "gateway"
    assert "email" not in raw and "platform_role" not in raw, "ids only"


def test_gateway_token_never_outlives_its_session() -> None:
    from alkera_core.auth import mint_gateway_token

    now = int(time.time())
    _, session = _session(now=now, ttl=600)
    _, minted = mint_gateway_token(
        user_id=session.user_id,
        org_team_id=session.org_team_id,
        chat_id=uuid4(),
        session=session,
        now=now,
    )
    assert minted.expires_at == session.expires_at


def test_gateway_token_from_an_expired_session_is_refused() -> None:
    from alkera_core.auth import mint_gateway_token

    now = int(time.time())
    _, session = _session(now=now - 1000, ttl=600)
    with pytest.raises(ValueError, match="expired"):
        mint_gateway_token(
            user_id=session.user_id,
            org_team_id=session.org_team_id,
            chat_id=uuid4(),
            session=session,
            now=now,
        )


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        pytest.param(lambda s: {"session": _legacy_claims(s)}, "revoked individually", id="no-jti"),
        pytest.param(
            lambda s: {"user_id": uuid4()}, "needs their membership", id="other-user-no-membership"
        ),
        pytest.param(
            lambda s: {"membership_id": uuid4(), "membership_epoch": 0},
            "carries the session's membership",
            id="self-billed-with-another-membership",
        ),
        pytest.param(lambda s: {"org_team_id": uuid4()}, "session's own org", id="other-org"),
        pytest.param(lambda s: {"ttl_seconds": 0}, "ttl must be", id="ttl-zero"),
        pytest.param(lambda s: {"ttl_seconds": 24 * 3600 + 1}, "ttl must be", id="ttl-over-max"),
    ],
)
def test_gateway_token_mint_refuses(mutate, match) -> None:
    from alkera_core.auth import mint_gateway_token

    _, session = _session()
    kwargs = {
        "user_id": session.user_id,
        "org_team_id": session.org_team_id,
        "chat_id": uuid4(),
        "session": session,
    }
    kwargs.update(mutate(session))
    with pytest.raises(ValueError, match=match):
        mint_gateway_token(**kwargs)


def _member_session() -> SessionClaims:
    """A box operator's session minted for their membership."""
    from alkera_core.auth import encode_session_token

    _, claims = encode_session_token(
        user_id=uuid4(),
        email="operator@alkera.dev",
        org_team_id=uuid4(),
        platform_role=None,
        membership_id=uuid4(),
        membership_epoch=3,
    )
    return claims


def test_a_gateway_token_billing_someone_else_names_both_and_rides_the_session() -> None:
    """A box on its operator's session runs another member's chat: the token
    bills that member through their membership, and names the operator and
    the operator's membership so it still dies with them."""
    from alkera_core.auth import decode_gateway_token, mint_gateway_token

    session = _member_session()
    owner, owner_membership = uuid4(), uuid4()
    token, minted = mint_gateway_token(
        user_id=owner,
        org_team_id=session.org_team_id,
        chat_id=uuid4(),
        session=session,
        membership_id=owner_membership,
        membership_epoch=7,
    )
    decoded = decode_gateway_token(token)
    assert decoded == minted
    assert (decoded.user_id, decoded.membership_id, decoded.membership_epoch) == (
        owner,
        owner_membership,
        7,
    )
    assert decoded.delegated
    assert decoded.parent_user_id == session.user_id
    assert (decoded.parent_membership_id, decoded.parent_membership_epoch) == (
        session.membership_id,
        session.membership_epoch,
    )
    assert (decoded.parent_jti, decoded.parent_issued_at) == (session.jti, session.issued_at)


def test_a_gateway_token_billing_its_session_names_no_parent_user() -> None:
    from alkera_core.auth import decode_gateway_token, mint_gateway_token

    session = _member_session()
    token, _ = mint_gateway_token(
        user_id=session.user_id,
        org_team_id=session.org_team_id,
        chat_id=uuid4(),
        session=session,
    )
    decoded = decode_gateway_token(token)
    assert not decoded.delegated
    assert decoded.parent_user_id is None and decoded.parent_membership_id is None
    assert decoded.membership_id == session.membership_id
    raw = jwt.decode(token, options={"verify_signature": False})
    assert not {"psub", "pmid", "pmep"} & set(raw), "the old shape, byte for byte"


def _gateway_payload(**extra: object) -> dict[str, object]:
    now = int(time.time())
    return {
        "scope": "gateway",
        "sub": uuid4().hex,
        "org": uuid4().hex,
        "chat": uuid4().hex,
        "jti": uuid4().hex,
        "parent_jti": uuid4().hex,
        "parent_iat": now,
        "iat": now,
        "exp": now + 60,
        **extra,
    }


@pytest.mark.parametrize(
    ("extra", "match"),
    [
        pytest.param({"psub": "SELF"}, "does not delegate to itself", id="parent-is-the-billed"),
        pytest.param(
            {"psub": "OTHER", "pk": "machine"}, "only a session parent", id="machine-delegates"
        ),
        pytest.param({"pmid": "MID", "pmep": 0}, "without parent", id="parent-membership-alone"),
        pytest.param({"psub": "not-hex"}, "parent user", id="malformed-parent"),
        pytest.param({"psub": "OTHER", "pmid": "MID"}, "membership", id="half-a-parent-membership"),
    ],
)
def test_a_gateway_token_whose_parent_cannot_be_stated_is_refused(
    extra: dict[str, object], match: str
) -> None:
    from alkera_core.auth import decode_gateway_token

    payload = _gateway_payload()
    filled = {
        key: (
            payload["sub"]
            if value == "SELF"
            else uuid4().hex
            if value in ("OTHER", "MID")
            else value
        )
        for key, value in extra.items()
    }
    payload.update(filled)
    token = jwt.encode(payload, settings.effective_jwt_secret, algorithm=settings.auth_jwt_alg)
    with pytest.raises(InvalidTokenError, match=match):
        decode_gateway_token(token)


def _legacy_claims(session: SessionClaims) -> SessionClaims:
    return SessionClaims(
        user_id=session.user_id,
        email=session.email,
        org_team_id=session.org_team_id,
        platform_role=None,
        issued_at=session.issued_at,
        expires_at=session.expires_at,
        jti=None,
    )


def test_a_gateway_token_is_not_a_session() -> None:
    from alkera_core.auth import mint_gateway_token

    _, session = _session()
    token, _ = mint_gateway_token(
        user_id=session.user_id, org_team_id=session.org_team_id, chat_id=uuid4(), session=session
    )
    with pytest.raises(InvalidTokenError, match="'gateway'-scoped token is not a session"):
        decode_session_token(token)
    with pytest.raises(InvalidTokenError):
        decode_session_token(token, allow_expired=True)


def test_a_session_is_not_a_gateway_token() -> None:
    from alkera_core.auth import decode_gateway_token

    token, _ = _session()
    with pytest.raises(InvalidTokenError, match="not a gateway token"):
        decode_gateway_token(token)


@pytest.mark.parametrize(
    "scope",
    [pytest.param("session", id="explicit-session"), pytest.param(None, id="absent")],
)
def test_a_session_scope_present_or_absent_still_decodes(scope) -> None:
    token = _legacy_token(**({} if scope is None else {"scope": scope}), jti="abc")
    assert decode_session_token(token).jti == "abc"


@pytest.mark.parametrize("scope", ["gateway", "ws_ticket", "anything-else", ""])
def test_any_other_scope_on_a_session_shaped_token_is_refused(scope) -> None:
    # The full session claim set is present: the scope alone refuses it, so a
    # forged "session with a scope" cannot pass as either kind.
    token = _legacy_token(scope=scope, jti="abc")
    with pytest.raises(InvalidTokenError, match="not a session"):
        decode_session_token(token)


def test_gateway_credential_dispatches_on_scope() -> None:
    from alkera_core.auth import GatewayTokenClaims, decode_gateway_credential, mint_gateway_token

    session_token, session = _session()
    gateway_token, minted = mint_gateway_token(
        user_id=session.user_id, org_team_id=session.org_team_id, chat_id=uuid4(), session=session
    )
    assert decode_gateway_credential(session_token) == session
    resolved = decode_gateway_credential(gateway_token)
    assert isinstance(resolved, GatewayTokenClaims) and resolved == minted


def test_an_expired_gateway_token_is_expired_not_merely_invalid() -> None:
    from alkera_core.auth import TokenExpiredError, decode_gateway_token, mint_gateway_token

    now = int(time.time())
    _, session = _session(now=now - 7200)
    token, _ = mint_gateway_token(
        user_id=session.user_id,
        org_team_id=session.org_team_id,
        chat_id=uuid4(),
        session=session,
        ttl_seconds=60,
        now=now - 7200,
    )
    with pytest.raises(TokenExpiredError):
        decode_gateway_token(token)


@pytest.mark.parametrize(
    "drop",
    ["sub", "org", "chat", "jti", "parent_jti", "parent_iat"],
)
def test_a_gateway_token_missing_a_binding_is_refused(drop: str) -> None:
    from alkera_core.auth import decode_gateway_token

    now = int(time.time())
    payload: dict[str, object] = {
        "scope": "gateway",
        "sub": uuid4().hex,
        "org": uuid4().hex,
        "chat": uuid4().hex,
        "jti": uuid4().hex,
        "parent_jti": uuid4().hex,
        "parent_iat": now,
        "iat": now,
        "exp": now + 60,
    }
    del payload[drop]
    token = jwt.encode(payload, settings.effective_jwt_secret, algorithm=settings.auth_jwt_alg)
    with pytest.raises(InvalidTokenError, match="gateway token"):
        decode_gateway_token(token)


def test_a_gateway_token_signed_with_another_key_is_refused() -> None:
    from alkera_core.auth import decode_gateway_token

    now = int(time.time())
    payload = {
        "scope": "gateway",
        "sub": uuid4().hex,
        "org": uuid4().hex,
        "chat": uuid4().hex,
        "jti": uuid4().hex,
        "parent_jti": uuid4().hex,
        "parent_iat": now,
        "iat": now,
        "exp": now + 60,
    }
    token = jwt.encode(payload, "not-the-signing-key", algorithm=settings.auth_jwt_alg)
    with pytest.raises(InvalidTokenError):
        decode_gateway_token(token)


# --- gateway token under a machine credential ---------------------------------


def test_a_machine_gateway_token_names_its_credential_as_its_parent() -> None:
    from alkera_core.auth import (
        GATEWAY_PARENT_MACHINE,
        GATEWAY_TOKEN_TTL_SECONDS,
        decode_gateway_token,
        mint_machine_gateway_token,
    )

    owner, org, chat, credential = uuid4(), uuid4(), uuid4(), uuid4()
    now = int(time.time())
    credential_minted_at = now - 30 * 86_400
    token, minted = mint_machine_gateway_token(
        user_id=owner,
        org_team_id=org,
        chat_id=chat,
        credential_id=credential,
        credential_issued_at=credential_minted_at,
        now=now,
    )
    decoded = decode_gateway_token(token)
    assert decoded == minted
    assert decoded.parent_kind == GATEWAY_PARENT_MACHINE
    assert decoded.parent_jti == credential.hex
    assert decoded.parent_issued_at == credential_minted_at
    assert (decoded.user_id, decoded.org_id, decoded.chat_id) == (owner, org, chat)
    # A credential has no expiry of its own to floor the token's: the TTL alone.
    assert decoded.expires_at == now + GATEWAY_TOKEN_TTL_SECONDS
    assert decoded.jti != credential.hex, "its own jti, not the credential's"
    raw = jwt.decode(token, options={"verify_signature": False})
    assert raw["scope"] == "gateway"
    assert raw["pk"] == "machine"
    assert "email" not in raw and "platform_role" not in raw, "ids only"


def test_a_session_minted_gateway_token_is_a_session_parent_without_saying_so() -> None:
    """The session mint writes no parent kind — the shape every token before
    parents had kinds carries — and decodes as a session parent, so nothing
    minted earlier is refused, or read as a machine's."""
    from alkera_core.auth import GATEWAY_PARENT_SESSION, decode_gateway_token, mint_gateway_token

    _, session = _session()
    token, minted = mint_gateway_token(
        user_id=session.user_id, org_team_id=session.org_team_id, chat_id=uuid4(), session=session
    )
    assert "pk" not in jwt.decode(token, options={"verify_signature": False})
    assert decode_gateway_token(token).parent_kind == GATEWAY_PARENT_SESSION
    assert minted.parent_kind == GATEWAY_PARENT_SESSION


@pytest.mark.parametrize(
    "kind",
    [
        pytest.param("operator", id="a-kind-nobody-defined"),
        pytest.param("", id="empty"),
        pytest.param("MACHINE", id="wrong-case"),
        pytest.param(1, id="not-a-string"),
        pytest.param(None, id="null"),
    ],
)
def test_a_gateway_token_naming_an_unknown_parent_kind_is_refused(kind: object) -> None:
    """A parent kind nobody defined is never read as "just a session": a forged
    claim must not pick the branch with the fewest checks."""
    from alkera_core.auth import decode_gateway_token

    now = int(time.time())
    payload: dict[str, object] = {
        "scope": "gateway",
        "sub": uuid4().hex,
        "org": uuid4().hex,
        "chat": uuid4().hex,
        "jti": uuid4().hex,
        "pk": kind,
        "parent_jti": uuid4().hex,
        "parent_iat": now,
        "iat": now,
        "exp": now + 60,
    }
    token = jwt.encode(payload, settings.effective_jwt_secret, algorithm=settings.auth_jwt_alg)
    with pytest.raises(InvalidTokenError, match="unknown parent kind"):
        decode_gateway_token(token)


@pytest.mark.parametrize("ttl", [0, -1, 24 * 3600 + 1])
def test_a_machine_gateway_token_mint_refuses_a_ttl_outside_the_bounds(ttl: int) -> None:
    from alkera_core.auth import mint_machine_gateway_token

    with pytest.raises(ValueError, match="ttl must be"):
        mint_machine_gateway_token(
            user_id=uuid4(),
            org_team_id=uuid4(),
            chat_id=uuid4(),
            credential_id=uuid4(),
            credential_issued_at=int(time.time()),
            ttl_seconds=ttl,
        )


def test_a_machine_gateway_token_is_not_a_session_and_is_a_gateway_credential() -> None:
    from alkera_core.auth import (
        GatewayTokenClaims,
        decode_gateway_credential,
        mint_machine_gateway_token,
    )

    token, _ = mint_machine_gateway_token(
        user_id=uuid4(),
        org_team_id=uuid4(),
        chat_id=uuid4(),
        credential_id=uuid4(),
        credential_issued_at=int(time.time()),
    )
    with pytest.raises(InvalidTokenError, match="'gateway'-scoped token is not a session"):
        decode_session_token(token)
    assert isinstance(decode_gateway_credential(token), GatewayTokenClaims)


# --- the machine socket ticket -------------------------------------------------


def _machine_ticket(**overrides: object) -> str:
    fields: dict[str, object] = {
        "credential_id": uuid4(),
        "machine_id": uuid4(),
        "org_id": uuid4(),
    }
    fields.update(overrides)
    return encode_ws_machine_ticket(**fields)  # type: ignore[arg-type]


def test_a_machine_ticket_round_trips_the_credential_and_the_machine() -> None:
    credential_id, machine_id, org_id = uuid4(), uuid4(), uuid4()
    now = int(time.time())
    ticket = decode_ws_machine_ticket(
        encode_ws_machine_ticket(
            credential_id=credential_id, machine_id=machine_id, org_id=org_id, now=now
        )
    )
    assert ticket == WsMachineTicket(
        credential_id=credential_id,
        machine_id=machine_id,
        org_id=org_id,
        jti=ticket.jti,
        issued_at=now,
        expires_at=now + WS_TICKET_TTL_SECONDS,
    )
    assert len(ticket.jti) == 32
    assert ticket.jti != decode_ws_machine_ticket(_machine_ticket()).jti


def test_a_machine_ticket_honours_a_given_jti_and_ttl() -> None:
    now = int(time.time())
    pinned = decode_ws_machine_ticket(
        _machine_ticket(jti="abc123", ttl_seconds=WS_TICKET_MAX_TTL_SECONDS, now=now)
    )
    assert pinned.jti == "abc123"
    assert (pinned.issued_at, pinned.expires_at) == (now, now + WS_TICKET_MAX_TTL_SECONDS)


@pytest.mark.parametrize("ttl", [0, -1, WS_TICKET_MAX_TTL_SECONDS + 1])
def test_a_machine_ticket_refuses_a_ttl_outside_the_bound(ttl: int) -> None:
    with pytest.raises(ValueError, match="ticket ttl"):
        _machine_ticket(ttl_seconds=ttl)


def test_a_machine_ticket_expires_after_its_ttl() -> None:
    old = _machine_ticket(now=int(time.time()) - WS_TICKET_TTL_SECONDS - 120)
    with pytest.raises(InvalidTokenError):
        decode_ws_machine_ticket(old)
    with pytest.raises(InvalidTokenError):
        decode_socket_ticket(old)


def test_a_machine_ticket_and_a_persons_ticket_are_never_read_as_each_other() -> None:
    """Two tickets, two ``typ`` values under one signature: the decoder for
    one refuses the other outright, and the dispatcher hands each back as its
    own type. Neither can be read as the other with a claim missing."""
    session = _session_claims()
    person = encode_ws_ticket(user_id=session.user_id, org_id=session.org_team_id, session=session)
    machine = _machine_ticket()
    with pytest.raises(InvalidTokenError, match="unexpected token type"):
        decode_ws_ticket(machine)
    with pytest.raises(InvalidTokenError, match="unexpected token type"):
        decode_ws_machine_ticket(person)
    assert isinstance(decode_socket_ticket(person), WsTicket)
    assert isinstance(decode_socket_ticket(machine), WsMachineTicket)
    assert decode_socket_ticket(machine) == decode_ws_machine_ticket(machine)
    assert decode_socket_ticket(person) == decode_ws_ticket(person)


def test_no_session_is_a_socket_ticket_and_no_machine_ticket_is_a_session() -> None:
    session = _session_claims()
    session_token, _claims = encode_session_token(
        user_id=session.user_id,
        email=session.email,
        org_team_id=session.org_team_id,
        platform_role=None,
    )
    cli_token, _cli = encode_cli_token(
        user_id=session.user_id,
        email=session.email,
        org_team_id=session.org_team_id,
        platform_role=None,
    )
    for token in (session_token, cli_token):
        with pytest.raises(InvalidTokenError, match="unexpected token type"):
            decode_ws_machine_ticket(token)
        with pytest.raises(InvalidTokenError, match="unexpected token type"):
            decode_socket_ticket(token)
    with pytest.raises(InvalidTokenError):
        decode_session_token(_machine_ticket())


def _machine_ticket_payload(**overrides: object) -> dict[str, object]:
    now = int(time.time())
    payload: dict[str, object] = {
        "typ": "ws_machine_ticket",
        "cred": uuid4().hex,
        "mach": uuid4().hex,
        "org": uuid4().hex,
        "jti": uuid4().hex,
        "iat": now,
        "exp": now + 30,
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"cred": None}, id="no-credential"),
        pytest.param({"cred": ""}, id="empty-credential"),
        pytest.param({"cred": "not-a-uuid"}, id="credential-not-an-id"),
        pytest.param({"cred": 42}, id="credential-not-a-string"),
        pytest.param({"mach": None}, id="no-machine"),
        pytest.param({"mach": "zz"}, id="machine-not-an-id"),
        pytest.param({"org": None}, id="no-org"),
        pytest.param({"org": "org-1"}, id="org-not-an-id"),
        pytest.param({"jti": None}, id="no-jti"),
        pytest.param({"jti": ""}, id="empty-jti"),
        pytest.param({"iat": None}, id="no-iat"),
        pytest.param({"iat": "now"}, id="iat-not-an-int"),
        pytest.param({"iat": True}, id="iat-a-bool"),
        pytest.param({"typ": "ws_ticket"}, id="a-persons-typ-over-machine-claims"),
        pytest.param({"typ": None}, id="no-typ"),
    ],
)
def test_a_machine_ticket_rejects_anything_it_cannot_fully_state(
    overrides: dict[str, object],
) -> None:
    payload = {k: v for k, v in _machine_ticket_payload(**overrides).items() if v is not None}
    token = jwt.encode(payload, settings.effective_jwt_secret, algorithm=settings.auth_jwt_alg)
    with pytest.raises(InvalidTokenError):
        decode_ws_machine_ticket(token)
    with pytest.raises(InvalidTokenError):
        decode_socket_ticket(token)


def test_a_tampered_machine_ticket_is_refused() -> None:
    payload = _machine_ticket_payload()
    token = jwt.encode(payload, settings.effective_jwt_secret, algorithm=settings.auth_jwt_alg)
    decode_ws_machine_ticket(token)
    forged = jwt.encode(payload, "not-the-secret", algorithm=settings.auth_jwt_alg)
    with pytest.raises(InvalidTokenError):
        decode_ws_machine_ticket(forged)
    with pytest.raises(InvalidTokenError):
        decode_socket_ticket(forged)
    header, body, signature = token.split(".")
    with pytest.raises(InvalidTokenError):
        decode_socket_ticket(f"{header}.{body}.{signature[:-3]}xyz")
