"""The content domain's only route: redeem a signed URL and stream the bytes.

This module exposes ``content_router`` rather than ``router``, so the package
walk mounts it on the content app (its own hostname, its own middleware, no
session cookie of its own) and never on the API host.

Redemption is not "check the token, then serve": the URL is the whole
credential (this origin carries no cookie and no ``Authorization``
header, so asking one who is calling would refuse every real download), so the
route rebuilds the acting context from the claim the token carries, takes the
grant in one statement (:func:`signed_urls.redeem`), and then **the policy runs
again** on the version's node. A grant minted five minutes ago proves only that its holder
could read the node then; a revoke, a trash or an erase between the mint and
the redemption has to be effective immediately, which
it can only be if the decision is re-made here rather than inherited from there.

Every refusal — a forged token, a replayed one, one minted for another session,
one whose node has since been trashed or whose grant belongs to another org —
is the same 404 with the same body, reached after the same single redemption
statement, so a caller cannot sort them by answer or by timing.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, replace
from typing import Annotated, Final
from uuid import UUID

from alkera_core.auth.machine_credential_standing import live_machine_of
from alkera_core.authz.principal import ActingContext
from alkera_core.compute.machines import machine_still_stands
from alkera_core.db.session import get_db
from alkera_core.files import names, signed_urls
from alkera_core.files.authz.actions import FilesAction
from alkera_core.files.authz.readable import access_by_id
from alkera_core.files.clock import SystemClock
from alkera_core.files.content import ContentService
from alkera_core.files.errors import FilesError, NotFound
from alkera_core.files.ids import NodeId, OrgScope, VersionId
from alkera_core.files.page_grants import PageGrant, redeem_page_grant
from alkera_core.files.repo import FilesRepo
from alkera_core.models.compute import PERSONAL_TENANCY
from alkera_core.models.files.tree import FileNode
from alkera_core.models.files.versions import FileVersion
from alkera_core.models.machine_credential import MachineCredential
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

# Re-exported rather than spelled twice: one definition of what a grant is
# bound to, shared by the route that mints and the route that redeems.
from backend.api import rate_limit
from backend.api.deps.files import as_platform
from backend.api.deps.files_facts import facts_for
from backend.api.routes.files.content import (
    authorized,
    content_disposition,
    parse_range,
    session_binding,
)
from backend.services.files.context import FilesContext, build_files_context

content_router = APIRouter()

#: Scan verdicts that must never reach a client. ``not_scanned`` and ``pending``
#: are servable today (the ClamAV worker has not landed) but only ever as an
#: ``attachment`` — that constraint lives in the disposition allowlist, not here.
UNSERVABLE_SCAN_STATES: frozenset[str] = frozenset({"infected", "error"})


class RangeNotSatisfiable(FilesError):  # noqa: N818 - matches the sibling names in files.errors
    """The ``Range`` names no span of this object, or none this URL was signed
    for (416)."""

    code = "files.range_not_satisfiable"
    status = 416


@content_router.get("/{token}")
async def serve(
    request: Request,
    token: str,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> StreamingResponse:
    """Redeem ``token`` and stream the version's bytes.

    Single use: the second request carrying the same token gets the identical
    404 a token that never existed gets.

    The redemption runs *before* the org's Files context is built, and that
    order is load-bearing: this origin is public and cookie-less, so the claim
    is attacker-supplied until the HMAC over it verifies, and
    :func:`build_files_context` provisions — and commits — a dedup domain, a
    drive and a root node for whichever org it is handed. Redeeming first means
    a forged claim is one statement that matches no row, and the org it named
    is left exactly as it was found.
    """
    from alkera_core.config import settings

    principal = await _with_personal_owner(db, _principal(token))
    if principal is None:
        raise NotFound()
    key = settings.effective_files_content_signing_key.encode()
    clock = SystemClock()
    # The take is scoped to the org the claim names rather than to a platform
    # role: the HMAC covers ``org_id``, so a claim naming the wrong org verifies
    # against no row anyway, and a repo scoped to a claimed org creates nothing
    # — it only narrows the nonce lookup that decides the refusal.
    unverified_scope = FilesRepo(db, OrgScope(org_team_id=principal.claim.org_id))
    grant = await signed_urls.redeem(
        unverified_scope,
        token=token,
        session_id=None,
        clock=clock,
        key=key,
    )
    if grant is None:
        raise NotFound()
    files = await _as_minted(
        db,
        await build_files_context(
            db,
            principal.context,
            settings=settings,
            clock=clock,
            org_id=principal.claim.org_id,
        ),
        principal,
    )
    async with files.repo.transaction():
        version = await files.repo.version(grant.version_id)
    if version is None or version.scan_state in UNSERVABLE_SCAN_STATES:
        raise NotFound()
    node = await _readable_node(request, db, files, version)
    span = _span(request, grant.range, version)
    service = ContentService(files.repo, principal.context, files.clock, files.store)
    body = await service.open(grant.version_id, range=span)
    return _response(
        body,
        version=version,
        node_name=node.name,
        span=span,
        attachment=principal.claim.attachment,
    )


#: The most segments a page may reach through. A page's assets sit beside it or
#: one folder down; a request naming more is walking a tree, not loading a
#: document, and the walk costs a statement per level.
MAX_PAGE_SEGMENTS: Final = 32

#: No name in the tree is longer than one path component a filesystem can hold,
#: so a longer segment can match nothing and is refused before it costs a query.
#: Deliberately the filesystem's ``NAME_MAX`` and not the namespace's tighter
#: ceiling: a row stored before that ceiling was lowered is still reachable, and
#: a cheap refusal that hid it would be a page asset that silently stopped
#: loading.
MAX_SEGMENT_BYTES: Final = names.FS_NAME_MAX_BYTES

#: Bytes that can never appear in a path segment this route will walk. The
#: backslash is here because a Windows client that built its request with the
#: wrong separator must not resolve *anything* — a name containing a literal
#: backslash is legal on POSIX, and silently treating one as a separator is how
#: a containment check and a filesystem come to disagree.
_REFUSED_IN_SEGMENT: Final = ("\x00", "\\")

#: Segment spellings that mean "somewhere else". Refused rather than resolved:
#: the walk starts at the grant's root and only ever descends, so a relative
#: segment has no meaning it could be given safely.
_REFUSED_SEGMENTS: Final = frozenset({"", ".", ".."})


@content_router.get("/p/{token}/{path:path}")
@content_router.head("/p/{token}/{path:path}")
async def serve_page(
    request: Request,
    token: str,
    path: str,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> StreamingResponse:
    """Serve one file of the folder ``token`` was granted, by its relative path.

    A rendered document is not one download: an HTML report fetches the chart
    beside it, and none of those fetches carries a credential, because this
    origin has none. So the grant covers a *folder* and every request under it
    is decided again — READ on each folder the path descends through, EXPORT on
    the file it lands on. Holding the URL buys the right to ask; it never
    decides the answer.

    The path is what Starlette has already percent-decoded once, and it is not
    decoded again: a second pass is what turns ``%252e%252e`` into ``..`` after
    the check that would have refused it. It is not normalized or case-folded
    either — the walk compares the raw UTF-8 of each segment against the name
    bytes stored on the node, so a name is matched as it was written and two
    Unicode spellings of one word are two different names, as they are in the
    tree.

    Every refusal — a forged signature, a spent grant, a segment that means
    "somewhere else", a sibling this reader may not see, a throttled burst — is
    the same opaque ``404`` the content mount answers everything else with.
    """
    from alkera_core.config import settings

    principal = await _with_personal_owner(db, _principal(token))
    if principal is None or principal.claim.kind != "page":
        raise NotFound()
    nonce = token.partition(".")[0]
    if not nonce or not rate_limit.page_grant_admits(request, nonce):
        raise NotFound()
    segments = _segments(path)
    key = settings.effective_files_content_signing_key.encode()
    clock = SystemClock()
    unverified_scope = FilesRepo(db, OrgScope(org_team_id=principal.claim.org_id))
    grant = await redeem_page_grant(unverified_scope, token=token, clock=clock, key=key)
    if grant is None or segments is None:
        raise NotFound()
    files = await _as_minted(
        db,
        await build_files_context(
            db,
            principal.context,
            settings=settings,
            clock=clock,
            org_id=principal.claim.org_id,
        ),
        principal,
    )
    walked = await _walk(files, grant, segments)
    node = await _may_serve_page(request, db, files, walked)
    async with files.repo.transaction():
        version = (
            None
            if node.head_version_id is None
            else await files.repo.version(VersionId(node.head_version_id))
        )
    if version is None or version.scan_state in UNSERVABLE_SCAN_STATES:
        raise NotFound()
    span = _span(request, None, version)
    service = ContentService(files.repo, principal.context, files.clock, files.store)
    body = await service.open(VersionId(version.id), range=span)
    return _response(
        body,
        version=version,
        node_name=bytes(node.name),
        span=span,
        attachment=False,
    )


def _segments(path: str) -> list[str] | None:
    """``path`` as the segments to walk, or ``None`` for the opaque refusal.

    The lexical gate, run before anything is read. Everything it refuses is
    refused for one reason: it would make the served node something other than
    "the child of the grant's root reached by these names" — an empty segment
    (``a//b``) hides one, ``.`` and ``..`` name a different node, a trailing
    slash asks for a folder, a backslash or a NUL is a separator or a terminator
    somewhere downstream, and an over-long path or segment is a walk rather than
    a document.
    """
    if not path or path.endswith("/"):
        return None
    segments = path.split("/")
    if len(segments) > MAX_PAGE_SEGMENTS:
        return None
    for segment in segments:
        if segment in _REFUSED_SEGMENTS:
            return None
        if any(bad in segment for bad in _REFUSED_IN_SEGMENT):
            return None
        if len(segment.encode("utf-8")) > MAX_SEGMENT_BYTES:
            return None
    return segments


@dataclass(frozen=True, slots=True)
class _Walked:
    """What a path resolved to: the folders above it, and the file it named.

    ``leaf`` is ``None`` when a name matched nothing, and the caller carries on
    regardless: a refusal that returned here would run fewer statements than a
    success, and the difference between the two is exactly the answer this route
    refuses to give — whether a file by that name is in that folder.
    """

    folders: Sequence[FileNode]
    leaf: FileNode | None


async def _walk(files: FilesContext, grant: PageGrant, segments: Sequence[str]) -> _Walked:
    """The nodes from the grant's root down to the file the path names.

    Matching is on the name BYTES: the UTF-8 of the segment against what the
    tree stores, with no case fold and no Unicode normalization. A name is a
    sequence of bytes here, exactly as it is on the box that wrote it, so the
    one spelling that resolves is the one that was stored.

    Only the last node may be a file, and only a folder may be descended into:
    a symlink, a special node or an object pointer is refused rather than
    followed — following one is how a grant over a folder becomes a read of
    whatever that node points at.

    A name that matches nothing ends the descent but not the request: the levels
    that did resolve are still handed back, and what remains is decided and
    refused at the same cost a served file pays.
    """
    async with files.repo.transaction():
        root = await files.repo.node(grant.root_node_id)
        if root is None or root.trashed_at is not None or root.kind != "folder":
            raise NotFound()
        folders: list[FileNode] = [root]
        leaf: FileNode | None = None
        for index, segment in enumerate(segments):
            wanted = segment.encode("utf-8")
            found = [
                child
                for child in await files.repo.siblings(NodeId(folders[-1].id))
                if bytes(child.name) == wanted
            ]
            last = index == len(segments) - 1
            kinds = ("file", "folder") if last else ("folder",)
            if len(found) != 1 or found[0].trashed_at is not None or found[0].kind not in kinds:
                break
            if last:
                leaf = found[0] if found[0].kind == "file" else None
            else:
                folders.append(found[0])
    return _Walked(folders=folders, leaf=leaf)


async def _may_serve_page(
    request: Request, db: AsyncSession, files: FilesContext, walked: _Walked
) -> FileNode:
    """Decide the whole chain for the grant's principal, or raise the opaque 404.

    One batched decision over the folders rather than one authorization per
    level: the answer is the same and the cost does not grow with the depth of
    the path. The file itself then goes through the route layer's own
    ``authorize`` so the decision lands in the audit trail as an allow or a deny
    on the node that was actually asked for.

    A path that named nothing is decided here too, against an id that exists
    nowhere — ``authorize`` runs its absent case, which performs the same loads
    and leaves the same shape of refusal. That is what makes "no such file" and
    "not yours" cost the same, which is the only reason the two can share a body.

    Every refusal collapses to the opaque answer, including the visible one:
    on the API host a reader who may see a node but not export it is told so,
    but this origin has no session to explain it to — a distinguishable refusal
    here would turn a page URL into a probe for what else is in the folder.
    """
    async with files.repo.transaction():
        async with as_platform(db):
            facts = await facts_for(
                request, db, files.ctx, files.drive, machine_id=files.agent_machine_id
            )
        access = await access_by_id(
            files.repo, files.ctx, [NodeId(node.id) for node in walked.folders], facts=facts
        )
    leaf = uuid.uuid4() if walked.leaf is None else walked.leaf.id
    try:
        allowed = await authorized(request, db, files, NodeId(leaf), FilesAction.EXPORT)
    except FilesError:
        raise NotFound() from None
    for folder in walked.folders:
        reachable = access.get(folder.id)
        if reachable is None or not reachable.allows(FilesAction.READ):
            raise NotFound()
    if walked.leaf is None or allowed.node.trashed_at is not None:
        raise NotFound()
    return allowed.node


@dataclass(frozen=True, slots=True)
class _Redeemer:
    """Who a token says it was minted for, and the context that acts as them."""

    claim: signed_urls.ContentClaim
    context: ActingContext


async def _as_minted(db: AsyncSession, files: FilesContext, principal: _Redeemer) -> FilesContext:
    """The context, carrying the machine the MINT proved rather than one this
    request could prove.

    Every other door checks an agent's assertion against its registration on
    the credential the request carries. This origin has no credential at all —
    the URL is the whole capability — so the check cannot be repeated here, and
    the session id in the claim is a box's public machine id, which would prove
    nothing. What is trusted instead is the server's own signature: the mint
    wrote the VERIFIED machine into the claim and the MAC covers it, so an
    edited or hand-built claim never reaches this line (``redeem`` has already
    refused it). A URL minted by anyone who was not a proven box carries no
    machine and redeems as the reader they are.

    What the signature cannot say is whether the box is STILL a machine: a
    URL minted a minute before its credential was revoked would otherwise
    carry the box's standing past the revoke. So the machine the claim names
    is re-asked here — live, and somebody (its credential, for a platform box)
    standing behind it — and a machine that no longer stands redeems as the
    plain agent, which a sealed chat folder refuses.
    """
    machine_id = principal.claim.machine_id
    if principal.context.is_machine:
        # Minted by a box on its own credential: there is no person to fall
        # back to, so a credential that no longer stands behind the machine
        # is the end of the URL rather than a downgrade — the same rule every
        # other door and the box's own socket re-ask.
        credential_id = principal.claim.credential_id
        held = None if credential_id is None else await live_machine_of(db, credential_id)
        if held is None or str(held) != machine_id:
            raise NotFound()
        return replace(files, agent_machine_id=machine_id)
    if machine_id is not None and not await machine_still_stands(db, machine_id=machine_id):
        machine_id = None
    return replace(files, agent_machine_id=machine_id)


async def _with_personal_owner(db: AsyncSession, principal: _Redeemer | None) -> _Redeemer | None:
    """A URL a person's own box minted redeems as that box, owner and all: the
    claim names the credential, and a personal credential's box runs its
    person's chats only, here as on every other door."""
    if principal is None or not principal.context.is_machine:
        return principal
    credential_id = principal.claim.credential_id
    row = None if credential_id is None else await db.get(MachineCredential, credential_id)
    if credential_id is None or row is None or row.tenancy != PERSONAL_TENANCY:
        return principal
    ctx = principal.context
    return replace(
        principal,
        context=ActingContext.for_machine(
            machine_id=UUID(ctx.acting_principal.id),
            credential_id=credential_id,
            org_id=ctx.org_id,
            label="",
            personal_owner_id=row.created_by,
        ),
    )


def _principal(token: str) -> _Redeemer | None:
    """The acting context this token names, or ``None`` for the opaque 404.

    An unauthenticated request to the content domain is *not* a 401: a 401 tells
    a prober that the token they hold names something real and only their
    credential is missing. There is nothing to authenticate here anyway — the
    grant IS the capability, so the caller is whoever the signed claim names,
    and what that buys them is decided a few lines later by re-running the
    policy, not by holding the URL. A token with no user in its claim is
    refused rather than served as somebody: an unauthorizable download is not a
    download.
    """
    claim = signed_urls.parse_claim(token)
    if claim is None:
        return None
    if claim.user_id is None:
        # Minted by a box on its own credential, which names a machine and the
        # credential it held and nobody at all. The policy is re-run as THAT
        # machine — a member of no org, admitted to its own chats' folders and
        # nothing else — and the claim's org is the drive's, which is the one
        # org such a context serves. A claim naming neither a user nor a
        # credential is nobody's, and nobody's download is refused.
        if claim.machine_id is None or claim.credential_id is None:
            return None
        try:
            machine_id = UUID(claim.machine_id)
        except ValueError:
            return None
        return _Redeemer(
            claim=claim,
            context=ActingContext.for_machine(
                machine_id=machine_id,
                credential_id=claim.credential_id,
                org_id=claim.org_id,
                label="",
            ),
        )
    if claim.session_id is not None:
        # Minted by an agent: the policy is re-run as THAT agent, not as the
        # person behind it. The one decision that differs is the chat folder's
        # NO_DOWNLOAD, which yields only to the agent holding the folder's
        # lease — a box redeeming its own mint carries the same fence it minted
        # under, and a person redeeming a URL an agent minted is not the box.
        return _Redeemer(
            claim=claim,
            context=ActingContext.for_agent(
                user_id=claim.user_id,
                org_id=claim.org_id,
                email="",
                session_id=str(claim.session_id),
            ),
        )
    return _Redeemer(
        claim=claim,
        context=ActingContext.for_user(user_id=claim.user_id, org_id=claim.org_id, email=""),
    )


async def _readable_node(
    request: Request, db: AsyncSession, files: FilesContext, version: FileVersion
) -> FileNode:
    """Re-run the policy for ``EXPORT`` on the version's node, or 404.

    This is the line that makes a revoke effective immediately: a full
    ``authorize`` against today's grants, not a replay of the decision the mint
    made. It refuses a trashed node for the same reason — the grant predates the
    trash, and a URL is not a way to keep reading something that has been thrown
    away.
    """
    allowed = await authorized(request, db, files, NodeId(version.node_id), FilesAction.EXPORT)
    if allowed.node.trashed_at is not None:
        raise NotFound()
    return allowed.node


def _span(
    request: Request, granted: tuple[int, int] | None, version: FileVersion
) -> tuple[int, int] | None:
    """The byte span to serve: the grant's, narrowed by the request's ``Range``.

    A grant minted for a span is a grant for that span only — a request cannot
    widen it by asking for more, so what gets served is the intersection and a
    request falling outside it is a 416 rather than a quiet full-object read.
    ``If-Range`` is honoured against the version's content hash: a mismatch means
    the representation moved under the client, and the whole object — not a
    silently misaligned slice of a different one — is the safe answer.
    """
    size = version.size_bytes
    asked = request.headers.get("Range")
    if asked is not None and not _if_range_matches(request, version):
        asked = None
    requested = None if asked is None else parse_range(asked, size)
    if asked is not None and requested is None:
        raise RangeNotSatisfiable("Range names no span of this object")
    if granted is None:
        return requested
    if requested is None:
        return granted
    lo = max(granted[0], requested[0])
    hi = min(granted[1], requested[1])
    if hi < lo:
        raise RangeNotSatisfiable("Range falls outside the span this URL was signed for")
    return lo, hi


def _if_range_matches(request: Request, version: FileVersion) -> bool:
    """Whether an ``If-Range`` (when present) still names this representation.

    Only the entity-tag form is understood; the HTTP-date form is treated as a
    mismatch, which downgrades the answer to the whole object — the conservative
    direction, and the one RFC 9110 allows for a validator you cannot compare.
    """
    raw = request.headers.get("If-Range")
    if raw is None:
        return True
    return raw.strip().removeprefix("W/").strip('"') == version.content_hash


def _response(
    body: AsyncIterator[bytes],
    *,
    version: FileVersion,
    node_name: bytes,
    span: tuple[int, int] | None,
    attachment: bool,
) -> StreamingResponse:
    """The streamed answer, with the headers this route owns.

    ``Content-Type`` is the *sniffed* type off the version row, never anything
    the uploader declared; the disposition comes from the same builder the
    content app uses, so ``inline`` stays confined to the sniffed allowlist. The
    rest of the A5 recipe is stamped by the content app's middleware, including
    on this route's refusals.

    ``attachment`` is the mint's request, carried under the URL's signature: it
    only ever narrows the builder's answer from ``inline`` to ``attachment``,
    and it reuses that answer's filename parameters verbatim so a name with a
    space or a non-ASCII character is byte-identical either way.
    """
    size = version.size_bytes
    headers = {
        "ETag": f'"{version.content_hash}"',
        "Accept-Ranges": "bytes",
        "Content-Disposition": content_disposition(node_name, version.mime_sniffed, attachment),
    }
    status = 200
    if span is not None and span != (0, size - 1):
        status = 206
        headers["Content-Range"] = f"bytes {span[0]}-{span[1]}/{size}"
        headers["Content-Length"] = str(span[1] - span[0] + 1)
    else:
        headers["Content-Length"] = str(size)
    return StreamingResponse(
        body, status_code=status, media_type=version.mime_sniffed, headers=headers
    )


__all__ = [
    "UNSERVABLE_SCAN_STATES",
    "RangeNotSatisfiable",
    "content_router",
    "serve",
    "session_binding",
]
