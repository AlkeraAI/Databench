"""The one place a wire :class:`Item` is built.

Every Files surface — listing, get, delta, search, an operation's result —
returns the same payload, so there is exactly one function that assembles it.
A route that builds an item by hand is the bug this module exists to prevent:
the facets it would forget (``capabilities``, ``lease``, ``nameFlags``) are
precisely the ones a client depends on to explain a refusal before attempting
it.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from alkera_core.compute.machines import UNREACHABLE, machine_state
from alkera_core.files import drives, names
from alkera_core.files.authz.actions import FilesAction
from alkera_core.files.authz.capabilities import capabilities as capability_map
from alkera_core.files.authz.decider import EffectiveAccess
from alkera_core.files.freshness import (
    ContentState,
    HeadFacts,
    HolderFacet,
    head_digest,
    landed_state,
    under_lease,
)
from alkera_core.files.lease_snapshots import LeaseFacet as LeaseSnapshot
from alkera_core.files.membership import active_member_of
from alkera_core.files.objects_bridge import FOLDER_OBJECT_KINDS, working_folder_nodes
from alkera_core.files.providers.registry import (
    CONTEXT_MEMBER_SEPARATOR,
    object_type_of,
    object_web_path,
)
from alkera_core.models.compute import ComputeAllocation
from alkera_core.models.files.tree import SYMLINK_KINDS, FileNode
from alkera_core.models.files.versions import FileVersion
from alkera_core.models.team import Team
from alkera_core.models.user import User
from alkera_core.models.workspace_object import WorkspaceObject
from alkera_core.schemas.files.item import (
    AttrsFacet,
    Capabilities,
    FileFacet,
    HomeFacet,
    Item,
    ItemKind,
    LeaseFacet,
    LeaseServed,
    LiveFacet,
    NameFlagsWire,
    ObjectFacet,
    SpecialFacet,
    SymlinkFacet,
)
from alkera_core.schemas.files.lease import LeasePurpose, LiveEntryState
from alkera_core.status import FilesLiveEvidence, StatusFact, files_live_status
from sqlalchemy import CTE, false, literal, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from backend.services.files.context import FilesContext

#: Kinds that survive onto the wire unchanged. Anything else the tree can hold
#: is rendered as a file so an old client is never handed a kind it cannot
#: switch on.
_WIRE_KINDS: frozenset[str] = frozenset({"file", "folder", "symlink", "object", "special"})


def _ns_to_dt(value: int) -> datetime | None:
    """A POSIX nanosecond stamp as UTC, or ``None`` for the unset zero."""
    if value == 0:
        return None
    return datetime.fromtimestamp(value / 1_000_000_000, tz=UTC)


def _readable_from(chain: Sequence[uuid.UUID], readable: Collection[uuid.UUID] | None) -> int:
    """Where a root-first chain starts once cut at its deepest unreadable ancestor.

    The node itself (the chain's last entry) is never what cuts it.
    """
    cut = 0
    if readable is not None:
        for index, node_id in enumerate(chain[:-1]):
            if node_id not in readable:
                cut = index + 1
    return cut


def readable_path_bytes(
    chain: Sequence[tuple[uuid.UUID, bytes]],
    *,
    readable: Collection[uuid.UUID] | None,
) -> bytes:
    """A root-first chain as a path, minus the ancestors this caller cannot read.

    A name is a fact about a folder, so a caller who may not read the folder may
    not learn what it is called. Sharing one file out of ``/home/dana/salaries``
    handed the grantee the whole chain of names in ``pathBytes`` — the shape of
    somebody else's drive, from a grant that was meant to reveal one file.

    So the path is cut at the DEEPEST ancestor missing from ``readable``:
    everything at or above it is dropped and what survives is the run of
    ancestors the caller may read, plus the node itself. The node's own name
    always stays — it rides on ``name`` regardless, and a path that dropped it
    would name a different node.

    A cut path is answered WITHOUT its leading slash, because it no longer
    starts at the drive root and a client that resolved it as though it did
    would be addressing some other node. An uncut path keeps the leading slash
    the root's empty name already produces.

    ``readable`` of ``None`` means the caller has already established the whole
    chain — the node it just created under a folder it writes, its own trash —
    and nothing is cut.

    ``chain`` ends with the node itself; :func:`_path_bytes` appends it for the
    callers whose chain stops at the parent.
    """
    cut = _readable_from([node_id for node_id, _name in chain], readable)
    joined = b"/".join(name for _id, name in chain[cut:])
    if cut:
        return joined
    return joined if joined.startswith(b"/") else b"/" + joined


def _path_bytes(
    node: FileNode,
    chain: Sequence[FileNode],
    readable: Collection[uuid.UUID] | None = None,
) -> bytes:
    """The node's path as raw bytes: names are bytes, so the path is too.

    The chain arrives root-first and includes the drive root, whose name is
    empty — joining on ``/`` therefore yields a leading slash without a special
    case. ``FilesRepo.chain`` returns the ancestors *and the node itself*, so
    the node's own name is appended only when the caller handed a chain that
    stops at the parent (a listing does: one chain serves every row on the
    page). Appending it unconditionally read ``/probe/a.txt`` back as
    ``/probe/a.txt/a.txt``.
    """
    pairs = [(ancestor.id, bytes(ancestor.name)) for ancestor in chain]
    if not pairs or pairs[-1][0] != node.id:
        pairs.append((node.id, bytes(node.name)))
    return readable_path_bytes(pairs, readable=readable)


#: One segment of a path a person reads: the segment's display name, and the
#: owner id when the segment is a member's home.
PathPart = tuple[str, str | None]


def path_parts(chain: Sequence[FileNode]) -> list[PathPart]:
    """A root-first chain as the parts :func:`display_path` joins.

    Separate from the joining because a route that renders several paths reads
    the chains while its rows are live and names the homes afterwards, once for
    the page (:func:`home_labels`).
    """
    parts: list[PathPart] = []
    for ancestor in chain:
        owner = drives.home_owner(ancestor)
        parts.append((names.display(bytes(ancestor.name)), str(owner) if owner else None))
    return parts


def display_path(parts: Sequence[PathPart], home_labels: Mapping[str, str]) -> str:
    """The path a person reads: ``/home/Dana Ruiz/papers``.

    A home's segment is its owner's label from ``home_labels`` — never its
    stored name, which is an id. The drive root's name is empty, so joining on
    ``/`` yields the leading slash; the root alone reads as ``/``.
    """
    shown = [
        home_labels.get(owner, HOME_FALLBACK_NAME) if owner is not None else name
        for name, owner in parts
    ]
    joined = "/".join(shown)
    return joined if joined.startswith("/") else "/" + joined


async def home_labels(
    session: AsyncSession, owner_ids: Iterable[str], *, org_team_id: uuid.UUID
) -> dict[str, str]:
    """What each home owner's home is called, in ONE statement.

    The display name of a member of ``org_team_id``; anybody else, and a member
    with no name, is absent, so the caller shows :data:`HOME_FALLBACK_NAME`.
    Never an address.
    """
    return {
        who: named.display
        for who, named in (await _named_users(session, owner_ids, org_team_id=org_team_id)).items()
        if named.in_org and named.display
    }


def _kind(node: FileNode) -> ItemKind:
    kind = node.kind
    if kind in _WIRE_KINDS:
        return kind  # type: ignore[return-value]
    return "file"


def _xattr_wire(value: object) -> str:
    """One stored xattr value as the wire's base64.

    An xattr value is arbitrary bytes (``schemas.files.attrs.Base64Bytes``), so
    it is base64 in both directions. ``str(value)`` rendered a Python repr that
    no client can decode back to the bytes the kernel gave us. A value the JSONB
    column already holds as text is already that base64.
    """
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, str):
        return value
    return base64.b64encode(str(value).encode()).decode("ascii")


def _attrs(node: FileNode) -> AttrsFacet:
    return AttrsFacet(
        mode=node.mode,
        uid=node.uid,
        gid=node.gid,
        owner=str(node.created_by) if node.created_by is not None else None,
        mtime=_ns_to_dt(node.mtime_ns),
        atime=_ns_to_dt(node.atime_ns),
        birthtime=_ns_to_dt(node.birthtime_ns),
        ctime=_ns_to_dt(node.ctime_ns),
        mtime_ns=node.mtime_ns or None,
        xattrs={key: _xattr_wire(value) for key, value in node.xattrs.items()},
    )


def _file(node: FileNode, version: FileVersion | None) -> FileFacet | None:
    """The content facet, present on a file and on nothing else.

    ``mime_type`` and ``scan_state`` come from the head version because they are
    facts about *bytes*, and the node outlives any one set of them; a node whose
    head version has not been loaded still reports its size, so a listing that
    did not join versions is not silently zero.
    """
    if node.kind != "file":
        return None
    if version is None:
        return FileFacet(size=node.size)
    return FileFacet(
        mime_type=version.mime_sniffed,
        size=version.size_bytes,
        content_hash=version.content_hash,
        block_hash=version.block_hash or None,
        scan_state=version.scan_state,  # type: ignore[arg-type]
        provider="bytes",
        # Where the head came from: a machine holding the folder treats a
        # live session's write back (``document_snapshot``) as a version it
        # sends its own newer bytes up against, never one it moves them aside
        # for.
        metadata={"head_source": version.source},
    )


def _symlink(node: FileNode) -> SymlinkFacet | None:
    if node.kind != "symlink":
        return None
    target = node.symlink_target or b""
    # The wire spells the stored vocabulary 1:1 (SymlinkFacet 2.0.0): a caller
    # that creates a link sends the kind it wants and reads the same word back.
    kind = node.symlink_kind if node.symlink_kind in SYMLINK_KINDS else "relative"
    return SymlinkFacet(target=names.display(target), kind=kind)  # type: ignore[arg-type]


def _object(node: FileNode) -> ObjectFacet | None:
    """The object behind a node, whatever shape the node has.

    The test is ``target_object_id``, not the node kind: a chat is a *folder*
    named ``<Title>.alkerachat`` and is still the chat, so a client that routes
    on this facet opens the conversation instead of listing the folder. The
    type and the address both come from the provider registry — the same one
    the on-disk pointer reads — so the wire facet and a mounted pointer can
    never name two different pages for one node.
    """
    if node.target_object_id is None:
        return None
    object_type = node.subtype or object_type_of(node) or ""
    if CONTEXT_MEMBER_SEPARATOR in object_type:
        # A replication context's `spec.json` / `README.md` point at the same
        # object their folder does, but they are not it: they are that object
        # rendered as bytes. Naming a destination here sent a double-click to
        # the report's own page instead of opening the file the reader asked
        # for — the human half of the folder was unreachable from the browser
        # even once its content became fetchable.
        return None
    return ObjectFacet(
        type=object_type,
        id=str(node.target_object_id),
        web_url=object_web_path(object_type, str(node.target_object_id)),
    )


#: What a home is called when its owner has no display name to show, or is no
#: longer somebody this org can name. Never their address.
HOME_FALLBACK_NAME = "Member"

#: What the Owner column reads for a node a machine is recorded as owning: a
#: box wrote it where no chat or workspace folder stands, or before a box's
#: writes were credited to the folder's owner. Never the machine's id.
MACHINE_OWNER_NAME = "Agent"


def _home(node: FileNode) -> HomeFacet | None:
    """The home facet, present on a member's home folder and nothing else.

    The owner's name is resolved per page by :func:`with_owner_names`; a route
    that renders without it still says whose home this is, and the label
    falls back to :data:`HOME_FALLBACK_NAME` rather than the stored name.
    """
    owner = drives.home_owner(node)
    if owner is None:
        return None
    return HomeFacet(node_id=str(node.id), owner_id=str(owner), owner_name=HOME_FALLBACK_NAME)


def _path_home(
    node: FileNode, chain: Sequence[FileNode], readable: Collection[uuid.UUID] | None
) -> HomeFacet | None:
    """The home the node's readable path runs through, the node itself included.

    Cut by the same rule the path is (:func:`readable_path_bytes`), so a caller
    who is not shown the home's segment is not told whose home it is either.
    """
    nodes = list(chain)
    if not nodes or nodes[-1].id != node.id:
        nodes.append(node)
    cut = _readable_from([row.id for row in nodes], readable)
    for row in nodes[cut:]:
        home = _home(row)
        if home is not None:
            return home
    return None


def _special(node: FileNode) -> SpecialFacet | None:
    """A device, fifo or socket node: the subtype and the device numbers a
    materializer needs to recreate it, and nothing a reader could follow."""
    if node.kind != "special":
        return None
    return SpecialFacet(type=node.subtype or "", rdev=node.rdev)


def lease_wire(snapshot: LeaseSnapshot | None) -> LeaseFacet | None:
    """The library's lease snapshot as the ``lease`` facet the wire declares.

    The two shapes are deliberately not the same object: the library one
    carries the leased node's id and the derived ``stale``, both of which the
    payload spells elsewhere (``stale`` sits beside ``lease``, not inside it).
    Everything a client renders — who holds it, on which machine, since when,
    until when, last pushed when — crosses unchanged, with the holder named by
    principal id because that is the only identifier every reader of the item
    is already allowed to see.
    """
    if snapshot is None:
        return None
    facet = LeaseFacet(
        holder=str(snapshot.holder_principal_id),
        machine=snapshot.machine,
        purpose=cast(LeasePurpose, snapshot.purpose),
        since=snapshot.since,
        expires_at=snapshot.expires_at,
        last_sync_at=snapshot.last_sync_at,
        mine=snapshot.mine,
        live=snapshot.live,
        inbound=snapshot.inbound,
        pending=snapshot.pending,
        live_seq=snapshot.live_seq,
        node_id=uuid.UUID(str(snapshot.node_id)),
        served=cast(LeaseServed, snapshot.served),
        landing_count=snapshot.landing_count,
        # The chat's title and the caller's right to open it are not lease
        # facts: the route that renders the payload resolves both, once for
        # the page, against the chat policy (`with_lease_chats`).
        chat_id=str(snapshot.chat_id) if snapshot.chat_id is not None else None,
        # A box the reader runs, or the reader's own chat on one, is resolved
        # by the route beside the names; the holder being the reader is a
        # lease fact.
        yours="you" if snapshot.mine else "none",
    )
    return facet.model_copy(update={"status": lease_status(facet, behind=snapshot.stale)})


def lease_status(
    facet: LeaseFacet, *, behind: bool, machine_unreachable: bool = False
) -> StatusFact:
    """The status the folder under ``facet`` reads, from the facet's own facts.

    Decided once here when the facet is built, naming the machine as well as
    the facet can; the route that resolves the machine's name and whether it
    still answers decides it again with those (``with_lease_machines``). A
    holder that named itself (a laptop mount) is called what it said.
    """
    return files_live_status(
        FilesLiveEvidence(
            live=facet.live,
            beating=facet.served == "live",
            behind=behind,
            machine_unreachable=machine_unreachable,
            landing=facet.landing_count,
            since=facet.since,
            last_sync_at=facet.last_sync_at,
            machine_name=facet.machine_name or _self_named(facet.machine),
        )
    )


def _self_named(machine: str) -> str:
    """The name a holder gave itself, or ``""`` for an id (a box)."""
    try:
        uuid.UUID(machine)
    except ValueError:
        return machine
    return ""


def holder_facet(node: FileNode) -> HolderFacet | None:
    """What the holder last reported for ``node``, when it reported anything."""
    if node.kind != "file" or node.holder_size is None:
        return None
    return HolderFacet(
        size=node.holder_size,
        mtime_ns=node.holder_mtime_ns or 0,
        hash=bytes(node.holder_hash) if node.holder_hash is not None else None,
    )


def head_facts(node: FileNode, version: FileVersion | None) -> HeadFacts | None:
    """What the drive holds for ``node``: nothing without a head, else the
    head's size and hash with the row's own modified time.

    A route that did not load the head still knows it has one; it answers the
    row's size, which the head swap keeps equal to the head's, and no hash, so
    the size and the modified time decide.
    """
    if node.head_version_id is None:
        return None
    if version is not None and version.id == node.head_version_id:
        return HeadFacts(
            size=version.size_bytes,
            mtime_ns=node.mtime_ns,
            content_hash=head_digest(version.content_hash),
        )
    return HeadFacts(size=node.size, mtime_ns=node.mtime_ns)


def _live(
    snapshot: LeaseSnapshot | None,
    *,
    landed: ContentState | None,
    holder_size: int | None,
    holder_mtime: datetime | None,
) -> LiveFacet | None:
    """The ``live`` facet from its two sources: what the holder is doing to
    the node right now (the live plane's row) and what it last reported the
    node's bytes to be. ``landed`` is the report's state before the lease is
    consulted, folded with the lease by the freshness rule's one owner."""
    moving = snapshot.live_state if snapshot is not None else None
    if moving is None and landed is None:
        return None
    lease_live = snapshot is not None and snapshot.live
    content: ContentState = (
        under_lease(landed, lease_live=lease_live) if landed is not None else "none"
    )
    facet = LiveFacet(
        state=cast(LiveEntryState, moving.state) if moving is not None else None,
        box_size=moving.box_size if moving is not None else None,
        box_mtime=moving.box_mtime if moving is not None else None,
        updated_at=moving.updated_at if moving is not None else None,
        content=content,
        holder_size=holder_size,
        holder_mtime=holder_mtime,
    )
    facet._landed = landed
    return facet


def live_wire(
    snapshot: LeaseSnapshot | None,
    node: FileNode | None = None,
    version: FileVersion | None = None,
) -> LiveFacet | None:
    """What THIS node is on the machine holding the lease, against the drive.

    Present on a node the holder is moving right now and on a file it reported,
    and nowhere else: a row that merely sits inside a streaming mount carries
    the lease facet and no live facet at all. Without ``node`` only the moving
    half can be answered.
    """
    holder = holder_facet(node) if node is not None else None
    landed = (
        landed_state(head_facts(node, version), holder)
        if node is not None and holder is not None
        else None
    )
    return _live(
        snapshot,
        landed=landed,
        holder_size=holder.size if holder is not None else None,
        holder_mtime=_ns_to_dt(holder.mtime_ns) if holder is not None else None,
    )


def _stale(snapshot: LeaseSnapshot | None) -> bool:
    """The library's clock-driven answer, carried through unchanged.

    ``stale`` is a statement about the *holder* — alive but no longer pushing —
    which only the lease's own clocks can answer, so the payload never derives
    it from who is asking. A node under no lease is not stale.
    """
    return snapshot is not None and snapshot.stale


def with_lease(item: Item, snapshot: LeaseSnapshot | None) -> Item:
    """The same item, carrying a lease resolved after it was rendered.

    A feed renders each row as its decision is made, and its single batched
    lease lookup runs once every decision is in. Folding it in here keeps
    `stale`'s rule in the module that owns the payload.
    """
    rendered = item.live
    return item.model_copy(
        update={
            "lease": lease_wire(snapshot),
            "live": _live(
                snapshot,
                landed=rendered._landed if rendered is not None else None,
                holder_size=rendered.holder_size if rendered is not None else None,
                holder_mtime=rendered.holder_mtime if rendered is not None else None,
            ),
            "stale": _stale(snapshot),
        }
    )


def with_path(
    item: Item,
    chain: Sequence[tuple[uuid.UUID, bytes]],
    *,
    readable: Collection[uuid.UUID],
) -> Item:
    """The same item, with its path cut to the chain this caller may read.

    A feed decides a row at a time and the one batched chain decision runs
    after the loop, from the ``(id, name)`` pairs the loop copied out.
    """
    update: dict[str, object] = {"path_bytes": readable_path_bytes(chain, readable=readable)}
    if item.path_home is not None:
        cut = _readable_from([node_id for node_id, _name in chain], readable)
        shown = {str(node_id) for node_id, _name in chain[cut:]}
        if item.path_home.node_id not in shown:
            update["path_home"] = None
    return item.model_copy(update=update)


#: What the product calls the drive itself, for the one row whose parent is the
#: drive root. The root's own name is empty — it is what produces the leading
#: slash of every path — so a location built from it would read as nothing.
DRIVE_ROOT_NAME = "Files"


def with_location(
    item: Item,
    chain: Sequence[tuple[uuid.UUID, bytes]],
    *,
    readable: Collection[uuid.UUID],
    root_id: uuid.UUID | None,
    root_name: str = DRIVE_ROOT_NAME,
) -> Item:
    """The same item, told what the folder it lives in is CALLED.

    A listing that already IS one folder never needs this — every row is in the
    folder the reader is standing in. A feed does: Recent draws rows from every
    corner of a drive, and three files called ``qa-report.md`` in three
    different folders arrive as three rows nothing tells apart.

    The name obeys the rule the path obeys: it is read off the one batched
    chain decision the caller's page already made, so a grantee who may read a
    file but not the folder above it is handed the file and not what the folder
    is called. An unreadable parent leaves ``parent_name`` unset rather than
    empty — absent is a thing a client draws as nothing, where an empty string
    is a folder with no name.
    """
    if item.parent_id is None:
        return item
    try:
        parent_id = uuid.UUID(item.parent_id)
    except ValueError:
        return item
    if parent_id not in readable:
        return item
    named = {ancestor_id: name for ancestor_id, name in chain}
    if parent_id not in named:
        return item
    shown = root_name if parent_id == root_id else names.display(named[parent_id])
    return item if not shown else item.model_copy(update={"parent_name": shown})


async def owner_names(session: AsyncSession, owner_ids: Iterable[str]) -> dict[str, str]:
    """The label to show beside each owner id, in ONE statement for the page.

    A row's owner reaches the wire as a uuid (``attrs.owner`` is
    ``file_nodes.created_by``), which is not a thing a person can read, so every
    surface that renders rows resolves the names here — once, for every id on
    the page. Resolving per row would be a statement per row on a listing, which
    is the cost the lease and star lookups are batched to avoid.

    Nothing is disclosed by this that the caller was not already handed: it is
    only ever called with the ids of rows the caller has already been allowed to
    read, so it can add no answer to the no-oracle contract. An id with no user
    row (a deleted account) is simply absent from the map, and the wire keeps
    the uuid alone.
    """
    return {who: named.label for who, named in (await _named_users(session, owner_ids)).items()}


@dataclass(frozen=True, slots=True)
class _Named:
    """One user as a page names them: display name, address, and whether they
    hold an active membership in the org the page belongs to."""

    display: str
    email: str
    in_org: bool

    @property
    def label(self) -> str:
        """What an owner or holder column shows: the name, else the address."""
        return self.display or self.email


async def _named_users(
    session: AsyncSession, user_ids: Iterable[str], *, org_team_id: uuid.UUID | None = None
) -> dict[str, _Named]:
    """Each id that names a user, with its names and whether it is an active
    member of ``org_team_id`` (never, when no org is given).

    The membership rides along because not every id resolved here is one the caller
    may be told a name for: an owner is a row the caller was already handed,
    while a lease holder is a principal, and a principal this org never
    admitted must not be named to it. One statement answers both questions, so
    the visibility check costs nothing beyond the lookup the page already makes.
    """
    wanted: set[uuid.UUID] = set()
    for raw in user_ids:
        try:
            wanted.add(uuid.UUID(raw))
        except (ValueError, AttributeError, TypeError):
            continue
    if not wanted:
        return {}
    in_org = false() if org_team_id is None else active_member_of(org_team_id)
    rows = await session.execute(
        select(User.id, User.first_name, User.last_name, User.email, in_org).where(
            User.id.in_(wanted)
        )
    )
    return {
        str(user_id): _Named(f"{first} {last}".strip(), email, bool(member))
        for user_id, first, last, email, member in rows.all()
    }


#: Hard bound on the team walk below, mirroring the one every other tenancy walk
#: carries: the walk is recursive SQL, so a malformed `parent_team_id` cycle
#: costs a bounded number of rows and yields a truncated answer rather than
#: spinning forever inside the event loop.
_MAX_TEAM_DEPTH = 64


async def principal_names(
    session: AsyncSession,
    principals: Iterable[tuple[str, uuid.UUID]],
    *,
    org_team_id: uuid.UUID,
) -> dict[tuple[str, uuid.UUID], str]:
    """The label to show beside each grantee, in two statements for the dialog.

    A grant reaches the wire as ``(kind, uuid)``, which is not a thing a person
    can read, so the sharing surfaces resolve the names here — the grantee twin
    of :func:`owner_names`, batched for the same reason.

    It adds no answer to the no-oracle contract because it can only be asked
    about grants that already exist on a node the caller may read, and every
    one of those rows was put there by a route that refused a foreign principal
    before writing it (the ``files_share_principal_in_org`` trigger is the
    backstop). Both lookups are nonetheless scoped to ``org_team_id`` rather
    than trusting that: a row that somehow names another tenant resolves to no
    name at all instead of leaking one. A principal with no row — a deleted
    account, a disbanded team, a share link, which has no row anywhere — is
    simply absent from the map, and the id beside it is unchanged.
    """
    users: set[uuid.UUID] = set()
    teams: set[uuid.UUID] = set()
    for kind, principal_id in principals:
        if kind == "user":
            users.add(principal_id)
        elif kind in ("team", "org"):
            teams.add(principal_id)
    resolved: dict[tuple[str, uuid.UUID], str] = {}
    if users:
        rows = await session.execute(
            select(User.id, User.first_name, User.last_name, User.email).where(
                User.id.in_(users), active_member_of(org_team_id)
            )
        )
        for user_id, first, last, email in rows.all():
            resolved[("user", user_id)] = f"{first} {last}".strip() or email
    if teams:
        in_org = _org_team_cte(org_team_id)
        rows = await session.execute(
            select(in_org.c.id, in_org.c.name).where(in_org.c.id.in_(teams))
        )
        for team_id, name in rows.all():
            # The org root is a team row, so one lookup answers both kinds.
            resolved[("team", team_id)] = name
            resolved[("org", team_id)] = name
    return resolved


def _org_team_cte(org_team_id: uuid.UUID) -> CTE:
    """Every team of this org, the root included — a depth-bounded walk down."""
    anchor = (
        select(Team.id.label("id"), Team.name.label("name"), literal(0).label("depth"))
        .where(Team.id == org_team_id)
        .cte("share_org_teams", recursive=True)
    )
    child = aliased(Team)
    return anchor.union_all(
        select(child.id, child.name, anchor.c.depth + 1).where(
            child.parent_team_id == anchor.c.id,
            anchor.c.depth < _MAX_TEAM_DEPTH,
        )
    )


def _owner_id(item: Item) -> str | None:
    return item.attrs.owner if item.attrs is not None else None


def _holder_id(item: Item) -> str | None:
    """Who holds the lease over this node, as :func:`lease_wire` wrote it."""
    return item.lease.holder or None if item.lease is not None else None


async def with_owner_names(
    session: AsyncSession, items: Sequence[Item], *, org_team_id: uuid.UUID
) -> list[Item]:
    """The same items, carrying the names a row is read by — one statement.

    The owner's name labels ``attrs.owner``; the holder's labels
    ``lease.holder``, which is the one field of the lease facet that goes into a
    sentence a person reads ("In use by … on …"); and a member's home is
    labelled by its owner's display name (``home.ownerName``). All are
    principal uuids on the wire, all are resolved here, and they are resolved
    TOGETHER because a second lookup would be a second statement per page — the
    cost this batching exists to avoid.

    A node whose ``created_by`` is empty (the server made it) or whose owner no
    longer has a user row keeps ``ownerName`` at ``None``: the label is absent
    rather than invented. An owner that is a machine reads
    :data:`MACHINE_OWNER_NAME`. A holder that resolves to nothing keeps its id, which
    is still true and is still what the server fences a write on.

    The name also lands in ``lease.holderName``, beside the id rather than over
    it: ``holder`` is a name when the holder is a person and an id when it is a
    machine, and nothing on the wire tells the two apart. A client that has
    ``holderName`` knows it has a person's name, and a machine holder — whose
    principal is an allocation and has no user row — leaves it empty rather
    than borrowing one.

    The holder and a home's owner are named only when they are members of
    ``org_team_id``, the org whose drive this is. An owner reached the wire
    because the caller was handed the row; a holder is a principal written on a
    lease, so naming one this org never admitted would tell a member somebody
    else's name. A home is labelled by its owner's display name alone — never
    their address, which is what the home label exists to keep off the screen —
    and by the neutral fallback when there is no name to show.
    """
    wanted = [who for who in map(_owner_id, items) if who is not None]
    wanted.extend(who for who in map(_holder_id, items) if who is not None)
    wanted.extend(item.path_home.owner_id for item in items if item.path_home is not None)
    resolved = await _named_users(session, wanted, org_team_id=org_team_id)
    machines = await _machine_ids(
        session, {who for who in map(_owner_id, items) if who is not None} - resolved.keys()
    )

    def home_named(facet: HomeFacet | None) -> HomeFacet | None:
        if facet is None:
            return None
        found = resolved.get(facet.owner_id)
        shown = found.display if found is not None and found.in_org else ""
        return facet.model_copy(update={"owner_name": shown or HOME_FALLBACK_NAME})

    filled: list[Item] = []
    for item in items:
        owner_id = _owner_id(item) or ""
        owner = resolved.get(owner_id)
        update: dict[str, object] = {
            "owner_name": owner.label
            if owner is not None
            else (MACHINE_OWNER_NAME if owner_id in machines else None)
        }
        holder = resolved.get(_holder_id(item) or "")
        if holder is not None and holder.in_org and item.lease is not None:
            named = holder.label
            update["lease"] = item.lease.model_copy(update={"holder": named, "holder_name": named})
        home = home_named(item.home)
        path_home = home_named(item.path_home)
        update["home"] = home
        update["path_home"] = path_home
        if (
            path_home is not None
            and item.parent_name is not None
            and item.parent_id == path_home.node_id
        ):
            # A row that sits directly in a home is located by the home's label.
            update["parent_name"] = path_home.owner_name
        filled.append(item.model_copy(update=update))
    return filled


async def _machine_ids(session: AsyncSession, owner_ids: Iterable[str]) -> set[str]:
    """The ids among ``owner_ids`` that name a machine rather than a person.

    A box on its own credential used to stamp its machine id as the owner of
    what it wrote. Those rows are labelled :data:`MACHINE_OWNER_NAME`, never by
    the id. Only the constant label leaves: the id was already on the row the
    caller was handed, and the machine's name and org stay unread.
    """
    wanted: set[uuid.UUID] = set()
    for raw in owner_ids:
        try:
            wanted.add(uuid.UUID(raw))
        except ValueError:
            continue
    if not wanted:
        return set()
    rows = await session.execute(
        select(ComputeAllocation.id).where(ComputeAllocation.id.in_(wanted))
    )
    return {str(machine_id) for machine_id in rows.scalars()}


async def with_owner_name(session: AsyncSession, item: Item, *, org_team_id: uuid.UUID) -> Item:
    """:func:`with_owner_names` for the routes that render a single item."""
    return (await with_owner_names(session, [item], org_team_id=org_team_id))[0]


async def object_titles(session: AsyncSession, object_ids: Iterable[str]) -> dict[str, str]:
    """What each object on the page currently calls itself, in ONE statement.

    A node's NAME is not its object's title. The name was minted from the title
    at creation and is a filesystem name from then on: an untitled chat is named
    after its logical id, and renaming a chat rewrites the row, never the folder.
    So the listing has to read the titles rather than trust the names — batched
    for the page, the way owners and leases are, because a statement per row is
    the cost every other lookup here exists to avoid.

    Nothing is disclosed that the caller was not already handed: only the ids of
    nodes they have been allowed to read reach this. A deleted object is simply
    absent from the map and the facet keeps its empty title.
    """
    titles, _spares = await _object_rows(session, object_ids)
    return titles


async def with_object_titles(session: AsyncSession, items: Sequence[Item]) -> list[Item]:
    """The same items, each object-backed one carrying its object's live title.

    Costs nothing on a page with no object rows — the common case — because the
    statement is only issued when there is an id to resolve.
    """
    wanted = [item.object.id for item in items if item.object is not None and item.object.id]
    if not wanted:
        return list(items)
    resolved, spares = await _object_rows(session, wanted)
    filled: list[Item] = []
    for item in items:
        facet = item.object
        if facet is not None and facet.id in spares:
            # A chat warmed ahead of its owner's first message has a folder
            # so the box can lease it, and no listing: it is not a chat to
            # anyone until that first message claims it.
            continue
        title = resolved.get(facet.id) if facet is not None else None
        if facet is None or title is None:
            filled.append(item)
            continue
        filled.append(item.model_copy(update={"object": facet.model_copy(update={"title": title})}))
    return filled


async def _object_rows(
    session: AsyncSession, object_ids: Iterable[str]
) -> tuple[dict[str, str], set[str]]:
    """Titles by id, and the ids that are spares — one statement for both."""
    wanted: set[uuid.UUID] = set()
    for raw in object_ids:
        try:
            wanted.add(uuid.UUID(raw))
        except (ValueError, AttributeError, TypeError):
            continue
    if not wanted:
        return {}, set()
    rows = await session.execute(
        select(
            WorkspaceObject.id, WorkspaceObject.title, WorkspaceObject.spec["spare"].as_boolean()
        ).where(WorkspaceObject.id.in_(wanted))
    )
    titles: dict[str, str] = {}
    spares: set[str] = set()
    for object_id, title, spare in rows.all():
        titles[str(object_id)] = title or ""
        if spare:
            spares.add(str(object_id))
    # A chat node whose object row is gone outright — a deleted chat keeps
    # its row — is a reaped spare's folder waiting for its lease to let go
    # before the sweep purges it. Hidden like the spare it was.
    spares.update(str(object_id) for object_id in wanted if str(object_id) not in titles)
    return titles, spares


async def with_object_title(session: AsyncSession, item: Item) -> Item:
    """:func:`with_object_titles` for the routes that render a single item.

    The fold hides a spare chat's folder, which is a listing's rule: a spare
    belongs in nobody's page of rows. A read by id is not a listing — the
    caller has already been authorized for this exact node, and the box
    holding the spare's lease has to read it to mount it — so an item the
    fold drops comes back the way it went in rather than leaving no row to
    return.
    """
    folded = await with_object_titles(session, [item])
    return folded[0] if folded else item


#: The key under which a chat folder's facet names the node its files live at.
#: It rides the facet's experimental bag rather than a typed slot: the bag is
#: already on the wire, and the client reads exactly one key from it. A chat
#: with no working directory yet carries no key, and a client then opens the
#: chat folder itself.
CHAT_FILES_NODE_KEY = "files_node_id"


async def with_chat_files_targets(session: AsyncSession, items: Sequence[Item]) -> list[Item]:
    """The same items, each object folder naming the node its files live at.

    "View files" on a chat opens the chat's working directory, never the
    folder that wraps it with the chat's own records — and a person is never
    shown the directory's name, so the client cannot be left to find it by
    name. A template answers the same way for the same reason: it owns a
    working directory too, and what a reader wants to see is what is in it,
    not the README rendered beside it. Which types own one is the bridge's
    registry, so a new one needs no line here. The lookup is the bridge's,
    keyed on the one constant that names the child, and batched for the page
    the way titles are.
    """
    chats = [
        uuid.UUID(item.id)
        for item in items
        if item.kind == "folder"
        and item.object is not None
        and item.object.type in FOLDER_OBJECT_KINDS
    ]
    if not chats:
        return list(items)
    targets = await working_folder_nodes(session, chats)
    filled: list[Item] = []
    for item in items:
        facet = item.object
        target = targets.get(uuid.UUID(item.id)) if facet is not None else None
        if facet is None or target is None:
            filled.append(item)
            continue
        metadata = {**facet.metadata, CHAT_FILES_NODE_KEY: str(target)}
        filled.append(
            item.model_copy(update={"object": facet.model_copy(update={"metadata": metadata})})
        )
    return filled


async def with_object_facets(session: AsyncSession, items: Sequence[Item]) -> list[Item]:
    """Everything an object-backed row carries that is not a Files fact: its
    object's live title, and — for a chat — where its files live."""
    return await with_chat_files_targets(session, await with_object_titles(session, items))


async def with_object_facet(session: AsyncSession, item: Item) -> Item:
    """:func:`with_object_facets` for the routes that render a single item.

    Drops the same way :func:`with_object_title` does, for the same reason,
    and answers the same: the item, unfolded.
    """
    folded = await with_object_facets(session, [item])
    return folded[0] if folded else item


def _capabilities(access: EffectiveAccess) -> Capabilities:
    """The wire capability map.

    Six of the ten flags are library actions outright — read, write, share and
    the three lease rungs. The rest are
    derived rather than invented: a rename is a write (POSIX renames a name in
    place), a download is a read whose bytes may leave the platform, which is
    exactly ``EXPORT``, and ``canDelete`` is the *trash* — the ladder puts
    trashing on the writer rung beside rename and move, and reserves the
    library's ``DELETE`` for the permanent purge ``canPurge`` answers for.
    Deriving them here rather than adding actions to the ladder keeps the
    policy's vocabulary the only one.
    """
    resolved = capability_map(access)
    return Capabilities(
        can_read=resolved.can(FilesAction.READ),
        can_write=resolved.can(FilesAction.WRITE),
        can_share=resolved.can(FilesAction.SHARE),
        can_delete=resolved.can(FilesAction.WRITE),
        can_purge=resolved.can(FilesAction.DELETE),
        can_rename=resolved.can(FilesAction.WRITE),
        can_download=resolved.can(FilesAction.READ) and resolved.can(FilesAction.EXPORT),
        can_lease=resolved.can(FilesAction.LEASE),
        can_lease_request=resolved.can(FilesAction.LEASE_REQUEST),
        can_lease_force=resolved.can(FilesAction.LEASE_FORCE),
        refusals=dict(resolved.refusals),
    )


def _content_tag(node: FileNode) -> str:
    """The tag a surface keys a file's content on: the etag, and -- while the
    holder's report stands -- the report's modified time and sequence with it.

    The etag alone is what ``If-Match`` compares, so it cannot carry anything
    else; a report is not a change to the node a writer would race on. But a
    preview keyed on the etag would sit on the old bytes while the disk moved
    on, so the tag a reader keys on moves with the report too.
    """
    if node.kind != "file" or node.holder_size is None:
        return str(node.etag)
    return f"{node.etag}.{node.holder_mtime_ns or 0}.{node.holder_seq or 0}"


def to_item(
    node: FileNode,
    chain: Sequence[FileNode],
    access: EffectiveAccess,
    lease: LeaseSnapshot | None = None,
    *,
    version: FileVersion | None = None,
    ctag: str | None = None,
    starred: bool = False,
    readable_chain: Collection[uuid.UUID] | None = None,
) -> Item:
    """The wire item for ``node`` as ``access``'s caller sees it.

    ``lease`` is passed in rather than derived because a lease is a fact
    about a *subtree*: the caller that resolved the leased ancestor already
    knows it, and re-deriving it per item would be one query per row.

    ``starred`` is passed in for the same reason and a sharper one: a star is
    per user, so it cannot be read off ``node`` at all — the node bit says only
    that *someone* starred it. The caller that ran the listing statement already
    carries the answer for this caller in ``Page.starred_ids``.

    ``readable_chain`` is the subset of the chain's ids this caller may READ,
    resolved in ONE batch by the surface that already decided the page; it cuts
    the path so a grant on one file does not publish the names of the folders
    above it. Left out, nothing is cut — for a caller that reached the node
    through the chain it owns.
    """
    name_flags = names.flags(node.name)
    etag = str(node.etag)
    home = _home(node)
    capabilities = _capabilities(access)
    if home is not None:
        # The namespace refuses a home's rename; the control says so up front.
        capabilities = capabilities.model_copy(
            update={
                "can_rename": False,
                "refusals": {**capabilities.refusals, "canRename": "files.home_name_fixed"},
            }
        )
    return Item(
        id=str(node.id),
        ino=node.ino,
        drive_id=str(node.drive_id),
        kind=_kind(node),
        subtype=node.subtype,
        name=names.display(node.name),
        name_display=node.name_display or names.display(node.name),
        name_encoding=node.name_encoding,
        name_flags=NameFlagsWire(
            windows_safe=name_flags.windows_safe,
            macos_safe=not bool(node.flags_names.get("macos_collision", False)),
            display_warning="unsafe characters" if name_flags.display_warning else None,
        ),
        path_bytes=_path_bytes(node, chain, readable_chain),
        parent_id=str(node.parent_id) if node.parent_id is not None else None,
        path=None,
        etag=etag,
        ctag=ctag if ctag is not None else _content_tag(node),
        attrs=_attrs(node),
        file=_file(node, version),
        symlink=_symlink(node),
        object=_object(node),
        special=_special(node),
        home=home,
        path_home=_path_home(node, chain, readable_chain),
        lease=lease_wire(lease),
        live=live_wire(lease, node, version),
        stale=_stale(lease),
        trust=node.trust,
        locked=node.state == "locked",
        held=bool(node.retention_label_id is not None),
        capabilities=capabilities,
        shared=node.acl_id is not None,
        starred=starred,
        trashed=node.trashed_at is not None,
        conflict_of=_conflict_of(node),
    )


def _conflict_of(node: FileNode) -> str | None:
    """The node a conflicted copy was split from, off the row itself.

    Recorded in the node's metadata when the drive minted the copy, so the
    chip costs no statement on a listing.
    """
    value = (node.node_metadata or {}).get("conflict_of")
    return value if isinstance(value, str) and value else None


async def with_lease_machines(files: FilesContext, items: list[Item]) -> list[Item]:
    """The same items, each lease naming the machine that holds it.

    ``lease.machine`` is the id the holder registered itself under. For a box
    that is the allocation's uuid — the same uuid the holder principal is, so a
    line built out of the facet alone names the machine twice and names it as
    an id both times. The allocation's own name (``team-gpu-box``) is what a
    person recognises, and it is resolved here, once for the page, the way the
    owner names and the chat's title are.

    Scoped to this drive's org: an id that belongs to no allocation of this org
    resolves to nothing rather than to another tenant's machine. A holder that
    sent a name of its own instead of an id — a laptop mount — keeps an empty
    name here, and the client falls back to what the holder called itself.
    """
    wanted: set[uuid.UUID] = set()
    for item in items:
        machine = item.lease.machine if item.lease is not None else ""
        if not machine:
            continue
        try:
            wanted.add(uuid.UUID(machine))
        except ValueError:
            continue
    if not wanted:
        return items
    rows = (
        (
            await files.repo.session.execute(
                select(ComputeAllocation).where(
                    ComputeAllocation.id.in_(wanted),
                    ComputeAllocation.org_team_id == files.repo.scope.org_team_id,
                )
            )
        )
        .scalars()
        .all()
    )
    names = {str(alloc.id): alloc.name for alloc in rows if alloc.name}
    reader = files.ctx.effective_user_id
    runs = {str(alloc.id) for alloc in rows if alloc.user_id == reader}
    now = files.clock.now()
    silent = {str(alloc.id) for alloc in rows if machine_state(alloc, now=now) == UNREACHABLE}
    filled: list[Item] = []
    for item in items:
        facet = item.lease
        if facet is None:
            filled.append(item)
            continue
        update: dict[str, object] = {}
        named = names.get(facet.machine)
        if named is not None:
            update["machine_name"] = named
        if facet.machine in runs and facet.holder == facet.machine and facet.yours == "none":
            # Only a box holds its lease as itself: its holder IS the machine
            # id the server proved. A person's mount carries a machine name of
            # their own choosing, which must not make somebody else's box
            # read as the holder.
            update["yours"] = "box"
        if named is not None or facet.machine in silent:
            # The folder's status, decided again now that the machine has a
            # name and the server knows whether it still answers.
            named_facet = facet.model_copy(update=update)
            update["status"] = lease_status(
                named_facet,
                behind=item.stale is True,
                machine_unreachable=facet.machine in silent,
            )
        filled.append(
            item.model_copy(update={"lease": facet.model_copy(update=update)}) if update else item
        )
    return filled


__all__ = [
    "CHAT_FILES_NODE_KEY",
    "DRIVE_ROOT_NAME",
    "HOME_FALLBACK_NAME",
    "PathPart",
    "display_path",
    "head_facts",
    "holder_facet",
    "home_labels",
    "lease_status",
    "lease_wire",
    "object_titles",
    "owner_names",
    "path_parts",
    "principal_names",
    "readable_path_bytes",
    "to_item",
    "with_chat_files_targets",
    "with_lease",
    "with_lease_machines",
    "with_location",
    "with_object_facet",
    "with_object_facets",
    "with_object_title",
    "with_object_titles",
    "with_owner_name",
    "with_owner_names",
    "with_path",
]
