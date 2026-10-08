"""Session JWTs.

Signed with HS256 + the runtime secret. Encoded into a single cookie
(`alkera_session`) and decoded on every authenticated request.

Claims:
- sub: user_id (UUID hex)
- email
- org_team_id (UUID hex)
- platform_role: "alkera_support" | "alkera_admin" | None
- jti: token id (UUID hex) — recorded server-side so the token can be
  individually revoked (see ``alkera_core.auth.revocation``).
- exp: unix timestamp
- iat: unix timestamp
- mid: the org membership the token was minted for (UUID hex). Absent on tokens
  minted before memberships existed.
- mep: that membership's credential epoch at mint (int). Absent with ``mid``.

The org claim is the request's org: every door verifies the membership it
names (``alkera_core.auth.tenancy``) and builds the acting context from it.

Lives in ``api-core`` (not the backend) so the backend, the CLI/daemon, and
the model-gateway all decode the same shape from one place.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import jwt

from alkera_core.auth.machine_token import MACHINE_WORKER_TOKEN_PREFIX
from alkera_core.authz.headers import AGENT_ID_PATTERN
from alkera_core.config import settings
from alkera_core.models._enums import PlatformRole

COOKIE_NAME: str = settings.auth_cookie_name


class InvalidTokenError(Exception):
    """Raised when a session token is missing/expired/tampered.

    Caller (typically a FastAPI dep) maps this to HTTP 401.
    """


class TokenExpiredError(InvalidTokenError):
    """The token verified but its ``exp`` has passed.

    Kept distinct from every other failure so a client holding a refresh
    credential can tell "renew and retry" apart from "you are signed out":
    a tampered or revoked token must never be answered with the renewal hint.
    """


def _candidate_jwt_secrets() -> list[str]:
    """Secrets to TRY when verifying a JWT: the active signing secret first, then
    any retired ``AUTH_JWT_SECRET_PREVIOUS``. Encoding always uses only the active
    secret — these exist so a rotation can verify already-issued tokens until they
    expire instead of invalidating every live session at once."""
    return [settings.effective_jwt_secret, *settings.effective_jwt_secret_previous_list]


def _decode_jwt(token: str, *, verify_exp: bool = True) -> dict[str, Any]:
    """Verify + decode a JWT against the active secret, falling back to each retired
    secret in turn. Raises ``TokenExpiredError`` if it is expired (and
    ``verify_exp`` is on), else ``InvalidTokenError`` if it is malformed or
    verifies under none of the configured secrets."""
    last_exc: jwt.PyJWTError | None = None
    for secret in _candidate_jwt_secrets():
        try:
            payload: dict[str, Any] = jwt.decode(
                token,
                secret,
                algorithms=[settings.auth_jwt_alg],
                options={"verify_exp": verify_exp},
            )
            return payload
        except jwt.ExpiredSignatureError as exc:
            # Expiry is independent of which secret signed the token, so once we
            # hit it there's no point trying the rest — the token is simply old.
            raise TokenExpiredError(str(exc)) from exc
        except jwt.PyJWTError as exc:
            last_exc = exc  # wrong secret (or malformed) — try the next candidate
    raise InvalidTokenError(str(last_exc) if last_exc else "invalid token")


@dataclass(frozen=True, slots=True)
class SessionClaims:
    user_id: UUID
    email: str
    org_team_id: UUID
    platform_role: PlatformRole | None
    issued_at: int
    expires_at: int
    # `jti` is None for legacy tokens minted before revocation existed — they
    # remain valid until expiry but can't be individually revoked.
    jti: str | None = None
    # The chat a gateway token was minted for — set only when the gateway
    # resolved a chat-scoped token into this shape, so metering can attribute
    # the spend to the chat. A person's own session names no chat.
    chat_id: UUID | None = None
    # The org membership the token was minted for and its credential epoch at
    # mint. None on tokens minted before memberships existed: those resolve the
    # membership by (user, org) and count as epoch 0.
    membership_id: UUID | None = None
    membership_epoch: int | None = None


# --- Token scope -------------------------------------------------------------
#
# A session token carries no ``scope`` claim (or ``"session"``); every other
# scope names a credential that is NOT a session and must be refused wherever a
# session is expected. ``"gateway"`` is the chat's inference credential: the
# box mints one per chat from its own session and hands the agent THAT, so a
# prompt-injected agent that reads its own environment holds a token the API
# refuses on every route and the gateway bills to the person whose chat it is.

SESSION_SCOPE = "session"
GATEWAY_SCOPE = "gateway"

#: What a chat's gateway token was minted under — the ``pk`` claim. A SESSION
#: parent is a person's (or a box operator's) session: the token dies with its
#: ``jti`` and its revoke-all. A MACHINE parent is a box's own machine
#: credential: the token is live while the credential is — revoked with it,
#: ended with its machine — and no person's logout bears on it. A token that
#: names a parent kind nobody defined is refused outright.
GATEWAY_PARENT_SESSION = "session"
GATEWAY_PARENT_MACHINE = "machine"
GATEWAY_PARENT_KINDS = frozenset({GATEWAY_PARENT_SESSION, GATEWAY_PARENT_MACHINE})

#: The longest a gateway token may live — a captured one is an inference
#: credential on the box's account for that long.
GATEWAY_TOKEN_MAX_TTL_SECONDS: int = 24 * 3600
#: How long a chat's gateway token lives (never past the session it was minted
#: under). The token reaches the agent only when it is spawned, so renewing it
#: means restarting the agent between turns; the longer the token, the longer a
#: single turn may run on it.
GATEWAY_TOKEN_TTL_SECONDS: int = GATEWAY_TOKEN_MAX_TTL_SECONDS
#: The daemon mints a fresh token every time it opens the chat's session, and
#: again before a turn starts when less than this remains — so every turn starts
#: with at least this long before its credential lapses.
GATEWAY_TOKEN_REFRESH_BEFORE_SECONDS: int = 12 * 3600


@dataclass(frozen=True, slots=True)
class GatewayTokenClaims:
    """A decoded chat-scoped gateway token: who is billed, which org, which
    chat, the token's own ``jti``, and the parent it was minted under (its
    id and issue time) so the gateway re-runs revocation against THAT parent.
    Under a session parent, a logout or a revoke-all kills the chat's token
    with it; under a machine parent, revoking the box's credential does."""

    user_id: UUID
    org_id: UUID
    chat_id: UUID
    jti: str
    parent_jti: str
    parent_issued_at: int
    issued_at: int
    expires_at: int
    parent_kind: str = GATEWAY_PARENT_SESSION
    # The membership the token bills through: the billed person's in the
    # token's org (the parent session's own when it bills itself). None on
    # tokens minted before memberships existed.
    membership_id: UUID | None = None
    membership_epoch: int | None = None
    # Set only when a session parent bills someone else (a box on its
    # operator's session running another person's chat): the operator whose
    # session the token dies with, and that session's membership, so the
    # gateway decides revocation against the operator and billing against the
    # person it names. None when the parent session's user is the one billed.
    parent_user_id: UUID | None = None
    parent_membership_id: UUID | None = None
    parent_membership_epoch: int | None = None

    @property
    def delegated(self) -> bool:
        """Whether the parent session belongs to someone other than the
        person the token bills."""
        return self.parent_user_id is not None


def _membership_claims(
    membership_id: UUID | None, membership_epoch: int | None, *, prefix: str = ""
) -> dict[str, object]:
    """The ``mid``/``mep`` pair, emitted only together and only when known.
    ``prefix`` names whose membership it is (``"p"``: the parent session's)."""
    if membership_id is None:
        if membership_epoch is not None:
            raise ValueError("a membership epoch needs the membership it belongs to")
        return {}
    if membership_epoch is None or isinstance(membership_epoch, bool) or membership_epoch < 0:
        raise ValueError("a membership needs the non-negative credential epoch it was minted at")
    return {f"{prefix}mid": membership_id.hex, f"{prefix}mep": membership_epoch}


def _membership_of(
    payload: dict[str, Any], *, what: str, prefix: str = ""
) -> tuple[UUID | None, int | None]:
    """The ``mid``/``mep`` pair a token carries, ``(None, None)`` for a token
    minted before memberships existed. A token that carries one without the
    other, or either malformed, is not a token at all."""
    raw_mid = payload.get(f"{prefix}mid")
    raw_mep = payload.get(f"{prefix}mep")
    if raw_mid is None and raw_mep is None:
        return None, None
    if not isinstance(raw_mid, str) or not raw_mid:
        raise InvalidTokenError(f"malformed {what}: membership")
    if isinstance(raw_mep, bool) or not isinstance(raw_mep, int) or raw_mep < 0:
        raise InvalidTokenError(f"malformed {what}: membership epoch")
    try:
        return UUID(hex=raw_mid), raw_mep
    except ValueError as exc:
        raise InvalidTokenError(f"malformed {what}: membership") from exc


def mint_gateway_token(
    *,
    user_id: UUID,
    org_team_id: UUID,
    chat_id: UUID,
    session: SessionClaims,
    membership_id: UUID | None = None,
    membership_epoch: int | None = None,
    ttl_seconds: int = GATEWAY_TOKEN_TTL_SECONDS,
    now: int | None = None,
) -> tuple[str, GatewayTokenClaims]:
    """Mint a gateway-only token for ``chat_id`` from ``session``.

    Signed with the session signing key, so the gateway verifies it the way it
    verifies a session; bound to the session by ``parent_jti`` so revoking the
    session revokes it. It never outlives the session it was minted under:
    ``exp`` is the earlier of ``now + ttl`` and the session's own expiry.

    ``user_id`` is the person billed. When it is the session's own user the
    token bills the session and carries the session's membership
    (``mid``/``mep``); ``membership_id``/``membership_epoch`` are then not
    given. When it is someone else (a box on its operator's session running
    another person's chat) the token bills that person through
    ``membership_id``/``membership_epoch``, their membership in the session's
    org, and also names the session's user (``psub``) and membership
    (``pmid``/``pmep``), so the gateway still revokes it with the session and
    with the operator's membership. Either way a membership revocation that
    kills the session kills the token too.

    Refuses (``ValueError``) a session without a ``jti`` (a token for a
    session that cannot be revoked individually would outlive a logout), a
    session not in ``org_team_id`` (a token never bills outside its parent's
    org), a token billing someone else without their membership, a token
    billing the session that names a membership of its own, a TTL outside
    ``1..GATEWAY_TOKEN_MAX_TTL_SECONDS``, and a session that has already expired.
    """
    if session.jti is None:
        raise ValueError("a gateway token needs a session that can be revoked individually")
    if session.org_team_id != org_team_id:
        raise ValueError("a gateway token must bill the session's own org")
    delegated = session.user_id != user_id
    if delegated and membership_id is None:
        raise ValueError("a gateway token billing someone else needs their membership")
    if not delegated and (membership_id is not None or membership_epoch is not None):
        raise ValueError("a gateway token billing the session carries the session's membership")
    if not 1 <= ttl_seconds <= GATEWAY_TOKEN_MAX_TTL_SECONDS:
        raise ValueError(f"gateway token ttl must be 1..{GATEWAY_TOKEN_MAX_TTL_SECONDS} seconds")
    issued = int(time.time()) if now is None else now
    expires = min(issued + ttl_seconds, session.expires_at)
    if expires <= issued:
        raise ValueError("the session this gateway token would be minted from has expired")
    jti = uuid4().hex
    if delegated:
        billed_mid, billed_mep = membership_id, membership_epoch
        parent: dict[str, object] = {
            "psub": session.user_id.hex,
            **_membership_claims(session.membership_id, session.membership_epoch, prefix="p"),
        }
    else:
        billed_mid, billed_mep = session.membership_id, session.membership_epoch
        parent = {}
    payload: dict[str, object] = {
        "scope": GATEWAY_SCOPE,
        "sub": user_id.hex,
        "org": org_team_id.hex,
        "chat": chat_id.hex,
        "jti": jti,
        "parent_jti": session.jti,
        "parent_iat": session.issued_at,
        "iat": issued,
        "exp": expires,
        **_membership_claims(billed_mid, billed_mep),
        **parent,
    }
    token = jwt.encode(payload, settings.effective_jwt_secret, algorithm=settings.auth_jwt_alg)
    return token, GatewayTokenClaims(
        user_id=user_id,
        org_id=org_team_id,
        chat_id=chat_id,
        jti=jti,
        parent_jti=session.jti,
        parent_issued_at=session.issued_at,
        issued_at=issued,
        expires_at=expires,
        membership_id=billed_mid,
        membership_epoch=billed_mep,
        parent_user_id=session.user_id if delegated else None,
        parent_membership_id=session.membership_id if delegated else None,
        parent_membership_epoch=session.membership_epoch if delegated else None,
    )


def mint_machine_gateway_token(
    *,
    user_id: UUID,
    org_team_id: UUID,
    chat_id: UUID,
    credential_id: UUID,
    credential_issued_at: int,
    membership_id: UUID | None = None,
    membership_epoch: int | None = None,
    ttl_seconds: int = GATEWAY_TOKEN_TTL_SECONDS,
    now: int | None = None,
) -> tuple[str, GatewayTokenClaims]:
    """Mint a gateway-only token for ``chat_id`` under a box's machine
    credential rather than a session.

    ``user_id`` and ``org_team_id`` are the chat's OWNER and org: the box has
    no person behind it, so the spend is attributed to the person whose chat
    it runs — their pools, their caps — and to nobody else. The parent is the
    credential (``credential_id``, and when it was issued), so the gateway
    checks the credential's standing where it would check a session's
    revocation; a credential has no expiry of its own, so the token's is the
    TTL alone. ``membership_id``/``membership_epoch`` are the owner's
    membership in the chat's org, so a revocation of that membership kills the
    token. Refuses (``ValueError``) a TTL outside
    ``1..GATEWAY_TOKEN_MAX_TTL_SECONDS``.
    """
    if not 1 <= ttl_seconds <= GATEWAY_TOKEN_MAX_TTL_SECONDS:
        raise ValueError(f"gateway token ttl must be 1..{GATEWAY_TOKEN_MAX_TTL_SECONDS} seconds")
    issued = int(time.time()) if now is None else now
    expires = issued + ttl_seconds
    jti = uuid4().hex
    payload: dict[str, object] = {
        "scope": GATEWAY_SCOPE,
        "sub": user_id.hex,
        "org": org_team_id.hex,
        "chat": chat_id.hex,
        "jti": jti,
        "pk": GATEWAY_PARENT_MACHINE,
        "parent_jti": credential_id.hex,
        "parent_iat": credential_issued_at,
        "iat": issued,
        "exp": expires,
        **_membership_claims(membership_id, membership_epoch),
    }
    token = jwt.encode(payload, settings.effective_jwt_secret, algorithm=settings.auth_jwt_alg)
    return token, GatewayTokenClaims(
        user_id=user_id,
        org_id=org_team_id,
        chat_id=chat_id,
        jti=jti,
        parent_jti=credential_id.hex,
        parent_issued_at=credential_issued_at,
        issued_at=issued,
        expires_at=expires,
        parent_kind=GATEWAY_PARENT_MACHINE,
        membership_id=membership_id,
        membership_epoch=membership_epoch,
    )


def _gateway_claims_from(payload: dict[str, Any]) -> GatewayTokenClaims:
    if payload.get("scope") != GATEWAY_SCOPE:
        raise InvalidTokenError("not a gateway token")
    try:
        user_id = UUID(hex=_require_str_claim(payload, "sub", what="gateway token: missing user"))
        org_id = UUID(hex=_require_str_claim(payload, "org", what="gateway token: missing org"))
        chat_id = UUID(hex=_require_str_claim(payload, "chat", what="gateway token: missing chat"))
    except ValueError as exc:
        raise InvalidTokenError(f"malformed gateway token: {exc}") from exc
    # A token minted before parents had kinds names none and was minted under
    # a session; one that names a kind nobody defined is not "just a session".
    parent_kind = payload.get("pk", GATEWAY_PARENT_SESSION)
    if parent_kind not in GATEWAY_PARENT_KINDS:
        raise InvalidTokenError("malformed gateway token: unknown parent kind")
    membership_id, membership_epoch = _membership_of(payload, what="gateway token")
    parent_user_id, parent_mid, parent_mep = _delegating_parent_of(
        payload, user_id=user_id, parent_kind=parent_kind
    )
    return GatewayTokenClaims(
        user_id=user_id,
        org_id=org_id,
        chat_id=chat_id,
        jti=_require_str_claim(payload, "jti", what="gateway token: missing jti"),
        parent_jti=_require_str_claim(payload, "parent_jti", what="gateway token: missing session"),
        parent_issued_at=_require_int_claim(
            payload, "parent_iat", what="gateway token: missing session issue time"
        ),
        issued_at=_require_int_claim(payload, "iat", what="gateway token: missing iat"),
        expires_at=_require_int_claim(payload, "exp", what="gateway token: missing exp"),
        parent_kind=parent_kind,
        membership_id=membership_id,
        membership_epoch=membership_epoch,
        parent_user_id=parent_user_id,
        parent_membership_id=parent_mid,
        parent_membership_epoch=parent_mep,
    )


def _delegating_parent_of(
    payload: dict[str, Any], *, user_id: UUID, parent_kind: str
) -> tuple[UUID | None, UUID | None, int | None]:
    """The parent session's user and membership when it bills someone else
    (``psub``, ``pmid``/``pmep``), else ``(None, None, None)``. Only a session
    parent can delegate, and only to someone other than itself; any other
    shape is not a token, so no reader can mistake whose session it rides."""
    raw = payload.get("psub")
    if raw is None:
        if payload.get("pmid") is not None or payload.get("pmep") is not None:
            raise InvalidTokenError("malformed gateway token: parent membership without parent")
        return None, None, None
    if parent_kind != GATEWAY_PARENT_SESSION:
        raise InvalidTokenError("malformed gateway token: only a session parent delegates")
    if not isinstance(raw, str) or not raw:
        raise InvalidTokenError("malformed gateway token: parent user")
    try:
        parent_user_id = UUID(hex=raw)
    except ValueError as exc:
        raise InvalidTokenError("malformed gateway token: parent user") from exc
    if parent_user_id == user_id:
        raise InvalidTokenError("malformed gateway token: a parent does not delegate to itself")
    parent_mid, parent_mep = _membership_of(payload, what="gateway token parent", prefix="p")
    return parent_user_id, parent_mid, parent_mep


def decode_gateway_token(token: str) -> GatewayTokenClaims:
    """Verify and decode a chat-scoped gateway token. Raises ``TokenExpiredError``
    past ``exp`` and ``InvalidTokenError`` on every other failure — including a
    session token, which is not a gateway token however valid it is."""
    return _gateway_claims_from(_decode_jwt(token))


def decode_gateway_credential(token: str) -> SessionClaims | GatewayTokenClaims:
    """What the GATEWAY accepts as a bearer: a session token (a person's, the
    CLI's, a box's) or a chat-scoped gateway token — verified once, dispatched
    on ``scope``. The API never calls this: it decodes sessions only, and
    :func:`decode_session_token` refuses a gateway token outright."""
    payload = _decode_jwt(token)
    if payload.get("scope") == GATEWAY_SCOPE:
        return _gateway_claims_from(payload)
    return _session_claims_from(payload)


def _encode_token(
    *,
    user_id: UUID,
    email: str,
    org_team_id: UUID,
    platform_role: PlatformRole | None,
    ttl_seconds: int,
    now: int | None,
    membership_id: UUID | None = None,
    membership_epoch: int | None = None,
) -> tuple[str, SessionClaims]:
    issued = int(time.time()) if now is None else now
    expires = issued + ttl_seconds
    jti = uuid4().hex
    payload: dict[str, object] = {
        "sub": user_id.hex,
        "email": email,
        "org_team_id": org_team_id.hex,
        "platform_role": platform_role.value if platform_role else None,
        "jti": jti,
        "iat": issued,
        "exp": expires,
        **_membership_claims(membership_id, membership_epoch),
    }
    token = jwt.encode(
        payload,
        settings.effective_jwt_secret,
        algorithm=settings.auth_jwt_alg,
    )
    claims = SessionClaims(
        user_id=user_id,
        email=email,
        org_team_id=org_team_id,
        platform_role=platform_role,
        issued_at=issued,
        expires_at=expires,
        jti=jti,
        membership_id=membership_id,
        membership_epoch=membership_epoch,
    )
    return token, claims


def encode_session_token(
    *,
    user_id: UUID,
    email: str,
    org_team_id: UUID,
    platform_role: PlatformRole | None,
    now: int | None = None,
    membership_id: UUID | None = None,
    membership_epoch: int | None = None,
) -> tuple[str, SessionClaims]:
    """Encode a session JWT (cookie). Returns (token, claims).

    Outside tests only ``backend.auth.membership_tokens`` calls this, with the
    membership the token is minted for."""
    return _encode_token(
        user_id=user_id,
        email=email,
        org_team_id=org_team_id,
        platform_role=platform_role,
        ttl_seconds=settings.auth_token_ttl_seconds,
        now=now,
        membership_id=membership_id,
        membership_epoch=membership_epoch,
    )


def encode_cli_token(
    *,
    user_id: UUID,
    email: str,
    org_team_id: UUID,
    platform_role: PlatformRole | None,
    now: int | None = None,
    membership_id: UUID | None = None,
    membership_epoch: int | None = None,
) -> tuple[str, SessionClaims]:
    """Encode a CLI JWT — same shape as a session token, longer TTL.

    Same signing key + algorithm so `decode_session_token` accepts it
    transparently when the CLI sends it as `Authorization: Bearer ...`.
    """
    return _encode_token(
        user_id=user_id,
        email=email,
        org_team_id=org_team_id,
        platform_role=platform_role,
        ttl_seconds=settings.auth_cli_token_ttl_seconds,
        now=now,
        membership_id=membership_id,
        membership_epoch=membership_epoch,
    )


# --- Transient OAuth handshake tokens --------------------------------------
#
# Two short-lived JWTs, signed with the SAME secret as the session token but
# stamped with a distinct ``typ`` claim so one can never be substituted for the
# other (or for a session). They keep the OAuth flow stateless — no server-side
# session store — consistent with the rest of the auth design.

# How long the cross-site handshake (state cookie) and the post-callback
# registration ticket stay valid. Generous enough for a human to complete a
# consent screen / fill a form; short enough to bound replay.
OAUTH_STATE_TTL_SECONDS: int = 600  # 10 min
OAUTH_REGISTER_TICKET_TTL_SECONDS: int = 900  # 15 min

_TYP_OAUTH_STATE = "oauth_tx"
_TYP_OAUTH_REGISTER = "oauth_register"


@dataclass(frozen=True, slots=True)
class OAuthStateClaims:
    """The signed transient state carried in the ``alkera_oauth_tx`` cookie."""

    state: str
    nonce: str
    provider: str
    intent: str
    invite_token: str | None
    return_to: str | None


@dataclass(frozen=True, slots=True)
class RegisterTicket:
    """Server-trusted proof of a verified external identity awaiting org choice.

    Minted by the OAuth callback when no account exists for the email, consumed
    by the OAuth-register endpoint. The email/provider/subject come ONLY from
    here (never from the client) — the user may only edit their name + org.
    """

    provider: str
    subject: str
    email: str
    email_verified: bool
    first_name: str
    last_name: str
    invite_token: str | None


def encode_oauth_state(
    *,
    state: str,
    nonce: str,
    provider: str,
    intent: str,
    invite_token: str | None,
    return_to: str | None,
    ttl_seconds: int = OAUTH_STATE_TTL_SECONDS,
    now: int | None = None,
) -> str:
    issued = int(time.time()) if now is None else now
    payload: dict[str, object] = {
        "typ": _TYP_OAUTH_STATE,
        "state": state,
        "nonce": nonce,
        "provider": provider,
        "intent": intent,
        "invite_token": invite_token,
        "return_to": return_to,
        "iat": issued,
        "exp": issued + ttl_seconds,
    }
    return jwt.encode(payload, settings.effective_jwt_secret, algorithm=settings.auth_jwt_alg)


def decode_oauth_state(token: str) -> OAuthStateClaims:
    """Decode the transient OAuth state. Raises ``InvalidTokenError`` on any
    failure (expired, bad signature, wrong ``typ``, missing claim)."""
    payload = _decode_typed(token, expected_typ=_TYP_OAUTH_STATE)
    try:
        return OAuthStateClaims(
            state=str(payload["state"]),
            nonce=str(payload["nonce"]),
            provider=str(payload["provider"]),
            intent=str(payload["intent"]),
            invite_token=(str(payload["invite_token"]) if payload.get("invite_token") else None),
            return_to=(str(payload["return_to"]) if payload.get("return_to") else None),
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise InvalidTokenError(f"malformed oauth state: {exc}") from exc


def encode_register_ticket(
    *,
    provider: str,
    subject: str,
    email: str,
    email_verified: bool,
    first_name: str,
    last_name: str,
    invite_token: str | None,
    ttl_seconds: int = OAUTH_REGISTER_TICKET_TTL_SECONDS,
    now: int | None = None,
) -> str:
    issued = int(time.time()) if now is None else now
    payload: dict[str, object] = {
        "typ": _TYP_OAUTH_REGISTER,
        "provider": provider,
        "subject": subject,
        "email": email,
        "email_verified": email_verified,
        "first_name": first_name,
        "last_name": last_name,
        "invite_token": invite_token,
        "iat": issued,
        "exp": issued + ttl_seconds,
    }
    return jwt.encode(payload, settings.effective_jwt_secret, algorithm=settings.auth_jwt_alg)


def decode_register_ticket(token: str) -> RegisterTicket:
    """Decode an OAuth registration ticket. Raises ``InvalidTokenError`` on any
    failure (expired, bad signature, wrong ``typ``, missing claim)."""
    payload = _decode_typed(token, expected_typ=_TYP_OAUTH_REGISTER)
    try:
        return RegisterTicket(
            provider=str(payload["provider"]),
            subject=str(payload["subject"]),
            email=str(payload["email"]),
            email_verified=bool(payload["email_verified"]),
            first_name=str(payload.get("first_name") or ""),
            last_name=str(payload.get("last_name") or ""),
            invite_token=(str(payload["invite_token"]) if payload.get("invite_token") else None),
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise InvalidTokenError(f"malformed register ticket: {exc}") from exc


def _decode_typed(token: str, *, expected_typ: str) -> dict[str, object]:
    payload = _decode_jwt(token)
    if payload.get("typ") != expected_typ:
        raise InvalidTokenError(f"unexpected token type: {payload.get('typ')!r}")
    return payload


# --- Realtime socket ticket ---------------------------------------------------
#
# A browser cannot put a header on a WebSocket handshake, and a credential in
# the URL would land in every access log between the browser and the process.
# So a socket is opened with a TICKET: a 30-second, single-use HS256 JWT the
# client mints from its session over an ordinary authenticated request and
# then offers as a subprotocol entry (``alkera-ticket.<jwt>``) in the
# ``Sec-WebSocket-Protocol`` header, which the edge redacts from its logs.
#
# The ticket is bound to the SESSION it was minted from — its ``jti``, its
# ``iat`` and its ``exp`` — so the socket can re-run the revocation check
# against that very session on a timer and close when it is logged out or
# expires. It carries ids only: no email, no role, nothing a leak would expose.

_TYP_WS_TICKET = "ws_ticket"

WS_TICKET_TTL_SECONDS: int = 30
#: The longest a ticket may live: past this a captured ticket is a credential
#: worth stealing.
WS_TICKET_MAX_TTL_SECONDS: int = 300


@dataclass(frozen=True, slots=True)
class WsTicket:
    """A decoded socket ticket: who, which org, which session it is bound to
    (so the socket re-checks THAT session), the ticket's own single-use
    ``jti``, and — when the mint request carried the agent assertion — the
    agent id the socket speaks as (``None`` for a person's own socket)."""

    user_id: UUID
    org_id: UUID
    session_jti: str
    session_issued_at: int
    session_expires_at: int
    jti: str
    issued_at: int
    expires_at: int
    agent_id: str | None = None
    # The membership of the session the ticket was minted from; None for a
    # ticket from a session minted before memberships existed.
    membership_id: UUID | None = None
    membership_epoch: int | None = None


def encode_ws_ticket(
    *,
    user_id: UUID,
    org_id: UUID,
    session: SessionClaims,
    jti: str | None = None,
    ttl_seconds: int = WS_TICKET_TTL_SECONDS,
    now: int | None = None,
    agent_id: str | None = None,
) -> str:
    """Mint a socket ticket for ``user_id`` in ``org_id`` from ``session``.

    Refuses (``ValueError``) a session without a ``jti`` — a ticket for a
    session that cannot be revoked individually would outlive a logout — a
    session that belongs to a different user or another org, a TTL outside
    ``1..WS_TICKET_MAX_TTL_SECONDS``, and an ``agent_id`` the assertion
    grammar does not admit.

    ``agent_id`` is the agent assertion the mint request carried — the id a
    daemon speaks as, its machine — copied onto the ticket so the socket knows
    it is an agent acting for the user rather than the user themself. The user
    behind it was authenticated; the id is what the client asserted, exactly
    as on a REST call.
    """
    if session.jti is None:
        raise ValueError("a socket ticket needs a session that can be revoked individually")
    if session.user_id != user_id:
        raise ValueError("a socket ticket must be minted from the user's own session")
    if session.org_team_id != org_id:
        raise ValueError("a socket ticket must be for the session's own org")
    if not 1 <= ttl_seconds <= WS_TICKET_MAX_TTL_SECONDS:
        raise ValueError(f"ticket ttl must be 1..{WS_TICKET_MAX_TTL_SECONDS} seconds")
    if agent_id is not None and not AGENT_ID_PATTERN.fullmatch(agent_id):
        raise ValueError(f"agent id {agent_id!r} is not a valid agent id")
    issued = int(time.time()) if now is None else now
    payload: dict[str, object] = {
        "typ": _TYP_WS_TICKET,
        "sub": user_id.hex,
        "org": org_id.hex,
        "sid": session.jti,
        "sia": session.issued_at,
        "sexp": session.expires_at,
        "jti": jti or uuid4().hex,
        "iat": issued,
        "exp": issued + ttl_seconds,
        **_membership_claims(session.membership_id, session.membership_epoch),
    }
    if agent_id is not None:
        payload["agt"] = agent_id
    return jwt.encode(payload, settings.effective_jwt_secret, algorithm=settings.auth_jwt_alg)


def _require_int_claim(payload: dict[str, object], key: str, *, what: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidTokenError(f"malformed {what}")
    return value


def decode_ws_ticket(token: str) -> WsTicket:
    """Verify and decode a socket ticket. Raises ``InvalidTokenError`` on any
    failure (expired, bad signature, wrong ``typ``, a missing binding) — a
    ticket that cannot state every claim it was minted with is not a ticket,
    never a ticket with one fewer check."""
    payload = _decode_typed(token, expected_typ=_TYP_WS_TICKET)
    try:
        user_id = UUID(hex=_require_str_claim(payload, "sub", what="socket ticket: missing user"))
        org_id = UUID(hex=_require_str_claim(payload, "org", what="socket ticket: missing org"))
    except ValueError as exc:
        raise InvalidTokenError(f"malformed socket ticket: {exc}") from exc
    agent_id = payload.get("agt")
    if agent_id is not None and not (
        isinstance(agent_id, str) and AGENT_ID_PATTERN.fullmatch(agent_id)
    ):
        # A ticket that names an agent names one the assertion grammar admits,
        # or it is no ticket at all: an unreadable agent is never "just the user".
        raise InvalidTokenError("malformed socket ticket: agent id")
    membership_id, membership_epoch = _membership_of(payload, what="socket ticket")
    return WsTicket(
        agent_id=agent_id,
        user_id=user_id,
        org_id=org_id,
        session_jti=_require_str_claim(payload, "sid", what="socket ticket: missing session"),
        session_issued_at=_require_int_claim(
            payload, "sia", what="socket ticket: missing session issue time"
        ),
        session_expires_at=_require_int_claim(
            payload, "sexp", what="socket ticket: missing session expiry"
        ),
        jti=_require_str_claim(payload, "jti", what="socket ticket: missing jti"),
        issued_at=_require_int_claim(payload, "iat", what="socket ticket: missing iat"),
        expires_at=_require_int_claim(payload, "exp", what="socket ticket: missing exp"),
        membership_id=membership_id,
        membership_epoch=membership_epoch,
    )


# A box on its own machine credential opens its socket with a MACHINE ticket:
# the same thirty-second single-use shape, bound to the credential rather than
# to a session — there is no session and no person behind it. It carries the
# credential's id (what the socket re-checks on its tick) and the machine the
# credential holds (what every channel it may hold is bound to), under a
# ``typ`` of its own, so a decoder written for a person's ticket refuses it
# outright and a decoder written for this one refuses a person's: neither can
# be read as the other with one claim missing.

_TYP_WS_MACHINE_TICKET = "ws_machine_ticket"


@dataclass(frozen=True, slots=True)
class WsMachineTicket:
    """A decoded machine socket ticket: which credential minted it, which
    machine that credential holds, the org (the operator org the credential
    was minted in, or the one org an org-bound worker is bound to), and the
    ticket's own single-use ``jti``. ``org_bound`` says the ticket was minted
    by an org-bound worker, so the socket is that worker's and reaches
    ``org_id`` alone; a ticket minted before the claim existed is not."""

    credential_id: UUID
    machine_id: UUID
    org_id: UUID
    jti: str
    issued_at: int
    expires_at: int
    org_bound: bool = False


def encode_ws_machine_ticket(
    *,
    credential_id: UUID,
    machine_id: UUID,
    org_id: UUID,
    jti: str | None = None,
    ttl_seconds: int = WS_TICKET_TTL_SECONDS,
    now: int | None = None,
    org_bound: bool = False,
) -> str:
    """Mint a socket ticket for the box holding ``credential_id`` on
    ``machine_id``; ``org_bound`` for an org-bound worker, whose socket must
    be rebuilt bound to ``org_id`` alone. Refuses (``ValueError``) a TTL
    outside ``1..WS_TICKET_MAX_TTL_SECONDS``, exactly as a person's ticket
    does."""
    if not 1 <= ttl_seconds <= WS_TICKET_MAX_TTL_SECONDS:
        raise ValueError(f"ticket ttl must be 1..{WS_TICKET_MAX_TTL_SECONDS} seconds")
    issued = int(time.time()) if now is None else now
    payload: dict[str, object] = {
        "typ": _TYP_WS_MACHINE_TICKET,
        "cred": credential_id.hex,
        "mach": machine_id.hex,
        "org": org_id.hex,
        "jti": jti or uuid4().hex,
        "iat": issued,
        "exp": issued + ttl_seconds,
    }
    if org_bound:
        payload["bound"] = True
    return jwt.encode(payload, settings.effective_jwt_secret, algorithm=settings.auth_jwt_alg)


def _machine_ticket_of(payload: dict[str, object]) -> WsMachineTicket:
    try:
        credential_id = UUID(
            hex=_require_str_claim(payload, "cred", what="machine ticket: missing credential")
        )
        machine_id = UUID(
            hex=_require_str_claim(payload, "mach", what="machine ticket: missing machine")
        )
        org_id = UUID(hex=_require_str_claim(payload, "org", what="machine ticket: missing org"))
    except ValueError as exc:
        raise InvalidTokenError(f"malformed machine ticket: {exc}") from exc
    return WsMachineTicket(
        credential_id=credential_id,
        machine_id=machine_id,
        org_id=org_id,
        jti=_require_str_claim(payload, "jti", what="machine ticket: missing jti"),
        issued_at=_require_int_claim(payload, "iat", what="machine ticket: missing iat"),
        expires_at=_require_int_claim(payload, "exp", what="machine ticket: missing exp"),
        org_bound=payload.get("bound") is True,
    )


def decode_ws_machine_ticket(token: str) -> WsMachineTicket:
    """Verify and decode a machine socket ticket; ``InvalidTokenError`` on any
    failure, a person's ticket included."""
    return _machine_ticket_of(_decode_typed(token, expected_typ=_TYP_WS_MACHINE_TICKET))


def decode_socket_ticket(token: str) -> WsTicket | WsMachineTicket:
    """Whichever socket ticket ``token`` is — a person's or a machine's —
    decided by the ``typ`` the signature covers, never by which claims happen
    to be present. Anything else is ``InvalidTokenError``."""
    payload = _decode_jwt(token)
    typ = payload.get("typ")
    if typ == _TYP_WS_MACHINE_TICKET:
        return _machine_ticket_of(payload)
    if typ == _TYP_WS_TICKET:
        return decode_ws_ticket(token)
    raise InvalidTokenError(f"unexpected token type: {typ!r}")


# --- Org-bound worker credential ------------------------------------------------
#
# A box serving several orgs runs one process per org, and that process must
# not hold anything that reaches another org. The box's machine credential
# stays with the box; for each org it mints, through the backend, a short-lived
# credential bound to (machine, org). It is signed rather than stored: the
# org, the machine and the machine credential behind it are claims the
# signature covers, and every request re-checks that the machine credential
# still stands, still holds that machine and may still serve that org, so a
# revoke of the box ends every worker credential it minted at once.

_TYP_MACHINE_WORKER = "machine_worker"

#: How long a worker credential lives. The box refreshes it well before this.
MACHINE_WORKER_TOKEN_TTL_SECONDS: int = 15 * 60
#: The longest one may live: past this a stolen one is worth keeping.
MACHINE_WORKER_TOKEN_MAX_TTL_SECONDS: int = 60 * 60


@dataclass(frozen=True, slots=True)
class MachineWorkerClaims:
    """A decoded org-bound worker credential: the machine credential that
    minted it, the machine that credential holds, the one org it is bound to,
    and its own ``jti``."""

    credential_id: UUID
    machine_id: UUID
    org_id: UUID
    jti: str
    issued_at: int
    expires_at: int


def mint_machine_worker_token(
    *,
    credential_id: UUID,
    machine_id: UUID,
    org_id: UUID,
    ttl_seconds: int = MACHINE_WORKER_TOKEN_TTL_SECONDS,
    now: int | None = None,
) -> tuple[str, MachineWorkerClaims]:
    """Mint the worker credential for ``org_id`` on the box holding
    ``credential_id`` on ``machine_id``. Returns ``(raw, claims)``; the raw
    value is the marker followed by the signed token. Refuses (``ValueError``)
    a TTL outside ``1..MACHINE_WORKER_TOKEN_MAX_TTL_SECONDS``."""
    if not 1 <= ttl_seconds <= MACHINE_WORKER_TOKEN_MAX_TTL_SECONDS:
        raise ValueError(
            f"worker credential ttl must be 1..{MACHINE_WORKER_TOKEN_MAX_TTL_SECONDS} seconds"
        )
    issued = int(time.time()) if now is None else now
    jti = uuid4().hex
    payload: dict[str, object] = {
        "typ": _TYP_MACHINE_WORKER,
        "cred": credential_id.hex,
        "mach": machine_id.hex,
        "org": org_id.hex,
        "jti": jti,
        "iat": issued,
        "exp": issued + ttl_seconds,
    }
    token = jwt.encode(payload, settings.effective_jwt_secret, algorithm=settings.auth_jwt_alg)
    return MACHINE_WORKER_TOKEN_PREFIX + token, MachineWorkerClaims(
        credential_id=credential_id,
        machine_id=machine_id,
        org_id=org_id,
        jti=jti,
        issued_at=issued,
        expires_at=issued + ttl_seconds,
    )


def decode_machine_worker_token(raw: str) -> MachineWorkerClaims:
    """Verify and decode a worker credential. ``TokenExpiredError`` past its
    ``exp`` (the box refreshes and retries), ``InvalidTokenError`` on every
    other failure: no marker, a bad signature, another token type, or a
    missing or malformed claim."""
    if not raw.startswith(MACHINE_WORKER_TOKEN_PREFIX):
        raise InvalidTokenError("not a worker credential")
    payload = _decode_typed(
        raw.removeprefix(MACHINE_WORKER_TOKEN_PREFIX), expected_typ=_TYP_MACHINE_WORKER
    )
    try:
        credential_id = UUID(
            hex=_require_str_claim(payload, "cred", what="worker credential: missing credential")
        )
        machine_id = UUID(
            hex=_require_str_claim(payload, "mach", what="worker credential: missing machine")
        )
        org_id = UUID(hex=_require_str_claim(payload, "org", what="worker credential: missing org"))
    except ValueError as exc:
        raise InvalidTokenError(f"malformed worker credential: {exc}") from exc
    return MachineWorkerClaims(
        credential_id=credential_id,
        machine_id=machine_id,
        org_id=org_id,
        jti=_require_str_claim(payload, "jti", what="worker credential: missing jti"),
        issued_at=_require_int_claim(payload, "iat", what="worker credential: missing iat"),
        expires_at=_require_int_claim(payload, "exp", what="worker credential: missing exp"),
    )


def decode_session_token(token: str, *, allow_expired: bool = False) -> SessionClaims:
    """Decode and verify a session token. Raises ``TokenExpiredError`` past
    ``exp`` and ``InvalidTokenError`` on every other failure mode (bad
    signature, missing claim, malformed UUID).

    Signature + expiry only — revocation is a separate check (it needs DB +
    the user row); see ``alkera_core.auth.revocation.assert_token_active``.

    ``allow_expired`` skips only the ``exp`` check (the signature is still
    verified). It exists for logout: an expired access token still names the
    session family it belongs to, and signing out must end that family rather
    than leave its refresh credential alive because the access half lapsed.
    """
    return _session_claims_from(_decode_jwt(token, verify_exp=not allow_expired))


def _session_claims_from(payload: dict[str, Any]) -> SessionClaims:
    scope = payload.get("scope")
    if scope is not None and scope != SESSION_SCOPE:
        # A token that names another scope was minted for another audience —
        # the chat's gateway token above all — and is never a session, whatever
        # else it carries. Refused before any claim is read.
        raise InvalidTokenError(f"a {scope!r}-scoped token is not a session")
    try:
        user_id = UUID(hex=payload["sub"])
        org_team_id = UUID(hex=payload["org_team_id"])
        email = str(payload["email"])
        raw_role = payload.get("platform_role")
        role: PlatformRole | None = PlatformRole(raw_role) if raw_role else None
        issued_at = int(payload["iat"])
        expires_at = int(payload["exp"])
        raw_jti = payload.get("jti")
        jti = str(raw_jti) if raw_jti else None
    except (KeyError, ValueError, TypeError) as exc:
        raise InvalidTokenError(f"malformed claims: {exc}") from exc
    membership_id, membership_epoch = _membership_of(payload, what="claims")

    return SessionClaims(
        user_id=user_id,
        email=email,
        org_team_id=org_team_id,
        platform_role=role,
        issued_at=issued_at,
        expires_at=expires_at,
        jti=jti,
        membership_id=membership_id,
        membership_epoch=membership_epoch,
    )


# --- GitHub App install claim -----------------------------------------------
#
# A short-lived state token round-tripped through GitHub's App install flow (the
# OAuth-state pattern). It binds the SIGNED STATE -- not the code or the
# installation id, which stay independent inputs -- to three things:
#
#   ``org``  the org that minted it; the claim accepts a state only for the
#            caller's own org, so a crafted state can't bind in another session.
#   ``uid``  the admin who started the install; the preview accepts a state only
#            from that same admin, so a same-org admin can't finish (and mint a
#            confirmation for themselves off) an install a colleague started.
#   ``jti``  single-use; the claim records it consumed, so a captured state can't
#            be replayed.
#   ``nch``  the SHA-256 of a nonce the backend plants in an HttpOnly cookie on
#            the minting browser. A state only verifies in the browser session
#            that started the install, so a captured state can't be presented
#            from another browser.
#
# The state never authorizes a bind on its own, and it does NOT vouch for the
# code or installation id. Proof that the caller controls the installed account
# (an OAuth ``code`` checked against ``GET /user/installations``) is the gate;
# that the code reaches only the installing browser (GitHub's fixed callback) is
# what keeps the code from being the attacker's -- see the claim service docstring.

_TYP_GITHUB_INSTALL_CLAIM = "github_install_claim"

GITHUB_INSTALL_CLAIM_TTL_SECONDS = 3600


@dataclass(frozen=True, slots=True)
class GithubInstallClaim:
    """The decoded install-claim state: which org minted it, WHICH ADMIN started
    the install, its single-use ``jti``, and the hash of the browser-binding
    nonce."""

    org_id: str
    admin_user_id: str
    jti: str
    nonce_hash: str


def encode_github_install_claim(
    *,
    org_id: str,
    admin_user_id: str,
    nonce_hash: str,
    jti: str | None = None,
    ttl_seconds: int = GITHUB_INSTALL_CLAIM_TTL_SECONDS,
    now: int | None = None,
) -> str:
    issued = int(time.time()) if now is None else now
    payload: dict[str, object] = {
        "typ": _TYP_GITHUB_INSTALL_CLAIM,
        "org": org_id,
        "uid": admin_user_id,
        "jti": jti or uuid4().hex,
        "nch": nonce_hash,
        "iat": issued,
        "exp": issued + ttl_seconds,
    }
    return jwt.encode(payload, settings.effective_jwt_secret, algorithm=settings.auth_jwt_alg)


def _require_str_claim(payload: dict[str, object], key: str, *, what: str) -> str:
    """The non-empty string claim at ``key``, or ``InvalidTokenError`` naming
    ``what`` is missing. An absent, non-string, or empty claim must read as an
    invalid token, never as a token with one fewer binding."""
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise InvalidTokenError(f"malformed {what}")
    return value


def decode_github_install_claim(token: str) -> GithubInstallClaim:
    """The org id, initiating admin, single-use ``jti``, and browser-nonce hash
    the claim state was minted for. Raises ``InvalidTokenError`` on any failure
    (expired, bad signature, wrong ``typ``, missing claim) -- including a state
    minted before a binding existed, which must read as invalid rather than as a
    claimable state with one fewer check."""
    payload = _decode_typed(token, expected_typ=_TYP_GITHUB_INSTALL_CLAIM)
    return GithubInstallClaim(
        org_id=_require_str_claim(payload, "org", what="github install claim: missing org"),
        admin_user_id=_require_str_claim(
            payload, "uid", what="github install claim: missing admin binding"
        ),
        jti=_require_str_claim(payload, "jti", what="github install claim: missing jti"),
        nonce_hash=_require_str_claim(
            payload, "nch", what="github install claim: missing nonce binding"
        ),
    )


# --- GitHub App install confirmation ----------------------------------------
#
# The claim is two calls, and this token is what carries the proof between them.
# The first call (preview) checks the state, spends GitHub's OAuth code, and
# proves the authorizing user controls the installed account; it then SHOWS the
# admin which GitHub account they are about to bind, and binds nothing. The
# admin's click is the second call, and it presents this token: the signed,
# short-lived record of what the first call proved.
#
# It exists because a human confirmation step needs something to confirm. The
# OAuth code is single-use at GitHub, so the second call cannot re-run the
# proof; and re-deriving it from a caller-supplied installation_id is exactly
# the squat this whole flow closes. So the proof is sealed into a signed token
# instead: the binding call verifies a signature and burns the state's ``jti``,
# and never dials GitHub at all.
#
# It carries the account facts the admin was SHOWN (``acct`` / ``tgt``), so the
# audit log records the decision the human actually made rather than a fact
# re-read afterwards.

_TYP_GITHUB_INSTALL_CONFIRM = "github_install_confirm"

#: How long the preview's proof stays redeemable. The window bounds a real race:
#: the redeem CANNOT re-verify org ownership (the OAuth code is single-use and
#: already spent, and the user token is deliberately never persisted), so an
#: owner removed right after previewing could still bind until the token expires.
#: Two minutes is ample to read one account name and click, and keeps that race
#: tight. A confirmation that lapses means restarting the install (the code is
#: gone) -- the cost of not carrying a re-verifiable credential.
GITHUB_INSTALL_CONFIRM_TTL_SECONDS = 120


@dataclass(frozen=True, slots=True)
class GithubInstallConfirmation:
    """A proven, admin-confirmable installation bind. It carries the org that may
    redeem it, the installation it binds, the state ``jti`` whose burn makes it
    single-use, the account facts the admin was shown, and the session it belongs
    to: the previewing admin (``admin_user_id``) and the SHA-256 of that browser's
    install-nonce cookie (``nonce_hash``). The redeem re-checks both, so this proof
    is not a bearer token -- a scripted confirm from another admin or another
    browser can't redeem someone else's preview.

    ``repository_selection`` / ``repos`` carry the scope the preview already
    fetched, so a claim landing before the webhook backfills the real scope onto
    the fresh registry row instead of leaving it at the ``all`` materialization
    default, which would over-match every repo in the account until sync."""

    org_id: str
    installation_id: int
    jti: str
    account_login: str
    target_type: str
    admin_user_id: str
    nonce_hash: str
    repository_selection: str
    repos: list[str] | None


def encode_github_install_confirmation(
    confirmation: GithubInstallConfirmation,
    *,
    ttl_seconds: int = GITHUB_INSTALL_CONFIRM_TTL_SECONDS,
    now: int | None = None,
) -> str:
    """Seal an assembled :class:`GithubInstallConfirmation` into its signed,
    short-lived token."""
    issued = int(time.time()) if now is None else now
    payload: dict[str, object] = {
        "typ": _TYP_GITHUB_INSTALL_CONFIRM,
        "org": confirmation.org_id,
        "iid": confirmation.installation_id,
        "jti": confirmation.jti,
        "acct": confirmation.account_login,
        "tgt": confirmation.target_type,
        "uid": confirmation.admin_user_id,
        "nch": confirmation.nonce_hash,
        "sel": confirmation.repository_selection,
        "rps": confirmation.repos,
        "iat": issued,
        "exp": issued + ttl_seconds,
    }
    return jwt.encode(payload, settings.effective_jwt_secret, algorithm=settings.auth_jwt_alg)


def decode_github_install_confirmation(token: str) -> GithubInstallConfirmation:
    """What the preview proved. Raises ``InvalidTokenError`` on any failure
    (expired, bad signature, wrong ``typ``, missing claim) -- a token that
    cannot state every field it was minted with is not a proof, including the
    session binding, which must read as invalid rather than as a session-less
    confirmation with one fewer check."""
    payload = _decode_typed(token, expected_typ=_TYP_GITHUB_INSTALL_CONFIRM)
    org = _require_str_claim(payload, "org", what="github install confirmation: missing org")
    installation_id = payload.get("iid")
    if not isinstance(installation_id, int) or isinstance(installation_id, bool):
        raise InvalidTokenError("malformed github install confirmation: missing installation")
    jti = _require_str_claim(payload, "jti", what="github install confirmation: missing jti")
    account_login = payload.get("acct")
    target_type = payload.get("tgt")
    if not isinstance(account_login, str) or not isinstance(target_type, str):
        raise InvalidTokenError("malformed github install confirmation: missing account")
    repository_selection = payload.get("sel")
    if not isinstance(repository_selection, str):
        raise InvalidTokenError(
            "malformed github install confirmation: missing repository selection"
        )
    repos_raw = payload.get("rps")
    repos = [str(name) for name in repos_raw] if isinstance(repos_raw, list) else None
    return GithubInstallConfirmation(
        org_id=org,
        installation_id=installation_id,
        jti=jti,
        account_login=account_login,
        target_type=target_type,
        admin_user_id=_require_str_claim(
            payload, "uid", what="github install confirmation: missing admin binding"
        ),
        nonce_hash=_require_str_claim(
            payload, "nch", what="github install confirmation: missing session binding"
        ),
        repository_selection=repository_selection,
        repos=repos,
    )
