"""Put a chat's attachments on the box's disk before the turn reads them.

A chat attachment is a Files NODE referenced from the chat, so the bytes are
never in the transcript: an incoming user message carries one ``file`` part per
attachment naming the node, and the box has to fetch it before the harness can
open it. This module is that step, and it is written around four properties:

* **it is contained.** Every file lands under
  ``<workspace>/.alkera/attachments/<chat_id>/`` through
  :class:`~alkera_cli.files.target.MaterializationTarget`, which refuses a name
  lexically and then proves the path lands beneath the root before any I/O — a
  transcript cannot name ``../../id_rsa`` and have the box write there. The
  name itself goes through the Files names contract
  (:mod:`alkera_core.files.names`), so what a chat calls a file is what a
  filesystem would accept, and two attachments that want the same name get the
  same ``file (1).csv`` rename the namespace gives siblings. The
  ``.alkera-part`` sidecar the bytes land in first is contained the same
  way and created exclusively, so a link planted at that name cannot carry
  the write out of the directory;
* **it is verified.** A part that states a size or a content digest is checked
  against the bytes that arrived; a mismatch is a failure, not a file on disk
  the agent then reads as if it were the attachment;
* **it never drops silently.** A node the box cannot fetch — refused, missing,
  a truncated body — becomes a NOTICE the caller puts in the chat, naming the
  file. The turn still runs with the attachments that did arrive, because a
  reader who attached three files and asked a question about two of them is
  better served by an answer plus a visible gap than by silence;
* **it is stated to the agent.** The prompt handed to the harness is prefixed
  with the block :data:`PROMPT_HEADER` and one absolute path per file, so the
  agent reads them with its ordinary read tool instead of being told about
  bytes it has no way to reach. A file that did NOT arrive is stated there too,
  under :data:`FAILED_HEADER`: the chat's own text links every file the reader
  attached, so an agent told only about the ones that landed goes looking for
  the rest, finds nothing, and reports the chat as broken. Naming the gap is
  what lets it answer on what it has.

The fetch itself is a seam (:class:`ContentFetcher`) so the whole thing is
driven in a test by a fake API and a fake content origin;
:class:`RestContentFetcher` is the real one, minting through the Files content
route on the backend and following the signed URL to the content origin exactly
as ``alkera files pull`` does.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import stat
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol

import httpx
from alkera_core.files import names
from alkera_core.files.conflicts import conflict_rename

from alkera_cli.files.live_sync import byte_deadline
from alkera_cli.files.target import ContainmentError, MaterializationTarget

__all__ = [
    "ATTACHMENTS_SUBDIR",
    "FAILED_HEADER",
    "PROMPT_HEADER",
    "Attachment",
    "AttachmentFetchError",
    "ContentFetcher",
    "LinkedFile",
    "MaterializedAttachment",
    "MaterializedAttachments",
    "RestContentFetcher",
    "attachments_in",
    "linked_files_in",
    "materialize_attachments",
    "prompt_with_attachments",
]

logger = logging.getLogger(__name__)

#: Where a chat's fetched attachments live, under the project directory.
ATTACHMENTS_SUBDIR: Final = "attachments"

#: The line that opens the block naming the files on disk. The live spec reads
#: it, so it is spelled once, here.
PROMPT_HEADER: Final = "Attached files (on disk):"

#: The line that opens the block naming the files that never reached the disk.
#: It is the same fact the reader is shown as a notice, said to the agent too,
#: so a question whose text links a file the box could not fetch is answered
#: with the gap named instead of being hunted for across the filesystem.
FAILED_HEADER: Final = "Attached files that did NOT arrive (do not look for them on disk):"

#: What a file with no usable name is called. A node whose name the transcript
#: lost still has bytes worth reading, and a name derived from the node id is
#: stable across a re-run.
_FALLBACK_STEM: Final = "attachment"

#: How much of a download is held in memory at a time — the pull's chunk.
_STREAM_CHUNK: Final = 1 << 20

#: The sidecar bytes land in before they are proven and promoted. The same
#: suffix the Files pull uses, imported from the one place that spells it so
#: the name ceiling reserving room for it can never be reserving the wrong room.
_PART_SUFFIX: Final = names.PULL_PART_SUFFIX

#: How the sidecar is created: exclusively, and never through a symlink, so a
#: name already standing there is a refusal rather than a write somewhere else.
#: ``O_NOFOLLOW`` is POSIX-only; Windows has no symlink to follow by default.
_PART_FLAGS: Final[int] = (
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
)

#: The redirect statuses the content route answers a mint with.
_REDIRECTS: Final = frozenset({301, 302, 303, 307, 308})


class AttachmentFetchError(Exception):
    """One attachment could not be put on disk. The message is written for a
    reader of the chat, not for a log: it names the file and what went wrong."""


@dataclass(frozen=True, slots=True)
class Attachment:
    """One ``file`` part of an incoming user message that names a Files node."""

    node_id: str
    filename: str = ""
    size: int | None = None
    sha256: str = ""
    mime: str = ""

    @property
    def display_name(self) -> str:
        """What the chat calls this file — never empty, never a path."""
        stem = self.filename.strip().replace("\\", "/").rsplit("/", 1)[-1]
        if stem and stem not in (".", ".."):
            return stem
        return f"{_FALLBACK_STEM}-{self.node_id}"


@dataclass(frozen=True, slots=True)
class MaterializedAttachment:
    """One attachment, on disk, verified."""

    attachment: Attachment
    path: Path
    size: int


@dataclass(frozen=True, slots=True)
class MaterializedAttachments:
    """What the turn gets: the files that landed, and what to say about the
    ones that did not."""

    files: tuple[MaterializedAttachment, ...] = ()
    notices: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.files or self.notices)


class ContentFetcher(Protocol):
    """The bytes behind one Files node, streamed in order.

    ``fetch`` hands out the chunks rather than the bytes: an attachment is a
    Files node of whatever size the drive holds, and a box that built the whole
    body in memory before writing any of it would be killed by one reader
    attaching one large file — taking every other chat it serves with it. It is
    a context manager so the connection the chunks come off closes when the
    writer is done with it, whether it finished or failed partway.
    """

    def fetch(
        self, node_id: str
    ) -> AbstractAsyncContextManager[AsyncIterator[bytes]]:  # pragma: no cover - protocol
        ...


def _part_field(part: Mapping[str, Any], *keys: str) -> Any:
    """The first of ``keys`` the part carries, snake or camel.

    ``node_id`` is a declared ``FilePart`` field as of 1.1.0, so the snake
    spelling is the typed one and is read first. The camel spelling stays as a
    fallback for a 1.0.0 writer, which had no such field and carried it on the
    part's EXTRA fields (``VersionedModel`` keeps what it does not know) —
    dropping it would make the box silently ignore that writer's attachments.
    """
    for key in keys:
        if key in part:
            value = part[key]
            if value is not None:
                return value
    return None


def attachments_in(message: Mapping[str, Any]) -> list[Attachment]:
    """Every ``file`` part of ``message`` that names a Files node, in order.

    ``message`` is read at either spelling: a transcript message carries its
    file parts under ``parts``, while a relay and a recorded prompt carry the
    same parts under ``attachments`` — the box takes both so a live question
    and the one it catches up on go down one path.

    A part with no node id is not an attachment this box can fetch (it is a
    blob-store reference from a local chat), so it is skipped rather than
    reported: the box has nothing to say about a file it was never asked for.
    """
    parts = message.get("parts")
    if parts is None:
        parts = message.get("attachments")
    if not isinstance(parts, Sequence) or isinstance(parts, (str, bytes)):
        return []
    found: list[Attachment] = []
    seen: set[str] = set()
    for part in parts:
        if not isinstance(part, Mapping) or part.get("type") != "file":
            continue
        node_id = _part_field(part, "node_id", "nodeId")
        if not isinstance(node_id, str) or not node_id.strip():
            continue
        node_id = node_id.strip()
        if node_id in seen:
            # The same node attached twice is one file on disk: a second copy
            # would only earn a conflict rename and confuse the agent about
            # which of two identical paths the reader meant.
            continue
        seen.add(node_id)
        raw_size = _part_field(part, "size")
        size = int(raw_size) if isinstance(raw_size, int) and raw_size >= 0 else None
        digest = _part_field(part, "sha256")
        mime = _part_field(part, "mime")
        filename = _part_field(part, "filename", "name")
        found.append(
            Attachment(
                node_id=node_id,
                filename=filename if isinstance(filename, str) else "",
                size=size,
                sha256=digest.strip().lower() if isinstance(digest, str) else "",
                mime=mime if isinstance(mime, str) else "",
            )
        )
    return found


#: What the composer's own uploads are stored under: ``uploads/file-1-ab12.csv``,
#: ``uploads/paste-2-9f3c.png``. The composer uploads a paste or a pick straight INTO the
#: chat's working folder and writes a chat-relative link in the message, so those
#: files are never ``file`` parts and are not fetched by this module at all —
#: they arrive on the live plane. The shape is matched narrowly on purpose: an
#: ordinary markdown link a person typed must never be mistaken for an
#: attachment the box then reports as missing. A composer puts what it stages
#: under ``uploads/``; a chat from before that has the same names at the top
#: level, and both still resolve.
_STAGED_LINK: Final = re.compile(
    r"!?\[(?P<label>[^\]\n]*)\]"
    r"\((?P<path>(?:uploads/)?(?:paste|file)-\d+-[A-Za-z0-9]+(?:\.[A-Za-z0-9]+)?)\)"
)


@dataclass(frozen=True, slots=True)
class LinkedFile:
    """One file the message's own text links, relative to the chat's folder."""

    #: The path as written, relative to the chat's working directory.
    path: str
    #: What the reader sees — ``File 1: apollo_leads_import.csv`` — trimmed to
    #: the name where the composer put one after the colon.
    label: str = ""

    @property
    def display_name(self) -> str:
        """The file's own name where the label carries one, else the path."""
        _, _, after = self.label.partition(":")
        named = after.strip()
        return named or self.path


def linked_files_in(message: Mapping[str, Any]) -> list[LinkedFile]:
    """Every composer-staged file the message's text links, in order.

    These are NOT attachments this module fetches: the composer already put
    them in the chat's folder on the drive, and the box takes them down on the
    live plane. What the caller needs them for is the opposite check — a link
    the reader can see with no bytes behind it on this machine, which is what
    made an agent report a chat that "renders a link, but no bytes ever land".
    """
    text = message.get("text")
    if not isinstance(text, str) or not text:
        return []
    found: list[LinkedFile] = []
    seen: set[str] = set()
    for match in _STAGED_LINK.finditer(text):
        path = match.group("path")
        if path in seen:
            continue
        seen.add(path)
        found.append(LinkedFile(path=path, label=match.group("label").strip()))
    return found


def _safe_name(attachment: Attachment, taken: set[bytes]) -> bytes:
    """A byte-safe, unique name for this attachment inside the chat's dir.

    The Files names contract decides what a name may be; a name it refuses is
    replaced wholesale rather than repaired, because a repaired name is a name
    the reader never chose and cannot recognise.
    """
    candidate = os.fsencode(attachment.display_name)
    try:
        names.validate(candidate)
    except names.InvalidName:
        candidate = os.fsencode(f"{_FALLBACK_STEM}-{attachment.node_id}")
        names.validate(candidate)
    chosen = conflict_rename(candidate, taken.__contains__)
    taken.add(chosen)
    return chosen


def _verify(attachment: Attachment, *, size: int, digest: str) -> None:
    if attachment.size is not None and attachment.size != size:
        raise AttachmentFetchError(f"the file is {size} bytes but the chat says {attachment.size}")
    if attachment.sha256 and attachment.sha256 != digest:
        raise AttachmentFetchError("the bytes that arrived do not match the file's digest")


async def materialize_attachments(
    attachments: Sequence[Attachment],
    *,
    project_path: Path,
    chat_id: str,
    fetcher: ContentFetcher,
) -> MaterializedAttachments:
    """Put every attachment under ``<project>/attachments/<chat_id>/``.

    One failure never costs another file: each is fetched, verified and
    promoted on its own, and a failure becomes a notice naming the file.
    """
    if not attachments:
        return MaterializedAttachments()
    root = project_path / ATTACHMENTS_SUBDIR / _chat_dirname(chat_id)
    root.mkdir(parents=True, exist_ok=True)
    landed: list[MaterializedAttachment] = []
    notices: list[str] = []
    taken: set[bytes] = {os.fsencode(entry.name) for entry in root.iterdir()}
    with MaterializationTarget(root) as target:
        for attachment in attachments:
            try:
                landed.append(
                    await _materialize_one(attachment, target=target, taken=taken, fetcher=fetcher)
                )
            except (AttachmentFetchError, ContainmentError, names.InvalidName) as refused:
                notices.append(f"{attachment.display_name}: {refused}")
                logger.warning(
                    "chat %s: attachment %s not fetched: %s",
                    chat_id,
                    attachment.node_id,
                    refused,
                    extra={"chat_id": chat_id, "node_id": attachment.node_id},
                )
            except Exception as failed:
                notices.append(f"{attachment.display_name}: could not be fetched ({failed})")
                logger.exception("chat %s: attachment %s failed", chat_id, attachment.node_id)
    return MaterializedAttachments(files=tuple(landed), notices=tuple(notices))


def _chat_dirname(chat_id: str) -> str:
    """The chat's own directory name, proven to be one path segment.

    The chat id comes off the wire, so it is refused by the same names contract
    the files themselves go through rather than pasted into a path.
    """
    encoded = os.fsencode(chat_id)
    names.validate(encoded)
    return chat_id


async def _materialize_one(
    attachment: Attachment,
    *,
    target: MaterializationTarget,
    taken: set[bytes],
    fetcher: ContentFetcher,
) -> MaterializedAttachment:
    relative = _safe_name(attachment, taken)
    # Resolved through the target BEFORE any byte is written: the path is
    # proven to be beneath the chat's directory, and a symlink standing where
    # the file goes is refused rather than followed.
    where = target.resolve_leaf(relative)
    # The sidecar is a path in its own right, and its name is derived from one
    # the transcript chose — so it goes through the same containment as the
    # file. Concatenating it onto the resolved path instead would let a
    # `<name>.alkera-part` symlink planted in the directory carry the write
    # outside it, and then carry the read when the promoted path is handed to
    # the harness.
    part = target.resolve_leaf(_sidecar_name(relative))
    # The bytes land in a sidecar and are promoted only once they verify, so a
    # truncated body is never visible under the real name — and each chunk goes
    # to disk as it arrives, off the loop, because a box that held a file whole
    # would be killed by its size and a box that wrote on the loop would stall
    # every other chat it is serving.
    async with fetcher.fetch(attachment.node_id) as chunks:
        size, digest = await _write_part(part, chunks)
    try:
        _verify(attachment, size=size, digest=digest)
        await asyncio.to_thread(_refuse_symlink_leaf, where)
        await asyncio.to_thread(os.replace, part, where)
    except BaseException:
        await asyncio.to_thread(part.unlink, True)
        raise
    return MaterializedAttachment(attachment=attachment, path=where, size=size)


def _sidecar_name(relative: bytes) -> bytes:
    """The sidecar's own name — itself a name a filesystem would accept.

    The naming contract's ceiling already holds back exactly this suffix's
    worth of room, so a name it accepted leaves the sidecar fitting. The trim
    is what answers for a name that came from somewhere else — a legacy row
    stored before the ceiling was lowered. The sidecar is never read by name
    and lives only between the write and the promotion, so a trimmed stem
    costs nothing; a collision between two trimmed stems is refused by the
    exclusive create rather than silently shared.
    """
    stem = relative[: names.FS_NAME_MAX_BYTES - len(_PART_SUFFIX)]
    return stem + _PART_SUFFIX


def _refuse_symlink_leaf(where: Path) -> None:
    """Refuse to promote onto a symlink standing where the file goes.

    ``os.replace`` renames onto the link rather than through it, so no byte
    leaves the directory — but a link that appeared while the bytes were in
    flight is a sign the name is contested, and promoting over it would hand
    the harness a path someone else chose the meaning of.
    """
    try:
        info = os.lstat(where)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode):
        raise ContainmentError(f"{where.name!r} is a symlink, not this attachment")


async def _write_part(part: Path, chunks: AsyncIterator[bytes]) -> tuple[int, str]:
    """Write the sidecar as the chunks arrive; return what landed, so the
    caller can verify it.

    Nothing is held: a chunk is written and hashed and then it is gone, so the
    memory this costs is one chunk whether the node is a note or a terabyte.
    The write and the hash go to a thread together — both are CPU or disk work
    that would otherwise stall every other chat the box is serving.

    The sidecar is created exclusively and without following a link: a name
    already standing there — a symlink planted so the bytes land outside the
    chat's directory, or a leftover another writer still owns — is refused,
    never written through.
    """
    hasher = hashlib.sha256()
    size = 0
    try:
        descriptor = await asyncio.to_thread(os.open, part, _PART_FLAGS, 0o600)
    except OSError as refused:
        raise AttachmentFetchError(
            f"the file could not be staged on disk ({refused.strerror})"
        ) from refused
    handle = os.fdopen(descriptor, "wb")

    def absorb(chunk: bytes) -> None:
        nonlocal size
        handle.write(chunk)
        hasher.update(chunk)
        size += len(chunk)

    try:
        async for chunk in chunks:
            await asyncio.to_thread(absorb, chunk)
    except BaseException:
        await asyncio.to_thread(handle.close)
        await asyncio.to_thread(part.unlink, True)
        raise
    await asyncio.to_thread(handle.close)
    return size, hasher.hexdigest()


def prompt_with_attachments(text: str, materialized: MaterializedAttachments) -> str:
    """The prompt the harness is handed: the files, then the question.

    The block goes FIRST because it is context the question is asked against,
    and each line carries the absolute path and the size, so an agent decides
    whether to read a 20 MB file whole before it opens it.

    The files that did NOT arrive are named right under it. Leaving them out is
    what made a failed fetch indistinguishable from a broken box: the reader's
    own words still link every file they attached, so an agent handed only the
    successes searches the filesystem for the rest and reports that the chat
    renders links to bytes that are nowhere — instead of answering on what it
    has and saying which file is missing.
    """
    blocks: list[str] = []
    if materialized.files:
        lines = [PROMPT_HEADER]
        lines += [f"- {file.path} ({file.size} bytes)" for file in materialized.files]
        blocks.append("\n".join(lines))
    if materialized.notices:
        lines = [FAILED_HEADER]
        lines += [f"- {notice}" for notice in materialized.notices]
        blocks.append("\n".join(lines))
    if not blocks:
        return text
    return "\n\n".join(blocks) + "\n\n" + text


class RestContentFetcher:
    """The real fetcher: mint on the backend, then read the content origin.

    The mint is the authorization — the backend decides whether this caller may
    read the node and answers with a short-lived signed URL — so the box holds
    no content credential of its own. The redirect is followed by hand (rather
    than with ``follow_redirects``) so the machine's ``Authorization`` header is
    never offered to the content origin.
    """

    def __init__(
        self,
        *,
        api_url: str,
        headers: Mapping[str, str],
        drive_id: str,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._api_url = api_url.rstrip("/")
        self._headers = dict(headers)
        self._drive_id = drive_id
        self._transport = transport
        #: What a request that is NOT moving bytes is given — the mint, the
        #: connect, the wait for a pool slot. The body gets its own budget,
        #: sized by how many bytes the store says are coming.
        self._timeout = timeout

    @asynccontextmanager
    async def fetch(self, node_id: str) -> AsyncIterator[AsyncIterator[bytes]]:
        url = f"{self._api_url}/api/v1/files/drives/{self._drive_id}/items/{node_id}/content"
        # No per-read cap: a body is cut off by the size-derived deadline below
        # and by nothing else, so a terabyte over a slow link is a long read
        # rather than a failed one.
        moving = httpx.Timeout(self._timeout, read=None, write=None, pool=self._timeout)
        async with httpx.AsyncClient(
            timeout=self._timeout, transport=self._transport, headers=self._headers
        ) as client:
            minted = await client.get(url, follow_redirects=False)
            if minted.status_code not in _REDIRECTS:
                raise AttachmentFetchError(
                    f"the file could not be read (the server answered {minted.status_code})"
                )
            location = minted.headers.get("location")
            if not location:
                raise AttachmentFetchError("the file's download link was not offered")
            # The signed URL is its own credential; the machine's token must not
            # ride along to whatever origin the mint pointed at.
            async with client.stream(
                "GET", location, headers={"Authorization": ""}, timeout=moving
            ) as downloaded:
                if downloaded.status_code >= 400:
                    raise AttachmentFetchError(
                        f"the file's bytes could not be read "
                        f"(the store answered {downloaded.status_code})"
                    )
                declared = _declared_length(downloaded)
                deadline = byte_deadline(declared, floor=self._timeout)
                yield self._body(downloaded, declared=declared, deadline=deadline)

    async def _body(
        self, downloaded: httpx.Response, *, declared: int | None, deadline: float
    ) -> AsyncIterator[bytes]:
        """The body, a chunk at a time, under one deadline for the whole of it.

        The deadline is absolute — it bounds the whole transfer, not each read
        — but it is armed around each read rather than around the loop: the
        writer these chunks are handed to spends its own time on disk between
        them, and a cancellation delivered while it sat in a thread would
        surface as a bare cancellation instead of the stall this names.
        """
        expires = asyncio.get_running_loop().time() + deadline
        body = downloaded.aiter_bytes(_STREAM_CHUNK).__aiter__()
        served = False
        while True:
            try:
                async with asyncio.timeout_at(expires):
                    chunk = await anext(body)
            except StopAsyncIteration:
                break
            except TimeoutError:
                raise AttachmentFetchError(
                    f"the file's {_said(declared)} stopped arriving "
                    f"({downloaded.num_bytes_downloaded} bytes did, "
                    f"nothing more within {deadline:.0f}s)"
                ) from None
            served = True
            yield chunk
        if not served:
            # An empty body is a file of no bytes, not a file that never came.
            yield b""


def _declared_length(response: httpx.Response) -> int | None:
    """How many bytes the store says are coming, or ``None`` if it did not say."""
    raw = response.headers.get("content-length")
    try:
        declared = int(raw) if raw is not None else None
    except ValueError:
        return None
    return declared if declared is not None and declared >= 0 else None


def _said(size: int | None) -> str:
    return f"{size} bytes" if size is not None else "bytes"
