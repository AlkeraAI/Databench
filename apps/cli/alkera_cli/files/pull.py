"""``alkera files pull`` — materialize an org subtree into a local directory.

The pull is the import half of the round-trip corpus, and it is written around
the three properties the corpus proves:

* **it is contained.** Every byte it writes goes through
  :class:`~alkera_cli.files.target.MaterializationTarget`, which refuses a path
  lexically and then proves it lands beneath the root before any I/O. A stored
  tree cannot make a pull write outside the directory the user named, and a
  symlink the pull wrote a moment ago cannot become a door out;
* **it is idempotent.** A file already on disk is hashed before anything is
  requested: a second pull of an unchanged subtree issues no content request
  and rewrites no byte. Hashing first (rather than trusting mtime) is what
  makes that true after a ``touch`` or a checkout;
* **it resumes.** Bytes land in a ``.alkera-part`` sidecar next to the target
  and are asked for with ``Range: bytes=<already here>-``, so a pull killed
  mid-file asks the server only for the tail. The part is promoted into place
  with :func:`os.replace` once its BLAKE3 matches the version's
  ``contentHash`` — a truncated or corrupted part is never visible under the
  real name.

Order matters and is fixed: folders, then bytes, pointers and specials, then
**symlinks last** (queued by the target and flushed at the end) so no link ever
resolves to a file the pull has not written; then attributes, deepest path
first, so writing into a directory cannot clobber the ``mtime`` just restored
on it.
"""

from __future__ import annotations

import ast
import base64
import contextlib
import os
import stat as stat_module
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal, Protocol

import httpx
from alkera_core.files import names
from alkera_core.files.links import LinkKind, materialize_link
from alkera_core.files.providers.registry import object_web_path
from alkera_core.project import is_local_state

from alkera_cli.files.chat_fs import ChatTree, ChatTreeError
from alkera_cli.files.conflict_staging import conflict_staging_name
from alkera_cli.files.hashing import stream_hash
from alkera_cli.files.progress import Progress
from alkera_cli.files.target import ContainmentError, MaterializationTarget
from alkera_cli.files.wire import conflict_code, facet_content_hash, item_etag

__all__ = [
    "KnownFile",
    "LocalChanges",
    "LocalChangesError",
    "PullSummary",
    "PulledFile",
    "pull",
]

#: What a pull does with a file already on disk whose bytes are not the
#: server's. ``overwrite`` is the plain ``alkera files pull``: the org copy is
#: the truth and the local one is a stale materialization. ``keep`` and
#: ``refuse`` exist because a *mount* re-pulls a directory this machine has been
#: editing, and silently re-materializing over an unsaved edit is data loss.
#:
#: ``keep_unseen`` is for a holder taking its own folder back after its lease
#: lapsed: a local file whose bytes the drive has not seen is kept when the
#: drive still holds exactly what the two last agreed (the edit is this
#: holder's own work that never reached the drive). Where the drive moved too,
#: its bytes take the name and the holder's are set aside under a conflict
#: staging name (``displaced_paths``), which the live sync files on the drive
#: as a conflicted copy: neither side's work is dropped.
#:
#: ``keep_edits`` is for a copy that is NOT the only writer: a reader's working
#: copy of a folder other people and a machine keep writing. A file whose bytes
#: are still the ones the caller last agreed with the drive (``known``) holds no
#: edit, so the drive's newer bytes replace it; only a file that moved away from
#: its agreed bytes, or that the caller never agreed at all, is kept.
LocalChanges = Literal["overwrite", "keep", "refuse", "keep_unseen", "keep_edits"]


class LocalChangesError(RuntimeError):
    """Files under the root differ from the org copy and would be overwritten.

    Raised before a single byte is written, so refusing costs the caller
    nothing: ``paths`` are the relative paths that differ, for the sentence the
    CLI prints.
    """

    def __init__(self, message: str, *, paths: Sequence[bytes]) -> None:
        super().__init__(message)
        self.paths: tuple[bytes, ...] = tuple(paths)


#: How much of a download is held in memory at a time.
_STREAM_CHUNK: Final = 1 << 20

#: The sidecar a partially downloaded file lands in. Imported rather than
#: spelled here: the server's own name ceiling is ``NAME_MAX`` less this
#: suffix, so the two are one decision and a copy of the literal here could
#: lengthen the sidecar without lowering the ceiling that reserves room for it.
_PART_SUFFIX: Final = names.PULL_PART_SUFFIX
PENDING_CODE: Final = "files.live_pending"
"""The content route's answer for a file whose bytes its writer has not synced."""

#: The wire's symlink vocabulary, back to the stored classification.
#:
#: `SymlinkFacet` 2.0.0 spells the three classes exactly as the tree stores
#: them, so the current names map through unchanged. The 1.0.0 spellings stay
#: because the facet's migration runs on the *server*: a pull reads the raw
#: JSON and gets whatever vocabulary the server it is talking to serializes,
#: and an unknown name falls back to `relative` — the kind whose text means the
#: same thing everywhere, and so the only safe thing to assume.
_LINK_KINDS: Final[dict[str, LinkKind]] = {
    "relative": LinkKind.RELATIVE,
    "canonical": LinkKind.CANONICAL,
    "host": LinkKind.HOST,
    "internal": LinkKind.RELATIVE,
    "external": LinkKind.CANONICAL,
    "absolute": LinkKind.HOST,
}


class FilesApi(Protocol):
    """The slice of the SDK's ``client.files`` namespace a pull drives."""

    def drive(self) -> dict[str, Any]: ...

    def item(self, drive_id: str, item_id: str, *, select: str | None = ...) -> dict[str, Any]: ...

    def item_by_path(self, drive_id: str, item_path: str) -> dict[str, Any]: ...

    def children(
        self,
        drive_id: str,
        item_id: str,
        *,
        limit: int = ...,
        order_by: str | None = ...,
        filters: Mapping[str, Any] | None = ...,
    ) -> Iterator[dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class PulledFile:
    """A file this pull left on disk holding exactly the drive's bytes.

    What the drive holds for it — the node, the etag it was read at, the
    whole-file hash and size — and the ``(size, mtime_ns)`` the file wore once
    the pull was done with it. A later reader that finds the same stamp on
    disk knows the bytes are still the drive's without reading them, and
    without asking the drive again.
    """

    node_id: str
    etag: str
    content_hash: str
    size: int
    stamp: tuple[int, int]


@dataclass
class PullSummary:
    """What one pull did, in the terms the CLI prints and a test asserts."""

    folders: int = 0
    files: int = 0
    unchanged: int = 0
    """Files already on disk with the server's bytes — nothing requested."""
    symlinks: int = 0
    specials: int = 0
    pointers: int = 0
    deduped: int = 0
    """Files filled from a twin already on disk — one download for the group."""
    bytes_downloaded: int = 0
    resumed: int = 0
    """Downloads that continued a ``.alkera-part`` rather than starting over."""
    kept: int = 0
    """Files left exactly as they are because they hold unsaved local edits."""
    kept_paths: list[bytes] = field(default_factory=list)
    undownloadable: int = 0
    """Subtrees the server says may not leave the platform — never descended."""
    unpullable: int = 0
    """Nodes whose stored name no machine can hold — never descended.

    Only a name the drive took before the ceiling was lowered to leave the
    sidecar room. Nothing renames it, so the pull says which node it is and
    keeps going rather than dying on it.
    """
    pending: int = 0
    """Files the drive lists whose bytes the machine writing them has not
    synced yet — left for a later pull, named in ``pending_paths``."""
    pending_paths: list[bytes] = field(default_factory=list)
    links_skipped: int = 0
    """Links the drive holds that a chat tree refuses to make: a target that is
    absolute or climbs above the root, which a daemon writing as root must
    not plant in a tree the sandbox reads. Named in ``links_skipped_paths``
    and explained in ``warnings``."""
    links_skipped_paths: list[bytes] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    agreed: dict[bytes, PulledFile] = field(default_factory=dict)
    """Every plain file the pull proved holds the drive's bytes — found so on
    disk, downloaded and verified, or filled from a twin — by relative path.
    A file kept for its local edits is not in it: those bytes are not the
    drive's."""
    moved: dict[bytes, bytes] = field(default_factory=dict)
    """Files recognised by their node under a name the drive has since given
    them, and moved there on disk rather than fetched again: old path to new."""
    removed: list[bytes] = field(default_factory=list)
    """Files whose node the drive has trashed, removed here as the drive did."""
    displaced_paths: list[bytes] = field(default_factory=list)
    """Where the holder's own unsent bytes of a file the drive also changed
    were set aside (``keep_unseen``), as conflict staging names beside it."""
    folder_ids: dict[bytes, str] = field(default_factory=dict)
    """The node id of every folder the pull made or found, by relative path."""


class KnownFile(Protocol):
    """What an earlier holder knew about one file: the node it is and the
    bytes it and the drive last agreed on."""

    @property
    def node_id(self) -> str: ...

    @property
    def content_hash(self) -> str: ...

    @property
    def size(self) -> int: ...


@dataclass(frozen=True, slots=True)
class _Node:
    """One item of the subtree, with the relative path it materializes at."""

    relative: bytes
    item: Mapping[str, Any]


def _holds(target: MaterializationTarget, relative: bytes, content_hash: str, size: int) -> bool:
    """Whether the file at ``relative`` holds exactly ``content_hash``.

    The size is asked first, and nothing is read when it already answers: a
    take of a folder holding a 20 GB sparse file the drive has no bytes for
    read all twenty gigabytes, on every take, to learn what the size said.
    A file with no hash to compare against is never read either."""
    if not content_hash:
        return False
    found = target.lstat(relative)
    if found is None or found.st_size != size:
        return False
    return _blake3_of(target, relative) == (content_hash, size)


def _blake3_of(target: MaterializationTarget, relative: bytes) -> tuple[str, int]:
    """The BLAKE3 and size of the file at ``relative``, read through the
    target (so through the chat tree when the folder is a chat's)."""
    with target.open_read(relative) as handle:
        return stream_hash(handle)


def _name_bytes(item: Mapping[str, Any]) -> bytes:
    """The real name bytes behind the item's displayed name.

    ``display`` is invertible by contract, so a Latin-1 archive name that
    reached the server survives the trip back rather than arriving as its own
    escape text.
    """
    raw = str(item.get("name") or "")
    try:
        return names.parse_display(raw)
    except ValueError:
        return os.fsencode(raw)


def _xattr_bytes(value: str) -> bytes:
    """The bytes behind one wire xattr value.

    The item serializer currently spells a stored value with ``str(value)``,
    which for ``bytes`` is its Python repr; base64 is the shape the attrs
    schema documents. Both are accepted so a pull restores the same bytes the
    push read, whichever spelling the server sends.
    """
    if value.startswith(("b'", 'b"')):
        with contextlib.suppress(ValueError, SyntaxError):
            literal = ast.literal_eval(value)
            if isinstance(literal, bytes):
                return literal
    with contextlib.suppress(ValueError):
        return base64.b64decode(value.encode("ascii"), validate=True)
    return value.encode("utf-8", "surrogateescape")


def _mtime_ns(attrs: Mapping[str, Any]) -> int | None:
    """The item's mtime in nanoseconds, from either spelling of the facet."""
    raw_ns = attrs.get("mtimeNs")
    if isinstance(raw_ns, int):
        return raw_ns
    raw = attrs.get("mtime")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return int(moment.timestamp() * 1_000_000_000)


def _may_download(item: Mapping[str, Any]) -> bool:
    """Whether the server says this node's bytes may leave the platform.

    A chat folder carries ``NO_DOWNLOAD``: the item reads fine, its content
    route refuses, and a pull that walked in anyway would write onto a laptop
    exactly the bytes the platform just said may not go there. The capability
    is the whole answer — it is already the AND of ``READ`` and ``EXPORT``, so
    a link that may list but not export refuses here too.

    A node with no capability map at all is an older server that does not
    answer the question, and the pull reads that as a yes: only an explicit
    refusal skips, so this can never silently stop copying an ordinary tree.
    """
    capabilities = item.get("capabilities")
    if not isinstance(capabilities, Mapping):
        return True
    for key in ("can_download", "canDownload"):
        value = capabilities.get(key)
        if isinstance(value, bool):
            return value
    return True


def _bytes_pending(exc: httpx.HTTPStatusError) -> bool:
    """Whether the content route said this file's bytes have not landed yet.

    Only that answer (``409 files.live_pending``): any other refusal of one
    file's bytes ends the pull as it always did, because reading a 403 or a 500
    as "not yet" would leave a silent hole where the caller expects a copy.
    """
    return exc.response.status_code == 409 and conflict_code(exc) == PENDING_CODE


def _is_special(item: Mapping[str, Any]) -> bool:
    """Whether a wire node is a fifo, socket or device node.

    ``special`` is its own item kind on the wire — the serializer never folds
    one into ``file`` — so the kind is the signal. A special carries no
    version, so mistaking one for a file asks the content route for bytes that
    were never written and the whole pull dies on that node's 404.
    """
    return item.get("kind") == "special"


def pull(
    *,
    files: FilesApi,
    http: httpx.Client,
    root: Path,
    source: str,
    node_id: str | None = None,
    drive_id: str | None = None,
    local_root: bytes | None = None,
    trusted: bool = False,
    base: str = "/api/v1/files",
    local_changes: LocalChanges = "overwrite",
    skip_local_state: bool = False,
    progress: Progress | None = None,
    known: Mapping[bytes, KnownFile] | None = None,
    only: bytes | None = None,
    chat_id: str | None = None,
    exclude: Callable[[bytes], bool] | None = None,
) -> PullSummary:
    """Materialize the org subtree ``source`` into ``root`` and report it.

    ``chat_id`` names the chat when ``root`` is its folder on a box. The
    folder is first given back to the chat's sandbox (:meth:`ChatTree.repair`:
    what an older daemon left root-owned or read-only becomes the sandbox's,
    with modes it can write), and every byte, move and delete of the pull then
    goes through the chat's tree (see :class:`MaterializationTarget`).

    ``local_root`` is the org tree's mount point on this machine and defaults
    to ``root``: it is what a canonical link target is rewritten against, the
    exact inverse of the classification the push did on the other machine.

    ``local_changes`` says what to do about a file already on disk whose bytes
    are not the server's — see :data:`LocalChanges`. Both non-default modes are
    decided in one pre-flight pass *before* any byte is written, so ``refuse``
    leaves the directory untouched and ``keep`` cannot half-materialize a tree.

    ``node_id`` names the subtree's root outright, for a caller that already
    knows which node it wants. It wins over ``source``, which then only says
    what the tree is called: a path is a NAME, and the node it resolves to is
    whatever sits under that name at this instant, so a caller holding the id
    would otherwise copy down a stranger's folder the moment the two disagreed.

    ``drive_id`` names the drive the subtree is on, for a caller that was
    handed it beside the node (a mount's record). Without it the drive is
    asked for as the caller's own — a person's org drive; nothing at all for a
    box on its machine credential, which serves chats in orgs it is no member
    of and is answered that it has no drive.

    ``known`` is what an earlier holder of this directory knew about its files,
    by relative path (:func:`_recognise`): with it, a file the drive renamed
    or trashed meanwhile is moved or removed here before anything is fetched,
    instead of arriving as a second copy beside the one already on disk.

    ``only`` keeps the walk to one child of the root and what is under it: a
    box holding a workspace's folder brings down the shared ``files/`` tree,
    and each chat's records under ``.chats/`` are brought down by that chat's
    own lease, never twice into two places.

    ``exclude`` names relative paths the caller never carries (a working copy's
    ignore policy). Such a node is neither materialized nor descended, so a
    path the caller would never send back is never brought down either.
    """
    summary = PullSummary()
    if drive_id is None:
        drive_id = str(files.drive()["id"])
    top = (
        files.item(drive_id, node_id)
        if node_id
        else files.item_by_path(drive_id, source.strip("/"))
    )

    skipped: list[_Node] = []
    unpullable: list[_Node] = []
    nodes: list[_Node] = []
    if not _may_download(top):
        # The root the caller named is itself undownloadable. Nothing under it
        # is reachable either, so the pull writes nothing and says why rather
        # than creating an empty directory that looks like a finished copy.
        skipped.append(_Node(relative=_name_bytes(top), item=top))
    elif _unholdable_name(_name_bytes(top)) is not None:
        # And the same for a root this machine could not name: the walk never
        # sees the root, so the one check it skips is the one a person reaches
        # by asking for the folder they were just told stayed behind.
        unpullable.append(_Node(relative=_name_bytes(top), item=top))
    else:
        nodes = list(
            _subtree(
                files,
                drive_id,
                top,
                skipped,
                unpullable,
                skip_local_state=skip_local_state,
                only=only,
                exclude=exclude,
            )
        )
    for refused in skipped:
        summary.undownloadable += 1
        summary.warnings.append(
            f"{os.fsdecode(refused.relative)} cannot be downloaded, so it and "
            "everything inside it stayed on the server"
        )
    for refused in unpullable:
        summary.unpullable += 1
        summary.warnings.append(
            f"{names.display(refused.relative)} stayed on the server: "
            f"{_unholdable_name(_leaf(refused.relative))}"
        )
    tree = ChatTree.for_chat(root, chat_id) if chat_id else None
    if tree is not None:
        try:
            tree.repair()
        except OSError as failed:
            summary.warnings.append(f"the folder was not given back to its sandbox: {failed}")
    target = MaterializationTarget(root, local_root=local_root, trusted=trusted, tree=tree)
    if known:
        _recognise(
            files,
            drive_id,
            target,
            nodes,
            known,
            summary,
            withheld=[node.relative for node in [*skipped, *unpullable]],
        )
    keep = _decide_local_changes(target, nodes, local_changes, known=known)
    displace = (
        _unseen_edits(target, nodes, keep, known=known)
        if local_changes == "keep_unseen"
        else frozenset()
    )

    for node in sorted(nodes, key=lambda node: node.relative):
        if node.item.get("kind") == "folder":
            target.mkdir(node.relative)
            summary.folders += 1
            summary.folder_ids[node.relative] = str(node.item["id"])

    seen_content: dict[str, bytes] = {}
    materialized: list[_Node] = []
    pending: set[bytes] = set()
    for node in nodes:
        kind = str(node.item.get("kind") or "file")
        if kind == "folder":
            continue
        if kind == "symlink":
            _queue_link(target, node, summary)
        elif kind == "object":
            _write_pointer(target, node, summary)
        elif _is_special(node.item):
            _make_special(target, node, summary)
        else:
            if node.relative in keep:
                summary.kept += 1
                summary.kept_paths.append(node.relative)
                continue
            if progress is not None:
                progress(node.relative)
            if node.relative in displace:
                _set_aside(target, node.relative, summary)
            try:
                _materialize_file(
                    http=http,
                    base=base,
                    drive_id=drive_id,
                    target=target,
                    node=node,
                    seen_content=seen_content,
                    summary=summary,
                )
            except httpx.HTTPStatusError as refused:
                if not _bytes_pending(refused):
                    raise
                # The drive lists the file, but the machine writing it has not
                # synced its bytes: that one file waits for them and the rest
                # of the tree lands, rather than one unsynced file refusing the
                # whole folder to whoever takes it next.
                pending.add(node.relative)
                summary.pending += 1
                summary.pending_paths.append(node.relative)
                summary.warnings.append(
                    f"{names.display(node.relative)} has not landed yet: the machine "
                    "writing it has not synced its bytes"
                )
                continue
            materialized.append(node)

    target.flush_links()
    for relative, why in target.skipped_links:
        summary.symlinks -= 1
        summary.links_skipped += 1
        summary.links_skipped_paths.append(relative)
        summary.warnings.append(f"{names.display(relative)} was not linked: {why}")

    for node in sorted(nodes, key=lambda node: len(node.relative.split(b"/")), reverse=True):
        if node.relative in pending:
            continue
        _restore_attrs(target, node, summary)
    # Stamped last, once the attributes are back: the mtime a file wears from
    # here on is the one the restore gave it.
    for node in materialized:
        _note_agreed(target, node, summary)
    return summary


def _recognise(
    files: FilesApi,
    drive_id: str,
    target: MaterializationTarget,
    nodes: Sequence[_Node],
    known: Mapping[bytes, KnownFile],
    summary: PullSummary,
    *,
    withheld: Sequence[bytes],
) -> None:
    """Put each known file where the drive files its node now, by node id.

    A path is only a name. A file an earlier holder knew as node N, still
    holding the bytes the two agreed on, IS node N: when the drive lists N
    under another name the file is moved there (so the pull finds it already
    in place and fetches nothing), and when the drive has trashed N the file
    is removed, as the drive's own delete would have removed it from a holder
    that was running. Without this the walk below meets the new name as a file
    to download and the push after it meets the old one as new work, and a
    rename made while the box was down becomes two files.

    Only agreed bytes are touched. A known file whose bytes moved since holds
    work the drive has not seen, and a destination already occupied belongs to
    something else: both are left exactly where they are, to the rules a
    first take follows. Nothing under a subtree the drive withheld from this
    walk is judged by its absence from it.
    """
    listed: dict[str, _Node] = {}
    for node in nodes:
        kind = str(node.item.get("kind") or "file")
        if kind == "file" and not _is_special(node.item):
            listed[str(node.item["id"])] = node
    names_listed = {node.relative for node in nodes}
    for relative, entry in sorted(known.items()):
        if not entry.node_id or not entry.content_hash:
            continue
        if any(relative == hidden or relative.startswith(hidden + b"/") for hidden in withheld):
            continue
        try:
            target.resolve(relative)
        except ContainmentError:
            continue
        if not target.is_regular(relative):
            continue
        drive_node = listed.get(entry.node_id)
        if drive_node is not None and drive_node.relative == relative:
            continue
        if drive_node is None and relative in names_listed:
            # The name is another node's on the drive now; the walk decides it.
            continue
        if not _holds_agreed(target, relative, entry):
            continue
        if drive_node is not None:
            try:
                target.resolve(drive_node.relative)
            except ContainmentError:
                continue
            if target.lstat(drive_node.relative) is not None:
                continue
            target.replace(relative, drive_node.relative)
            summary.moved[relative] = drive_node.relative
        else:
            if not _trashed(files, drive_id, entry.node_id):
                continue
            target.unlink(relative)
            summary.removed.append(relative)
        _prune_emptied(target, relative.rpartition(b"/")[0])


def _holds_agreed(target: MaterializationTarget, relative: bytes, entry: KnownFile) -> bool:
    try:
        return _holds(target, relative, entry.content_hash, entry.size)
    except (OSError, ContainmentError):
        return False


def _trashed(files: FilesApi, drive_id: str, node_id: str) -> bool:
    """Whether the drive says ``node_id`` is gone: trashed, or not there at all.

    Any other answer — a node that merely moved out of this folder, or a read
    that failed — is not a delete, and the file stays.
    """
    try:
        item = files.item(drive_id, node_id)
    except Exception as refused:
        status = getattr(refused, "status", None) or getattr(
            getattr(refused, "response", None), "status_code", None
        )
        return status == 404
    return bool(item.get("trashed"))


def _prune_emptied(target: MaterializationTarget, directory: bytes) -> None:
    """Remove the directories a move or a removal left empty, up to the root.

    A folder the drive still lists is made again by the walk; one it does not
    would otherwise be pushed back as a folder nobody has. ``directory`` is
    relative to the root; the root itself is never removed.
    """
    current = directory
    while current:
        try:
            target.rmdir(current)
        except (OSError, ContainmentError):
            return
        current = current.rpartition(b"/")[0]


def _note_agreed(target: MaterializationTarget, node: _Node, summary: PullSummary) -> None:
    """Record that ``node``'s bytes on disk are the drive's, with its stamp.

    Only a node whose read carried a hash: without one the pull could not
    tell, and "I cannot tell" is never "they agree".
    """
    facet = node.item.get("file") or {}
    content_hash = facet_content_hash(facet)
    if content_hash is None:
        return
    info = target.lstat(node.relative)
    size = int(facet.get("size") or 0)
    if info is None or not stat_module.S_ISREG(info.st_mode) or info.st_size != size:
        return
    summary.agreed[node.relative] = PulledFile(
        node_id=str(node.item["id"]),
        etag=item_etag(node.item),
        content_hash=content_hash,
        size=size,
        stamp=(info.st_size, info.st_mtime_ns),
    )


def _decide_local_changes(
    target: MaterializationTarget,
    nodes: Sequence[_Node],
    policy: LocalChanges,
    *,
    known: Mapping[bytes, KnownFile] | None = None,
) -> frozenset[bytes]:
    """The paths this pull must not write, or the refusal, decided up front.

    A plain pull overwrites and never pays for this walk. A mount's re-pull
    does: the org copy cannot have moved while this machine holds the lease, so
    a file on disk whose bytes are not the server's holds an edit nobody has
    saved yet, and re-materializing over it destroys work with no warning.

    ``keep_edits`` narrows that to the files the caller actually changed: a
    differing file that still holds the bytes it last agreed with the drive is
    stale, not edited, and takes the drive's newer bytes.
    """
    if policy == "overwrite":
        return frozenset()
    modified = [node.relative for node in nodes if _differs_on_disk(target, node)]
    if policy == "keep_unseen":
        agreed = {entry.node_id: entry.content_hash for entry in (known or {}).values()}
        return frozenset(
            node.relative
            for node in nodes
            if node.relative in modified
            and agreed.get(str(node.item.get("id"))) == _remote_hash(node)
        )
    if policy == "keep_edits":
        last_agreed = known or {}
        modified = [
            relative
            for relative in modified
            if not _still_agreed(target, relative, last_agreed.get(relative))
        ]
    if policy == "refuse" and modified:
        raise LocalChangesError(
            "these files have local edits the org copy does not have", paths=modified
        )
    return frozenset(modified)


def _unseen_edits(
    target: MaterializationTarget,
    nodes: Sequence[_Node],
    keep: frozenset[bytes],
    *,
    known: Mapping[bytes, KnownFile] | None,
) -> frozenset[bytes]:
    """The files a ``keep_unseen`` pull would write over the holder's own work.

    Every file that differs from the drive and is not kept moved on the drive.
    It holds the holder's work too unless its bytes are still the ones the two
    last agreed (then it is merely behind): a file whose agreed bytes nobody
    knows, or that moved away from them, is an edit the drive never saw.
    """
    agreed = {entry.node_id: entry for entry in (known or {}).values()}
    return frozenset(
        node.relative
        for node in nodes
        if node.relative not in keep
        and _differs_on_disk(target, node)
        and not _still_agreed(target, node.relative, agreed.get(str(node.item.get("id"))))
    )


def _set_aside(target: MaterializationTarget, relative: bytes, summary: PullSummary) -> None:
    """Move the holder's bytes at ``relative`` to a conflict staging name
    beside it, for the live sync to file as a conflicted copy of the node."""
    parent, _, name = relative.rpartition(b"/")
    staged_name = conflict_staging_name(os.fsdecode(name), uuid.uuid4().hex).encode()
    staged = parent + b"/" + staged_name if parent else staged_name
    target.replace(relative, staged)
    summary.displaced_paths.append(staged)


def _remote_hash(node: _Node) -> str | None:
    return facet_content_hash(node.item.get("file") or {})


def _still_agreed(target: MaterializationTarget, relative: bytes, entry: KnownFile | None) -> bool:
    """Whether the file at ``relative`` holds exactly the bytes ``entry`` agreed."""
    if entry is None or not entry.content_hash:
        return False
    return _holds_agreed(target, relative, entry)


def _differs_on_disk(target: MaterializationTarget, node: _Node) -> bool:
    """Whether ``node`` is a plain file whose local bytes are not the server's."""
    kind = str(node.item.get("kind") or "file")
    if kind in {"folder", "symlink", "object"} or _is_special(node.item):
        return False
    try:
        target.resolve(node.relative)
    except ContainmentError:
        return False
    if not target.is_regular(node.relative):
        return False
    facet = node.item.get("file") or {}
    remote_hash = facet_content_hash(facet)
    if remote_hash is None:
        # The read carried no hash — an older server, or a facet with no head
        # version joined. "I cannot tell" is not "it differs": claiming an edit
        # here would make `keep` refuse to write a file nobody has touched, and
        # `refuse` abort the whole pull on a tree that is identical.
        return False
    return not _holds(target, node.relative, remote_hash, int(facet.get("size") or 0))


def _leaf(relative: bytes) -> bytes:
    """The last path component of ``relative`` — the node's own name."""
    return relative.rpartition(b"/")[2]


def _unholdable_name(name: bytes) -> str | None:
    """Why this stored name cannot be written to disk, or ``None`` when it can.

    The drive refuses a name over :data:`~alkera_core.files.names.NAME_MAX_BYTES`
    precisely so the sidecar beside it still fits, but a row created before
    that ceiling was lowered is still in the tree and is never migrated or
    renamed. Reached here, it would take the kernel's ``ENAMETOOLONG`` halfway
    through a download — with nothing naming the file. So the walk asks first,
    and the answer is the sentence the summary prints.
    """
    if len(name) + len(_PART_SUFFIX) <= names.FS_NAME_MAX_BYTES:
        return None
    return (
        f"its name is {len(name)} bytes and a name this machine can hold is at "
        f"most {names.FS_NAME_MAX_BYTES - len(_PART_SUFFIX)}; rename it to "
        "something shorter and pull again"
    )


def _subtree(
    files: FilesApi,
    drive_id: str,
    top: Mapping[str, Any],
    skipped: list[_Node] | None = None,
    unpullable: list[_Node] | None = None,
    *,
    skip_local_state: bool = False,
    only: bytes | None = None,
    exclude: Callable[[bytes], bool] | None = None,
) -> Iterator[_Node]:
    """Every descendant of ``top``, parents before children, in byte order.

    The walk is iterative so a ``node_modules``-deep tree cannot exhaust the
    interpreter's stack, and the root itself is not yielded: it is the
    directory the caller named, not a child of it.

    A node the server will not let this caller download is not yielded **and
    not descended** — it lands in ``skipped`` instead. Pruning here rather than
    filtering afterwards is the point: a chat's working directory is asked for
    only if the chat folder itself was downloadable, so nothing under an
    undownloadable node is ever requested, listed or written.
    """
    frontier: list[tuple[bytes, Mapping[str, Any]]] = [(b"", top)]
    while frontier:
        prefix, item = frontier.pop(0)
        children = sorted(files.children(drive_id, str(item["id"])), key=_name_bytes)
        for child in children:
            name = _name_bytes(child)
            relative = prefix + b"/" + name if prefix else name
            if only is not None and not prefix and name != only:
                continue
            if skip_local_state and is_local_state(relative):
                # Not a refusal and not reported as one: this copy will make
                # its own, and a stranger's is inert at best.
                continue
            if exclude is not None and exclude(relative):
                continue
            if not _may_download(child):
                if skipped is not None:
                    skipped.append(_Node(relative=relative, item=child))
                continue
            if _unholdable_name(name) is not None:
                # Not descended either: a folder this machine cannot name has
                # no path for its children to hang off.
                if unpullable is not None:
                    unpullable.append(_Node(relative=relative, item=child))
                continue
            yield _Node(relative=relative, item=child)
            if child.get("kind") == "folder":
                frontier.append((relative, child))


def _queue_link(target: MaterializationTarget, node: _Node, summary: PullSummary) -> None:
    facet = node.item.get("symlink") or {}
    raw = str(facet.get("target") or "")
    try:
        stored = names.parse_display(raw)
    except ValueError:
        stored = raw.encode("utf-8", "surrogateescape")
    kind = _LINK_KINDS.get(str(facet.get("kind") or "relative"), LinkKind.RELATIVE)
    if _link_already_says(target, node.relative, kind, stored):
        summary.unchanged += 1
        return
    target.write_symlink(node.relative, kind, stored)
    summary.symlinks += 1


def _link_already_says(
    target: MaterializationTarget, relative: bytes, kind: LinkKind, stored: bytes
) -> bool:
    """Whether the link on disk already reads exactly what this pull would write.

    Idempotence, the same property the file path gets by hashing first: a
    second pull of an unchanged subtree must leave the tree alone. Without it
    a re-pull does not merely rewrite the link, it *fails* — containment
    refuses a path whose own last component is a symlink, which is what makes a
    symlink the pull wrote a moment ago unusable as a door out, and the leaf of
    a link is exactly that component.

    So the parent is resolved (every component of it still proven to be a real
    directory beneath the root) and the leaf appended without walking through
    it; ``os.readlink`` reads the link's own text and never follows it.
    """
    try:
        target.resolve_leaf(relative)
    except ContainmentError:
        return False
    wanted = materialize_link(kind, stored, target.local_root)
    if target.tree is not None:
        # The tree writes a link in its own spelling (an absolute target under
        # the root becomes relative) and refuses one that points outside; what
        # it refuses is never what is on disk, and is skipped again below.
        try:
            wanted = target.tree.link_text(relative, wanted)
        except ChatTreeError:
            return False
    return target.readlink(relative) == wanted


def _facet_url(facet: Mapping[str, Any], *keys: str) -> str | None:
    """The first of ``keys`` the facet actually carries, as a non-empty string."""
    for key in keys:
        value = facet.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _web_url(facet: Mapping[str, Any], object_type: str, object_id: str) -> str | None:
    """Where this object opens, derived from the registry both sides read.

    The wire facet is computed with :func:`object_web_path` as well, so
    deriving the path here rather than copying a key off the item means the
    document on disk and the item in the drive cannot name two different pages
    for one object — and a pointer is not left with a null address when the
    server spells the facet a way this pull does not know, or omits it.
    """
    if object_type and object_id:
        return object_web_path(object_type, object_id)
    return _facet_url(facet, "web_url", "webUrl")


def _write_pointer(target: MaterializationTarget, node: _Node, summary: PullSummary) -> None:
    """Refresh the ``.alkera-<kind>`` file that stands in for a row-backed object.

    A pointer is derived state, so it is rewritten from the item on every pull
    rather than compared: whatever is on disk is stale by definition.
    """
    facet = node.item.get("object") or {}
    object_type = str(facet.get("type") or "")
    object_id = str(facet.get("id") or "")
    payload = {
        "kind": object_type,
        "nodeId": str(node.item.get("id") or ""),
        "webUrl": _web_url(facet, object_type, object_id),
        "appUrl": _facet_url(facet, "app_url", "appUrl"),
        "schemaVersion": "1.0.0",
    }
    target.write_pointer(node.relative, payload)
    summary.pointers += 1


def _make_special(target: MaterializationTarget, node: _Node, summary: PullSummary) -> None:
    """Recreate a fifo where the platform allows one; report it where it does not.

    Only fifos are ever created: a device node on a machine the user does not
    administer is refused by the kernel anyway, and creating one on a shared
    box is exactly what the materialization rules forbid.
    """
    target.resolve(node.relative)
    if target.lstat(node.relative) is not None:
        summary.specials += 1
        return
    try:
        target.make_fifo(node.relative)
    except (OSError, AttributeError) as exc:
        summary.warnings.append(
            f"could not recreate the special {os.fsdecode(node.relative)}: {exc}"
        )
        return
    summary.specials += 1


def _materialize_file(
    *,
    http: httpx.Client,
    base: str,
    drive_id: str,
    target: MaterializationTarget,
    node: _Node,
    seen_content: dict[str, bytes],
    summary: PullSummary,
) -> None:
    """Put one file's bytes on disk, downloading only what is not there."""
    facet = node.item.get("file") or {}
    content_hash = facet_content_hash(facet) or ""
    size = int(facet.get("size") or 0)
    target.resolve(node.relative)

    if target.is_regular(node.relative) and _holds(target, node.relative, content_hash, size):
        summary.unchanged += 1
        seen_content.setdefault(content_hash, node.relative)
        return

    twin = seen_content.get(content_hash) if content_hash else None
    if twin is not None:
        # Two nodes with the same bytes inside one pull: take the second from
        # the copy already on disk rather than asking for the bytes again. That
        # is what makes a pnpm-style store, and a pushed hard-link group, cost
        # one download instead of one per member.
        _copy_inside_root(target, twin, node.relative, summary)
        return

    _download(
        http=http,
        base=base,
        drive_id=drive_id,
        item_id=str(node.item["id"]),
        target=target,
        relative=node.relative,
        content_hash=content_hash,
        summary=summary,
    )
    summary.files += 1
    if content_hash:
        seen_content[content_hash] = node.relative


def _copy_inside_root(
    target: MaterializationTarget, existing: bytes, relative: bytes, summary: PullSummary
) -> None:
    """Fill ``relative`` from the twin already materialized at ``existing``.

    A copy and not a hard link, even though the bytes are identical and a link
    would be free. Two files that merely *happen* to share content are still
    two files: a hard link fuses them into one inode, so restoring the second's
    mtime or mode would silently rewrite the first's, and a later edit to
    either would appear in both. Sharing an inode is only correct when the
    server says they are one file, which a content hash does not say — so the
    dedup saves the download and nothing else.
    """
    target.copy(existing, relative)
    summary.deduped += 1


def _download(
    *,
    http: httpx.Client,
    base: str,
    drive_id: str,
    item_id: str,
    target: MaterializationTarget,
    relative: bytes,
    content_hash: str,
    summary: PullSummary,
) -> None:
    """Stream one version's bytes into place, resuming a partial download.

    The part file is promoted only once its hash matches what the server said
    it committed, so a truncated body or a flipped bit never appears under the
    real name.
    """
    part = relative + _PART_SUFFIX
    target.resolve(part)
    found = target.lstat(part)
    already = found.st_size if found is not None and stat_module.S_ISREG(found.st_mode) else 0
    if already:
        summary.resumed += 1
    for attempt in (already, 0):
        summary.bytes_downloaded += _fetch(
            http=http,
            base=base,
            drive_id=drive_id,
            item_id=item_id,
            target=target,
            part=part,
            start=attempt,
        )
        if not content_hash or _blake3_of(target, part)[0] == content_hash:
            target.replace(part, relative)
            return
        # A resumed part that does not verify was built on stale bytes: throw
        # it away and take the whole object once, rather than looping.
        target.unlink(part)
    raise RuntimeError(f"alkera files pull: {os.fsdecode(relative)} did not match its content hash")


def _fetch(
    *,
    http: httpx.Client,
    base: str,
    drive_id: str,
    item_id: str,
    target: MaterializationTarget,
    part: bytes,
    start: int,
) -> int:
    """Append the object from ``start`` onwards into ``part``; return the bytes."""
    headers = {"Range": f"bytes={start}-"} if start else {}
    response = http.get(
        f"{base}/drives/{drive_id}/items/{item_id}/content",
        headers=headers,
        follow_redirects=False,
    )
    if response.status_code not in (301, 302, 303, 307, 308):
        response.raise_for_status()
        raise RuntimeError("alkera files pull: the content route did not redirect")
    location = response.headers.get("location")
    if not location:
        raise RuntimeError("alkera files pull: the content redirect carried no Location")

    append = bool(start)
    written = 0
    with http.stream("GET", location, headers=headers) as streamed:
        if streamed.status_code >= 400:
            streamed.read()
            streamed.raise_for_status()
        # A server that answers 200 to a ranged request sent the whole object,
        # so the part must be rewritten rather than appended to.
        if start and streamed.status_code != 206:
            append = False
        with target.open_write(part, append=append) as handle:
            for chunk in streamed.iter_bytes(_STREAM_CHUNK):
                handle.write(chunk)
                written += len(chunk)
    return written


def _restore_attrs(target: MaterializationTarget, node: _Node, summary: PullSummary) -> None:
    """Apply mode, mtime and ``user.*`` xattrs to one materialized path."""
    if node.item.get("kind") in ("symlink", "folder"):
        # A link carries the mode of the link itself, which POSIX ignores, and
        # `lchmod` does not exist on Linux; resolving a path whose last
        # component is a symlink is refused by the target anyway.
        #
        # A folder is skipped for a different reason: `push` creates the whole
        # skeleton with one `tree` call and never PATCHes a directory, so the
        # mode and mtime the wire reports for one are the server's defaults,
        # not the user's tree. Applying them would chmod a directory to
        # something the user never chose — and a directory without its execute
        # bit is one nothing beneath it can be read through.
        return
    attrs = node.item.get("attrs") or {}
    if not attrs:
        return
    mode = attrs.get("mode")
    xattrs = {
        os.fsencode(str(name)): _xattr_bytes(str(value))
        for name, value in (attrs.get("xattrs") or {}).items()
    }
    try:
        target.set_attrs(
            node.relative,
            mode=stat_module.S_IMODE(int(mode)) if isinstance(mode, int) and mode else None,
            mtime_ns=_mtime_ns(attrs),
            xattrs=xattrs or None,
        )
    except OSError as exc:
        summary.warnings.append(
            f"could not restore attributes on {os.fsdecode(node.relative)}: {exc}"
        )
