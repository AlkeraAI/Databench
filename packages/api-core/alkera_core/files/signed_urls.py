"""Signed, single-use content URLs.

A download is never served from the API host: the API answers with a short-TTL
URL on the content domain, and that URL is the whole credential. Two properties
make it safe to hand out.

*The grant is a row, not a claim inside the token.* The token carries a 128-bit
random nonce, a claim naming the minting org/user/session, and an HMAC over all
of it; everything else the redeemer needs (the version, the byte range, the
deadline) is read from ``file_content_grants``. That is what lets the collector
treat a live grant as a GC root: a grant that lived only inside an opaque token
would be invisible to it. The claim rides along only because the content domain
carries no credential of its own and the redeeming route has to know
which user's access to resolve before it can resolve anything at all.

*Redemption is one statement.* ``UPDATE … WHERE nonce = $n AND used_at IS NULL
AND expires_at > now() RETURNING`` decides "unused and unexpired" and takes the
grant in the same statement, so a replay and a genuine race both lose. Expiry
is Postgres ``now()``, never a Python clock, so a skewed API instance cannot
extend a deadline.

Every failure (a bad signature, a used grant, an expired grant, a grant minted
for another session) returns ``None`` after exactly one statement, so a caller
cannot tell them apart by answer or by timing.
"""

from __future__ import annotations

import base64
import hmac
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Literal, get_args
from uuid import UUID

from sqlalchemy import func, insert, update

from alkera_core.authz.principal import ActingContext
from alkera_core.config import get_settings
from alkera_core.db.base import Base
from alkera_core.files.clock import Clock
from alkera_core.files.ids import SessionId, VersionId
from alkera_core.files.repo import FilesRepo

#: Looked up rather than imported as the mapped class: statements naming a
#: Files ORM class belong in ``repo.py`` alone (the hygiene scan). Both
#: statements below carry the org predicate explicitly; RLS is the second net.
_GRANTS: Final = Base.metadata.tables["file_content_grants"]


def content_url_ttl() -> timedelta:
    """How long a download URL lives (five minutes by default).

    Read from the deployment settings on every mint rather than frozen at
    import, so raising ``FILES_CONTENT_URL_TTL_SECONDS`` moves the expiry of
    the very next grant. Part URLs are longer and minted elsewhere.
    """
    return timedelta(seconds=get_settings().files_content_url_ttl_seconds)


#: 16 bytes = 128 bits of nonce, rendered as 32 hex characters (content URL
#: nonces are 128-bit random).
NONCE_BYTES: Final = 16

#: Where the content mount serves a redeemed grant.
CONTENT_PATH_PREFIX: Final = "/c/"

_SEPARATOR: Final = b"\x1f"

#: How many segments a content token has: ``<nonce>.<claim>.<signature>``.
_TOKEN_SEGMENTS: Final = 3

#: What a token grants: one version once (``file``), or a page's folder for a
#: few minutes (``page``). The two are redeemed from different tables, and the
#: kind rides in the claim so a route can refuse the other one before it reads
#: anything at all.
GrantKind = Literal["file", "page"]
GRANT_KINDS: Final[tuple[GrantKind, ...]] = get_args(GrantKind)

#: The claim's fields: ``org``, ``user``, ``session``, ``attachment``, ``kind``,
#: ``machine``, ``credential``. Older claims carry fewer — a claim minted before
#: the disposition flag existed carries the first three, one minted before page
#: grants the first four, one minted before the machine was signed the first
#: five, one minted before a box could mint on its own credential the first six
#: — and each reads back as the default it was signed as.
_CLAIM_SHAPES: Final = (3, 4, 5, 6, 7)


@dataclass(frozen=True, slots=True)
class ContentClaim:
    """Who minted a content URL, carried in the URL itself.

    The content domain has no cookie and no ``Authorization`` header by design,
    so a redeemer that asked an ambient credential who was calling
    would refuse every real download. The claim is what makes the URL
    self-sufficient the way an archive token is: it names the minting user, org
    and session, the HMAC covers all three, and the route rebuilds the acting
    context from it and then re-runs the policy. Nothing here is *trusted*; it
    only says which access to resolve from scratch.
    """

    org_id: UUID
    user_id: UUID | None
    session_id: SessionId | None
    #: Whether the redeeming origin must answer ``Content-Disposition:
    #: attachment`` rather than the disposition the sniffed type would choose.
    #: It rides in the claim — and therefore under the HMAC — because the
    #: content origin has no other input a minting caller can bind: a flag on
    #: the redeeming request's query string is one whoever holds the URL could
    #: flip, and a column on the grant row would be a second place a download's
    #: shape is decided. A mint asks for it; a redeemer can only present it.
    attachment: bool = False
    #: Which grant table redeems this token. Under the MAC like every other
    #: field, so a page token cannot be re-presented as a file token or the
    #: reverse; a route reads it to refuse the wrong kind before it touches a
    #: row.
    kind: GrantKind = "file"
    #: The machine the MINTING request was proven to be, when it was a box.
    #: The content origin carries no credential of its own, so it cannot check
    #: an assertion against a registration the way every other door does — and
    #: the session id in the claim is a box's PUBLIC machine id, which proves
    #: nothing. This is the server's own signed word that the check was made
    #: and passed, written from the verified fact and never from anything the
    #: minting request asserted; under the MAC, so whoever holds the URL cannot
    #: add it or change it. ``None`` for a person, and for an agent whose
    #: assertion nobody verified: those redeem as the reader they are.
    machine_id: str | None = None
    #: The machine credential the minting request was made on, when the minter
    #: was a box on its own credential rather than a person or an agent. Such a
    #: claim names no user at all — nobody is behind a box — so this is what
    #: the origin rebuilds the minter from, and what it re-asks the standing of
    #: before serving a byte: a URL minted a minute before the credential was
    #: revoked must not carry the box past the revoke. Under the MAC like every
    #: other field.
    credential_id: UUID | None = None


def claim_fields(claim: ContentClaim) -> tuple[bytes, ...]:
    """Every field of ``claim``, in the one order the token and the MAC share.

    Spelled once so the two can never disagree: a field the encoder writes but
    the signing payload leaves out is a field whoever holds the URL can rewrite.
    """
    return (
        str(claim.org_id).encode("ascii"),
        b"" if claim.user_id is None else str(claim.user_id).encode("ascii"),
        b"" if claim.session_id is None else str(claim.session_id).encode("ascii"),
        b"1" if claim.attachment else b"",
        claim.kind.encode("ascii"),
        b"" if claim.machine_id is None else claim.machine_id.encode("ascii"),
        b"" if claim.credential_id is None else str(claim.credential_id).encode("ascii"),
    )


def encode_claim(claim: ContentClaim) -> str:
    """The claim segment of a token: its fields, separated and base64url'd."""
    raw = _SEPARATOR.join(claim_fields(claim))
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def parse_claim(token: str) -> ContentClaim | None:
    """The claim ``token`` carries, or ``None`` for anything malformed.

    Parsing is deliberately signature-free: the HMAC is checked in
    :func:`redeem`, against a payload that covers every field of the claim, so a
    caller who edits the org or the user here cannot redeem the grant. What this
    buys is the ordering the content domain needs — the org has to be known
    before a scoped repo exists to run the take statement in.
    """
    parts = token.split(".")
    if len(parts) != _TOKEN_SEGMENTS:
        return None
    encoded = parts[1]
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        fields = raw.split(_SEPARATOR)
        if len(fields) not in _CLAIM_SHAPES:
            return None
        org, user, session, *rest = (field.decode("ascii") for field in fields)
        spelled = rest[1] if len(rest) > 1 else "file"
        if spelled not in GRANT_KINDS:
            return None
        return ContentClaim(
            org_id=UUID(org),
            user_id=UUID(user) if user else None,
            session_id=SessionId(UUID(session)) if session else None,
            attachment=bool(rest and rest[0]),
            kind=spelled,
            machine_id=(rest[2] or None) if len(rest) > 2 else None,
            credential_id=UUID(rest[3]) if len(rest) > 3 and rest[3] else None,
        )
    except (ValueError, UnicodeDecodeError):
        return None


def minting_user(ctx: ActingContext) -> UUID | None:
    """The user a URL minted in ``ctx`` will act as when it is redeemed.

    The delegating user when there is one — an agent's URL is redeemed as the
    human whose session it runs in, so a revoke on that human closes it — and
    the acting principal otherwise. ``None`` for a principal whose id is not a
    user id (a CI or proxy token), which mints a URL the content domain refuses
    rather than one that acts as nobody — and ``None`` for a box on its machine
    credential, whose principal id is a uuid but names a machine, not a person:
    its URL is redeemed as the box, from the credential the claim carries.
    """
    if ctx.is_machine:
        return None
    principal = ctx.delegating_user or ctx.acting_principal
    try:
        return UUID(principal.id)
    except (ValueError, TypeError):
        return None


@dataclass(frozen=True, slots=True)
class ContentGrant:
    """One redeemed signed URL."""

    nonce: str
    version_id: VersionId
    session_id: SessionId | None
    range: tuple[int, int] | None
    expires_at: datetime


def _signing_payload(
    *,
    nonce: str,
    claim: ContentClaim,
    version_id: VersionId,
    session_id: SessionId | None,
    range_lo: int | None,
    range_hi: int | None,
    expires_at: datetime,
) -> bytes:
    """The exact bytes the HMAC covers.

    Field-separated rather than concatenated so no two different grants can
    render to the same payload by moving a boundary.

    Every field the *token* carries is in here, the claim's session included:
    the content domain has no cookie, so the claim is the only thing on a
    redeeming request that names a session, and a field the MAC does not cover
    is a field whoever holds the URL can rewrite.
    """
    parts = (
        nonce.encode("ascii"),
        *claim_fields(claim),
        str(version_id).encode("ascii"),
        b"" if session_id is None else str(session_id).encode("ascii"),
        b"" if range_lo is None else str(range_lo).encode("ascii"),
        b"" if range_hi is None else str(range_hi).encode("ascii"),
        expires_at.astimezone(UTC).isoformat().encode("ascii"),
    )
    return _SEPARATOR.join(parts)


def sign_token(parts: Sequence[bytes], key: bytes) -> str:
    """The signature over ``parts``, field-separated rather than concatenated.

    Separated so no two different grants can render to the same payload by
    moving a boundary; shared with the page grants so both token families are
    signed the one way.
    """
    return hmac.new(key, _SEPARATOR.join(parts), "sha256").hexdigest()


def _sign(payload: bytes, key: bytes) -> str:
    return hmac.new(key, payload, "sha256").hexdigest()


async def mint_content_url(
    repo: FilesRepo,
    *,
    version_id: VersionId,
    session_id: SessionId | None,
    range: tuple[int, int] | None = None,
    ttl: timedelta | None = None,
    clock: Clock,
    key: bytes,
    user_id: UUID | None = None,
    attachment: bool = False,
    machine_id: str | None = None,
    credential_id: UUID | None = None,
) -> str:
    """Write a grant and return the URL path that redeems it.

    The deadline is stamped from the injected clock so a test can step past it;
    it is *checked* against Postgres ``now()``, which is the only clock every
    API instance shares.

    ``user_id`` is who the URL will act as when it is redeemed. Omitting it
    mints a URL the content domain refuses — fail-closed, because a URL that
    redeemed with no user would be a download nobody re-authorizes.

    ``attachment`` asks the content origin for ``Content-Disposition:
    attachment`` instead of the disposition the sniffed type would pick, so a
    "Download" really downloads a file the browser would otherwise render. It is
    covered by the signature, so a holder of the URL cannot turn a download into
    a rendered page — or the reverse — after the mint.

    ``machine_id`` is the machine the minting request was PROVEN to be. It is
    the caller's verified fact, never the id the minting request asserted, and
    it rides under the MAC — the content origin has no credential with which to
    re-check an assertion, so the alternative is either trusting a public id or
    refusing a box its own chat's bytes. ``credential_id`` is the machine
    credential a box minted on when it minted as itself, with no user behind
    it: the one exception to "no user, no URL", because the origin can rebuild
    the box from it and re-ask its standing.
    """
    nonce = secrets.token_hex(NONCE_BYTES)
    range_lo, range_hi = (None, None) if range is None else range
    expires_at = clock.now() + (content_url_ttl() if ttl is None else ttl)
    async with repo.transaction():
        await repo.session.execute(
            insert(_GRANTS).values(
                nonce=nonce,
                org_team_id=repo.scope.org_team_id,
                version_id=version_id,
                session_id=session_id,
                range_lo=range_lo,
                range_hi=range_hi,
                expires_at=expires_at,
            )
        )
    claim = ContentClaim(
        org_id=repo.scope.org_team_id,
        user_id=user_id,
        session_id=session_id,
        attachment=attachment,
        machine_id=machine_id,
        credential_id=credential_id,
    )
    signature = _sign(
        _signing_payload(
            nonce=nonce,
            claim=claim,
            version_id=version_id,
            session_id=session_id,
            range_lo=range_lo,
            range_hi=range_hi,
            expires_at=expires_at,
        ),
        key,
    )
    return f"{CONTENT_PATH_PREFIX}{nonce}.{encode_claim(claim)}.{signature}"


async def redeem(
    repo: FilesRepo,
    *,
    token: str,
    session_id: SessionId | None,
    clock: Clock,
    key: bytes,
) -> ContentGrant | None:
    """Take the grant this token names, or return ``None``.

    ``session_id`` is the session the *presenting* caller is acting in, or
    ``None`` on an origin that carries no credential of its own — which is the
    content domain, always. "Session-bound" therefore means: the session
    recorded on the grant row at mint time must be the session the token's
    MAC-covered claim names, and a caller that does present one must be that
    session too. A URL minted under session A and presented with a claim naming
    session B is refused; there is no third party for the row to be compared
    against, so the binding is between the two things the mint decided.

    ``None`` covers every failure without distinguishing them: an unknown or
    tampered nonce, a used grant, an expired grant, a forged signature and a
    grant minted for another session all answer the same after the same single
    statement.
    """
    claim = parse_claim(token)
    if claim is None or claim.org_id != repo.scope.org_team_id:
        return None
    if claim.kind != "file":
        # A page grant is a different row in a different table with a different
        # lifetime; refused here before the statement, so presenting one to the
        # single-use route costs nothing and says nothing.
        return None
    nonce, _, signature = token.partition(".")
    signature = signature.rpartition(".")[2]
    if not nonce or not signature:
        return None
    async with repo.transaction():
        row = (
            (
                await repo.session.execute(
                    update(_GRANTS)
                    .where(
                        _GRANTS.c.nonce == nonce,
                        _GRANTS.c.org_team_id == repo.scope.org_team_id,
                        _GRANTS.c.used_at.is_(None),
                        _GRANTS.c.expires_at > func.now(),
                    )
                    .values(used_at=func.now())
                    .returning(
                        _GRANTS.c.nonce,
                        _GRANTS.c.version_id,
                        _GRANTS.c.session_id,
                        _GRANTS.c.range_lo,
                        _GRANTS.c.range_hi,
                        _GRANTS.c.expires_at,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        return None
    expected = _sign(
        _signing_payload(
            nonce=row["nonce"],
            claim=claim,
            version_id=VersionId(row["version_id"]),
            session_id=None if row["session_id"] is None else SessionId(row["session_id"]),
            range_lo=row["range_lo"],
            range_hi=row["range_hi"],
            expires_at=row["expires_at"],
        ),
        key,
    )
    if not hmac.compare_digest(expected, signature):
        return None
    # The signature already ties the row's session to the claim's; the explicit
    # comparison is the fail-closed second net, and the presenting session is
    # checked on top of it for the callers that have one.
    if row["session_id"] != claim.session_id:
        return None
    if session_id is not None and row["session_id"] != session_id:
        return None
    span = (
        None
        if row["range_lo"] is None or row["range_hi"] is None
        else (int(row["range_lo"]), int(row["range_hi"]))
    )
    return ContentGrant(
        nonce=row["nonce"],
        version_id=VersionId(row["version_id"]),
        session_id=None if row["session_id"] is None else SessionId(row["session_id"]),
        range=span,
        expires_at=row["expires_at"],
    )


__all__ = [
    "CONTENT_PATH_PREFIX",
    "GRANT_KINDS",
    "NONCE_BYTES",
    "ContentClaim",
    "ContentGrant",
    "GrantKind",
    "claim_fields",
    "content_url_ttl",
    "encode_claim",
    "mint_content_url",
    "minting_user",
    "parse_claim",
    "redeem",
    "sign_token",
]
