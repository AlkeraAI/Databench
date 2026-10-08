"""The item payload and the versioned facets it is built from.

The `Item` itself is in-flight only (it is regenerated into the SDKs), so it
stays a plain `BaseModel` with camelCase aliases. Its `attrs`, `capabilities`,
`file`, `symlink`, `object` and `lease` facets are `VersionedModel`s, so a
field added to one of them later is additive for every persisted copy of it.

A facet is absent, not empty, when it does not apply: a folder has no `file`
facet, and only a leased subtree carries `lease`.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any, ClassVar, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, PrivateAttr
from pydantic.alias_generators import to_camel

from alkera_core.status import FilesLiveEvidence, StatusFact, files_live_status
from alkera_core.versioning import VersionedModel

from .lease import LeasePurpose, LiveEntryState

ItemKind = Literal["file", "folder", "symlink", "object", "special"]

#: What a row's bytes are against the disk of the machine holding its folder;
#: the vocabulary ``alkera_core.files.freshness`` derives.
ContentState = Literal["on_drive", "behind", "unlanded", "unsynced", "none"]

#: Whether a leased folder is being served by a live holder right now.
LeaseServed = Literal["live", "offline"]

#: How a lease's holder stands to the reader: the reader themselves, the box
#: running the reader's own chat, a box the reader runs, or none of these.
LeaseYours = Literal["none", "you", "chat", "box"]

#: The wire spelling of ``file_nodes.symlink_kind``; the two are the same three
#: words so a create round-trips through a read.
SymlinkKind = Literal["relative", "canonical", "host"]


def _metadata(title: str) -> Any:
    """The inherited experimental-field bag, titled for THIS model.

    Every `VersionedModel` carries a `metadata` bag, and its JSON Schema title
    is `Metadata` on all of them. `openapi-python-client` names a nested
    object's model after that title, so seven facets meant seven models called
    `Metadata`: it printed "duplicate models" and dropped every one of them,
    taking the facets that held them out of the generated package. A distinct
    title per model is what keeps them all.
    """
    return Field(default_factory=dict, title=title)


class NameFlagsWire(VersionedModel):
    """What the server knows about how this name behaves on a client's filesystem."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"
    metadata: dict[str, Any] = _metadata("NameFlagsMetadata")

    windows_safe: bool = True
    macos_safe: bool = True
    display_warning: str | None = None


class AttrsFacet(VersionedModel):
    """POSIX-shaped attributes; `ctime` is server-owned and read-only.

    ``mtime_ns`` rides beside the RFC 3339 ``mtime`` because the two are not
    interchangeable: a float second cannot hold a nanosecond stamp — an IEEE
    double has run out of mantissa by roughly 256 ns at today's epoch — so a
    client that rebuilt nanoseconds from the rendered ``mtime`` wrote back a
    *different* time than the one it read, and every pulled file then looked
    modified to a tool that compares stat blocks. The integer is the exact
    value the tree stores; ``mtime`` stays for the readers that want a date.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.1.0"
    metadata: dict[str, Any] = _metadata("AttrsFacetMetadata")

    mode: int = 0
    uid: int = 0
    gid: int = 0
    owner: str | None = None
    mtime: datetime | None = None
    #: The exact POSIX nanoseconds behind ``mtime``. Spelled camelCase on the
    #: wire so a client hands what it read straight back as ``AttrsPatch``'s
    #: ``mtimeNs``.
    mtime_ns: int | None = Field(
        default=None,
        serialization_alias="mtimeNs",
        validation_alias=AliasChoices("mtimeNs", "mtime_ns"),
    )
    atime: datetime | None = None
    birthtime: datetime | None = None
    ctime: datetime | None = None
    xattrs: dict[str, str] = Field(default_factory=dict)


class FileFacet(VersionedModel):
    """Content facts. `mime_type` is what the server sniffed, never what a client claimed."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"
    metadata: dict[str, Any] = _metadata("FileFacetMetadata")

    mime_type: str = "application/octet-stream"
    size: int = 0
    content_hash: str = ""
    block_hash: str | None = None
    scan_state: Literal["pending", "clean", "infected", "skipped"] = "pending"
    provider: str = "bytes"


def _symlink_kind_1_0_0(data: dict[str, Any]) -> dict[str, Any]:
    """`internal | external | absolute` were the 1.0.0 spellings of the same
    three classifications the tree stores."""
    moved = dict(data)
    moved["kind"] = {"internal": "relative", "external": "canonical", "absolute": "host"}.get(
        str(data.get("kind", "internal")), "relative"
    )
    moved["schema_version"] = "2.0.0"
    return moved


class SymlinkFacet(VersionedModel):
    """A symlink's stored target and what its text points at.

    ``kind`` is the tree's own vocabulary (``file_nodes.symlink_kind``:
    ``relative`` — resolved against this directory, ``canonical`` — an absolute
    path in the org's namespace that a materializer rewrites to the local root,
    ``host`` — a path on some machine, stored and recreated verbatim, never
    followed server-side). The wire maps 1:1 to what is stored, so
    ``POST …/children`` takes the kind it reads back. 1.0.0 spelled the three
    ``internal | external | absolute``; the migration maps them forward.
    """

    SCHEMA_VERSION: ClassVar[str] = "2.0.0"
    MIGRATIONS: ClassVar[dict[str, Any]] = {"1.0.0": _symlink_kind_1_0_0}
    metadata: dict[str, Any] = _metadata("SymlinkFacetMetadata")

    target: str = ""
    kind: SymlinkKind = "relative"


class ObjectFacet(VersionedModel):
    """A row-backed object (a chat, a query, a board) rendered as a node.

    ``title`` is the object's own CURRENT title, which is not the node's name.
    The name was minted from the title the day the node was created and is a
    filesystem name ever since — an untitled chat is a uuid, and renaming the
    chat afterwards does not rename the folder. A surface that renders the node
    name therefore shows ``3952c9e2-….alkerachat`` for a conversation the person
    has been calling something else all morning. Empty when the object has no
    title (or, on a record written before 1.1.0, when nobody asked).
    """

    SCHEMA_VERSION: ClassVar[str] = "1.1.0"
    metadata: dict[str, Any] = _metadata("ObjectFacetMetadata")

    type: str = ""
    id: str = ""
    title: str = ""
    web_url: str | None = None
    app_url: str | None = None


class HomeFacet(VersionedModel):
    """Present on a member's home folder, and on nothing else.

    A home's stored name is its owner's id — an address, never a label. What a
    person reads for it is ``owner_name``: the owner's CURRENT display name,
    resolved when the payload is rendered, so a changed name shows at once and
    nothing about the account is ever written into the tree. Every surface
    that shows a node's name shows this instead when it is present.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"
    metadata: dict[str, Any] = _metadata("HomeFacetMetadata")

    #: The home folder itself.
    node_id: str = ""
    owner_id: str = ""
    #: The owner's display name, or a neutral fallback when they have none —
    #: never their address.
    owner_name: str = ""


class SpecialFacet(VersionedModel):
    """A device, fifo or socket node.

    ``rdev`` is the POSIX device number a materializer needs to recreate the
    node; the server never opens one, so nothing here is followed.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"
    metadata: dict[str, Any] = _metadata("SpecialFacetMetadata")

    type: str = ""
    rdev: int = 0


class LeaseFacet(VersionedModel):
    """Carried by the leased folder and everything under it, so a client can
    explain why a write would be refused before it attempts one.

    1.2.0 names the chat whose folder the lease holds: its id, its current
    title, and whether THIS caller may open it. A folder a box is running a
    chat in is read-only to a browser for the whole lease, and the browser has
    to say which conversation holds it and link there for the people who may
    follow the link. The permission is the chat policy's own READ answer,
    resolved for the caller by the route; a reader of a file six levels down
    who holds no rung on the chat is told the title and given no link.

    1.3.0 carries the two names a person reads. ``holder`` and ``machine`` are
    both ids when a box holds the folder — the same id twice, in fact, because
    a box's lease names itself as both — and a status line built out of them
    reads as two uuids. The names are resolved by the route beside the chat's
    title, and a surface that has neither says what the holder IS rather than
    what it is called.

    1.4.0 says whether the holder is serving and how much is still on its way.
    ``served`` is ``live`` while the lease is streaming and its holder has
    beaten recently, and ``offline`` otherwise -- a box that died keeps its
    lease until the TTL runs out, and a reader must not be told it is watching
    a live machine for those minutes. ``landing_count`` is how many files under
    the lease the drive holds an older copy of, or none at all. ``node_id`` is
    the leased folder itself, so a surface listing a folder deep inside the
    lease can match the lease's own frames without knowing which ancestor
    holds it.

    1.5.0 says whether the holder acts for the reader. A refusal that names
    "someone" when the folder is held by the reader's own chat or box sends
    them looking for a colleague who does not exist. ``yours`` is resolved by
    the route beside the names, and reads ``none`` on a row it did not resolve.

    1.6.0 carries the status the server decided for the folder: live, sync
    paused (and why), or a saved copy, with the words to draw it. A reader
    renders it and never judges the lease's raw fields itself.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.6.0"
    metadata: dict[str, Any] = _metadata("LeaseFacetMetadata")

    holder: str = ""
    machine: str = ""
    purpose: LeasePurpose = "mount"
    since: datetime | None = None
    expires_at: datetime | None = None
    last_sync_at: datetime | None = None
    mine: bool = False
    #: Whether the holder is streaming what it writes as it writes it. A lease
    #: without this is still a lease — the subtree is fenced — but the drive's
    #: copy only moves at a checkpoint, so nothing under it is ever newer than
    #: `last_sync_at`.
    live: bool = False
    #: Whether a write by someone other than the holder is admitted into the
    #: subtree and handed to the holder, rather than refused.
    inbound: bool = False
    #: How many nodes under the lease are in flight right now.
    pending: int = 0
    #: Bumped by every change to the live plane; a client that has seen a lower
    #: number knows it is behind without diffing anything.
    live_seq: int = 0
    #: The chat whose own folder this lease is on, or ``None`` for a lease on
    #: any other folder. Carried by everything under the chat's folder, the
    #: way the rest of the facet is.
    chat_id: str | None = None
    #: That chat's current title. Empty when there is no chat, and on a row a
    #: route rendered without resolving it.
    chat_title: str = ""
    #: Whether the caller this payload was rendered for may open the chat:
    #: the chat policy's READ answer, never derived from the reader's rung on
    #: the folder they are looking at.
    can_open_chat: bool = False
    #: What the machine in ``machine`` is called (the allocation's own name).
    #: Empty when the id names no machine this org has, or
    #: when the holder mounted the folder from a laptop that sent a name of its
    #: own in ``machine``.
    machine_name: str = ""
    #: The holder's display name, when the holder is a person. Empty for a
    #: machine holder: a box is named by ``machine_name``, and inventing a
    #: person for it would be a lie a reader cannot check.
    holder_name: str = ""
    #: The leased folder: the lease's own node, the same on every row under it.
    node_id: uuid.UUID | None = None
    #: ``live`` while the lease is streaming and its holder beat recently;
    #: ``offline`` for a lease that is not streaming or whose holder went quiet.
    served: LeaseServed = "offline"
    #: Files under the lease whose bytes are still on their way to the drive.
    landing_count: int = 0
    #: How the holder stands to the reader: ``you`` when the reader holds it,
    #: ``chat`` when it is the box running the reader's own chat, ``box`` when
    #: it is a box the reader runs, ``none`` otherwise.
    yours: LeaseYours = "none"
    #: Whether the files under the lease are the machine's live work or the
    #: last copy it saved, as the server decided it.
    status: StatusFact | None = None


class LiveFacet(VersionedModel):
    """What ONE node is on the machine holding the lease, against the drive.

    Present on a node the holder reported (``content``, ``holder_size``,
    ``holder_mtime``) or one it is moving right now (``state`` and the ``box_*``
    pair); absent on every other node, including a folder that merely sits
    inside a leased subtree, which carries the lease facet alone.

    1.1.0 adds the holder's report. ``content`` is derived per read by
    ``alkera_core.files.freshness`` and never stored: ``on_drive`` (the drive
    has the holder's bytes), ``behind`` (it has an older version), ``unlanded``
    (it has none yet), ``unsynced`` (the holder's lease is gone and these bytes
    never arrived) or ``none``. ``state`` became optional with it: a file the
    holder reported and is not moving has no state, and a default of
    ``writing`` would have said it was being written.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.1.0"
    metadata: dict[str, Any] = _metadata("LiveFacetMetadata")

    state: LiveEntryState | None = None
    #: The size the machine reports, which the drive does not have yet.
    box_size: int | None = None
    box_mtime: datetime | None = None
    updated_at: datetime | None = None
    content: ContentState = "none"
    #: The size and modified time on the holder's disk at its last report.
    holder_size: int | None = None
    holder_mtime: datetime | None = None
    #: What ``content`` is before the lease is consulted -- ``unlanded``,
    #: ``on_drive`` or ``behind`` -- for a surface that learns the lease after it
    #: rendered the row (a feed). Never on the wire.
    _landed: ContentState | None = PrivateAttr(default=None)


class Capabilities(VersionedModel):
    """What this caller may do with this node, and why not when they may not.

    `refusals` is surface-specific: the same node can be undownloadable on a
    link and downloadable in the portal, and the UI shows the reason rather
    than a dead button.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.2.0"
    metadata: dict[str, Any] = _metadata("CapabilitiesMetadata")

    can_read: bool = False
    can_write: bool = False
    can_share: bool = False
    # Trashing is undoable and sits on the writer rung; the purge that is not
    # undoable is the owner's alone, so the two are separate answers.
    can_delete: bool = False
    can_purge: bool = False
    can_rename: bool = False
    can_download: bool = False
    # Taking a folder out, asking for it back and taking it back are three
    # different rungs — a reader may ask, a writer may take it out, only a
    # manager may take it back — so the surface that draws those three controls
    # gets three answers rather than inferring them from a role it is never told.
    can_lease: bool = False
    can_lease_request: bool = False
    can_lease_force: bool = False
    refusals: dict[str, str] = Field(default_factory=dict)


class Item(BaseModel):
    """The one item payload every Files surface returns."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str
    ino: int = 0
    drive_id: str = ""
    kind: ItemKind = "file"
    subtype: str | None = None
    name: str = ""
    name_display: str = ""
    name_encoding: str = "utf-8"
    name_flags: NameFlagsWire = Field(default_factory=NameFlagsWire)
    path_bytes: bytes = b""
    parent_id: str | None = None
    path: str | None = None
    etag: str = ""
    ctag: str = ""
    attrs: AttrsFacet | None = None
    file: FileFacet | None = None
    symlink: SymlinkFacet | None = None
    object: ObjectFacet | None = None
    special: SpecialFacet | None = None
    #: Present only on a member's home folder: whose it is and what to call it.
    home: HomeFacet | None = None
    #: The member's home that `pathBytes` runs through, when it runs through
    #: one the caller may read: a surface that shows the path shows this
    #: owner's name for that segment instead of the stored one. Absent when the
    #: path is cut above the home or never enters one.
    path_home: HomeFacet | None = None
    lease: LeaseFacet | None = None
    #: Present only while this node is in flight on the machine holding the
    #: lease; absent — not empty — on every settled node.
    live: LiveFacet | None = None
    stale: bool = False
    trust: str | None = None
    locked: bool = False
    held: bool = False
    capabilities: Capabilities = Field(default_factory=Capabilities)
    #: The owner's name for a person to read, resolved from `attrs.owner`:
    #: their display name, their email when they have not named themselves,
    #: and `None` when the node has no owner (a server-created node) or the
    #: caller's page did not resolve one. The id in `attrs.owner` stays the
    #: identifier; this is only ever the label beside it, so a client that
    #: renders the raw uuid is not shipping a person's name.
    owner_name: str | None = None
    #: What the folder this node sits in is CALLED, for a surface that lists
    #: rows from many folders at once — a feed, where two files of the same
    #: name in two different folders are otherwise indistinguishable. The id
    #: stays `parent_id`; this is only ever the label beside it.
    #:
    #: `None` — not empty — whenever there is no name to show: a listing that
    #: already IS the folder does not repeat it, and a caller who may not read
    #: the parent is told nothing rather than handed its name.
    parent_name: str | None = None
    shared: bool = False
    #: Whether *this caller* starred the node. A star is per user, so the
    #: same node reads `True` for its owner and `False` for a colleague;
    #: `capabilities` stays about permissions and says nothing about it.
    starred: bool = False
    trashed: bool = False
    #: The node this one is a conflicted copy of: the drive kept these bytes
    #: here when a later write displaced them from that node's name. `None` on
    #: every file that is not such a copy. An id, like `parent_id`.
    conflict_of: str | None = None


def _attrs() -> dict[str, Any]:
    return {"mode": 33188, "uid": 501, "gid": 20}


FIXTURE_EXAMPLES: list[tuple[str, Callable[[], VersionedModel]]] = [
    (
        "item_attrs_facet",
        lambda: AttrsFacet(
            **_attrs(),
            owner="6b1f0b1e-0000-4000-8000-000000000002",
            mtime=datetime.fromisoformat("2026-01-01T12:00:00+00:00"),
            mtime_ns=1_767_268_800_123_456_789,
            xattrs={"user.alkera.trust": "verified"},
        ),
    ),
    (
        "item_file_facet",
        lambda: FileFacet(
            mime_type="application/pdf",
            size=65_536,
            content_hash="ab" * 32,
            block_hash="cd" * 32,
            scan_state="clean",
            provider="bytes",
        ),
    ),
    ("item_symlink_facet", lambda: SymlinkFacet(target="../sibling", kind="relative")),
    ("item_special_facet", lambda: SpecialFacet(type="chardev", rdev=8_631)),
    (
        "item_home_facet",
        lambda: HomeFacet(
            node_id="6b1f0b1e-0000-4000-8000-000000000004",
            owner_id="6b1f0b1e-0000-4000-8000-000000000002",
            owner_name="Ana Ruiz",
        ),
    ),
    (
        "item_object_facet",
        lambda: ObjectFacet(
            type="chat",
            id="6b1f0b1e-0000-4000-8000-000000000003",
            title="Wednesday's warehouse spike",
            web_url="https://app.example.test/chats/1",
            app_url="alkera://chats/1",
        ),
    ),
    (
        "item_lease_facet",
        lambda: LeaseFacet(
            holder="6b1f0b1e-0000-4000-8000-000000000002",
            machine="machine-a",
            purpose="mount",
            since=datetime.fromisoformat("2026-01-01T12:00:00+00:00"),
            expires_at=datetime.fromisoformat("2026-01-01T12:01:00+00:00"),
            last_sync_at=datetime.fromisoformat("2026-01-01T12:00:48+00:00"),
            mine=True,
            live=True,
            inbound=True,
            pending=2,
            live_seq=57,
            machine_name="alkera-demo-box",
            holder_name="Ana Ruiz",
            node_id=uuid.UUID("6b1f0b1e-0000-4000-8000-000000000009"),
            served="live",
            landing_count=17,
            yours="you",
            status=files_live_status(
                FilesLiveEvidence(
                    live=True,
                    beating=True,
                    behind=False,
                    landing=17,
                    since=datetime.fromisoformat("2026-01-01T12:00:00+00:00"),
                    last_sync_at=datetime.fromisoformat("2026-01-01T12:00:48+00:00"),
                    machine_name="alkera-demo-box",
                )
            ),
        ),
    ),
    (
        "item_live_facet",
        lambda: LiveFacet(
            state="uploading",
            box_size=1_048_576,
            box_mtime=datetime.fromisoformat("2026-01-01T12:00:47+00:00"),
            updated_at=datetime.fromisoformat("2026-01-01T12:00:48+00:00"),
            content="behind",
            holder_size=1_048_576,
            holder_mtime=datetime.fromisoformat("2026-01-01T12:00:47+00:00"),
        ),
    ),
    (
        "item_capabilities",
        lambda: Capabilities(
            can_read=True,
            can_write=False,
            can_download=False,
            refusals={"canWrite": "files.leased", "canDownload": "files.held"},
        ),
    ),
    (
        "item_name_flags",
        lambda: NameFlagsWire(
            windows_safe=False,
            macos_safe=True,
            display_warning="reserved on Windows",
        ),
    ),
]
