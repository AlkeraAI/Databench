"""The upload-session family: open, parts, status, complete, abort.

Every handler here is the same five lines in a different order — resolve the
context, authorize a node through ``files.authz.authorize`` with the backend's
``enforce`` injected, call the library, render. The interesting decisions (the
quota hold at open, the checksum agreement on a part, the compare-and-swap into
``committing``) all live in :mod:`alkera_core.files.uploads`; nothing about
them is restated here, so a change to the rules changes one place.

Two contracts are worth naming because they are invisible in the handler
bodies:

* **A session id is a node.** Every session route authorizes ``WRITE`` on the
  folder the session was opened against, and a session this org does not own
  authorizes against an id that does not exist — the same ``authorize`` call,
  reaching the same refusal, with no early return in front of it. A stranger,
  another org and a member who cannot write the folder are one answer.
* **``complete`` never touches bytes.** It returns ``202`` and an operation
  whether the parts are three or three thousand, and whether or not the org
  already holds identical content, so nothing a client can time says what the
  store contains.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated, Final, Literal

from alkera_core.config import settings
from alkera_core.db.base import Base
from alkera_core.db.session import get_db
from alkera_core.files import names
from alkera_core.files.authz.actions import FilesAction
from alkera_core.files.authz.authorize import Denied, authorize
from alkera_core.files.clock import SystemClock
from alkera_core.files.db_retry import with_db_retries
from alkera_core.files.errors import InvalidRequest, NotFound
from alkera_core.files.idempotency import MissingIdempotencyKey, StoredResponse, idempotent
from alkera_core.files.ids import DriveId, NodeId, OperationId, SessionId
from alkera_core.files.ops import OperationState
from alkera_core.files.repo import FilesRepo
from alkera_core.files.uploads import (
    PART_SIZE,
    CommitBehavior,
    ConflictSubmission,
    PartRef,
    UploadCompletion,
    UploadService,
)
from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps.files import (
    FILES_RETRY_BUDGET,
    Idempotency,
    IdempotencyHeader,
    IdempotencyKey,
    Lease,
    _parse_etag,
    files_enforcer,
    ratelimited,
)
from backend.api.deps.files_context import FilesCtx
from backend.api.deps.files_facts import facts_for
from backend.api.routes.files.content import fence
from backend.services.files.context import FilesContext, restart_attempt
from backend.services.files.operations_runner import queue_inline

#: The upload-session table, reached the way :mod:`alkera_core.files.idempotency`
#: reaches its own: statements naming a Files ORM class belong to ``repo.py``,
#: and the route needs exactly one column pair off this row.
_SESSIONS: Final = Base.metadata.tables["file_upload_sessions"]

#: A proxied part is capped so one request cannot pin a worker's memory or its
#: connection for an unbounded time. Larger parts are a typed 422, never a
#: timeout: a published limit is always a typed 4xx. It is the deployment's
#: own setting rather than the library's constant, so what ``limits`` publishes
#: and what the part PUT enforces cannot drift apart.
MAX_PROXIED_PART_BYTES: Final = settings.files_part_max_bytes

router = APIRouter(prefix="/uploads", tags=["files"])


class _Wire(BaseModel):
    """camelCase on the wire, snake_case in Python, both directions."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class OpenUploadRequest(_Wire):
    declared_size: int = Field(ge=0)
    name: str
    parent_id: uuid.UUID
    mime: str | None = None
    #: The holder's conflict submission: bytes its disk held until a write from
    #: the web displaced them, filed beside this node as a conflicted copy the
    #: drive names. Only the fenced holder of the folder's lease may send it;
    #: anyone else is refused ``409 files.lease_mismatch``.
    conflict_of: uuid.UUID | None = None
    #: A conflict submission that cannot name the node it displaced: filed as a
    #: new file the drive names with the conflicted-copy namer, linked to
    #: nothing. Implied by ``conflictOf``; the same holder-only rule.
    conflict_copy: bool = False


class UploadLimits(_Wire):
    """What this deployment will accept, published before the first byte.

    Every number here is enforced: ``maxFileBytes`` and ``maxParts`` at open,
    ``maxPartBytes`` at the part PUT, ``singlePutMaxBytes`` on the content PUT
    a client uses instead of a session. A client that reads them never meets a
    ceiling it could not have known about.
    """

    max_file_bytes: int
    single_put_max_bytes: int
    max_part_bytes: int
    max_parts: int


class OpenUploadResponse(_Wire):
    """What to send and where to send it.

    ``partsTotal`` is never zero. A file of ``declaredSize: 0`` is published as
    one part, and that single ``Content-Length: 0`` PUT is how an empty file is
    created — there is no separate "no parts" session, so a client cuts every
    size the same way.
    """

    upload_id: str
    part_size: int
    parts_total: int
    limits: UploadLimits
    expires_at: str


class UploadStatusResponse(_Wire):
    upload_id: str
    state: str
    offset: int
    length: int
    complete: bool
    parts_done: int
    parts_total: int
    accepted_parts: list[int]
    expires_at: str


class PartResponse(_Wire):
    part_no: int
    size: int
    duplicate: bool


class CompletePart(_Wire):
    part_no: int = Field(ge=1)
    #: Zero is a legal part size, and only for the one part of a zero-byte
    #: file: ``put_part`` refuses an empty part on any session that declared
    #: bytes, so the completion list never has to police it a second time.
    size: int = Field(ge=0)
    checksum: str


#: The most part descriptors one completion may carry, and so the bound on the
#: object tree FastAPI builds from this body before any dependency runs. The
#: value is `files_max_upload_parts`' default rather than a read of it: this is
#: part of the published OpenAPI schema, which must not vary with whose
#: environment generated it. A test pins the two together.
MAX_COMPLETE_PARTS = 10_000


class CompleteUploadRequest(_Wire):
    parts: list[CompletePart] = Field(max_length=MAX_COMPLETE_PARTS)
    #: ``fail``/``rename`` decide how a *new* node takes the name. ``replace``
    #: lands the bytes as a new version on the live same-name file — the same
    #: head swap a content PUT performs, and the only way a file larger than
    #: the single-call PUT cap can have its bytes replaced. Send ``If-Match``
    #: with it to fence the version the caller actually saw.
    conflict_behavior: Literal["fail", "rename", "replace"] = "fail"


class OperationResponse(_Wire):
    id: str
    kind: str
    state: str
    done: int
    total: int | None
    #: A conflict submission's copy, named by the drive when the session
    #: completed — the holder renames its staged file to ``name``. Null on
    #: every other upload, and null on a submission kept only as a version of
    #: the node it displaced (a machine-managed path, or no room for a file).
    node_id: str | None = None
    name: str | None = None
    #: The conflict row linking the copy to the node it displaced; null when
    #: the submission named no node.
    conflict_id: str | None = None


def _operation(
    state: OperationState, submission: ConflictSubmission | None = None
) -> OperationResponse:
    return OperationResponse(
        id=str(state.id),
        kind=state.kind,
        state=state.state,
        done=state.done,
        total=state.total,
        node_id=None
        if submission is None or submission.node_id is None
        else str(submission.node_id),
        name=None
        if submission is None or submission.name is None
        else names.display(submission.name),
        conflict_id=None
        if submission is None or submission.conflict_id is None
        else str(submission.conflict_id),
    )


async def _writable_parent(
    request: Request,
    db: AsyncSession,
    files: FilesContext,
    parent_id: NodeId,
) -> None:
    """Refuse unless the caller may write into ``parent_id``.

    The result is deliberately discarded: what the handler needs is the
    *refusal*, and the library re-reads the folder inside its own transaction.
    """
    facts = await facts_for(request, db, files.ctx, files.drive)
    repo = _repo(files)
    async with repo.transaction():
        await authorize(
            files.ctx,
            repo,
            parent_id,
            FilesAction.WRITE,
            facts=facts,
            enforce=files_enforcer(request, db),
        )


async def _session_parent(files: FilesContext, session_id: SessionId) -> NodeId:
    """The folder a session was opened against, as an id ``authorize`` can take.

    A session that is not this org's yields a random id rather than an early
    ``404``: the policy still runs, over a node that does not exist, and the
    caller cannot tell "no such session" from "not your session" from "you may
    not write there".
    """
    statement = select(_SESSIONS.c.parent_id).where(
        _SESSIONS.c.id == session_id,
        _SESSIONS.c.org_team_id == files.repo.scope.org_team_id,
    )
    found = (await files.repo.session.execute(statement)).scalar_one_or_none()
    return NodeId(found) if found is not None else NodeId(uuid.uuid4())


async def _writable_session(
    request: Request,
    db: AsyncSession,
    files: FilesContext,
    session_id: SessionId,
) -> None:
    """Refuse unless the caller may write into the session's folder, opaquely.

    The id the caller named is the *session*, so every refusal has to read like
    the miss an invented id earns. The policy already hides a folder the caller
    cannot read, but it answers a coded ``403`` when they can read it and may
    not write there — honest about the folder, and an existence oracle over
    session ids once the folder is only reached through one: a real session
    would answer ``403`` where an invented one answers ``404``. Session-addressed
    routes take the refusal as absence; the open route, where the caller names
    the folder itself, keeps the coded answer.
    """
    try:
        await _writable_parent(request, db, files, await _session_parent(files, session_id))
    except Denied:
        raise NotFound() from None


def _repo(files: FilesContext) -> FilesRepo:
    """The request's joined repo, built once and reused.

    ``idempotent`` claims the key, runs the effect and stores the answer in one
    transaction — that single unit of work is what makes a replay free of a
    second effect. A collaborator that opens its own transaction would abort
    against a repo that owns one, so every collaborator gets the same joined
    handle and the re-entry is the same unit of work rather than a second one.
    The join is decided by *this* handle's open flag, so it must be the same
    object every time.
    """
    cached: FilesRepo | None = getattr(files.repo, "_route_repo", None)
    if cached is None:
        cached = FilesRepo.joined(files.repo.session, files.repo.scope)
        files.repo._route_repo = cached  # type: ignore[attr-defined]
    return cached


def _limits(files: FilesContext) -> UploadLimits:
    """The deployment's real ceilings, read at request time.

    Not the library's constants: a self-hosted install that lowers a cap must
    publish the cap it will actually enforce, or the published numbers are a
    promise the commit breaks.
    """
    return UploadLimits(
        max_file_bytes=files.settings.files_max_file_bytes,
        single_put_max_bytes=files.settings.files_single_put_max_bytes,
        max_part_bytes=MAX_PROXIED_PART_BYTES,
        max_parts=files.settings.files_max_upload_parts,
    )


def part_size() -> int:
    """The proxied part size, read through a function rather than baked into a
    default argument, so a multi-part upload can be driven end to end through
    the real routes without pushing 64 MiB of body through them."""
    return PART_SIZE


def _uploads(files: FilesContext, *, repo: FilesRepo | None = None) -> UploadService:
    return UploadService(
        repo or _repo(files),
        files.ctx,
        files.clock,
        files.store,
        part_size=part_size(),
        ceilings=files.ceilings,
    )


def _completion(files: FilesContext) -> UploadCompletion:
    return UploadCompletion(
        _repo(files), files.ctx, files.clock, files.store, part_size=part_size()
    )


def _json(status: int, model: BaseModel) -> StoredResponse:
    return StoredResponse(
        status=status,
        body=model.model_dump_json(by_alias=True).encode(),
        headers={"content-type": "application/json"},
    )


def _replay(stored: StoredResponse) -> Response:
    return Response(
        content=stored.body,
        status_code=stored.status,
        media_type=stored.headers.get("content-type", "application/json"),
    )


async def _idempotently(
    files: FilesContext,
    key: IdempotencyKey,
    *,
    route: str,
    run: Callable[[BackgroundTasks], Awaitable[StoredResponse]],
    background: BackgroundTasks | None = None,
) -> StoredResponse:
    """Claim the key, run the effect, and replay the two Postgres verdicts.

    The session family is the only Files family that writes its own claim
    instead of wearing ``idempotent_route``, because a part PUT must stay
    outside the record and the three that surround it were built beside it.
    Writing the claim by hand also left them without the retry ladder the
    decorator carries, so ``40001`` / ``40P01`` — Postgres saying "you two
    collided, run it again" — reached the client as a 500: a browser uploading
    a folder opens dozens of sessions at once, every one of them locking the
    same drive row for its quota hold, and each lost race dropped a file on the
    floor with no answer a client could retry. The ladder rides the claim, so a
    replay re-runs the operation rather than repeating its effect.

    A replayed attempt starts from a clean session: Postgres rolled the losing
    transaction back whole, so the claim it wrote went with it and the joined
    repo it ran under cannot be reused. Inline work is collected per attempt
    for the same reason the decorator does it — an attempt that lost a deadlock
    must not leave its background task behind for the winner to run twice.
    """
    attempts = 0
    collected = BackgroundTasks()

    async def once() -> StoredResponse:
        nonlocal attempts, collected
        attempts += 1
        if attempts > 1:
            await restart_attempt(files)
            files.repo._route_repo = None  # type: ignore[attr-defined]
        collected = BackgroundTasks()
        staged = collected

        async def claimed() -> StoredResponse:
            return await run(staged)

        return await idempotent(
            _repo(files),
            files.ctx,
            route=route,
            key=key.key,
            request_hash=key.request_hash,
            run=claimed,
        )

    answered = await with_db_retries(
        once, budget=FILES_RETRY_BUDGET, clock=SystemClock(), sleep=asyncio.sleep
    )
    if background is not None:
        background.tasks.extend(collected.tasks)
    return answered


Db = Annotated[AsyncSession, Depends(get_db, scope="function")]


@router.post("", status_code=201, dependencies=[Depends(ratelimited("uploads"))])
async def open_upload(
    request: Request,
    db: Db,
    files: FilesCtx,
    body: OpenUploadRequest,
    key: Idempotency,
    lease: Lease,
) -> Response:
    """Reserve room and a session id. Over quota is a 507 before any byte."""
    assert key is not None
    parent = NodeId(body.parent_id)
    await _writable_parent(request, db, files, parent)
    limits = _limits(files)

    async def run(_inline: BackgroundTasks) -> StoredResponse:
        # Read inside the attempt: a replay reloads the context's drive before
        # it runs again, so the column is loaded on every attempt.
        session = await _uploads(files).open(
            DriveId(files.drive.id),
            parent,
            body.name.encode("utf-8"),
            declared_size=body.declared_size,
            mime_hint=body.mime,
            lease=fence(lease, files),
            conflict_of=None if body.conflict_of is None else NodeId(body.conflict_of),
            conflict_copy=body.conflict_copy,
        )
        return _json(
            201,
            OpenUploadResponse(
                upload_id=str(session.id),
                part_size=session.part_size,
                parts_total=session.parts_total,
                limits=limits,
                expires_at=session.expires_at.isoformat(),
            ),
        )

    return _replay(await _idempotently(files, key, route="files.uploads.open", run=run))


@router.get("/{session_id}", dependencies=[Depends(ratelimited("upload_parts"))])
async def upload_status(
    request: Request,
    db: Db,
    files: FilesCtx,
    session_id: uuid.UUID,
) -> UploadStatusResponse:
    """What a resuming client needs: the offset and exactly which parts landed."""
    sid = SessionId(session_id)
    await _writable_session(request, db, files, sid)
    status = await _uploads(files).status(sid)
    return UploadStatusResponse(
        upload_id=str(status.id),
        state=status.state,
        offset=status.offset,
        length=status.length,
        complete=status.complete,
        parts_done=status.parts_done,
        parts_total=status.parts_total,
        accepted_parts=list(status.accepted_parts),
        expires_at=status.expires_at.isoformat(),
    )


@router.put("/{session_id}/parts/{part_no}", dependencies=[Depends(ratelimited("upload_parts"))])
async def put_part(
    request: Request,
    db: Db,
    files: FilesCtx,
    session_id: uuid.UUID,
    part_no: int,
    _key: IdempotencyHeader,
) -> PartResponse:
    """Stream one proxied part in, verified against ``X-Part-Checksum``.

    The part is *not* wrapped in the idempotency record: a resend is already a
    no-op decided by the stored checksum, and buffering a 32 MiB body to hash it
    for a key would defeat the streaming this route exists for. The key is
    still required, so a part with no key is the same 428 as every other
    non-GET.
    """
    sid = SessionId(session_id)
    await _writable_session(request, db, files, sid)
    checksum = _checksum(request)
    size = _declared_length(request)
    # The context's own repo, not the request-joined one: the library commits
    # its claim before the body streams and writes the part's row in a short
    # transaction afterwards, so no row lock and no pooled connection is held
    # while a client sends the bytes. The write decision above commits with
    # the claim.
    result = await _uploads(files, repo=files.repo).put_part(
        sid, part_no, _body(request), size=size, checksum=checksum
    )
    return PartResponse(part_no=result.part_no, size=result.size, duplicate=result.duplicate)


def _checksum(request: Request) -> bytes:
    raw = request.headers.get("X-Part-Checksum")
    if raw is None or not raw.strip():
        raise InvalidRequest(
            "files.part_checksum_required", "a proxied part carries X-Part-Checksum"
        )
    try:
        return bytes.fromhex(raw.strip())
    except ValueError:
        raise InvalidRequest(
            "files.part_checksum_required", "X-Part-Checksum must be hex"
        ) from None


def _declared_length(request: Request) -> int:
    """The part's length, which a proxied part must declare up front.

    The store writes a fixed-size object; a chunked body with no length would
    have to be buffered to find out how big it is, which is the thing this
    route refuses to do.

    ``Content-Length: 0`` is a length like any other: it is the one part of a
    zero-byte file's session. Whether a session may be satisfied with it is not
    a header question — the library decides it against the size the session
    declared — so all this refuses is a header that is absent or nonsense.
    """
    raw = request.headers.get("content-length")
    if raw is None:
        raise InvalidRequest("files.empty_part", "a proxied part must declare Content-Length")
    try:
        size = int(raw)
    except ValueError:
        raise InvalidRequest("files.empty_part", "Content-Length must be an integer") from None
    if size < 0:
        raise InvalidRequest("files.empty_part", "a part may not declare negative bytes")
    if size > MAX_PROXIED_PART_BYTES:
        raise InvalidRequest(
            "files.part_too_large",
            f"a proxied part declared {size} bytes; "
            f"this deployment accepts at most {MAX_PROXIED_PART_BYTES}",
        )
    return size


async def _body(request: Request) -> AsyncIterator[bytes]:
    async for chunk in request.stream():
        yield chunk


@router.post("/{session_id}/complete", dependencies=[Depends(ratelimited("upload_parts"))])
async def complete_upload(
    request: Request,
    db: Db,
    files: FilesCtx,
    session_id: uuid.UUID,
    body: CompleteUploadRequest,
    key: Idempotency,
    background: BackgroundTasks,
) -> Response:
    """Agree on the parts and queue the commit. Always ``202``."""
    assert key is not None
    sid = SessionId(session_id)
    if_match = _completion_precondition(request, body.conflict_behavior)
    await _writable_session(request, db, files, sid)

    async def run(inline: BackgroundTasks) -> StoredResponse:
        completion = _completion(files)
        state = await completion.complete(
            sid,
            [
                PartRef(part_no=p.part_no, size=p.size, checksum=_hex(p.checksum))
                for p in body.parts
            ],
            conflict=body.conflict_behavior,
            if_match=if_match,
        )
        submission = await completion.submission(OperationId(state.id))
        # A replay does not reach here, so the commit is asked for exactly once
        # however many times the client retries this key.
        queue_inline(inline, files, "promote", OperationId(state.id))
        return _json(202, _operation(state, submission))

    return _replay(
        await _idempotently(
            files, key, route="files.uploads.complete", run=run, background=background
        )
    )


def _completion_precondition(request: Request, conflict: CommitBehavior) -> int | None:
    """The ``If-Match`` a completion carries — required only for ``replace``.

    Every other Files mutation names a node and so must name the version it
    believes it is changing. A completion usually names none: it creates a
    file, and there is nothing to fence, so ``fail`` and ``rename`` stay
    header-optional. ``replace`` does write onto a node that already exists,
    which puts it under the same rule as every other mutation: without the
    header the commit would fence against whatever the etag happened to be by
    the time the bytes landed, silently overwriting whoever wrote in between.
    A malformed etag is still a 422.
    """
    raw = request.headers.get("If-Match")
    if raw is None or not raw.strip():
        if conflict == "replace":
            raise MissingIdempotencyKey(
                "files.if_match_required",
                "conflictBehavior=replace requires an If-Match header",
            )
        return None
    return _parse_etag(raw)


def _hex(raw: str) -> bytes:
    try:
        return bytes.fromhex(raw)
    except ValueError:
        raise InvalidRequest("files.part_mismatch", "a part checksum must be hex") from None


@router.delete(
    "/{session_id}", status_code=204, dependencies=[Depends(ratelimited("upload_parts"))]
)
async def abort_upload(
    request: Request,
    db: Db,
    files: FilesCtx,
    session_id: uuid.UUID,
    key: Idempotency,
) -> Response:
    """End the session and give its reserved room back in the same transaction."""
    assert key is not None
    sid = SessionId(session_id)
    await _writable_session(request, db, files, sid)

    async def run(_inline: BackgroundTasks) -> StoredResponse:
        await _uploads(files).abort(sid)
        return StoredResponse(status=204, body=b"", headers={})

    stored = await _idempotently(files, key, route="files.uploads.abort", run=run)
    return Response(status_code=stored.status)


__all__ = [
    "MAX_PROXIED_PART_BYTES",
    "abort_upload",
    "complete_upload",
    "open_upload",
    "part_size",
    "put_part",
    "router",
    "upload_status",
]
