"""A reader's working copy of a folder other writers keep changing.

``alkera files mount`` checks a folder out to one machine under a lease: a
single writer, nothing inbound. A working copy is the other shape. The folder
stays where it is (a chat's working directory, held by the chat's box while the
chat is awake, or by nobody while it sleeps), and this machine keeps a plain
local copy of it that a person edits in their editor:

* **down**: the drive's tree is pulled with :func:`~alkera_cli.files.pull.pull`
  under the ``keep_edits`` policy, so a newer version from somebody else replaces
  a file this copy has not touched and never a file it has;
* **up**: a file edited here is written back as an ordinary member's write that
  names the version it was edited from (``If-Match``). The drive decides the
  rest. While a box holds the chat's lease, the lease admits that write because
  it lands under the working directory, and the box applies it on its next
  drain; while nobody holds it, the write lands on the drive directly. The
  client sends the same request either way, so there is no "awake" branch here
  to drift from the server's;
* **both**: a file changed here AND on the drive since the copy last agreed it
  is a conflict. Neither side wins silently: the local bytes stay, the drive's
  stay, and the report names the path until the person picks one with
  :meth:`WorkingCopy.resolve`.

What is copied is decided by a **target**: something a person can open (a chat
today, a workspace next) that a registered resolver turns into one drive folder.
A new kind of target is a :func:`register_target` call, not a change here.

The copy's agreement with the drive is a :class:`WorkingCopyRecord` kept under
``ALKERA_HOME``, never inside the copied folder: a bookkeeping file in the
folder would be the person's file to the drive, and would sync.
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal, Protocol

import httpx
from alkera_core.files import PULL_PART_SUFFIX
from alkera_core.project import FileLock, LockHeldError, is_local_state, write_json_atomic
from alkera_core.versioning import VersionedModel
from alkera_sdk.client import AlkeraHTTPError
from pydantic import Field

from alkera_cli.files import hashing
from alkera_cli.files.pull import PullSummary, pull
from alkera_cli.files.push import SINGLE_PUT_MAX_BYTES, conflict_code
from alkera_cli.files.walk import EXCLUDE_PRESETS, EntryKind, walk
from alkera_cli.files.wire import facet_content_hash, item_etag
from alkera_cli.host import paths as cli_paths

__all__ = [
    "CHAT_FILES_NODE_KEY",
    "COPY_EXCLUDE_PRESETS",
    "MASS_DELETE_FILES",
    "SINGLE_PUT_MAX_BYTES",
    "AccessLostError",
    "Conflict",
    "CopiedFile",
    "CopyDetachedError",
    "CopyFilesApi",
    "CopyStoppedError",
    "CopyTarget",
    "Deletions",
    "HttpTargetReader",
    "Keep",
    "Refusal",
    "ResolveContext",
    "ResolvedRoot",
    "SyncReport",
    "TargetReader",
    "TargetUnavailableError",
    "WorkingCopy",
    "WorkingCopyAuthError",
    "WorkingCopyRecord",
    "WorkingCopyStore",
    "concerns",
    "deletion_needs_confirmation",
    "folder_name_for",
    "ignored",
    "register_target",
    "registered_kinds",
    "resolve_chat",
    "resolve_target",
    "resolve_workspace",
]

#: The presets a working copy leaves out on the way up, the same ones a held
#: chat folder's live sync folds: the caches a working directory grows when it
#: doubles as the agent's home are not anybody's work.
COPY_EXCLUDE_PRESETS: Final[tuple[str, ...]] = ("caches",)

#: Names an editor or a tool writes for a moment and removes: swap files,
#: backups, atomic-write temporaries. Never anybody's work.
_TEMPORARY_SUFFIXES: Final[tuple[str, ...]] = (
    ".alkera-tmp",
    PULL_PART_SUFFIX.decode("ascii"),
    ".swp",
    ".swo",
    ".swx",
    "~",
    "___jb_tmp___",
    "___jb_old___",
    ".crswap",
)
_TEMPORARY_PREFIXES: Final[tuple[str, ...]] = (".#", "#")

#: A pass that would trash this many files on the drive stops and asks, and so
#: does one that would trash more than ``MASS_DELETE_SHARE`` of the tree (once
#: it is at least ``MASS_DELETE_FLOOR`` files). An emptied folder is far more
#: often a moved, unmounted or wiped copy than a person deleting their work.
MASS_DELETE_FILES: Final = 20
MASS_DELETE_SHARE: Final = 0.5
MASS_DELETE_FLOOR: Final = 5


def deletion_needs_confirmation(deleting: int, known: int) -> bool:
    """Whether trashing ``deleting`` of ``known`` files in one pass is too many
    to do without asking."""
    if deleting >= MASS_DELETE_FILES:
        return True
    return deleting >= MASS_DELETE_FLOOR and deleting > known * MASS_DELETE_SHARE


def ignored(relative: str) -> bool:
    """The one ignore policy, for both directions.

    A path this answers ``True`` for is never sent up, never brought down, and
    never deleted on the drive for being absent here. One answer for both
    directions is the point: a path brought down but never sent would read as
    deleted locally on the next pass.
    """
    parts = [part for part in relative.split("/") if part]
    if not parts:
        return False
    caches = {name.decode("ascii") for name in EXCLUDE_PRESETS["caches"]}
    if any(part in caches for part in parts):
        return True
    if is_local_state(relative):
        return True
    leaf = parts[-1]
    if leaf in {".DS_Store", "4913"} or leaf.startswith("._"):
        return True
    return leaf.endswith(_TEMPORARY_SUFFIXES) or leaf.startswith(_TEMPORARY_PREFIXES)


#: How a conflict came about, for the sentence the editor shows.
ConflictReason = Literal["edited_both", "created_both"]

#: Which version a person keeps when they settle a conflict.
Keep = Literal["mine", "theirs"]

#: What a pass does with files missing here: ``guarded`` trashes them on the
#: drive unless there are too many; ``apply`` trashes them however many there
#: are (the person said so); ``restore`` brings them back down instead.
Deletions = Literal["guarded", "apply", "restore"]

_UUID: Final = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_FOLDER_NAME_CHARS: Final = re.compile(r"[^A-Za-z0-9 ._-]+")
_FOLDER_NAME_MAX: Final = 60


# ---------------------------------------------------------------------------
# The persisted agreement
# ---------------------------------------------------------------------------


class CopiedFile(VersionedModel):
    """What this copy and the drive last agreed one file holds."""

    SCHEMA_VERSION = "1.0.0"

    node_id: str = ""
    etag: str = ""
    """The version the local bytes were agreed at: the ``If-Match`` an edit of
    them is written back with."""
    content_hash: str = ""
    size: int = 0
    mtime_ns: int = 0
    """The local file's stamp when the agreement was made. A file still wearing
    it is not re-hashed to learn it is unchanged."""


class WorkingCopyRecord(VersionedModel):
    """One working copy, as the machine holding it remembers it."""

    SCHEMA_VERSION = "1.0.0"

    kind: str = ""
    target_id: str = ""
    drive_id: str = ""
    node_id: str = ""
    """The drive folder the copy mirrors."""
    title: str = ""
    root: str = ""
    """The local directory the copy lives in."""
    watch_ids: list[str] = Field(default_factory=list)
    """Nodes beyond the copied tree whose change means "look again" (a chat's
    own folder, whose lease the box takes and gives back)."""
    files: dict[str, CopiedFile] = Field(default_factory=dict)
    """By root-relative POSIX path."""
    folders: dict[str, str] = Field(default_factory=dict)
    """Folder node ids by root-relative POSIX path."""


class WorkingCopyStore:
    """Where working-copy records live: ``<home>/files/working-copies/``.

    ``home`` is read when the store is built, not when the module is imported,
    so a test (or a daemon started with ``ALKERA_HOME``) gets its own.
    """

    def __init__(self, home: Path | None = None) -> None:
        base = home if home is not None else cli_paths.ALKERA_HOME
        self.directory = base / "files" / "working-copies"

    def path_for(self, kind: str, target_id: str) -> Path:
        return self.directory / f"{kind}-{target_id}.json"

    def load(self, kind: str, target_id: str) -> WorkingCopyRecord | None:
        return self._read(self.path_for(kind, target_id))

    def find_by_root(self, root: Path) -> WorkingCopyRecord | None:
        """The record whose copy lives at ``root``, compared after resolving."""
        wanted = _resolved(root)
        if not self.directory.is_dir():
            return None
        for candidate in sorted(self.directory.glob("*.json")):
            record = self._read(candidate)
            if record is not None and record.root and _resolved(Path(record.root)) == wanted:
                return record
        return None

    def save(self, record: WorkingCopyRecord) -> None:
        write_json_atomic(
            self.path_for(record.kind, record.target_id), record.model_dump(mode="json")
        )

    def backup_dir(self, record: WorkingCopyRecord) -> Path:
        """A new folder for local versions set aside from this copy, outside
        the copied folder so they never sync."""
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        return self.directory / "backups" / f"{record.kind}-{record.target_id}" / stamp

    def lock_for(self, record: WorkingCopyRecord) -> FileLock:
        return FileLock(self.path_for(record.kind, record.target_id).with_suffix(".lock"))

    @staticmethod
    def _read(path: Path) -> WorkingCopyRecord | None:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            return WorkingCopyRecord.model_validate_json(text)
        except ValueError:
            return None


def _resolved(path: Path) -> Path:
    return Path(os.path.realpath(path))


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CopyTarget:
    """What a person asked to open: a kind and an id, nothing else."""

    kind: str
    id: str


@dataclass(frozen=True, slots=True)
class ResolvedRoot:
    """The drive folder a target copies, and what else to watch for it."""

    drive_id: str
    node_id: str
    title: str
    watch_ids: tuple[str, ...] = ()
    canonical: CopyTarget | None = None
    """The target this one IS, when it is another name for it: a workspace of
    one is its chat, so both links share one copy instead of two."""


class TargetUnavailableError(Exception):
    """The target cannot be copied, for a reason the person can act on.

    ``reason`` is one of ``invalid`` (not an id), ``unsupported`` (no resolver
    for this kind on this build), ``not_found`` (missing, or not visible to
    this account) and ``no_files`` (it exists but has no folder to copy).
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class WorkingCopyAuthError(Exception):
    """The API refused the stored credential: the person has to sign in."""


class CopyStoppedError(Exception):
    """The copy cannot be synced any more and must stop, changing nothing.

    ``reason`` says why, for the editor: ``access_lost`` or ``detached``.
    """

    reason: str = ""

    def __init__(self, message: str, name: str) -> None:
        super().__init__(message)
        self.name = name


class AccessLostError(CopyStoppedError):
    """The copied tree is no longer this account's to read: a share was
    revoked, or the folder was deleted or trashed.

    Raised before a pass changes anything. A tree that answers "not found"
    would otherwise read as every file deleted on the drive, and the copy
    would delete the local files or try to upload them all as new.
    """

    reason = "access_lost"

    def __init__(self, name: str) -> None:
        super().__init__(f"You no longer have access to {name}. Local files are kept.", name)


class CopyDetachedError(CopyStoppedError):
    """The local folder is gone (deleted, moved, an unmounted volume).

    A missing folder is never read as the person deleting every file: that
    reading trashed the whole tree on the drive, and a box holding the chat
    applied the deletes to the running agent's directory.
    """

    reason = "detached"

    def __init__(self, name: str) -> None:
        super().__init__(
            f"The local folder for {name} is gone. Nothing was changed in Alkera.", name
        )


class TargetReader(Protocol):
    """Reads what a link names, as the chat and workspace APIs answer it."""

    def read_chat(self, chat_id: str) -> Mapping[str, Any]: ...

    def read_workspace(self, workspace_id: str) -> Mapping[str, Any]: ...


class CopyFilesApi(Protocol):
    """The slice of the SDK's ``client.files`` namespace a working copy drives."""

    def drive(self) -> dict[str, Any]: ...

    def item(self, drive_id: str, item_id: str, *, select: str | None = ...) -> dict[str, Any]: ...

    def item_by_path(self, drive_id: str, item_path: str) -> dict[str, Any]: ...

    def item_under(self, drive_id: str, item_id: str, item_path: str) -> dict[str, Any]: ...

    def children(
        self,
        drive_id: str,
        item_id: str,
        *,
        limit: int = ...,
        order_by: str | None = ...,
        filters: Mapping[str, Any] | None = ...,
    ) -> Iterator[dict[str, Any]]: ...

    def put_content(
        self,
        drive_id: str,
        item_id: str,
        data: bytes,
        *,
        if_match: str | Mapping[str, Any],
        conflict_behavior: str = ...,
        mime: str | None = ...,
    ) -> dict[str, Any]: ...

    def trash(
        self, drive_id: str, item_id: str, *, if_match: str | Mapping[str, Any]
    ) -> dict[str, Any]: ...

    def create_folder(
        self, drive_id: str, parent_id: str, name: str, *, conflict_behavior: str = ...
    ) -> dict[str, Any]: ...

    def download(self, drive_id: str, item_id: str, dest: str | os.PathLike[str]) -> Path: ...

    def upload_file(
        self,
        source: str | os.PathLike[str],
        *,
        drive_id: str,
        parent_id: str,
        name: str | None = ...,
        mime: str | None = ...,
        conflict_behavior: str = ...,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class ResolveContext:
    """What a resolver may read with: the Files surface and the target APIs."""

    files: CopyFilesApi
    reader: TargetReader


Resolver = Callable[[ResolveContext, str], ResolvedRoot]

_RESOLVERS: dict[str, Resolver] = {}


def register_target(kind: str, resolver: Resolver) -> None:
    """Teach the working copy a new kind of target.

    Each resolver answers one drive folder; everything downstream (the record,
    the sync, the daemon methods, the editor) speaks in targets.
    """
    _RESOLVERS[kind] = resolver


def registered_kinds() -> tuple[str, ...]:
    return tuple(sorted(_RESOLVERS))


def resolve_target(ctx: ResolveContext, target: CopyTarget) -> ResolvedRoot:
    """The drive folder ``target`` copies, or :class:`TargetUnavailableError`."""
    if not _UUID.match(target.id):
        raise TargetUnavailableError("invalid", "That link does not name a valid id.")
    resolver = _RESOLVERS.get(target.kind)
    if resolver is None:
        raise TargetUnavailableError(
            "unsupported", f"This version of Alkera cannot open a {target.kind}."
        )
    return resolver(ctx, target.id.lower())


#: The key a chat folder's object facet names its working directory under. The
#: server owns the answer (``backend.services.files.items.CHAT_FILES_NODE_KEY``),
#: so the copy follows it wherever the working directory moves.
CHAT_FILES_NODE_KEY: Final = "files_node_id"


def resolve_chat(ctx: ResolveContext, chat_id: str) -> ResolvedRoot:
    """A chat copies its working directory, never the chat folder around it.

    The chat folder also holds the conversation's own records (the transcript,
    the manifest, the runtime directory), which are not a person's to edit and
    which the box's lease refuses to anyone else. The server names the working
    directory on the chat folder's facet; a chat whose facet names none has
    nothing to copy, and the copy says so rather than falling back to the chat
    folder and its records.
    """
    chat = ctx.reader.read_chat(chat_id)
    drive_id = chat.get("files_drive_id")
    chat_node = chat.get("files_node_id")
    title = str(chat.get("title") or "")
    if not drive_id or not chat_node:
        raise TargetUnavailableError("no_files", "This chat has no files yet.")
    folder = ctx.files.item(str(drive_id), str(chat_node))
    facet = folder.get("object") or {}
    metadata = facet.get("metadata") if isinstance(facet, Mapping) else None
    working = metadata.get(CHAT_FILES_NODE_KEY) if isinstance(metadata, Mapping) else None
    if not isinstance(working, str) or not working:
        raise TargetUnavailableError("no_files", "This chat has no files yet.")
    return ResolvedRoot(
        drive_id=str(drive_id), node_id=working, title=title, watch_ids=(str(chat_node),)
    )


register_target("chat", resolve_chat)


def resolve_workspace(ctx: ResolveContext, workspace_id: str) -> ResolvedRoot:
    """A workspace copies its shared working tree, never the folder around it.

    A workspace of one (a chat adopted as a workspace) has the chat's own
    folder as its folder, transcript and all; its working tree is the chat's
    working directory, so it resolves as that chat and shares that chat's
    copy. A project workspace copies its ``files/`` tree, which the server
    names as ``working_node_id``. A read that names no working tree, or names
    the workspace folder itself, has nothing safe to copy and says so rather
    than falling back to the folder that holds the records.
    """
    workspace = ctx.reader.read_workspace(workspace_id)
    adopted = workspace.get("adopted_chat_id")
    if isinstance(adopted, str) and adopted:
        chat = resolve_chat(ctx, adopted.lower())
        return ResolvedRoot(
            drive_id=chat.drive_id,
            node_id=chat.node_id,
            title=chat.title,
            watch_ids=chat.watch_ids,
            canonical=CopyTarget("chat", adopted.lower()),
        )
    drive_id = workspace.get("files_drive_id")
    folder = workspace.get("files_node_id")
    working = workspace.get("working_node_id")
    if not drive_id or not isinstance(working, str) or not working or working == folder:
        raise TargetUnavailableError("no_files", "This workspace has no files yet.")
    return ResolvedRoot(
        drive_id=str(drive_id),
        node_id=working,
        title=str(workspace.get("title") or ""),
        watch_ids=(str(folder),) if folder else (),
    )


register_target("workspace", resolve_workspace)


class HttpTargetReader:
    """The chat and workspace reads over the HTTP client the Files namespace
    speaks on."""

    def __init__(self, http: httpx.Client) -> None:
        self._http = http

    def read_chat(self, chat_id: str) -> Mapping[str, Any]:
        response = self._http.get(f"/api/v1/chats/{chat_id}")
        if response.status_code == 401:
            raise WorkingCopyAuthError("the stored sign-in was refused")
        if response.status_code in (403, 404):
            raise TargetUnavailableError(
                "not_found", "This chat does not exist or is not shared with this account."
            )
        response.raise_for_status()
        body: Any = response.json()
        if not isinstance(body, dict):
            raise TargetUnavailableError("not_found", "The chat API answered with no chat.")
        return body

    def read_workspace(self, workspace_id: str) -> Mapping[str, Any]:
        response = self._http.get(f"/api/v1/workspaces/{workspace_id}")
        if response.status_code == 401:
            raise WorkingCopyAuthError("the stored sign-in was refused")
        # A server from before workspaces answers 404 too, indistinguishable
        # from a workspace this account cannot see; a link that also names its
        # chat falls back to the chat either way.
        if response.status_code in (403, 404):
            raise TargetUnavailableError(
                "not_found", "This workspace does not exist or is not shared with this account."
            )
        response.raise_for_status()
        body: Any = response.json()
        if not isinstance(body, dict):
            raise TargetUnavailableError(
                "not_found", "The workspace API answered with no workspace."
            )
        return body


# ---------------------------------------------------------------------------
# What a sync did
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Conflict:
    path: str
    reason: ConflictReason


@dataclass(frozen=True, slots=True)
class Refusal:
    """A local change the drive would not take, with its own code."""

    path: str
    code: str
    message: str


@dataclass
class SyncReport:
    """What one pass did, by root-relative POSIX path."""

    downloaded: list[str] = field(default_factory=list)
    uploaded: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    """Deleted here because the drive deleted them."""
    trashed: list[str] = field(default_factory=list)
    """Deleted on the drive because they were deleted here."""
    restored: list[str] = field(default_factory=list)
    """Deleted here but changed on the drive meanwhile, so brought back."""
    pending: list[str] = field(default_factory=list)
    """Listed by the drive, bytes not landed yet; fetched on a later pass."""
    conflicts: list[Conflict] = field(default_factory=list)
    refused: list[Refusal] = field(default_factory=list)
    busy: bool = False
    """Another process was syncing this copy; this pass did nothing."""
    deletions_held: list[str] = field(default_factory=list)
    """Files missing here that this pass would have trashed on the drive, held
    because there were too many to be an ordinary edit. Nothing else ran; the
    person says whether to delete them there or bring them back here."""
    backups: dict[str, str] = field(default_factory=dict)
    """Local versions set aside before being replaced, by path: where each
    one was saved."""
    needs_full: bool = False
    """A refresh of single files met a change it cannot apply alone (a
    rename, a move) and the next pass has to read the whole tree."""

    @property
    def needs_answer(self) -> bool:
        return bool(self.conflicts or self.refused or self.deletions_held or self.backups)

    @property
    def changed(self) -> bool:
        return bool(
            self.downloaded or self.uploaded or self.removed or self.trashed or self.restored
        )


# ---------------------------------------------------------------------------
# The copy
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _LocalFile:
    size: int
    mtime_ns: int


def _posix(relative: bytes) -> str:
    return os.fsdecode(relative)


def _refusal(path: str, exc: AlkeraHTTPError) -> Refusal:
    code = conflict_code(exc) or f"http_{exc.status}"
    if code == "files.leased":
        message = (
            f"{path} was not saved: that folder is held by the chat's machine and only "
            "its working folder takes edits."
        )
    else:
        message = f"{path} was not saved: {exc.message or code}"
    return Refusal(path=path, code=code, message=message)


def _raise_if_auth(exc: AlkeraHTTPError) -> None:
    if exc.status == 401:
        raise WorkingCopyAuthError("the stored sign-in was refused") from exc


def folder_name_for(title: str, target_id: str) -> str:
    """The local directory a new copy is made in: a readable, single, safe
    path component, unique by the target's id."""
    readable = _FOLDER_NAME_CHARS.sub(" ", title).strip(" .")
    readable = re.sub(r"\s+", " ", readable)[:_FOLDER_NAME_MAX].strip(" .")
    return f"{readable or 'chat'} {target_id[:8]}"


class WorkingCopy:
    """One local copy and the drive folder it mirrors.

    Synchronous on purpose: every call is a handful of blocking HTTP requests
    and file writes, and the daemon runs it on a worker thread. The live
    runner (:mod:`alkera_cli.files.working_copy_live`) decides *when* to sync;
    this decides *what* a sync does.
    """

    def __init__(
        self,
        record: WorkingCopyRecord,
        *,
        files: CopyFilesApi,
        http: httpx.Client,
        store: WorkingCopyStore,
        single_put_max_bytes: int = SINGLE_PUT_MAX_BYTES,
    ) -> None:
        self.record = record
        self._files = files
        self._http = http
        self._store = store
        self._single_put_max = single_put_max_bytes
        self._dirty: frozenset[str] = frozenset()

    @property
    def root(self) -> Path:
        return Path(self.record.root)

    # ---- opening ---------------------------------------------------------

    @classmethod
    def open(
        cls,
        target: CopyTarget,
        *,
        location: Path,
        files: CopyFilesApi,
        http: httpx.Client,
        reader: TargetReader,
        store: WorkingCopyStore,
    ) -> tuple[WorkingCopy, SyncReport]:
        """Make (or bring up to date) the copy of ``target`` and sync it once.

        A target copied before keeps its directory, so a second open of the
        same chat lands in the same folder. A new copy is made in a fresh
        directory under ``location``; a directory already there that this
        store did not make is never adopted, because syncing it would upload
        whatever it held into the chat.
        """
        resolved = resolve_target(ResolveContext(files=files, reader=reader), target)
        # Another name for a target copies as that target: one folder, one copy.
        own = resolved.canonical or CopyTarget(target.kind, target.id.lower())
        record = store.load(own.kind, own.id)
        if record is None or not Path(record.root).is_dir():
            root = cls._fresh_root(location, resolved.title, own.id)
            record = WorkingCopyRecord(kind=own.kind, target_id=own.id, root=str(root))
        record.drive_id = resolved.drive_id
        record.node_id = resolved.node_id
        record.title = resolved.title
        record.watch_ids = list(resolved.watch_ids)
        store.save(record)
        copy = cls(record, files=files, http=http, store=store)
        return copy, copy.sync()

    @staticmethod
    def _fresh_root(location: Path, title: str, target_id: str) -> Path:
        if not location.is_absolute():
            raise ValueError("a working copy's location must be an absolute path")
        root = location / folder_name_for(title, target_id)
        if root.exists() and (not root.is_dir() or any(root.iterdir())):
            raise FileExistsError(f"{root} already exists and is not an Alkera working copy")
        root.mkdir(parents=True, exist_ok=True)
        return root

    @classmethod
    def attach(
        cls, root: Path, *, files: CopyFilesApi, http: httpx.Client, store: WorkingCopyStore
    ) -> WorkingCopy | None:
        """The copy living at ``root``, or ``None`` when it is not one."""
        record = store.find_by_root(root)
        if record is None:
            return None
        return cls(record, files=files, http=http, store=store)

    # ---- the editor's unsaved buffers ----------------------------------

    @property
    def dirty(self) -> frozenset[str]:
        return self._dirty

    def set_dirty(self, paths: frozenset[str]) -> frozenset[str]:
        """Record which files have unsaved edits in an editor; answer the paths
        that were dirty before and are not any more.

        A dirty file is held exactly as it is: the drive's newer bytes are not
        written under the buffer, and the version its edit will be saved
        against is not moved. Moving it would make the save name a base the
        person never saw, and the drive would take it as a plain new version
        over somebody else's edit instead of refusing it as a conflict.
        """
        cleaned = self._dirty - paths
        self._dirty = frozenset(paths)
        return cleaned

    # ---- syncing ---------------------------------------------------------

    def local_changed(self) -> bool:
        """Whether anything on disk moved away from the last agreement.

        The cheap question a filesystem event asks before a full pass: it reads
        stamps and hashes and makes no request, so the copy's own downloads
        (which also raise filesystem events) cost nothing to dismiss.
        """
        local = self._scan()
        if set(local) != set(self.record.files):
            return True
        return any(self._edited(path, stamp) for path, stamp in local.items())

    def resolve_deletions(self, *, apply: bool) -> SyncReport:
        """Answer held deletions: trash them on the drive, or bring them back."""
        lock = self._store.lock_for(self.record)
        try:
            lock.acquire()
        except LockHeldError:
            return SyncReport(busy=True)
        try:
            return self._sync_locked(deletions="apply" if apply else "restore")
        finally:
            lock.release()

    def sync(self, *, pull: bool = True) -> SyncReport:
        """One pass. See the module docstring for the rules.

        ``pull=False`` sends what changed here and reads nothing back: a save
        has nothing to learn from the drive's whole tree, and a copy with a
        file being edited live elsewhere would otherwise read the tree on
        every keystroke-sized save.
        """
        lock = self._store.lock_for(self.record)
        try:
            lock.acquire()
        except LockHeldError:
            return SyncReport(busy=True)
        try:
            return self._sync_locked(pull=pull)
        finally:
            lock.release()

    def refresh(self, paths: frozenset[str]) -> SyncReport:
        """Bring just these known files up to the drive, one read each.

        What a realtime frame naming one file asks for. A file renamed or moved
        on the drive cannot be followed from its own item alone, so the report
        then asks for a full pass instead.
        """
        lock = self._store.lock_for(self.record)
        try:
            lock.acquire()
        except LockHeldError:
            return SyncReport(busy=True)
        try:
            self._require_root()
            self._require_access()
            report = SyncReport()
            try:
                for path in sorted(paths):
                    self._refresh_one(path, report)
            except AlkeraHTTPError as exc:
                _raise_if_auth(exc)
                raise
            finally:
                self._store.save(self.record)
            return report
        finally:
            lock.release()

    def paths_of_node(self, node_id: str) -> frozenset[str]:
        """The known files whose node is ``node_id``."""
        return frozenset(
            path for path, entry in self.record.files.items() if entry.node_id == node_id
        )

    def resolve(self, path: str, keep: Keep) -> SyncReport:
        """Settle a conflict on ``path`` by keeping one side, then sync.

        ``path`` arrives from the editor, so it is held to what a path inside
        the copy can look like before anything is read or deleted by it.
        """
        parts = path.split("/")
        if not path or path.startswith("/") or "\\" in path or {"", ".", ".."} & set(parts):
            raise ValueError(f"{path!r} is not a path inside the working copy")
        lock = self._store.lock_for(self.record)
        try:
            lock.acquire()
        except LockHeldError:
            return SyncReport(busy=True)
        try:
            self._require_root()
            self._require_access()
            report = SyncReport()
            if keep == "mine":
                self._keep_mine(path, report)
            else:
                self._keep_theirs(path, report)
            follow = self._sync_locked()
            follow.refused[:0] = report.refused
            follow.uploaded[:0] = report.uploaded
            follow.backups.update(report.backups)
            return follow
        finally:
            lock.release()

    def _require_access(self) -> None:
        """Stop here, before anything moves, when the tree is gone for us."""
        try:
            item = self._files.item(self.record.drive_id, self.record.node_id)
        except AlkeraHTTPError as exc:
            _raise_if_auth(exc)
            if exc.status in (403, 404):
                raise AccessLostError(self._display_name()) from exc
            raise
        if item.get("trashed"):
            raise AccessLostError(self._display_name())

    def _display_name(self) -> str:
        return self.record.title or self.root.name

    def _require_root(self) -> None:
        """A missing local folder stops the copy; it is never "all deleted"."""
        if not self.root.is_dir():
            raise CopyDetachedError(self._display_name())

    def _forget_ignored(self) -> None:
        """Drop paths the ignore policy covers from the record. An older build
        brought some down; they are absent from every scan, and a pass must not
        read that absence as a deletion."""
        for path in [path for path in self.record.files if ignored(path)]:
            del self.record.files[path]
        for path in [path for path in self.record.folders if ignored(path)]:
            del self.record.folders[path]

    def _sync_locked(self, *, pull: bool = True, deletions: Deletions = "guarded") -> SyncReport:
        self._require_root()
        self._require_access()
        self._forget_ignored()
        report = SyncReport()
        local = self._scan()
        missing = sorted(set(self.record.files) - set(local))
        if deletions == "guarded" and self._too_many(len(missing)):
            # Nothing else runs: a pull would quietly bring them back, and a
            # push would act on a tree that may not be the person's at all.
            report.deletions_held = missing
            return report
        try:
            if deletions != "restore":
                self._push_deletions(local, report)
            self._push_edits(local, report)
            self._push_new(local, report)
            if pull:
                self._pull(report)
            if deletions == "restore":
                report.restored.extend(path for path in missing if (self.root / path).is_file())
        except AlkeraHTTPError as exc:
            _raise_if_auth(exc)
            raise
        finally:
            self._store.save(self.record)
        return report

    # ---- the local side --------------------------------------------------

    def _scan(self) -> dict[str, _LocalFile]:
        """Every plain file the copy would carry, by root-relative POSIX path."""
        found: dict[str, _LocalFile] = {}
        for entry in walk(
            self.root,
            respect_gitignore=False,
            exclude_presets=COPY_EXCLUDE_PRESETS,
            skip_local_state=True,
        ):
            if entry.skipped is not None or entry.kind is not EntryKind.FILE:
                continue
            path = _posix(entry.relative)
            if ignored(path):
                continue
            found[path] = _LocalFile(size=entry.size, mtime_ns=entry.mtime_ns)
        return found

    def _edited(self, path: str, stamp: _LocalFile) -> bool:
        known = self.record.files.get(path)
        if known is None:
            return True
        if stamp.size == known.size and stamp.mtime_ns == known.mtime_ns:
            return False
        if stamp.size != known.size:
            return True
        return hashing.file_hash(self.root / path)[0] != known.content_hash

    def _stamp(self, path: str) -> tuple[int, int]:
        info = (self.root / path).stat()
        return info.st_size, info.st_mtime_ns

    # ---- up: deletions, edits, new files -------------------------------

    def _too_many(self, deleting: int) -> bool:
        return deletion_needs_confirmation(deleting, len(self.record.files))

    def _push_deletions(self, local: Mapping[str, _LocalFile], report: SyncReport) -> None:
        for path in sorted(set(self.record.files) - set(local)):
            known = self.record.files[path]
            try:
                self._files.trash(self.record.drive_id, known.node_id, if_match=known.etag)
            except AlkeraHTTPError as exc:
                _raise_if_auth(exc)
                if exc.status == 404:
                    del self.record.files[path]
                elif exc.status == 412:
                    # Changed on the drive since this copy last saw it: the
                    # newer version is somebody's work, so it comes back here
                    # rather than being deleted on the strength of an older one.
                    del self.record.files[path]
                    report.restored.append(path)
                else:
                    report.refused.append(_refusal(path, exc))
                continue
            del self.record.files[path]
            report.trashed.append(path)
        self._trash_emptied_folders(local, report)

    def _trash_emptied_folders(self, local: Mapping[str, _LocalFile], report: SyncReport) -> None:
        """A folder deleted here goes from the drive too, once it is empty there."""
        for path in sorted(self.record.folders, key=lambda p: p.count("/"), reverse=True):
            if (self.root / path).is_dir():
                continue
            node_id = self.record.folders[path]
            try:
                item = self._files.item(self.record.drive_id, node_id)
                if next(iter(self._files.children(self.record.drive_id, node_id, limit=1)), None):
                    continue
                self._files.trash(self.record.drive_id, node_id, if_match=item)
            except AlkeraHTTPError as exc:
                _raise_if_auth(exc)
                if exc.status != 404:
                    report.refused.append(_refusal(path, exc))
                    continue
            del self.record.folders[path]
            report.trashed.append(path)

    def _push_edits(self, local: Mapping[str, _LocalFile], report: SyncReport) -> None:
        for path in sorted(set(self.record.files) & set(local)):
            if not self._edited(path, local[path]):
                continue
            known = self.record.files[path]
            source = self.root / path
            if local[path].size > self._single_put_max:
                report.refused.append(
                    Refusal(
                        path=path,
                        code="too_large",
                        message=(
                            f"{path} was not saved: an edited file over "
                            f"{self._single_put_max // (1 << 20)} MB is not written back from here."
                        ),
                    )
                )
                continue
            data = source.read_bytes()
            try:
                item = self._files.put_content(
                    self.record.drive_id, known.node_id, data, if_match=known.etag
                )
            except AlkeraHTTPError as exc:
                _raise_if_auth(exc)
                if exc.status == 412:
                    report.conflicts.append(Conflict(path=path, reason="edited_both"))
                elif exc.status == 404:
                    # Deleted on the drive while edited here: the edit is the
                    # newer work, so it goes back up as a new file.
                    del self.record.files[path]
                else:
                    report.refused.append(_refusal(path, exc))
                continue
            self._agree(path, item, hashing.content_hash(data), len(data))
            report.uploaded.append(path)

    def _push_new(self, local: Mapping[str, _LocalFile], report: SyncReport) -> None:
        for path in sorted(set(local) - set(self.record.files)):
            parent, _, name = path.rpartition("/")
            try:
                parent_id = self._folder_id(parent)
                self._files.upload_file(
                    self.root / path,
                    drive_id=self.record.drive_id,
                    parent_id=parent_id,
                    name=name,
                    conflict_behavior="fail",
                )
                item = self._files.item_under(self.record.drive_id, parent_id, name)
            except AlkeraHTTPError as exc:
                _raise_if_auth(exc)
                if exc.status == 409 and conflict_code(exc) == "files.exists":
                    self._settle_same_name(path, report)
                else:
                    report.refused.append(_refusal(path, exc))
                continue
            local_hash = hashing.file_hash(self.root / path)[0]
            self._agree(path, item, local_hash, local[path].size)
            report.uploaded.append(path)

    def _settle_same_name(self, path: str, report: SyncReport) -> None:
        """A file made here under a name the drive took meanwhile: the same
        bytes are one file; different bytes are a conflict."""
        item = self._files.item_under(self.record.drive_id, self.record.node_id, path)
        local_hash = hashing.file_hash(self.root / path)[0]
        if facet_content_hash(item.get("file") or {}) == local_hash:
            size, _ = self._stamp(path)
            self._agree(path, item, local_hash, size)
            return
        report.conflicts.append(Conflict(path=path, reason="created_both"))

    def _folder_id(self, relative: str) -> str:
        """The node of the folder at ``relative``, made on the drive if missing."""
        if not relative:
            return self.record.node_id
        known = self.record.folders.get(relative)
        if known is not None:
            return known
        parent, _, name = relative.rpartition("/")
        parent_id = self._folder_id(parent)
        try:
            made = self._files.create_folder(
                self.record.drive_id, parent_id, name, conflict_behavior="fail"
            )
        except AlkeraHTTPError as exc:
            if not (exc.status == 409 and conflict_code(exc) == "files.exists"):
                raise
            made = self._files.item_under(self.record.drive_id, parent_id, name)
        node_id = str(made["id"])
        self.record.folders[relative] = node_id
        return node_id

    def _agree(self, path: str, item: Mapping[str, Any], content_hash: str, size: int) -> None:
        _, mtime_ns = self._stamp(path)
        self.record.files[path] = CopiedFile(
            node_id=str(item["id"]),
            etag=item_etag(item),
            content_hash=content_hash,
            size=size,
            mtime_ns=mtime_ns,
        )

    # ---- down -------------------------------------------------------------

    def _pull(self, report: SyncReport) -> None:
        # A dirty file is left out of what the pull knows: a differing file it
        # does not know is one it keeps, and it neither moves nor removes it.
        dirty = self._dirty
        known = {
            os.fsencode(path): entry
            for path, entry in self.record.files.items()
            if path not in dirty
        }
        before = {path: entry.content_hash for path, entry in self.record.files.items()}
        summary = pull(
            files=self._files,
            http=self._http,
            root=self.root,
            source="",
            node_id=self.record.node_id,
            drive_id=self.record.drive_id,
            local_changes="keep_edits",
            known=known,
            skip_local_state=True,
            exclude=lambda relative: ignored(_posix(relative)),
        )
        self._absorb(summary, before, report)

    def _absorb(self, summary: PullSummary, before: Mapping[str, str], report: SyncReport) -> None:
        for old, new in summary.moved.items():
            entry = self.record.files.pop(_posix(old), None)
            if entry is not None:
                self.record.files[_posix(new)] = entry
        for gone in summary.removed:
            self.record.files.pop(_posix(gone), None)
            report.removed.append(_posix(gone))
        for relative, agreed in summary.agreed.items():
            path = _posix(relative)
            if before.get(path) != agreed.content_hash and path not in report.uploaded:
                report.downloaded.append(path)
            self.record.files[path] = CopiedFile(
                node_id=agreed.node_id,
                etag=agreed.etag,
                content_hash=agreed.content_hash,
                size=agreed.size,
                mtime_ns=agreed.stamp[1],
            )
        self.record.folders = {
            _posix(relative): node_id for relative, node_id in summary.folder_ids.items()
        }
        report.pending.extend(_posix(path) for path in summary.pending_paths)
        accounted = {conflict.path for conflict in report.conflicts}
        accounted.update(refusal.path for refusal in report.refused)
        # Held for an unsaved buffer, not in conflict: the save says which.
        accounted.update(self._dirty)
        for kept in summary.kept_paths:
            path = _posix(kept)
            if path in accounted:
                continue
            # Kept by the pull without this pass having tried to write it: the
            # drive holds other bytes under a name this copy never agreed.
            reason: ConflictReason = "edited_both" if path in self.record.files else "created_both"
            report.conflicts.append(Conflict(path=path, reason=reason))
        report.downloaded.sort()

    def _refresh_one(self, path: str, report: SyncReport) -> None:
        known = self.record.files.get(path)
        if known is None:
            return
        try:
            item: dict[str, Any] | None = self._files.item(self.record.drive_id, known.node_id)
        except AlkeraHTTPError as exc:
            _raise_if_auth(exc)
            if exc.status != 404:
                raise
            item = None
        held = path in self._dirty or self._edited_on_disk(path)
        if item is None or item.get("trashed"):
            if not held:
                (self.root / path).unlink(missing_ok=True)
                del self.record.files[path]
                report.removed.append(path)
            return
        if not self._still_at(path, item):
            report.needs_full = True
            return
        if item_etag(item) == known.etag or held:
            # Unchanged, or held for an edit: the base stays where it is.
            return
        remote_hash = facet_content_hash(item.get("file") or {})
        if remote_hash and remote_hash == known.content_hash:
            known.etag = item_etag(item)
            return
        part = self.root / f"{path}{PULL_PART_SUFFIX.decode('ascii')}"
        try:
            self._files.download(self.record.drive_id, known.node_id, part)
        except AlkeraHTTPError as exc:
            _raise_if_auth(exc)
            part.unlink(missing_ok=True)
            if exc.status == 409 and conflict_code(exc) == "files.live_pending":
                report.pending.append(path)
                return
            raise
        landed = hashing.file_hash(part)[0]
        if remote_hash and landed != remote_hash:
            # Moved again while it was read: the next frame fetches it.
            part.unlink(missing_ok=True)
            return
        os.replace(part, self.root / path)
        self._agree(path, item, landed, (self.root / path).stat().st_size)
        report.downloaded.append(path)

    def _edited_on_disk(self, path: str) -> bool:
        try:
            info = (self.root / path).stat()
        except FileNotFoundError:
            return False
        return self._edited(path, _LocalFile(size=info.st_size, mtime_ns=info.st_mtime_ns))

    def _still_at(self, path: str, item: Mapping[str, Any]) -> bool:
        """Whether the drive still files ``item`` under ``path``."""
        parent, _, leaf = path.rpartition("/")
        expected = self.record.folders.get(parent) if parent else self.record.node_id
        parent_id = item.get("parentId") or item.get("parent_id")
        return item.get("name") == leaf and expected is not None and parent_id == expected

    # ---- settling a conflict -------------------------------------------

    def _remote(self, path: str) -> dict[str, Any] | None:
        try:
            return self._files.item_under(self.record.drive_id, self.record.node_id, path)
        except AlkeraHTTPError as exc:
            _raise_if_auth(exc)
            if exc.status == 404:
                return None
            raise

    def _keep_mine(self, path: str, report: SyncReport) -> None:
        source = self.root / path
        if not source.is_file():
            return
        remote = self._remote(path)
        if remote is None:
            # Nothing there any more: the sync after this uploads it as new.
            self.record.files.pop(path, None)
            return
        data = source.read_bytes()
        try:
            item = self._files.put_content(
                self.record.drive_id, str(remote["id"]), data, if_match=item_etag(remote)
            )
        except AlkeraHTTPError as exc:
            _raise_if_auth(exc)
            if exc.status != 412:
                report.refused.append(_refusal(path, exc))
            return
        self._agree(path, item, hashing.content_hash(data), len(data))
        report.uploaded.append(path)

    def _keep_theirs(self, path: str, report: SyncReport) -> None:
        """Take the drive's version. The local one is moved aside first, out of
        the synced folder, so choosing wrongly loses nothing."""
        target = self.root / path
        self.record.files.pop(path, None)
        if not target.is_file() or target.is_symlink():
            return
        saved = self._store.backup_dir(self.record) / path
        saved.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(target), str(saved))
        report.backups[path] = str(saved)


def concerns(record: WorkingCopyRecord, frame: Mapping[str, Any]) -> bool:
    """Whether a realtime frame says something under this copy changed.

    A node frame names the changed node and the folder it sits in; a lease
    frame names the leased folder. Either naming a folder or file this copy
    knows (or the chat folder around it) means "look again". A frame naming no
    known id costs nothing, so a busy org does not turn into a sync per frame.
    """
    kind = frame.get("type")
    data = frame.get("data")
    if not isinstance(data, Mapping):
        return False
    watched = {record.node_id, *record.watch_ids, *record.folders.values()}
    watched.update(entry.node_id for entry in record.files.values())
    watched.discard("")
    if kind == "file_node.changed":
        named = (data.get("parent_id"), data.get("entity_id"))
    elif kind == "file_lease.changed":
        named = (data.get("lease_node_id"), data.get("entity_id"))
    else:
        return False
    return any(isinstance(value, str) and value in watched for value in named)
