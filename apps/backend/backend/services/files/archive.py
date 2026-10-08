"""A subtree as a ZIP64 stream that is never held in memory.

The whole point of this module is the bound: an archive of a 200 GiB tree must
cost the same resident memory as an archive of one file. ``zipfile`` will
happily write into a file object, so the trick is to hand it a *pipe* rather
than a buffer — every byte the writer produces is passed to the caller and
dropped on the next iteration — and to refuse ``seek`` on that pipe, which is
what makes ``zipfile`` emit data descriptors instead of rewinding to patch
sizes it could not have known in advance.

Two consequences are deliberate:

* every member is written with ``force_zip64``, so a member that turns out to
  be larger than 4 GiB does not have to be rewritten (there is nothing to
  rewind to) and the archive is a valid ZIP64 whatever it ends up containing;
* an item that cannot be archived is never guessed at. It is *listed* — by id,
  never by name — in the ``_alkera/skipped.txt`` member and returned to the
  caller, so the operation's ``errors[]`` says exactly what the archive says.
"""

from __future__ import annotations

import base64
import hmac
import uuid
import zipfile
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import IO, Any, Protocol, cast

from alkera_core.authz.decision import Decision
from alkera_core.authz.engine import authorize as engine_authorize
from alkera_core.authz.enums import Action, ResourceType
from alkera_core.authz.principal import ActingContext
from alkera_core.authz.resource import Resource
from alkera_core.files.authz.actions import FilesAction
from alkera_core.files.authz.authorize import EXISTS, Authorized, platform_action
from alkera_core.files.authz.decider import AccessFacts, EffectiveAccess
from alkera_core.files.authz.policy_attrs import policy_attrs
from alkera_core.files.authz.readable import access_by_id
from alkera_core.files.content import ContentService
from alkera_core.files.ids import DriveId, NodeId, VersionId
from alkera_core.models.files.stores import FileDrive
from alkera_core.models.files.tree import FileNode

from backend.services.files.context import FilesContext


class PolicyDecider(Protocol):
    """The pure decision one member is judged by: the platform engine, or a
    test's stand-in for it."""

    def __call__(
        self,
        ctx: ActingContext,
        action: Action,
        resource: Resource,
        attrs: Mapping[str, object],
    ) -> Decision: ...


#: The member that always tells the truth about what is missing. Someone who
#: only ever opens the archive still learns that something was left out.
SKIPPED_MEMBER = "_alkera/skipped.txt"

#: How much the pipe accumulates before it is handed on. Small enough that
#: peak memory is a constant; large enough that a tree of 1 KiB files does not
#: cost a yield per member.
FLUSH_BYTES = 1 << 20

#: Why an item is not in the archive. Codes, because a reason that named the
#: item would be an oracle for something the caller may not read.
SKIP_UNREADABLE = "files.unreadable"
SKIP_NOT_BYTES = "files.not_bytes"
#: The caller may read this node but may not take its bytes out — a chat
#: folder, or anything else carrying ``NO_DOWNLOAD``. A zip of the folder above
#: it is still an export, so the subtree is pruned rather than flattened in.
SKIP_NO_EXPORT = "files.no_export"

#: What ``open`` returns: a fresh stream over one member's bytes.
ByteStream = Callable[[], Awaitable[AsyncIterator[bytes]]]


@dataclass(frozen=True, slots=True)
class ArchiveEntry:
    """One member of the archive.

    ``open`` is a factory rather than an open stream because the walk happens
    long before the bytes are wanted: holding 50,000 open readers is precisely
    the resource shape this module exists to avoid. ``None`` means a directory.
    """

    path: str
    size: int = 0
    modified: datetime | None = None
    open: ByteStream | None = None

    @property
    def is_dir(self) -> bool:
        return self.open is None


@dataclass(frozen=True, slots=True)
class ArchiveSkip:
    """An item the archive does not contain, named by id only."""

    node_id: str
    code: str

    def as_error(self) -> dict[str, str]:
        """The shape an operation's ``errors[]`` carries."""
        return {"itemId": self.node_id, "code": self.code, "message": self.code}


@dataclass(frozen=True, slots=True)
class ArchivePlan:
    """What the walk decided: what goes in, and what could not.

    The materialised shape, for an archive small enough to hold — a test's
    fixture, a handful of entries assembled by hand. A subtree is archived
    through :class:`SubtreeArchive` instead, which is the same thing produced a
    page at a time.
    """

    entries: Sequence[ArchiveEntry] = field(default_factory=tuple)
    skips: Sequence[ArchiveSkip] = field(default_factory=tuple)
    #: Omissions past the recording ceiling. Always zero here.
    omitted: int = 0


class _Pipe:
    """A write-only sink that answers ``tell`` and refuses ``seek``.

    Refusing ``seek`` is the load-bearing part: it is how ``zipfile`` is told
    the output is a stream, which makes it write a data descriptor after each
    member rather than seeking back to patch the local header.
    """

    def __init__(self) -> None:
        self._parts: list[bytes] = []
        self._pending = 0
        self._offset = 0

    def write(self, data: bytes) -> int:
        payload = bytes(data)
        self._parts.append(payload)
        self._pending += len(payload)
        self._offset += len(payload)
        return len(payload)

    def flush(self) -> None:
        return None

    def tell(self) -> int:
        return self._offset

    @property
    def pending(self) -> int:
        return self._pending

    def drain(self) -> bytes:
        out = b"".join(self._parts)
        self._parts.clear()
        self._pending = 0
        return out


def _info(path: str, *, modified: datetime | None = None, is_dir: bool = False) -> zipfile.ZipInfo:
    when = (
        (
            modified.year,
            modified.month,
            modified.day,
            modified.hour,
            modified.minute,
            modified.second,
        )
        if modified is not None
        else (1980, 1, 1, 0, 0, 0)
    )
    info = zipfile.ZipInfo(path.rstrip("/") + "/" if is_dir else path, date_time=when)
    info.compress_type = zipfile.ZIP_STORED
    info.external_attr = (0o40755 << 16) | 0x10 if is_dir else (0o644 << 16)
    return info


def skipped_text(skips: Sequence[ArchiveSkip], *, omitted: int = 0) -> bytes:
    """The ``_alkera/skipped.txt`` body: one id and one code per line.

    ``omitted`` is how many more were left out than this list names. It is
    stated rather than enumerated because the list is capped: a subtree none of
    which the caller may read would otherwise put one line per node into the
    archive, and a count is the honest answer at that size.
    """
    header = b"# items omitted from this archive, by id\n"
    body = b"".join(f"{skip.node_id}\t{skip.code}\n".encode() for skip in skips)
    if omitted > 0:
        body += f"# and {omitted} more, not listed\n".encode()
    return header + body


async def _iter_entries(source: ArchiveSource) -> AsyncIterator[ArchiveEntry]:
    """The source's members, whether it holds them or produces them."""
    if isinstance(source, ArchivePlan):
        for entry in source.entries:
            yield entry
        return
    async for entry in source.entries():
        yield entry


async def stream_zip64(source: ArchiveSource) -> AsyncIterator[bytes]:
    """Yield the archive of ``source`` a chunk at a time, buffering nothing.

    Every member is opened only when its turn comes and closed before the next
    one starts, so the resident cost is one chunk plus one central-directory
    entry per member — never a member's contents, and never the plan either
    when the source is a :class:`SubtreeArchive`, which decides the next page
    of members only once the previous page has been written.

    The skipped list is read *after* the members, which is what lets a source
    that is still walking report what it left out: by the time the last member
    is written the walk is over, so the list is complete.
    """
    pipe = _Pipe()
    with zipfile.ZipFile(cast("IO[bytes]", pipe), mode="w", allowZip64=True) as archive:
        async for entry in _iter_entries(source):
            if entry.open is None:
                archive.writestr(_info(entry.path, modified=entry.modified, is_dir=True), b"")
                if pipe.pending:
                    yield pipe.drain()
                continue
            with archive.open(
                _info(entry.path, modified=entry.modified), mode="w", force_zip64=True
            ) as member:
                async for chunk in await entry.open():
                    member.write(chunk)
                    if pipe.pending >= FLUSH_BYTES:
                        yield pipe.drain()
            if pipe.pending:
                yield pipe.drain()
        skips = source.skips
        omitted = source.omitted
        if skips or omitted:
            archive.writestr(_info(SKIPPED_MEMBER), skipped_text(skips, omitted=omitted))
            if pipe.pending:
                yield pipe.drain()
    rest = pipe.drain()
    if rest:
        yield rest


_SEPARATOR = b"\x1f"


@dataclass(frozen=True, slots=True)
class ArchiveClaim:
    """What a redeemed archive token asserts. Every field is re-checked."""

    operation_id: uuid.UUID
    node_id: uuid.UUID
    org_id: uuid.UUID
    user_id: uuid.UUID
    expires_at: datetime


def _payload(claim: ArchiveClaim) -> bytes:
    return _SEPARATOR.join(
        (
            str(claim.operation_id).encode("ascii"),
            str(claim.node_id).encode("ascii"),
            str(claim.org_id).encode("ascii"),
            str(claim.user_id).encode("ascii"),
            claim.expires_at.astimezone(UTC).isoformat().encode("ascii"),
        )
    )


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def sign_claim(claim: ArchiveClaim, key: bytes) -> str:
    """``<payload>.<hmac>`` — the whole token, nothing stored server-side."""
    raw = _payload(claim)
    return f"{_b64(raw)}.{hmac.new(key, raw, 'sha256').hexdigest()}"


def verify_token(token: str, key: bytes, *, now: datetime) -> ArchiveClaim | None:
    """The claim this token carries, or ``None``.

    ``None`` covers a tampered payload, a forged signature and an expired
    deadline without distinguishing them, and the signature comparison is
    constant-time so the token itself is not an oracle.
    """
    encoded, _, signature = token.partition(".")
    if not encoded or not signature:
        return None
    try:
        raw = _unb64(encoded)
        parts = raw.split(_SEPARATOR)
        if len(parts) != 5:
            return None
        claim = ArchiveClaim(
            operation_id=uuid.UUID(parts[0].decode("ascii")),
            node_id=uuid.UUID(parts[1].decode("ascii")),
            org_id=uuid.UUID(parts[2].decode("ascii")),
            user_id=uuid.UUID(parts[3].decode("ascii")),
            expires_at=datetime.fromisoformat(parts[4].decode("ascii")),
        )
    except (ValueError, UnicodeDecodeError):
        return None
    if not hmac.compare_digest(hmac.new(key, raw, "sha256").hexdigest(), signature):
        return None
    if claim.expires_at <= now.astimezone(UTC):
        return None
    return claim


def signing_key(files: FilesContext) -> bytes:
    return files.settings.effective_files_content_signing_key.encode()


def mint_archive_token(
    files: FilesContext,
    *,
    operation_id: uuid.UUID,
    node_id: uuid.UUID,
    expires_at: datetime,
) -> str:
    """The token for this caller's download of this subtree."""
    principal = files.ctx.delegating_user or files.ctx.acting_principal
    claim = ArchiveClaim(
        operation_id=operation_id,
        node_id=node_id,
        org_id=files.repo.scope.org_team_id,
        user_id=uuid.UUID(principal.id),
        expires_at=expires_at,
    )
    return sign_claim(claim, signing_key(files))


def actor_for(claim: ArchiveClaim) -> ActingContext:
    """The principal a redemption acts as: the user the token names, whose
    access is then resolved from scratch."""
    return ActingContext.for_user(user_id=claim.user_id, org_id=claim.org_id, email="")


def _opener(
    content: ContentService, version_id: uuid.UUID
) -> Callable[[], Awaitable[AsyncIterator[bytes]]]:
    async def open_stream() -> AsyncIterator[bytes]:
        return await content.open(VersionId(version_id))

    return open_stream


@dataclass(frozen=True, slots=True)
class WalkBounds:
    """How much of a subtree a walk holds and enumerates at once."""

    page: int
    max_recorded_skips: int

    @staticmethod
    def from_settings(files: FilesContext) -> WalkBounds:
        return WalkBounds(
            page=files.settings.files_archive_page_nodes,
            max_recorded_skips=files.settings.files_archive_max_recorded_skips,
        )


class SubtreeArchive:
    """``root``'s subtree as archive members, decided a page at a time.

    Folders become directory members (so an empty folder survives the round
    trip, which is the thing browsers get wrong); files with bytes become
    members opened lazily; everything else — a symlink, an object node, a file
    whose head version is missing — is skipped by id.

    Every member is decided for EXPORT, not merely READ, because putting a
    node's bytes in a zip *is* taking them out: a chat folder carries
    ``NO_DOWNLOAD``, and zipping the folder it sits in must not be the way
    around that. A refused folder prunes its whole subtree — the pages arrive
    shallowest first, so a parent is always decided before its children.

    Two things make that affordable on a subtree of any size. The rows arrive
    in keyset pages, so what is resident is one page and not a tree. And the
    page's access is resolved for the whole page at once, through the same
    batched seam every other Files feed filters with — a constant number of
    statements per page rather than one authorization per node per action.

    The decision itself is the policy's, unchanged: each node's facts are built
    by :func:`policy_attrs` from rows already in memory and handed to the same
    ``files.access`` policy the per-node path calls, for READ and then EXPORT.
    What the page-at-a-time shape gives up is a decision *row* per member — the
    root's own EXPORT is still enforced through the auditing sink, and a
    refused member is on record in the archive and in the operation's
    ``errors[]`` rather than in the outbox.

    Only what the walk must remember across pages is kept: the path of each
    folder it has admitted, and the ids of the folders it refused. A file is a
    leaf — nothing is ever addressed relative to it — so neither set grows with
    the count of files, which is what a tree of 100,000 documents is made of.
    """

    def __init__(
        self,
        files: FilesContext,
        root: Authorized[Any],
        facts: AccessFacts,
        *,
        bounds: WalkBounds,
        decide: PolicyDecider = engine_authorize,
    ) -> None:
        self._files = files
        self._root = root
        self._facts = facts
        self._bounds = bounds
        self._decide = decide
        self._skips: list[ArchiveSkip] = []
        self._omitted = 0
        self._counted = 0
        self._bytes = 0

    @property
    def skips(self) -> Sequence[ArchiveSkip]:
        """The omissions this walk recorded, up to its ceiling."""
        return tuple(self._skips)

    @property
    def omitted(self) -> int:
        """How many further omissions there were past that ceiling."""
        return self._omitted

    @property
    def counted(self) -> int:
        """Members yielded so far."""
        return self._counted

    @property
    def total_bytes(self) -> int:
        """Σ size of the file members yielded so far."""
        return self._bytes

    def _skip(self, node_id: uuid.UUID, code: str) -> None:
        if len(self._skips) < self._bounds.max_recorded_skips:
            self._skips.append(ArchiveSkip(node_id=str(node_id), code=code))
        else:
            self._omitted += 1

    async def entries(self) -> AsyncIterator[ArchiveEntry]:
        """The archive's members, in the order they are written.

        Each page is read and decided inside a transaction of its own, which is
        then closed before the page's bytes are streamed: an archive of a
        hundred thousand files over a slow link must not be one database
        transaction held open for the length of the transfer.
        """
        repo = self._files.repo
        content = ContentService(repo, self._files.ctx, self._files.clock, self._files.store)
        root_id = self._root.node.id
        paths: dict[uuid.UUID, str] = {root_id: ""}
        pruned: set[uuid.UUID] = set()
        cursor: tuple[int, uuid.UUID] | None = None
        while True:
            async with repo.transaction():
                page = await repo.subtree_page(
                    self._root.node, limit=self._bounds.page, after=cursor
                )
                if not page:
                    return
                drive = await repo.drive(DriveId(self._root.node.drive_id))
                wanted = [NodeId(node.id) for node in page if node.id != root_id]
                decided = await access_by_id(repo, self._files.ctx, wanted, facts=self._facts)
            last = page[-1]
            cursor = (last.depth, last.id)
            for node in page:
                if node.id == root_id or drive is None:
                    continue
                entry = self._member(
                    node, drive, decided, paths=paths, pruned=pruned, content=content
                )
                if entry is None:
                    continue
                self._counted += 1
                self._bytes += entry.size
                yield entry

    def _member(
        self,
        node: FileNode,
        drive: FileDrive,
        decided: Mapping[uuid.UUID, EffectiveAccess],
        *,
        paths: dict[uuid.UUID, str],
        pruned: set[uuid.UUID],
        content: ContentService,
    ) -> ArchiveEntry | None:
        """One node's verdict, from rows the page already holds.

        ``None`` means the node produces no member — it was pruned with its
        parent, refused, or has no bytes to carry.
        """
        parent_id = node.parent_id
        if parent_id is None or parent_id in pruned:
            if node.kind == "folder":
                pruned.add(node.id)
            return None
        parent_path = paths.get(parent_id)
        if parent_path is None:
            # A parent this walk never admitted: the member would have to be
            # addressed from outside the archive's own namespace, so there is
            # no path for it to have.
            if node.kind == "folder":
                pruned.add(node.id)
            return None
        access = decided.get(node.id)
        if access is None:
            self._refuse(node, pruned, SKIP_UNREADABLE)
            return None
        attrs = {**policy_attrs(access, node, drive, self._files.ctx, self._facts), EXISTS: True}
        resource = Resource(
            type=ResourceType.FILE_NODE,
            id=str(node.id),
            org_id=node.org_team_id,
            team_id=drive.org_team_id,
        )
        for action, code in (
            (FilesAction.READ, SKIP_UNREADABLE),
            (FilesAction.EXPORT, SKIP_NO_EXPORT),
        ):
            if not self._decide(self._files.ctx, platform_action(action), resource, attrs).allowed:
                self._refuse(node, pruned, code)
                return None
        name = node.name.decode("utf-8", "replace")
        path = f"{parent_path}/{name}" if parent_path else name
        if node.kind == "folder":
            paths[node.id] = path
            return ArchiveEntry(path=path)
        if node.kind != "file" or node.head_version_id is None:
            self._skip(node.id, SKIP_NOT_BYTES)
            return None
        return ArchiveEntry(path=path, size=node.size, open=_opener(content, node.head_version_id))

    def _refuse(self, node: FileNode, pruned: set[uuid.UUID], code: str) -> None:
        if node.kind == "folder":
            pruned.add(node.id)
        self._skip(node.id, code)


#: What ``stream_zip64`` writes: members held, or members produced.
ArchiveSource = ArchivePlan | SubtreeArchive


def subtree_archive(
    files: FilesContext,
    root: Authorized[Any],
    facts: AccessFacts,
    *,
    bounds: WalkBounds | None = None,
) -> SubtreeArchive:
    """The archive of ``root``'s subtree, bounded by the deployment's settings."""
    return SubtreeArchive(
        files, root, facts, bounds=bounds if bounds is not None else WalkBounds.from_settings(files)
    )


__all__ = [
    "FLUSH_BYTES",
    "SKIPPED_MEMBER",
    "SKIP_NOT_BYTES",
    "SKIP_NO_EXPORT",
    "SKIP_UNREADABLE",
    "ArchiveClaim",
    "ArchiveEntry",
    "ArchivePlan",
    "ArchiveSkip",
    "ArchiveSource",
    "ByteStream",
    "SubtreeArchive",
    "WalkBounds",
    "actor_for",
    "mint_archive_token",
    "sign_claim",
    "signing_key",
    "skipped_text",
    "stream_zip64",
    "subtree_archive",
    "verify_token",
]
