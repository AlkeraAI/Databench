"""Multi-use grants over a page's own folder.

A rendered page is not one download. An HTML report opened in a preview fetches
the chart beside it, its font and its stylesheet itself, and none of those
requests carries a credential — the content origin has no cookie by design. A
single-use content URL therefore cannot serve a page at all, and handing the
page a URL per asset would mean the API guessing, at mint time, every byte the
browser will ask for.

So the mint grants a *root* — the folder the entry file sits in — and each
request under it is re-authorized against the node it actually names. Three
things keep that bounded, and all three are the row rather than the token:

*The grant is short and countable.* :func:`page_grant_ttl` long, no refresh, and
at most :func:`page_grant_max_requests` served; the counter is incremented by
the same statement that decides the request is allowed, so a grant cannot be
spent past its ceiling by racing. Both figures are deployment settings, read
where they are enforced.

*The grant names who it acts as.* Every request under it resolves that user's
access to that node from scratch: the grant is permission to *ask*, never
permission to read. It also records the credential it was minted on, which is
what lets a logout close the pages that credential opened.

*The grant can be closed.* ``revoked_at`` is set by the logout path and by a
share revoke, which reaches every grant rooted on the chain whose permissions
changed — the page's root is an ancestor of everything it can serve, so the
chain is the right unit.

The token is the one the content URLs use — ``<nonce>.<claim>.<sig>`` — with the
claim's ``kind`` field spelling ``page``. The MAC covers the nonce, every claim
field, and the root, entry and deadline the mint decided, so none of those can
be rewritten by whoever holds the URL; the signature is compared before the
counter moves, so a forged token never spends a real grant's budget.
"""

from __future__ import annotations

import hmac
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final
from urllib.parse import quote
from uuid import UUID

from sqlalchemy import func, insert, select, update

from alkera_core.config import get_settings
from alkera_core.db.base import Base
from alkera_core.files.clock import Clock
from alkera_core.files.ids import NodeId, SessionId
from alkera_core.files.repo import FilesRepo
from alkera_core.files.signed_urls import (
    NONCE_BYTES,
    ContentClaim,
    claim_fields,
    encode_claim,
    parse_claim,
    sign_token,
)

#: Looked up rather than imported as the mapped class: statements naming a
#: Files ORM class belong in ``repo.py`` alone (the hygiene scan). Every
#: statement below carries the org predicate explicitly; RLS is the second net.
_PAGE_GRANTS: Final = Base.metadata.tables["file_page_grants"]


def page_grant_ttl() -> timedelta:
    """How long a page stays openable (fifteen minutes by default).

    Long enough for a reader to scroll a report and for the browser to fetch
    what it references, short enough that a leaked URL is not a standing read
    channel. There is no refresh: a page still open past it asks the API for a
    new grant, which re-decides the access. Read from the deployment settings
    at mint time, so ``FILES_PAGE_GRANT_TTL_SECONDS`` governs the next grant.
    """
    return timedelta(seconds=get_settings().files_page_grant_ttl_seconds)


def page_grant_max_requests() -> int:
    """The most requests one grant will ever serve (five thousand by default).

    A page and its assets is tens of requests; a ceiling three orders of
    magnitude above that costs a real reader nothing and stops a grant being
    used to walk a folder. Read from the deployment settings on the redemption
    that checks it, so ``FILES_PAGE_GRANT_MAX_REQUESTS`` governs the grants
    already minted as well as the next one.
    """
    return get_settings().files_page_grant_max_requests


#: Where the content mount serves a page. The entry's name rides after the
#: token so relative references inside the page resolve against the root, and
#: so a saved page keeps a sensible filename.
PAGE_PATH_PREFIX: Final = "/c/p/"


@dataclass(frozen=True, slots=True)
class PageGrant:
    """One redeemed page grant: what to serve from, and whose access to resolve."""

    nonce: str
    root_node_id: NodeId
    entry_node_id: NodeId
    minted_by_user_id: UUID
    credential_id: str | None
    session_id: SessionId | None
    expires_at: datetime
    #: The count *including* this request, so a caller can see the ceiling
    #: approaching without a second read.
    requests_served: int


def _signing_payload(
    *,
    nonce: str,
    claim: ContentClaim,
    root_node_id: UUID,
    entry_node_id: UUID,
    expires_at: datetime,
) -> tuple[bytes, ...]:
    """The exact fields the HMAC covers.

    The root and the entry are in here because they are what the grant *is*: a
    token that named its own root would be a token whose holder picks the folder
    they may read. The deadline is covered for the same reason.
    """
    return (
        nonce.encode("ascii"),
        *claim_fields(claim),
        str(root_node_id).encode("ascii"),
        str(entry_node_id).encode("ascii"),
        expires_at.astimezone(UTC).isoformat().encode("ascii"),
    )


async def mint_page_grant(
    repo: FilesRepo,
    *,
    root_node_id: NodeId,
    entry_node_id: NodeId,
    entry_name: bytes,
    user_id: UUID,
    credential_id: str | None = None,
    session_id: SessionId | None = None,
    machine_id: str | None = None,
    ttl: timedelta | None = None,
    clock: Clock,
    key: bytes,
) -> str:
    """Write a grant over ``root_node_id`` and return the path that serves it.

    The deadline is stamped from the injected clock so a test can step past it;
    it is *checked* against Postgres ``now()``, the only clock every API
    instance shares.

    ``user_id`` is required rather than optional: a page grant redeems as
    somebody, and a caller whose principal is not a user (a CI or proxy token)
    has no access for the walk to resolve, so it must be refused at the route
    rather than minted into a grant that acts as nobody.

    ``machine_id`` is the machine the minting request was PROVEN to be, signed
    into the claim because the content origin holds no credential with which to
    re-check an assertion of its own.

    The entry's name is percent-encoded into the path with nothing left safe:
    it is a display tail, and a stored name containing ``/``, ``%`` or a control
    byte must not turn into a second path segment.
    """
    nonce = secrets.token_hex(NONCE_BYTES)
    expires_at = clock.now() + (page_grant_ttl() if ttl is None else ttl)
    async with repo.transaction():
        await repo.session.execute(
            insert(_PAGE_GRANTS).values(
                org_team_id=repo.scope.org_team_id,
                nonce=nonce,
                root_node_id=root_node_id,
                entry_node_id=entry_node_id,
                minted_by_user_id=user_id,
                credential_id=credential_id,
                session_id=session_id,
                expires_at=expires_at,
            )
        )
    claim = ContentClaim(
        org_id=repo.scope.org_team_id,
        user_id=user_id,
        session_id=session_id,
        kind="page",
        machine_id=machine_id,
    )
    signature = sign_token(
        _signing_payload(
            nonce=nonce,
            claim=claim,
            root_node_id=root_node_id,
            entry_node_id=entry_node_id,
            expires_at=expires_at,
        ),
        key,
    )
    token = f"{nonce}.{encode_claim(claim)}.{signature}"
    return f"{PAGE_PATH_PREFIX}{token}/{quote(entry_name, safe='')}"


async def redeem_page_grant(
    repo: FilesRepo,
    *,
    token: str,
    clock: Clock,
    key: bytes,
) -> PageGrant | None:
    """Spend one request of the grant this token names, or return ``None``.

    ``None`` covers every failure without distinguishing them: an unknown or
    tampered nonce, a revoked grant, an expired one, a grant that has served its
    ceiling, a file token presented here, and a forged signature all answer the
    same.

    The read comes first and the increment second, deliberately. The signature
    can only be checked against the root, entry and deadline the row carries, so
    checking it after the take would let anyone holding a nonce burn a real
    grant's budget with garbage signatures. The increment then repeats the
    ceiling predicate, so two concurrent requests at the boundary cannot both
    win: the statement that does not match returns nothing and the caller is
    refused.
    """
    claim = parse_claim(token)
    if claim is None or claim.org_id != repo.scope.org_team_id or claim.kind != "page":
        return None
    nonce = token.partition(".")[0]
    signature = token.rpartition(".")[2]
    if not nonce or not signature:
        return None
    #: Read once, so the read's predicate and the increment's are the same
    #: ceiling even if the setting is reloaded between the two statements.
    ceiling = page_grant_max_requests()
    async with repo.transaction():
        row = (
            (
                await repo.session.execute(
                    select(
                        _PAGE_GRANTS.c.nonce,
                        _PAGE_GRANTS.c.root_node_id,
                        _PAGE_GRANTS.c.entry_node_id,
                        _PAGE_GRANTS.c.minted_by_user_id,
                        _PAGE_GRANTS.c.credential_id,
                        _PAGE_GRANTS.c.session_id,
                        _PAGE_GRANTS.c.expires_at,
                    ).where(
                        _PAGE_GRANTS.c.nonce == nonce,
                        _PAGE_GRANTS.c.org_team_id == repo.scope.org_team_id,
                        _PAGE_GRANTS.c.revoked_at.is_(None),
                        _PAGE_GRANTS.c.expires_at > func.now(),
                        _PAGE_GRANTS.c.requests_served < ceiling,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        expected = sign_token(
            _signing_payload(
                nonce=row["nonce"],
                claim=claim,
                root_node_id=row["root_node_id"],
                entry_node_id=row["entry_node_id"],
                expires_at=row["expires_at"],
            ),
            key,
        )
        if not hmac.compare_digest(expected, signature):
            return None
        served = (
            await repo.session.execute(
                update(_PAGE_GRANTS)
                .where(
                    _PAGE_GRANTS.c.nonce == nonce,
                    _PAGE_GRANTS.c.org_team_id == repo.scope.org_team_id,
                    _PAGE_GRANTS.c.revoked_at.is_(None),
                    _PAGE_GRANTS.c.expires_at > func.now(),
                    _PAGE_GRANTS.c.requests_served < ceiling,
                )
                .values(requests_served=_PAGE_GRANTS.c.requests_served + 1)
                .returning(_PAGE_GRANTS.c.requests_served)
            )
        ).scalar_one_or_none()
    if served is None:
        return None
    return PageGrant(
        nonce=row["nonce"],
        root_node_id=NodeId(row["root_node_id"]),
        entry_node_id=NodeId(row["entry_node_id"]),
        minted_by_user_id=row["minted_by_user_id"],
        credential_id=row["credential_id"],
        session_id=None if row["session_id"] is None else SessionId(row["session_id"]),
        expires_at=row["expires_at"],
        requests_served=int(served),
    )


async def revoke_page_grants(
    repo: FilesRepo,
    *,
    user_id: UUID | None = None,
    credential_id: str | None = None,
    root_chain: Sequence[NodeId] | None = None,
) -> int:
    """Close the live grants a logout or a permission change invalidates.

    ``root_chain`` is the chain of the node whose access changed, root first. A
    page grant serves everything under its own root, so a grant is reached by
    the change exactly when its root is on that chain — an ancestor of the
    changed node, or the node itself.

    At least one selector is required. A call that named none would revoke the
    org's pages wholesale, which is never what a caller means and is not
    something a typo should be able to ask for.
    """
    if user_id is None and credential_id is None and not root_chain:
        raise ValueError("revoke_page_grants needs a user, a credential or a root chain")
    statement = (
        update(_PAGE_GRANTS)
        .where(
            _PAGE_GRANTS.c.org_team_id == repo.scope.org_team_id,
            _PAGE_GRANTS.c.revoked_at.is_(None),
        )
        .values(revoked_at=func.now())
        .returning(_PAGE_GRANTS.c.nonce)
    )
    if user_id is not None:
        statement = statement.where(_PAGE_GRANTS.c.minted_by_user_id == user_id)
    if credential_id is not None:
        statement = statement.where(_PAGE_GRANTS.c.credential_id == credential_id)
    if root_chain:
        statement = statement.where(_PAGE_GRANTS.c.root_node_id.in_(list(root_chain)))
    async with repo.transaction():
        revoked = (await repo.session.execute(statement)).fetchall()
    return len(revoked)


__all__ = [
    "PAGE_PATH_PREFIX",
    "PageGrant",
    "mint_page_grant",
    "page_grant_max_requests",
    "page_grant_ttl",
    "redeem_page_grant",
    "revoke_page_grants",
]
