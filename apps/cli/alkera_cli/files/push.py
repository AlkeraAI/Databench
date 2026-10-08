"""``alkera files push`` — walk a local directory into an org path.

The push is the export half of the round-trip corpus, so it is written around
three properties rather than around convenience:

* **it uploads nothing it does not have to.** Before a single byte moves, the
  file's BLAKE3 and size are compared against the server's head version for
  that path; a match is a skip. A second push of an unchanged tree therefore
  opens no upload session and sends no part — that is the acceptance
  criterion, and it is why the hash is computed locally rather than inferred
  from mtime, which a checkout or a ``touch`` resets;
* **it resumes.** Every open session id is persisted under
  ``ALKERA_HOME/files/pushes/<digest>.json`` keyed by the file's relative path
  and content hash. A push killed mid-part re-opens nothing: it asks the
  server which parts landed (``acceptedParts``) and sends only the rest;
* **it never invents a version.** A pointer file (``*.alkera<kind>``) is
  derived state that ``pull`` writes, so pushing one back would turn a
  rendered artefact into authoritative bytes. Pointers are skipped with a
  warning whether or not they were modified.

Everything the walker classified as skipped (sidecars, gitignored paths, an
excluded preset directory) is reported, never silently dropped.
"""

from __future__ import annotations

import contextlib
import errno
import os
import re
import stat as stat_module
import uuid
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import IO, Any, Final, Protocol

import httpx
from alkera_core.files.links import LinkKind, stored_link_stays_inside
from alkera_core.files.providers.registry import POINTER_EXTENSIONS

from alkera_cli.files import hashing
from alkera_cli.files.agreed_base import AgreedBase
from alkera_cli.files.name_rules import split_unfileable
from alkera_cli.files.progress import Progress
from alkera_cli.files.push_state import load_state, push_state_path, save_state
from alkera_cli.files.refusals import dead_session as _dead_session
from alkera_cli.files.refusals import files_own_refusal as _files_own_refusal
from alkera_cli.files.refusals import no_room as _no_room
from alkera_cli.files.refusals import refusal_text as _refusal_text
from alkera_cli.files.refusals import stale_version as _stale_version
from alkera_cli.files.refusals import status_of as _status_of
from alkera_cli.files.walk import Entry, EntryKind, SkipReason, walk
from alkera_cli.files.wire import conflict_code, facet_content_hash, item_etag

__all__ = [
    "POINTER_SUFFIXES",
    "SINGLE_PUT_MAX_BYTES",
    "AgreedBase",
    "CheckpointKilled",
    "PushSummary",
    "SourceRefusedError",
    "SourceUnreadableError",
    "TransferTimeoutError",
    "UploadSessionError",
    "conflict_code",
    "push",
    "push_paths",
    "push_state_path",
]

#: A pointer file is ``<name>.alkera<kind>``. The set is read off the one
#: extension registry the server names nodes with rather than sniffed from a
#: substring, so a new object type is skipped here the day it is registered and
#: an ordinary file that merely *contains* ``.alkera`` (a ``.alkera-part``
#: resume sidecar, say) is never mistaken for derived state.
POINTER_SUFFIXES: Final[tuple[bytes, ...]] = tuple(
    extension.encode("ascii") for extension in POINTER_EXTENSIONS.values()
)


#: The server's ``files_single_put_max_bytes`` default. A content PUT larger
#: than this is refused before a byte lands, so a push routes anything over it
#: through the upload session instead.
#:
#: TODO: read the deployment's real cap rather than this constant. ``POST
#: /uploads`` now publishes ``limits.singlePutMaxBytes`` (with ``maxFileBytes``,
#: ``maxPartBytes`` and ``maxParts``), but a push must choose *between* the
#: single PUT and a session before it has opened one, so the number it needs is
#: not on any response it has seen yet. Publishing the same block on the drive
#: capabilities is what closes this; until then a self-hosted install with a
#: lower cap meets a hard error where it should have taken the session path.
SINGLE_PUT_MAX_BYTES: Final = 67_108_864


class CheckpointKilled(RuntimeError):  # noqa: N818 - the name the resume contract uses
    """Raised by a caller's checkpoint hook to stand in for a killed process.

    It lives here rather than in the test so the resume path has one named
    interruption to document: a push that dies at *any* checkpoint must leave
    behind a state file able to finish the job, and the test proves it by
    raising this from the hook the push calls after every durable step.
    """


class SourceUnreadableError(RuntimeError):
    """A file on this machine the push could not read, named.

    The platform's own error carries an errno and nothing else, and a push
    walks thousands of entries — so the refusal is re-raised here with the path
    in it. It is deliberately not an ``OSError``: the transfer retries a write
    whose version moved and nothing else, and a local read that failed is never
    worth a second attempt.
    """


class UploadSessionError(RuntimeError):
    """An upload session whose own terms cannot move the file's bytes.

    A part size the server did not give (or a resumed session whose recorded
    one is gone) would make the part loop send nothing and then commit an empty
    version over real content, and a source that no longer holds the bytes the
    session was opened for would commit a truncated one. Both stop here, with
    the file and the numbers, rather than at the far end as a silent version.
    """


class FilesApi(Protocol):
    """The slice of the SDK's ``client.files`` namespace a push drives."""

    def drive(self) -> dict[str, Any]: ...

    def item(self, drive_id: str, item_id: str, *, select: str | None = ...) -> dict[str, Any]: ...

    def item_by_path(self, drive_id: str, item_path: str) -> dict[str, Any]: ...

    def item_under(self, drive_id: str, item_id: str, item_path: str) -> dict[str, Any]: ...

    def open_upload(
        self,
        *,
        parent_id: str,
        name: str,
        declared_size: int,
        mime: str | None = None,
    ) -> dict[str, Any]: ...

    def put_part(
        self, session_id: str, part_no: int, data: bytes, *, checksum: str
    ) -> dict[str, Any]: ...

    def complete_upload(
        self,
        session_id: str,
        parts: Sequence[Mapping[str, Any]],
        *,
        conflict_behavior: str = "fail",
    ) -> dict[str, Any]: ...

    def upload_status(self, session_id: str) -> dict[str, Any]: ...

    def await_operation(
        self,
        drive_id: str,
        operation_id: str,
        *,
        timeout: float = ...,
        interval: float = ...,
    ) -> dict[str, Any]: ...


#: How long a push waits for one upload's commit to land before it stops
#: waiting on commits for the rest of the push.
COMMIT_WAIT_SECONDS = 5.0


@dataclass
class PushSummary:
    """What one push did, in the terms the CLI prints and a test asserts."""

    folders: int = 0
    uploaded: int = 0
    unchanged: int = 0
    """Files whose bytes the server already held at that path — no upload."""
    symlinks: int = 0
    specials: int = 0
    pointers_skipped: int = 0
    excluded: int = 0
    bytes_uploaded: int = 0
    parts_sent: int = 0
    warnings: list[str] = field(default_factory=list)
    #: The etag each path's node was read at holding exactly the bytes sent; missing
    #: when unconfirmed (the commit had not landed, or another write moved it on).
    agreed: dict[str, str] = field(default_factory=dict)
    #: Root-relative paths the drive refused for a reason of their own — a
    #: commit it would not take, a name it would not file — so the rest of the
    #: tree landed without them. Each is also said in ``warnings``; this is the
    #: list a release names as what stayed behind.
    failed: list[str] = field(default_factory=list)
    #: Uploads whose commit was not seen to land: the first one that outlasted
    #: its wait, and every one after it, which the push stopped waiting on.
    commits_unconfirmed: int = 0


#: The checksum each upload part carries; the live sync imports it by this name.
_blake3_hex = hashing.content_hash


def _extended_length(source: Path) -> Path:
    """``source`` spelled so Windows' own file APIs will accept it.

    The Win32 path parser refuses anything past 260 characters unless the name
    carries the extended-length prefix, and a tree deeper than that is ordinary
    (a nested ``node_modules``, a corpus of long segments). Prefixing an
    absolute path with ``\\\\?\\`` — ``\\\\?\\UNC\\`` for a share — turns the
    refusal into a working open. The prefix disables relative-path and forward
    slash normalization, so the name is resolved first and separators are
    rewritten; everywhere but Windows this is the identity.
    """
    if os.name != "nt":
        return source
    text = str(source)
    if text.startswith("\\\\?\\"):
        return source
    absolute = str(Path(os.path.abspath(text))).replace("/", "\\")
    if absolute.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + absolute.lstrip("\\"))
    return Path("\\\\?\\" + absolute)


class SourceRefusedError(SourceUnreadableError):
    """A path under the push root that is not the regular file the walk saw.

    The folder a push reads is often written by somebody else at the same
    time — a chat's agent owns its folder while a root daemon pushes it — so
    a name the walk classified as a file can be a link, a directory or a fifo
    by the time it is opened. Following it would read whatever the link names,
    with the pusher's privileges, into the pusher's destination. Such a path
    is refused rather than read, and the push passes it by with a warning.
    """


#: ``(st_dev, st_ino)``: what makes two opens the same file.
_Identity = tuple[int, int]

_O_CLOEXEC: Final = getattr(os, "O_CLOEXEC", 0)
_O_NOFOLLOW: Final = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY: Final = getattr(os, "O_DIRECTORY", 0)
_O_BINARY: Final = getattr(os, "O_BINARY", 0)

#: Whether this platform can open a path one component at a time from a
#: directory descriptor without following a link at any step. Every POSIX the
#: push runs on can; Windows has neither ``dir_fd`` nor ``O_NOFOLLOW``.
_ANCHORED_OPEN: Final = bool(_O_NOFOLLOW and _O_DIRECTORY) and os.open in os.supports_dir_fd

#: ``ELOOP`` is what a link opened with ``O_NOFOLLOW`` answers (``EMLINK`` on
#: the BSDs); ``ENOTDIR`` is a component that stopped being a directory.
_REDIRECTED_ERRNOS: Final = frozenset(
    code for code in (errno.ELOOP, errno.ENOTDIR, getattr(errno, "EMLINK", None)) if code
)

_FILE_ATTRIBUTE_REPARSE_POINT: Final = 0x400


def _identity(status: os.stat_result) -> _Identity:
    return (status.st_dev, status.st_ino)


@dataclass(frozen=True)
class _Source:
    """One file under the push root, addressed so no link can redirect it.

    ``root`` is followed as the caller named it and pinned to the directory
    the push started from (``root_identity``); everything below it is opened
    one component at a time relative to its parent's descriptor, never
    following a link. ``identity`` is the inode the hash read, so the upload
    can prove it sends the bytes that were hashed and not a file swapped in
    under the same name since.
    """

    root: Path
    relative: bytes
    root_identity: _Identity | None = None
    identity: _Identity | None = None

    @property
    def path(self) -> Path:
        return self.root / os.fsdecode(self.relative)

    def __str__(self) -> str:
        return str(self.path)


def _refused(source: _Source, why: str) -> SourceRefusedError:
    return SourceRefusedError(f"{source}: the push did not read this path: {why}")


def _open_anchored(source: _Source) -> int:
    """Open ``source`` from the root down, following no link below the root."""
    components = source.relative.split(b"/")
    if any(component in (b"", b".", b"..") for component in components):
        raise _refused(source, "the name is not a plain relative path")
    current = os.open(source.root, os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC)
    try:
        if source.root_identity is not None and (
            _identity(os.fstat(current)) != source.root_identity
        ):
            raise _refused(source, "the push root was replaced since the push began")
        for component in components[:-1]:
            below = os.open(
                component, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC, dir_fd=current
            )
            os.close(current)
            current = below
        # O_NONBLOCK: a fifo swapped in for the file must not park the push on
        # an open that waits for a writer. It changes nothing on a regular file.
        return os.open(
            components[-1],
            os.O_RDONLY
            | _O_NOFOLLOW
            | _O_CLOEXEC
            | getattr(os, "O_NOCTTY", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=current,
        )
    finally:
        os.close(current)


def _open_unanchored(source: _Source) -> int:
    """The Windows open: refuse any link or reparse point below the root.

    Windows offers no descriptor-relative open, so each component is checked
    with ``lstat`` and the file then opened by name. That closes a link that
    was already there, not one swapped in between the check and the open;
    the inode comparison after the open narrows what such a swap can do.
    """
    here = source.root
    for component in source.relative.split(b"/"):
        here = here / os.fsdecode(component)
        status = os.lstat(_extended_length(here))
        attributes = getattr(status, "st_file_attributes", 0)
        if stat_module.S_ISLNK(status.st_mode) or attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise _refused(source, f"{here} is a link or a reparse point")
    return os.open(_extended_length(source.path), os.O_RDONLY | _O_BINARY | _O_CLOEXEC)


@contextlib.contextmanager
def _open_source(source: Path | _Source) -> Iterator[IO[bytes]]:
    """Open a file the push is about to send, or say which one it could not.

    Every byte a push moves is read through here for two reasons: the name is
    handed to the platform in the spelling it accepts, and an ``OSError`` on
    the way in is re-raised as a failure that names the file. A bare
    ``FileNotFoundError`` surfacing from inside a transfer says nothing about
    which of ten thousand entries the push gave up on, and — because the caller
    retries a write whose version moved — an error with no identity is the
    hardest kind to act on.

    A :class:`_Source` is opened without following a link below the push root,
    must be a regular file once open, and — when it carries the identity its
    hash read — must still be that inode; anything else is a
    :class:`SourceRefusedError`. A bare ``Path`` is opened as named.
    """
    try:
        if isinstance(source, _Source):
            fd = _open_anchored(source) if _ANCHORED_OPEN else _open_unanchored(source)
            handle: IO[bytes] = os.fdopen(fd, "rb")
        else:
            handle = _extended_length(source).open("rb")
    except SourceUnreadableError:
        raise
    except OSError as unreadable:
        if isinstance(source, _Source) and unreadable.errno in _REDIRECTED_ERRNOS:
            raise _refused(
                source, f"a component is a link or no longer a directory ({unreadable})"
            ) from unreadable
        raise SourceUnreadableError(
            f"{source}: the push could not read this file "
            f"({type(unreadable).__name__}: {unreadable})"
        ) from unreadable
    try:
        if isinstance(source, _Source):
            status = os.fstat(handle.fileno())
            if not stat_module.S_ISREG(status.st_mode):
                raise _refused(source, "it is not a regular file")
            if source.identity is not None and _identity(status) != source.identity:
                raise _refused(source, "it was replaced by another file since the push hashed it")
        yield handle
    finally:
        handle.close()


def _hash_file(source: Path) -> tuple[str, int]:
    """The file's BLAKE3 and its size, read once.

    Whole-file BLAKE3 is what a version's ``contentHash`` is, so this is the
    value the dedup comparison needs; the read streams so a 10 GB checkpoint
    never sits in memory.
    """
    with _open_source(source) as handle:
        return hashing.stream_hash(handle)


def _hash_source(source: _Source) -> tuple[str, int, _Source]:
    """:func:`_hash_file` for a push entry, returning it pinned to the inode read."""
    with _open_source(source) as handle:
        content_hash, size = hashing.stream_hash(handle)
        pinned = replace(source, identity=_identity(os.fstat(handle.fileno())))
    return content_hash, size, pinned


def _wire_name(name: bytes) -> str | None:
    """The JSON-safe spelling of a name, or ``None`` when there is none.

    A byte sequence that is not valid UTF-8 (a Latin-1 archive name) cannot
    ride in a JSON body, so such an entry is reported rather than pushed under
    a lossy substitute that ``pull`` could not invert.
    """
    try:
        return name.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _is_pointer(relative: bytes) -> bool:
    name = relative.rsplit(b"/", 1)[-1]
    return any(name.endswith(suffix) and len(name) > len(suffix) for suffix in POINTER_SUFFIXES)


class TransferTimeoutError(RuntimeError):
    """A transfer the server did not answer within the client's timeout.

    Raised in place of the bare ``httpx`` timeout so the failure names the
    file it happened on: which of ten thousand entries stalled is the only
    thing the operator needs and the transport exception never carries.
    """


class _Gap:
    """The two routes the generated SDK does not model, driven over its own
    ``httpx`` client so a push still speaks one session and one credential.

    ``POST …/children`` with a non-folder kind and ``PATCH …/items/{id}`` are
    surfaces a push needs and ``client.files`` does not expose; both are plain
    JSON bodies whose only subtlety is the header contract every Files mutation
    carries (``Idempotency-Key`` always, ``If-Match`` on a PATCH). ``POST
    …/tree`` is here for a different reason: the route answers with a *list* of
    the folders it made and the SDK's wrapper insists on an object, so its
    method raises on every success. Drop this once the SDK models the list.
    """

    def __init__(self, http: httpx.Client, base: str = "/api/v1/files") -> None:
        self._http = http
        self._base = base

    def create_tree(self, drive_id: str, item_id: str, paths: Sequence[str]) -> list[Any]:
        response = self._http.post(
            f"{self._base}/drives/{drive_id}/items/{item_id}/tree",
            json={"paths": list(paths)},
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        response.raise_for_status()
        parsed: Any = response.json()
        return parsed if isinstance(parsed, list) else []

    def put_content(
        self, drive_id: str, item_id: str, etag: str, source: Path | _Source
    ) -> dict[str, Any]:
        """A new version on a node that already exists, in one call.

        Only for a file within the server's single-call cap — over it the route
        refuses the request, and the caller commits an upload session with
        ``replace`` instead — which is also what bounds the buffer here: the
        body is read whole, but only ever up to the cap. ``If-Match`` is what
        keeps two pushes of the same tree from clobbering each other.
        """
        with _open_source(source) as handle:
            response = self._http.put(
                f"{self._base}/drives/{drive_id}/items/{item_id}/content",
                params={"conflictBehavior": "replace"},
                content=handle.read(),
                headers={
                    "Idempotency-Key": str(uuid.uuid4()),
                    "If-Match": etag,
                    "Content-Type": "application/octet-stream",
                },
            )
        response.raise_for_status()
        parsed: Any = response.json()
        return parsed if isinstance(parsed, dict) else {}

    def complete_upload(
        self, session_id: str, parts: Sequence[Mapping[str, Any]], *, if_match: str
    ) -> dict[str, Any]:
        """Commit a session onto the node that already holds the name.

        The SDK's own ``complete_upload`` sends no ``If-Match``, and a
        ``replace`` without one would land the bytes on whatever version the
        node has reached rather than the one this push compared against — so
        the replacing commit is sent here, where the header can ride along.
        """
        response = self._http.post(
            f"{self._base}/uploads/{session_id}/complete",
            json={"parts": [dict(part) for part in parts], "conflictBehavior": "replace"},
            headers={"Idempotency-Key": str(uuid.uuid4()), "If-Match": if_match},
        )
        response.raise_for_status()
        parsed: Any = response.json()
        return parsed if isinstance(parsed, dict) else {}

    def create_child(
        self, drive_id: str, parent_id: str, body: Mapping[str, Any]
    ) -> dict[str, Any]:
        response = self._http.post(
            f"{self._base}/drives/{drive_id}/items/{parent_id}/children",
            json=dict(body),
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        response.raise_for_status()
        parsed: Any = response.json()
        return parsed if isinstance(parsed, dict) else {}

    def head_hash(self, drive_id: str, item_id: str) -> str | None:
        """The head version's whole-file hash, when the item facet omits it."""
        response = self._http.get(f"{self._base}/drives/{drive_id}/items/{item_id}/versions")
        if response.status_code != 200:
            return None
        parsed: Any = response.json()
        versions = parsed.get("versions") if isinstance(parsed, dict) else None
        for version in versions or []:
            if isinstance(version, Mapping) and version.get("isHead"):
                value = version.get("contentHash")
                return value if isinstance(value, str) and value else None
        return None

    def patch_attrs(
        self, drive_id: str, item_id: str, etag: str, attrs: Mapping[str, Any]
    ) -> dict[str, Any]:
        response = self._http.patch(
            f"{self._base}/drives/{drive_id}/items/{item_id}",
            json={"attrs": dict(attrs)},
            headers={"Idempotency-Key": str(uuid.uuid4()), "If-Match": etag},
        )
        response.raise_for_status()
        parsed: Any = response.json()
        return parsed if isinstance(parsed, dict) else {}


def _existing(files: FilesApi, drive_id: str, item_path: str) -> dict[str, Any] | None:
    """The server's item at that path, or ``None`` when there is none.

    A 404 is the *only* absence: every other status is a real failure and is
    allowed to propagate, because silently reading a 403 or a 500 as "not
    there" would re-upload the whole tree on a transient error.
    """
    try:
        return files.item_by_path(drive_id, item_path)
    except Exception as exc:  # re-raised unless it is the 404
        if _status_of(exc) == 404:
            return None
        raise


def _existing_under(
    files: FilesApi, drive_id: str, anchor_id: str, step: str
) -> dict[str, Any] | None:
    """The server's item ``step`` below the anchor, or ``None``; the same rule
    about absence as :func:`_existing`."""
    try:
        return files.item_under(drive_id, anchor_id, step)
    except Exception as exc:  # re-raised unless it is the 404
        if _status_of(exc) == 404:
            return None
        raise


@dataclass(frozen=True)
class _Anchor:
    """The node a push is anchored on, and how the server spelled its path.

    ``cut`` is the server's own word for it: a path is spelled from the deepest
    ancestor the caller may read, and one that had ancestors cut off comes
    WITHOUT a leading slash. A box on a chat's lease reads nothing above the
    chat folder, so it is told the folder's bare name — a spelling nothing
    absolute can be built from.
    """

    node_id: str
    path: str
    cut: bool


@dataclass(frozen=True)
class _Lookup:
    """How this push finds a node by name.

    Absolute, from the drive root, when the caller can spell absolute paths —
    a person in their own drive, an org box on its operator's session, any
    push with no anchor. Relative to the anchor when the server CUT the
    anchor's path: every item path this push builds starts with the anchor's
    spelling, so the step below it is what is looked up, from the anchor, by
    the anchored read. What the push looks for is always at or under its
    anchor, so that is all it ever needs to be able to say.
    """

    files: FilesApi
    drive_id: str
    anchor: _Anchor | None

    def existing(self, item_path: str) -> dict[str, Any] | None:
        if self.anchor is None or not self.anchor.cut:
            return _existing(self.files, self.drive_id, item_path)
        wanted = item_path.strip("/")
        base = self.anchor.path.strip("/")
        if wanted == base:
            return _existing_under(self.files, self.drive_id, self.anchor.node_id, "")
        step = _step_below(base, wanted)
        if not step:
            raise RuntimeError(
                f"alkera files push: {item_path!r} is not under the anchored folder "
                f"{base!r}; nothing outside it is this push's to address"
            )
        return _existing_under(self.files, self.drive_id, self.anchor.node_id, step)


def push(
    *,
    files: FilesApi,
    http: httpx.Client,
    root: Path,
    dest: str,
    node_id: str | None = None,
    drive_id: str | None = None,
    inside: str = "",
    respect_gitignore: bool = True,
    exclude_presets: Iterable[str] = (),
    skip_local_state: bool = False,
    exclude_presets_within: Mapping[str, Sequence[str]] | None = None,
    skip: Collection[str] = (),
    include_link_targets: bool = False,
    home: Path | None = None,
    checkpoint: Callable[[str, int], None] | None = None,
    single_put_max_bytes: int = SINGLE_PUT_MAX_BYTES,
    progress: Progress | None = None,
    bases: Mapping[str, AgreedBase] | None = None,
) -> PushSummary:
    """Push ``root`` into the org path ``dest`` and report what happened.

    ``node_id`` names the destination folder outright, for a caller that already
    holds it (a mount's record). Without it the destination is reached by a
    ``tree`` posted at the drive root that walks down ``dest`` — and the root is
    a signpost a plain member may only walk, so a box pushing a chat folder it
    leases was refused there before the folder it holds was ever addressed.

    ``drive_id`` names the drive the same way, for the same caller: a mount's
    record carries it beside the node. Without it the drive is asked for as
    the caller's own, which a person has and a box on its machine credential
    — serving chats in orgs it is no member of — does not.
    With it the skeleton starts at the folder itself and the root is never
    asked; ``dest`` still names where that folder is, for the per-file reads.

    ``inside`` is the step from that anchored folder down to the directory
    being pushed, for a caller whose anchor sits ABOVE its bytes — a box
    streams the working directory inside the chat folder its lease is on, and
    the lease is the only node it was handed. Named rather than guessed from
    ``dest``: the anchor may have been renamed since, so the only thing both
    sides can agree on is the relative step. Left empty the anchor IS the
    destination, which is every other caller.

    ``progress`` is called with each entry's relative path as the push reaches
    it, so a caller can put a counter on the terminal without the library
    knowing what a terminal is.

    ``checkpoint`` is called with ``(relative path, part number)`` after every
    durable step — a persisted session, a stored part — so a caller can kill
    the push exactly where a closed laptop lid would and prove the resume.

    ``skip_local_state`` leaves this machine's own hold on the tree behind (see
    :mod:`alkera_core.project.local_state`) — for a folder that is pushed so
    another machine can take it over, never for a person's own directory.

    ``skip`` names relative paths this push must leave alone — the paths a
    holder has already trashed on the server since the last push. Without it
    the walk would find the bytes still on disk (or a name the trash has not
    caught up with) and upload them back, undoing the delete the user just
    made. A skipped folder takes everything under it with it. ``bases`` fences
    each path on what the caller last agreed (:class:`AgreedBase`).
    """
    root_identity = _identity(os.stat(root))
    entries = _selected(
        list(
            walk(
                root,
                respect_gitignore=respect_gitignore,
                exclude_presets=tuple(exclude_presets),
                skip_local_state=skip_local_state,
                exclude_presets_within=exclude_presets_within,
            )
        ),
        skip=skip,
    )
    summary = _transfer(
        files=files,
        http=http,
        root=root,
        root_identity=root_identity,
        dest=dest,
        node_id=node_id,
        inside=inside,
        entries=entries,
        home=home,
        checkpoint=checkpoint,
        single_put_max_bytes=single_put_max_bytes,
        progress=progress,
        bases=bases,
        drive_id=drive_id,
    )
    if include_link_targets:
        summary.warnings.append(
            "--include-link-targets stages a link's target only on a box mount; "
            "a push records the link itself"
        )
    return summary


def push_paths(
    *,
    files: FilesApi,
    http: httpx.Client,
    root: Path,
    dest: str,
    node_id: str | None = None,
    inside: str = "",
    paths: Sequence[Path],
    respect_gitignore: bool = True,
    exclude_presets: Iterable[str] = (),
    skip_local_state: bool = False,
    home: Path | None = None,
    checkpoint: Callable[[str, int], None] | None = None,
    single_put_max_bytes: int = SINGLE_PUT_MAX_BYTES,
    progress: Progress | None = None,
    bases: Mapping[str, AgreedBase] | None = None,
    drive_id: str | None = None,
) -> PushSummary:
    """Push only ``paths`` out of ``root``, on the terms :func:`push` uses.

    This is the same push, narrowed: the same walk classifies the tree (so a
    gitignored or sidecar path stays out even when it is named), the same
    skeleton call makes the folders a named path needs, the same dedup compares
    hashes before a byte moves, and the same resume state file is read and
    written — so a session a full push left open is finished here rather than
    abandoned, and vice versa.

    ``bases`` holds, by root-relative path, the base a live holder last agreed
    for a file (:class:`AgreedBase`); a path with one is fenced on it rather
    than on whatever etag the push happens to read.

    ``paths`` are absolute paths under ``root`` or paths already relative to
    it. One outside ``root`` is a ``ValueError`` rather than a push of somebody
    else's directory. A named path the walk does not reach — deleted since the
    caller noticed it, or classified away — is reported in ``warnings``, not
    raised: a holder pushing what it just saw racing a delete is ordinary.

    The tree is still walked once, because what a path IS (a link, a special, a
    sidecar) and which folders it needs are the walker's answers, not a
    ``stat``'s. That is a directory scan per call, so a caller with a burst of
    changes should name them in ONE call rather than one call each.
    """
    wanted = {_relative_key(root, path) for path in paths}
    root_identity = _identity(os.stat(root))
    entries = list(
        walk(
            root,
            respect_gitignore=respect_gitignore,
            exclude_presets=tuple(exclude_presets),
            skip_local_state=skip_local_state,
        )
    )
    summary = _transfer(
        files=files,
        http=http,
        root=root,
        root_identity=root_identity,
        dest=dest,
        node_id=node_id,
        inside=inside,
        entries=_selected(entries, wanted=wanted),
        home=home,
        checkpoint=checkpoint,
        single_put_max_bytes=single_put_max_bytes,
        progress=progress,
        bases=bases,
        drive_id=drive_id,
    )
    reached = {entry.relative for entry in entries}
    for missing in sorted(wanted - reached):
        summary.warnings.append(
            f"{os.fsdecode(missing)} is not in the push root any more; nothing was sent for it"
        )
    return summary


def _relative_key(root: Path, path: Path) -> bytes:
    """``path`` as the walker spells it: bytes, relative to ``root``, ``/``-joined."""
    candidate = Path(path)
    if candidate.is_absolute():
        for base in (root, Path(root).resolve()):
            with contextlib.suppress(ValueError):
                candidate = candidate.relative_to(base)
                break
        else:
            raise ValueError(f"alkera files push: {path} is not under {root}")
    return os.fsencode(candidate.as_posix().strip("/"))


def _selected(
    entries: Sequence[Entry],
    *,
    wanted: Collection[bytes] | None = None,
    skip: Collection[str] = (),
) -> list[Entry]:
    """The entries this push will actually visit.

    ``wanted`` keeps a named path and the folders above it — the folders
    because a file cannot land in a parent that was never created. ``skip``
    drops a path and its subtree. A caller passing neither gets the walk back
    unchanged.
    """
    keep = list(entries)
    if wanted is not None:
        ancestors: set[bytes] = set()
        for key in wanted:
            parts = key.split(b"/")
            for depth in range(1, len(parts)):
                ancestors.add(b"/".join(parts[:depth]))
        keep = [
            entry
            for entry in keep
            if entry.relative in wanted
            or (entry.kind is EntryKind.DIRECTORY and entry.relative in ancestors)
        ]
    dropped = {os.fsencode(name.strip("/")) for name in skip if name.strip("/")}
    if dropped:
        keep = [
            entry
            for entry in keep
            if entry.relative not in dropped
            and not any(entry.relative.startswith(name + b"/") for name in dropped)
        ]
    return keep


def _transfer(
    *,
    files: FilesApi,
    http: httpx.Client,
    root: Path,
    root_identity: _Identity,
    dest: str,
    node_id: str | None,
    inside: str,
    entries: Sequence[Entry],
    home: Path | None,
    checkpoint: Callable[[str, int], None] | None,
    single_put_max_bytes: int,
    progress: Progress | None,
    bases: Mapping[str, AgreedBase] | None = None,
    drive_id: str | None = None,
) -> PushSummary:
    """Move the entries a caller selected, and report what happened.

    Both entry points land here, so the skeleton, the dedup comparison, the
    resume state and the retry-once-on-a-moved-version rule are one
    implementation rather than two that drift.
    """
    summary = PushSummary()
    gap = _Gap(http)
    dest_path = dest.strip("/")
    anchor: _Anchor | None = None
    if node_id:
        # Anchored on a node the caller holds: the drive is the one it was
        # handed beside it, and only a caller handed none asks for its own.
        if drive_id is None:
            drive_id = str(files.drive()["id"])
        dest_id = node_id
        # The id anchors the push; `dest` is only what the anchor was called
        # when the caller wrote it down. Somebody may have renamed or moved the
        # folder since, and the per-file reads below are relative to wherever it
        # is NOW — a push anchored on the stale name looks for a skeleton that
        # moved and refuses, leaving the work on this machine.
        anchor = _node_anchor(files, drive_id, node_id) or _Anchor(node_id, dest_path, cut=False)
        anchor_path = anchor.path
        inside = inside.strip("/") or _step_below(anchor_path, dest_path)
        dest_path = f"{anchor_path}/{inside}" if inside else anchor_path
        lookup = _Lookup(files, drive_id, anchor)
        if inside:
            # The files land in a folder BELOW the anchor — a box streams the
            # working directory inside the chat folder its lease is on. Reading
            # and writing them against the anchor would file every one of them
            # one level too high, and the per-file lookup that decides
            # create-versus-replace would never find the version it is
            # replacing: every rewrite is then sent as a create and refused
            # because the name is taken.
            dest_id = _under(lookup, gap, node_id, inside, dest_path)
    else:
        # Walked down from the drive root, which only the caller's own drive
        # names: a caller with no drive of its own is refused here, by the
        # drive, exactly as it would be at the root.
        drive = files.drive()
        drive_id = str(drive["id"])
        dest_id = _destination(files, gap, drive, dest_path)
        lookup = _Lookup(files, drive_id, None)

    entries, unfileable = split_unfileable(entries)
    for relative, reason in unfileable:
        shown = relative.decode("utf-8", "backslashreplace")
        summary.warnings.append(f"{shown}: not saved (the drive does not file this name: {reason})")
        summary.failed.append(shown)
    summary.folders = _skeleton(gap, drive_id, dest_id, entries, summary)

    state_file = push_state_path(root, dest, home=home)
    sessions = load_state(state_file)
    #: The smallest file the drive refused for want of room in this push. A
    #: larger one would be refused too, so it is not read or asked for; a
    #: smaller one may still fit and is.
    no_room_from: int | None = None

    for entry in entries:
        if entry.skipped is not None:
            summary.excluded += 1
            if entry.skipped is SkipReason.SIDECAR:
                summary.warnings.append(f"folded sidecar {os.fsdecode(entry.relative)}")
            continue
        if entry.kind is EntryKind.DIRECTORY:
            continue
        wire = _wire_name(entry.relative)
        if wire is None:
            summary.warnings.append(f"skipped a name that is not UTF-8: {entry.relative!r}")
            continue
        if _is_pointer(entry.relative):
            summary.pointers_skipped += 1
            summary.warnings.append(
                f"{wire} is a pointer file; pointers are derived and are never pushed"
            )
            continue

        if progress is not None:
            progress(entry.relative)

        parent_wire = wire.rsplit("/", 1)[0] if "/" in wire else ""
        base_name = wire.rsplit("/", 1)[-1]
        item_path = f"{dest_path}/{wire}" if dest_path else wire

        if entry.kind in (EntryKind.SYMLINK, EntryKind.SPECIAL):
            _push_node(
                lookup=lookup,
                gap=gap,
                drive_id=drive_id,
                dest_id=dest_id,
                dest_path=dest_path,
                parent_wire=parent_wire,
                name=base_name,
                item_path=item_path,
                entry=entry,
                summary=summary,
            )
            continue

        if no_room_from is not None and entry.size >= no_room_from:
            summary.warnings.append(f"{wire}: not saved (the drive has no room for it)")
            summary.failed.append(wire)
            continue
        try:
            content_hash, size, source = _hash_source(
                _Source(root, entry.relative, root_identity=root_identity)
            )
        except SourceRefusedError as refused_source:
            # The walk saw a regular file here; what the name reaches now is a
            # link, a directory or some other thing -- the shape a writer of
            # the folder can swap in to point the push at a file outside it.
            summary.warnings.append(f"skipped {wire}: {refused_source}")
            continue
        except SourceUnreadableError as unreadable:
            if not isinstance(unreadable.__cause__, FileNotFoundError):
                raise
            # The walk is a snapshot too: the agent's log rotates and its
            # tools rewrite their scratch while a checkpoint push is under
            # way, so a file the walk listed can be gone by the time the push
            # reaches it. There is nothing to send for it, and failing the
            # whole folder over it cost the chat every other file's checkpoint.
            summary.warnings.append(f"skipped {wire}: it was gone before the push read it")
            continue
        found = lookup.existing(item_path)
        facet = (found or {}).get("file") or {}
        known = facet_content_hash(facet)
        if found is not None and known is None and facet.get("size") == size:
            known = gap.head_hash(drive_id, str(found["id"]))
        if found is not None and known == content_hash and facet.get("size") == size:
            summary.unchanged += 1
            reached = _apply_attrs(gap, drive_id, found, entry, summary)
            summary.agreed[wire] = reached or item_etag(found)
            continue

        base = (bases or {}).get(wire)
        if found is not None and base is not None and base.behind(content_hash):
            continue
        passed_by: str | None = None
        for retry in (False, True):
            try:
                precondition = None if found is None else _precondition(gap, drive_id, found, base)
                if found is not None and precondition is not None and size <= single_put_max_bytes:
                    # The path is already a node: a new version goes on it, rather
                    # than a second node beside it under a conflict rename.
                    gap.put_content(drive_id, str(found["id"]), precondition, source)
                else:
                    # Over the single-call cap the content PUT is refused outright,
                    # so a replacement travels the same streamed session a create
                    # does and commits with ``replace`` — fenced by the etag the
                    # push just read, so a file somebody else changed meanwhile is
                    # refused rather than clobbered.
                    parent_id = _parent_id(
                        lookup=lookup,
                        gap=gap,
                        drive_id=drive_id,
                        dest_id=dest_id,
                        dest_path=dest_path,
                        parent_wire=parent_wire,
                        summary=summary,
                    )
                    _upload(
                        files=files,
                        sessions=sessions,
                        state_file=state_file,
                        drive_id=drive_id,
                        parent_id=parent_id,
                        name=base_name,
                        key=wire,
                        source=source,
                        size=size,
                        content_hash=content_hash,
                        summary=summary,
                        checkpoint=checkpoint,
                        gap=gap,
                        replacing=precondition,
                    )
            except httpx.TimeoutException as stalled:
                raise TransferTimeoutError(
                    f"{wire}: the server did not answer within the transfer timeout "
                    f"({type(stalled).__name__}); the parts already stored are kept, "
                    "so re-running the push resumes where it stopped"
                ) from stalled
            except SourceRefusedError as refused_source:
                # Reopened for the upload, the name no longer reaches the inode
                # that was hashed: the bytes that would go are not the ones the
                # dedup and the session were agreed for, and may not be the
                # folder's at all.
                passed_by = f"skipped {wire}: {refused_source}"
                break
            except SourceUnreadableError as unreadable:
                # Deleted after the hash and before the upload reopened it: the
                # same churn the hash-time skip covers, one step later. Any
                # other read failure is still the push's failure.
                if not isinstance(unreadable.__cause__, FileNotFoundError):
                    raise
                passed_by = f"skipped {wire}: it was gone before the push read it"
                break
            except Exception as refused:
                if _no_room(refused):
                    # Too large for the room the drive has left: that file,
                    # not the folder. A 20 GB scratch file once failed every
                    # checkpoint of its workspace and left the rest unsaved.
                    no_room_from = size if no_room_from is None else min(no_room_from, size)
                    passed_by = f"{wire}: not saved (the drive has no room for it)"
                    summary.failed.append(wire)
                    break
                if _files_own_refusal(refused):
                    # The drive refused THIS file for a reason that is the
                    # file's own and will not change on retry. It is said,
                    # counted, and named at the release as what stayed
                    # behind; the rest of the tree lands rather than one
                    # entry failing the whole push.
                    passed_by = f"{wire}: not saved ({_refusal_text(refused)})"
                    summary.failed.append(wire)
                    break
                if retry or not _stale_version(refused):
                    raise
                # The version compared against moved under this push (or the
                # name landed since it looked): once more, against what is
                # there now. A second refusal is reported, not retried. A path
                # with an agreed base re-reads the head and fences on the same
                # rule, so the retry cannot turn into a write over bytes the
                # holder never saw.
                found = lookup.existing(item_path)
                continue
            break
        if passed_by is not None:
            summary.warnings.append(passed_by)
            continue
        summary.uploaded += 1
        summary.bytes_uploaded += size
        landed = lookup.existing(item_path)
        if landed is not None:
            reached = _apply_attrs(gap, drive_id, landed, entry, summary)
            if _head_hash(gap, drive_id, landed) == content_hash:
                summary.agreed[wire] = reached or item_etag(landed)

    return summary


def _head_hash(gap: _Gap, drive_id: str, item: Mapping[str, Any]) -> str | None:
    """The whole-file hash of ``item``'s head, from its facet or its versions."""
    return facet_content_hash(item.get("file") or {}) or gap.head_hash(drive_id, str(item["id"]))


def _precondition(
    gap: _Gap, drive_id: str, found: Mapping[str, Any], base: AgreedBase | None
) -> str:
    """The ``If-Match`` a write onto ``found`` carries.

    With no agreed base, the etag just read. With one, that same etag while
    the head still holds the agreed content -- a rename or an attribute edit
    moves the etag without touching the bytes, and is no conflict -- and the
    agreed etag once the head holds anything else, so the drive sees the
    write's base is behind its head and keeps both.
    """
    current = item_etag(found)
    if base is None or _head_hash(gap, drive_id, found) == base.content_hash:
        return current
    return base.etag


def _node_anchor(files: FilesApi, drive_id: str, node_id: str) -> _Anchor | None:
    """Where the server files that node right now, and whether it spelled the
    whole path: a leading slash is the root, its absence the server's cut at
    the deepest ancestor this caller may not read.

    Best effort: a lookup that fails leaves the caller on the name it already
    had, because a push that can still find its folder under the old name is a
    better outcome than one that refuses to run.
    """
    try:
        item = files.item(drive_id, node_id)
    except Exception:
        return None
    raw = item.get("pathBytes") or item.get("path_bytes") or item.get("path")
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "surrogateescape")
    if not isinstance(raw, str):
        return None
    spelled = raw.strip()
    path = spelled.strip("/")
    if not path:
        return None
    return _Anchor(node_id=node_id, path=path, cut=not spelled.startswith("/"))


def _destination(files: FilesApi, gap: _Gap, drive: Mapping[str, Any], dest_path: str) -> str:
    """The node id of the destination folder, created if it is not there."""
    root_id = str(drive["rootId"])
    if not dest_path:
        return root_id
    gap.create_tree(str(drive["id"]), root_id, [dest_path])
    found = _existing(files, str(drive["id"]), dest_path)
    if found is None:
        raise RuntimeError(f"alkera files push: {dest_path!r} did not exist after create_tree")
    return str(found["id"])


def _step_below(anchor_path: str, dest_path: str) -> str:
    """The step from ``anchor_path`` down to ``dest_path``, when there is one.

    A caller that anchored on one node and named a directory inside it has said
    what it means without a second argument, so the ordinary case needs no
    ``inside``. It answers nothing when the two are the same folder, and
    nothing when ``dest`` is somewhere else entirely — which is what a renamed
    anchor looks like from here, and why a caller that cannot rely on the two
    agreeing names the step outright instead.
    """
    anchor = anchor_path.strip("/")
    dest = dest_path.strip("/")
    if not anchor or not dest.startswith(f"{anchor}/"):
        return ""
    return dest[len(anchor) + 1 :]


def _under(lookup: _Lookup, gap: _Gap, anchor_id: str, inside: str, dest_path: str) -> str:
    """The node id of ``inside`` under the anchored folder, created if missing.

    Reached from the anchor rather than from the drive root: the root is a
    signpost a plain member may only walk, and the anchor is the one node this
    caller was handed. The folder is made rather than required because a
    working directory can be gone — trashed on the web while the box held the
    folder — and a push that refused there would strand the turn's bytes.
    """
    gap.create_tree(lookup.drive_id, anchor_id, [inside])
    found = lookup.existing(dest_path)
    if found is None:
        raise RuntimeError(f"alkera files push: {dest_path!r} did not exist after create_tree")
    return str(found["id"])


def _skeleton(
    gap: _Gap,
    drive_id: str,
    dest_id: str,
    entries: Sequence[Entry],
    summary: PushSummary,
) -> int:
    """Create every folder in one ``tree`` call.

    The route walks into a folder that already exists, so a re-push creates
    nothing and a fresh push creates the whole skeleton in one transaction
    rather than one round trip per level.

    A head start, not a guarantee. It is one call made before a byte moves, and
    which folder a file lands in is decided later and one file at a time by
    :func:`_parent_id` — which makes whatever this call left behind. The two
    read the same walk, so they never disagree about WHICH folders a push
    needs; they can disagree about which ones the drive holds, and the lookup
    at upload time is the one that is right.
    """
    folders: list[str] = []
    for entry in entries:
        if entry.skipped is not None or entry.kind is not EntryKind.DIRECTORY:
            continue
        wire = _wire_name(entry.relative)
        if wire is None:
            summary.warnings.append(
                f"skipped a directory name that is not UTF-8: {entry.relative!r}"
            )
            continue
        folders.append(wire)
    if not folders:
        return 0
    return len(gap.create_tree(drive_id, dest_id, sorted(folders)))


def _push_node(
    *,
    lookup: _Lookup,
    gap: _Gap,
    drive_id: str,
    dest_id: str,
    dest_path: str,
    parent_wire: str,
    name: str,
    item_path: str,
    entry: Entry,
    summary: PushSummary,
) -> None:
    """Record a symlink or a special as a node — never by resolving it."""
    if lookup.existing(item_path) is not None:
        return
    body: dict[str, Any] = {
        "name": name,
        "kind": "symlink" if entry.kind is EntryKind.SYMLINK else "special",
    }
    if entry.kind is EntryKind.SYMLINK:
        if entry.link_target is None:
            summary.warnings.append(f"{item_path} is a symlink with no readable target")
            return
        decoded = _wire_name(entry.link_target)
        if decoded is None:
            summary.warnings.append(f"{item_path} points at a target that is not UTF-8")
            return
        below = len(re.split(r"[\\/]" if os.sep == "\\" else "/", os.fsdecode(entry.relative))) - 1
        kind = str(entry.link_kind) if entry.link_kind is not None else LinkKind.RELATIVE
        if not stored_link_stays_inside(kind, below, entry.link_target):
            # The drive refuses a link out of the tree it is stored in; the
            # rest of the push goes ahead without it.
            summary.warnings.append(
                f"{item_path} was not pushed: it points outside the folder being pushed"
            )
            return
        body["symlinkTarget"] = decoded
        # The target alone does not say how to read it back: `/data/notes.txt`
        # is an org path a puller must re-anchor at its own root, and
        # `/usr/bin/env` is a host path it must write verbatim. Omitting the
        # kind makes the server store `relative` for both, which silently
        # turns every canonical link into the literal text of an org path.
        if entry.link_kind is not None:
            body["symlinkKind"] = str(entry.link_kind)
        summary.symlinks += 1
    else:
        summary.specials += 1
    parent_id = _parent_id(
        lookup=lookup,
        gap=gap,
        drive_id=drive_id,
        dest_id=dest_id,
        dest_path=dest_path,
        parent_wire=parent_wire,
        summary=summary,
    )
    gap.create_child(drive_id, parent_id, body)


def _parent_id(
    *,
    lookup: _Lookup,
    gap: _Gap,
    drive_id: str,
    dest_id: str,
    dest_path: str,
    parent_wire: str,
    summary: PushSummary,
) -> str:
    """The node id of the folder a child belongs in, made again if it is gone.

    The skeleton is a snapshot of a drive other writers keep moving: this box's
    own live sync trashes the paths the agent deleted while the push is walking
    the tree, and a person on the web may trash or move a folder at any moment.
    So a folder the skeleton made can be gone by the time the file that needs
    it is reached, and a folder the ``tree`` call did not make is discovered
    only here — the push never reads which folders came back.

    Reading that as an impossible state and raising cost the whole folder: a
    box's checkpoint push aborted on the harness's own ``.runtime/agent`` and
    every other file in the chat went with it, hand-back and all. The folder is
    made again instead. ``tree`` walks into what already exists, so the
    ordinary case sends nothing twice and the raced case leaves the file
    somewhere to land.
    """
    if not parent_wire:
        return dest_id
    item_path = f"{dest_path}/{parent_wire}" if dest_path else parent_wire
    found = lookup.existing(item_path)
    if found is None:
        summary.folders += len(gap.create_tree(drive_id, dest_id, [parent_wire]))
        found = lookup.existing(item_path)
    if found is None:
        raise RuntimeError(
            f"alkera files push: {item_path!r} is not there and making it left nothing "
            "at that path; the files under it were not sent"
        )
    return str(found["id"])


def _apply_attrs(
    gap: _Gap, drive_id: str, item: Mapping[str, Any], entry: Entry, summary: PushSummary
) -> str | None:
    """Restore the POSIX attributes git and the box both depend on, and answer
    the etag the node reached (the one it had, when nothing moved it).

    ``mtime`` above all: git's index treats a file whose mtime moved as racy,
    so a tree pulled back without its mtimes makes git re-hash every file.
    The patch is fenced on ``item``'s etag, so the etag it answers still names
    the bytes ``item`` held.
    """
    etag = item_etag(item)
    if not etag:
        return None
    # `AttrsPatch` is the one Files body with no camelCase alias generator and
    # `extra="forbid"`, so a camel key is a 422 rather than a silent drop.
    attrs = {"mode": entry.mode, "uid": entry.uid, "gid": entry.gid, "mtime_ns": entry.mtime_ns}
    try:
        patched = gap.patch_attrs(drive_id, str(item["id"]), etag, attrs)
    except httpx.HTTPStatusError as exc:
        summary.warnings.append(
            f"could not set attributes on {os.fsdecode(entry.relative)}: {exc.response.status_code}"
        )
        return etag
    return item_etag(patched) or etag


def _upload(
    *,
    files: FilesApi,
    sessions: dict[str, dict[str, Any]],
    state_file: Path,
    drive_id: str,
    parent_id: str,
    name: str,
    key: str,
    source: _Source,
    size: int,
    content_hash: str,
    summary: PushSummary,
    checkpoint: Callable[[str, int], None] | None,
    gap: _Gap,
    replacing: str | None = None,
    reopened: bool = False,
) -> None:
    """Move one file's bytes, resuming a session this push already opened.

    A remembered session the server has since aborted (its sweep, a stop that
    gave up on it) answers the commit ``409 files.session_state``, and would
    answer every replay the same: resuming it can never land. Such a session is
    forgotten and the file goes up once more through a fresh session; a second
    refusal is raised (``reopened`` says this call is that second try).

    ``replacing`` is the etag of the node whose bytes these replace: the commit
    then adopts that node and writes a new version onto it, instead of refusing
    the name a create would have to take.

    A remembered session is reused only when it is for *these* bytes: a file
    edited between the kill and the resume gets a fresh session, because the
    parts already stored belong to content that no longer exists.
    """
    # The source is opened before a session is opened or resumed, so a path
    # the push refuses to read leaves nothing behind on the server.
    with _open_source(source) as handle:
        remembered = sessions.get(key)
        session_id: str | None = None
        part_size = 0
        done: set[int] = set()
        if remembered is not None and remembered.get("contentHash") == content_hash:
            session_id = str(remembered["uploadId"])
            part_size = int(remembered["partSize"])
            try:
                state = files.upload_status(session_id)
            except Exception:  # an expired or swept session just re-opens
                session_id = None
            else:
                done = {int(part) for part in state.get("acceptedParts") or []}

        if session_id is None:
            opened = files.open_upload(parent_id=parent_id, name=name, declared_size=size)
            session_id = str(opened["uploadId"])
            part_size = int(opened["partSize"])
            done = set()
            sessions[key] = {
                "uploadId": session_id,
                "partSize": part_size,
                "contentHash": content_hash,
                "size": size,
            }
            save_state(state_file, sessions)
            if checkpoint is not None:
                checkpoint(key, 0)

        if part_size <= 0:
            raise UploadSessionError(
                f"{name}: the upload session offers a part size of {part_size}; "
                "no part can be cut from the file, so nothing is committed"
            )

        # The session was opened for exactly `size` bytes, so that is what bounds
        # the read: a handle that stops answering short and one that never reaches
        # its end are both a failure naming the file rather than a commit of the
        # wrong bytes or a loop with no end to it.
        parts: list[Mapping[str, Any]] = []
        part_no = 0
        read = 0
        while read < size:
            chunk = handle.read(min(part_size, size - read))
            if not chunk:
                break
            read += len(chunk)
            part_no += 1
            digest = _blake3_hex(chunk)
            if part_no not in done:
                files.put_part(session_id, part_no, chunk, checksum=digest)
                summary.parts_sent += 1
                if checkpoint is not None:
                    checkpoint(key, part_no)
            parts.append({"partNo": part_no, "size": len(chunk), "checksum": digest})
        if size == 0:
            # A zero-byte file is one part, not none: a session with no parts
            # has nothing for the commit to agree with the store about and is
            # refused (``files.parts_mismatch``), and a folder's ``__init__.py``
            # or an empty log is as much the folder's as any other file.
            digest = _blake3_hex(b"")
            if 1 not in done:
                files.put_part(session_id, 1, b"", checksum=digest)
                summary.parts_sent += 1
                if checkpoint is not None:
                    checkpoint(key, 1)
            parts.append({"partNo": 1, "size": 0, "checksum": digest})
    if read != size:
        raise UploadSessionError(
            f"{name}: the file held {read} bytes where the session was opened for {size}; "
            "it changed under the push, so the session is left open rather than committed short"
        )

    try:
        if replacing is None:
            operation = files.complete_upload(session_id, parts, conflict_behavior="fail")
        else:
            operation = gap.complete_upload(session_id, parts, if_match=replacing)
    except Exception as refused:
        if reopened or not _dead_session(refused):
            raise
        sessions.pop(key, None)
        save_state(state_file, sessions)
        _upload(
            files=files,
            sessions=sessions,
            state_file=state_file,
            drive_id=drive_id,
            parent_id=parent_id,
            name=name,
            key=key,
            source=source,
            size=size,
            content_hash=content_hash,
            summary=summary,
            checkpoint=checkpoint,
            gap=gap,
            replacing=replacing,
            reopened=True,
        )
        return
    sessions.pop(key, None)
    save_state(state_file, sessions)
    # The commit is an operation the server owns; the push's job ends when the
    # parts are agreed. Waiting is a courtesy so the summary reflects a landed
    # version where the commit is quick, never a requirement — so once one
    # commit in a push has not landed inside its wait, the server is behind
    # and the rest of the push does not wait on its commits at all: seven
    # hundred files at five seconds apiece held a box's hand-back for most of
    # an hour.
    if summary.commits_unconfirmed:
        summary.commits_unconfirmed += 1
        return
    try:
        files.await_operation(
            drive_id, str(operation["id"]), timeout=COMMIT_WAIT_SECONDS, interval=0.1
        )
    except TimeoutError:
        summary.commits_unconfirmed += 1
