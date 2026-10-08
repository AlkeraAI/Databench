"""The holder's tree report: every name under a leased folder, ahead of its bytes.

In-flight only, so plain ``BaseModel`` rather than ``VersionedModel``: a batch
is applied inside the request that carried it and never stored, and the one
thing a replay needs -- the answer -- is kept by the idempotency store as bytes.

Paths are relative to the leased folder, ``/``-separated, and carry the bytes
of a name exactly: a name that is not UTF-8 travels as the surrogate escapes
Python's ``surrogateescape`` produces, and the server encodes it back the same
way. The route refuses the whole batch when any path steps outside the folder.
"""

from __future__ import annotations

import uuid
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: The most entries one batch may name. The holder is told the same number in
#: its grant (``metadataMaxEntries``), and a longer batch is refused with 413 so
#: the holder splits it rather than dropping entries.
TREE_MAX_ENTRIES: Final = 2_000
#: The most bytes a batch may decode to, gzip or not. Checked on the decoded
#: body, so a small compressed body cannot expand past it.
TREE_MAX_BODY_BYTES: Final = 4 * 1024 * 1024
#: A BLAKE3 as the holder spells it: the algorithm, then the 32-byte digest.
HOLDER_HASH_PATTERN: Final = r"^b3:[0-9a-f]{64}$"
#: The most folders one digest request may name. A walk of a clone asks in
#: batches of this many; a longer request is refused with 413 so it splits.
DIGEST_MAX_PATHS: Final = 4_000
#: The most folders one digest request may ask the children of. A walk asks
#: only about the folders whose digests differed, in batches of this many.
DIGEST_MAX_NAMES: Final = 1_000
#: A folder digest's ``xor`` on the wire: sixteen lowercase hex digits.
DIGEST_XOR_PATTERN: Final = r"^[0-9a-f]{16}$"

TreeOp = Literal["upsert", "rename", "delete"]
TreeKind = Literal["file", "dir"]


class TreeEntry(BaseModel):
    """One change under the leased folder, as the holder saw it on disk."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    op: TreeOp
    #: Relative to the lease root; for a rename, where the node ends up.
    path: str = Field(min_length=1, max_length=65_536)
    #: Required for an upsert and a rename; ignored on a delete.
    kind: TreeKind | None = None
    #: Where a rename's node was. Required for a rename, refused elsewhere.
    from_: str | None = Field(default=None, alias="from", min_length=1, max_length=65_536)
    #: The file's size on disk. Files only.
    size: int | None = Field(default=None, ge=0)
    #: The file's modified time on disk, POSIX nanoseconds. Files only.
    mtime_ns: int | None = Field(default=None, ge=0)
    #: The POSIX mode bits. Files only.
    mode: int | None = Field(default=None, ge=0, le=0o7777)
    #: ``b3:<hex>`` when the holder has hashed the file, else absent.
    hash: str | None = Field(default=None, pattern=HOLDER_HASH_PATTERN)

    @model_validator(mode="after")
    def _shape_matches_op(self) -> TreeEntry:
        if self.op in ("upsert", "rename") and self.kind is None:
            raise ValueError(f"an {self.op} names the kind it leaves on disk")
        if self.op == "rename" and self.from_ is None:
            raise ValueError("a rename names the path it came from")
        if self.op != "rename" and self.from_ is not None:
            raise ValueError("only a rename names a path it came from")
        if self.kind == "dir" and any(
            value is not None for value in (self.size, self.mtime_ns, self.mode, self.hash)
        ):
            raise ValueError("a directory carries no size, time, mode or hash")
        return self


class TreeBatch(BaseModel):
    """One flush of the holder's metadata queue."""

    model_config = ConfigDict(extra="forbid")

    #: Minted by the holder per flush. A resend of the same id is answered with
    #: the first answer and changes nothing, for a day.
    batch_id: uuid.UUID
    entries: list[TreeEntry] = Field(min_length=1, max_length=TREE_MAX_ENTRIES)


class TreeBatchAnswer(BaseModel):
    """What a batch did."""

    #: The lease's ``live_seq`` after the batch.
    live_seq: int
    #: How many entries the batch applied. A replay answers the first count.
    applied: int
    #: Files under the lease whose bytes are still on their way: ``behind`` or
    #: ``unlanded``.
    landing_count: int


class DigestRequest(BaseModel):
    """The folders a holder's walk wants the drive's digests of.

    Relative to the leased folder, ``/``-separated, ``""`` for the folder
    itself; the same byte rules as a tree report's paths.

    ``names`` asks, for some of those folders, the names of their children as
    the drive files them: what a walk that found a folder differing needs to
    learn which of the drive's rows its disk no longer has. Each must also be
    one of ``paths``.
    """

    model_config = ConfigDict(extra="forbid")

    paths: list[str] = Field(max_length=DIGEST_MAX_PATHS)
    names: list[str] = Field(default_factory=list, max_length=DIGEST_MAX_NAMES)

    @model_validator(mode="after")
    def _names_are_asked_paths(self) -> DigestRequest:
        if not set(self.names) <= set(self.paths):
            raise ValueError("every folder in names must also be one of paths")
        return self


class FolderDigest(BaseModel):
    """One folder's digest: its direct children counted and XORed."""

    count: int = Field(ge=0)
    xor: str = Field(pattern=DIGEST_XOR_PATTERN)


class DigestAnswer(BaseModel):
    """The drive's digest of every folder the request named, keyed by the path
    exactly as it was sent. A folder the drive does not have answers as an
    empty one: no children, a zero XOR.

    ``children`` answers ``names``: for each folder named there, the names of
    its direct files and folders, spelled like the paths (an undecodable byte
    as its surrogate escape). A child the drive is still waiting for the
    holder to take is left out, because a disk that lacks it has not deleted
    it. A folder the drive does not have lists none."""

    digests: dict[str, FolderDigest]
    children: dict[str, list[str]] = Field(default_factory=dict)


__all__ = [
    "DIGEST_MAX_NAMES",
    "DIGEST_MAX_PATHS",
    "DIGEST_XOR_PATTERN",
    "HOLDER_HASH_PATTERN",
    "TREE_MAX_BODY_BYTES",
    "TREE_MAX_ENTRIES",
    "DigestAnswer",
    "DigestRequest",
    "FolderDigest",
    "TreeBatch",
    "TreeBatchAnswer",
    "TreeEntry",
    "TreeKind",
    "TreeOp",
]
