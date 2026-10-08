"""Which node each file of a held folder IS, kept across the holder's restarts.

A path is only what a node is called at the moment. While a box holds a folder
the live sync knows which node every file is, but only in memory. Without a
record, a box that restarts after somebody renamed a file on the drive would
meet the new name as a stranger to download and the old name as new work to
push: two files where the person renamed one. The map written here is what lets the
next process recognise a file by the node it is rather than the name it had.

One JSON file per mount, beside the mount record (and named so the record
listing never reads it as one), written 0600 and atomically. Keyed by the
path under the mount's root, the pull's own coordinates; a live sync that
watches a directory inside the folder reads and writes its part of the map
through :class:`NodeMapStore`.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Final

from alkera_core.atomic_io import write_text_atomic
from alkera_core.versioning import VersionedModel
from pydantic import BaseModel, ConfigDict, Field

from alkera_cli.files.mount import mount_record_path
from alkera_cli.files.pull import PulledFile, PullSummary
from alkera_cli.files.push import AgreedBase

__all__ = [
    "KnownNode",
    "NodeMap",
    "NodeMapStore",
    "agreed_bases",
    "known_for_pull",
    "load_node_map",
    "node_map_path",
    "remember_pull",
    "restore_agreements",
    "save_node_map",
    "under_working_dir",
]

logger = logging.getLogger(__name__)

_SUFFIX: Final = ".nodes"


class KnownNode(BaseModel):
    """One file's identity, and the bytes the holder and the drive agreed on."""

    model_config = ConfigDict(extra="allow")

    node_id: str = ""
    size: int = 0
    content_hash: str = ""
    """The whole-file BLAKE3 both sides held; empty when never agreed."""
    etag: str = ""
    etag_hash: str = ""
    """The content hash of the bytes the holder uploaded and the drive filed
    at ``etag``, while the file on disk still descends from them: what lets a
    restarted holder name that version as the one its file was made on.
    Empty when the drive never confirmed ``content_hash`` at ``etag`` (an
    unanswered upload), when the bytes were written onto the disk (a pull, a
    write back) rather than uploaded from it, when the box's text peer has
    written or sent the file since, or in a map from before the field."""


class NodeMap(VersionedModel):
    """Every file of one held folder the holder knew the node of."""

    SCHEMA_VERSION: ClassVar[str] = "1.1.0"

    files: dict[str, KnownNode] = Field(default_factory=dict)
    """By path under the mount's root, ``/``-separated."""


def node_map_path(root: Path, *, home: Path | None = None) -> Path:
    """Where the node map of the mount at ``root`` lives: beside its record,
    under a suffix the record listing (``*.json``) does not match."""
    return mount_record_path(root, home=home).with_suffix(_SUFFIX)


def load_node_map(root: Path, *, home: Path | None = None) -> NodeMap | None:
    """The map this directory's earlier holders left, or ``None``.

    An unreadable map is no map: the take then behaves exactly as a first take
    does, which loses nothing but the renames it would have recognised.
    """
    try:
        raw = json.loads(node_map_path(root, home=home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        return NodeMap.model_validate(raw)
    except ValueError:
        return None


def save_node_map(root: Path, node_map: NodeMap, *, home: Path | None = None) -> bool:
    """Write the map 0600 and atomically; answer whether it was written.

    The map is a net under the holder, never a gate on it: a disk that refuses
    the write costs the next restart its recognition of renames, and nothing
    the holder is doing now.
    """
    path = node_map_path(root, home=home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(path, node_map.model_dump_json(), mode=0o600)
    except OSError as failed:
        logger.warning("the node map of %s was not written (%s)", root, failed)
        return False
    return True


def known_for_pull(node_map: NodeMap | None) -> dict[bytes, KnownNode] | None:
    """The map in the pull's own coordinates, or ``None`` for a first take."""
    if node_map is None:
        return None
    return {os.fsencode(path): known for path, known in node_map.files.items()}


def agreed_bases(node_map: NodeMap | None, inside: str | None) -> dict[str, AgreedBase]:
    """Each watched file's agreed bytes and the etag the drive held them at,
    by root-relative path: the bases a push fences its writes on, so a write
    over a head somebody else moved keeps their version as a conflicted copy
    instead of replacing it (with no base a push fences on the etag it just
    read, which always matches).

    ``inside`` is the watched directory, ``None`` when no live sync runs.
    Only that directory, and only while its sync runs: the sync re-agrees its
    part of the map after every write it makes or takes. The rest of a folder
    (a box's own records) is re-agreed only by a pull, so a base there would be
    stale after the holder's own first push and mint a copy of its own work.
    """
    if inside is None or node_map is None:
        return {}
    prefix = f"{inside}/" if inside else ""
    return {
        path: AgreedBase(etag=known.etag, content_hash=known.content_hash)
        for path, known in node_map.files.items()
        if path.startswith(prefix) and known.etag and known.content_hash
    }


def remember_pull(
    root: Path, before: NodeMap | None, pulled: PullSummary, *, home: Path | None = None
) -> NodeMap:
    """Write down what a take left on disk, and answer it.

    What was known before carries forward under the names the pull moved it to,
    less what it removed and what is no longer on disk; every file the pull
    proved holds the drive's bytes is known afresh from the drive's answer. A
    file kept for its local edits keeps the node an earlier holder knew it as.
    """
    files: dict[str, KnownNode] = dict(before.files) if before is not None else {}
    for old, new in pulled.moved.items():
        known = files.pop(os.fsdecode(old), None)
        if known is not None:
            files[os.fsdecode(new)] = known
    for gone in pulled.removed:
        files.pop(os.fsdecode(gone), None)
    files = {path: known for path, known in files.items() if (Path(root) / path).is_file()}
    for relative, agreed in pulled.agreed.items():
        files[os.fsdecode(relative)] = KnownNode(
            node_id=agreed.node_id,
            size=agreed.size,
            content_hash=agreed.content_hash,
            etag=agreed.etag,
        )
    remembered = NodeMap(files=files)
    save_node_map(root, remembered, home=home)
    return remembered


@dataclass(frozen=True, slots=True)
class NodeMapStore:
    """One live sync's window onto the mount's map.

    ``inside`` is the step from the mount's root down to the directory the
    sync watches. The sync speaks in paths under that directory; the map keeps
    paths under the mount's root, so what lies outside the watched directory
    (the folder's own records, which the pull maintains) is carried through a
    save untouched.
    """

    mount_root: Path
    inside: str = ""
    home: Path | None = None

    def _prefix(self) -> str:
        step = self.inside.strip("/")
        return f"{step}/" if step else ""

    def load(self) -> dict[str, KnownNode]:
        """The map's entries under the watched directory, by path inside it."""
        found = load_node_map(self.mount_root, home=self.home)
        if found is None:
            return {}
        prefix = self._prefix()
        return {
            path[len(prefix) :]: known
            for path, known in found.files.items()
            if path.startswith(prefix) and path != prefix
        }

    def save(self, files: Mapping[str, KnownNode]) -> None:
        """Replace the watched directory's part of the map with ``files``."""
        prefix = self._prefix()
        existing = load_node_map(self.mount_root, home=self.home)
        kept = (
            {}
            if existing is None or not prefix
            else {
                path: known for path, known in existing.files.items() if not path.startswith(prefix)
            }
        )
        kept.update({f"{prefix}{path}": known for path, known in files.items()})
        save_node_map(self.mount_root, NodeMap(files=kept), home=self.home)


def restore_agreements(
    remembered: Mapping[str, KnownNode],
    *,
    root: Path,
    hasher: Callable[[Path], tuple[str, int]],
    stamp_of: Callable[[Path], tuple[int, int] | None],
    nodes: dict[str, str],
    known: dict[str, tuple[str, int]],
    etags: dict[str, str],
    stamps: dict[str, tuple[int, int]],
    etag_hashes: dict[str, str] | None = None,
) -> int:
    """Take back what an earlier process wrote down about the files still on
    disk: each one's node, and the bytes and etag last agreed for it. Fills
    the four maps (a path already in ``nodes`` keeps what it has) and answers
    how many files it took.

    What the two last agreed is the base of whatever the file holds now,
    edited while no sync ran or not: the next upload is fenced on it, never on
    the drive's head, which would claim a version the box never saw (and read
    everything written since as removed). Only a file still holding those
    bytes is taken as agreed outright (its stamp recorded). ``etag_hashes``
    takes back which etags still name the version the file was made on."""
    taken = 0
    for relative, entry in remembered.items():
        if not entry.node_id or relative in nodes:
            continue
        local = root / relative
        if local.is_symlink() or not local.is_file():
            continue
        nodes[relative] = entry.node_id
        taken += 1
        if not entry.content_hash:
            continue
        try:
            held = hasher(local)
        except OSError:
            continue
        known[relative] = (entry.content_hash, entry.size)
        if entry.etag:
            etags[relative] = entry.etag
            if etag_hashes is not None and entry.etag_hash:
                etag_hashes[relative] = entry.etag_hash
        if held != (entry.content_hash, entry.size):
            continue
        stamp = stamp_of(local)
        if stamp is not None:
            stamps[relative] = stamp
    return taken


def under_working_dir(agreed: Mapping[bytes, PulledFile], inside: str) -> dict[str, PulledFile]:
    """The pull's agreed files that sit in the watched directory, keyed by
    their path inside it. The pull is of the whole chat folder and the watch
    is of the working directory ``inside`` it; the rest of the folder is the
    box's own records and never streams."""
    prefix = f"{inside.strip('/')}/" if inside.strip("/") else ""
    found: dict[str, PulledFile] = {}
    for relative, pulled in agreed.items():
        path = os.fsdecode(relative)
        if prefix and not path.startswith(prefix):
            continue
        found[path[len(prefix) :]] = pulled
    return found
