"""The org tree skeleton: the drive, its root, ``/Shared``, ``/home`` and ``/Teams``.

One org, one drive, one shape. The root ``/`` carries **no grants at all** — a
grant there would trickle down to every node in the org and hand every member
everything — so it, ``home/`` and ``Teams/`` are *traversal-only containers*:
every member may list them, and a listing returns only the children that member
can already reach by some other grant. They are signposts, not permissions.

Everything here is idempotent under a race, and idempotent by a database
constraint rather than by a read-then-write check. Each row is written with
``INSERT … ON CONFLICT DO NOTHING`` against the unique index that already
defines its uniqueness — ``(org_team_id, region, store_id)`` for a dedup domain,
``(org_team_id, kind)`` for a drive, the partial ``(parent_id, name) WHERE
trashed_at IS NULL`` for a folder — and then reads the row back. A loser's
insert affects no row and its read blocks on the winner's index entry until that
transaction commits, so it returns the winner's row. Two sessions calling
``ensure_org_drive`` concurrently therefore produce one drive, one root and one
``/Shared``, not two of each.

The skeleton below the drive row is built under the drive's own ``FOR UPDATE``
lock, which is the first lock in the fixed drive → parent → node order every
Files mutation takes.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Final, cast

from sqlalchemy import CursorResult, text

from alkera_core.authz.principal import ActingContext
from alkera_core.config import settings as default_settings
from alkera_core.files import acl_intern
from alkera_core.files.authz.actions import FilesAction
from alkera_core.files.authz.decider import (
    DEFAULT_DECIDER,
    AccessDecider,
    AccessFacts,
    effective_role,
)
from alkera_core.files.authz.defaults import (
    ORG_POLICY_RESTRICTED,
    DriveFolder,
    default_acl,
)
from alkera_core.files.authz.grants import (
    ORIGIN_DRIVE_DEFAULT,
    FilesGrantSource,
    GrantSource,
)
from alkera_core.files.checkpoints import Checkpoints, NoopCheckpoints
from alkera_core.files.errors import ContainerReadOnly, InvalidRequest, NotFound
from alkera_core.files.history import actor_ref, emit_node_changed, record
from alkera_core.files.ids import AclId, DriveId, NodeId
from alkera_core.files.ino import InoAllocator
from alkera_core.files.names import InvalidName, escape_to_name, name_key, validate
from alkera_core.files.path_labels import ino_label
from alkera_core.files.repo import FilesRepo
from alkera_core.models.files.stores import FileDrive
from alkera_core.models.files.tree import FileNode

DomainCreated = Callable[[uuid.UUID], Awaitable[None]]
"""Told the id of a dedup domain in the call that inserted it."""

#: The three folders every org drive is born with. ``home`` and ``Teams`` are
#: traversal-only containers; ``Shared`` is the org root team's own space.
SHARED_NAME = b"Shared"
HOME_NAME = b"home"
TEAMS_NAME = b"Teams"

#: The folder inside a member's home that a new chat's folder is created in.
#: Nothing about it is special beyond the name: it is an ordinary folder its
#: owner may rename, move, fill or delete, and it is created on demand by
#: whoever makes the first chat rather than by the drive skeleton, so a member
#: who has never opened a chat does not carry an empty folder for one.
CHATS_NAME = b"Chats"

#: The folder inside a member's home that a new chat template's folder is
#: created in — the same kind of ordinary, on-demand folder ``Chats`` is, and
#: the place a template a member saves goes without being asked where to put it.
CHAT_TEMPLATES_NAME = b"Chat Templates"

#: What a caller is told when they aim a write at a signpost. Prose, not a
#: code: it is shown to whoever clicked, and it names the two places that do
#: take files rather than only saying no.
CONTAINER_READONLY_MESSAGE: Final = "Files live in your home folder or a team folder, not here."


async def ensure_org_drive(
    repo: FilesRepo,
    ctx: ActingContext,
    org_team_id: uuid.UUID,
    *,
    store_id: uuid.UUID,
    ino_allocator: InoAllocator | None = None,
    org_policy: str = ORG_POLICY_RESTRICTED,
    quota_bytes: int | None = None,
    quota_nodes: int | None = None,
    checkpoints: Checkpoints | None = None,
    on_domain_created: DomainCreated | None = None,
) -> FileDrive:
    """The org's drive, with its root, ``/Shared``, ``/home`` and ``/Teams``.

    ``on_domain_created`` runs once, in the call that actually inserted the
    dedup domain, with the new domain's id. It is how the caller that holds a
    store stamps the domain's prefix as this deployment's before a byte lands
    under it; this module holds no store and never learns what the hook does.

    Safe to call on every session open: an existing drive is read, never
    rebuilt, and a drive whose skeleton is half-built (a crash between the drive
    row and ``/Shared``) is completed rather than duplicated.

    ``org_policy`` decides whether ``/Shared`` grants the org ``writer`` or
    ``reader``; it defaults to the restricted policy, so an unconfigured org
    gets the narrower of the two.

    ``quota_bytes`` and ``quota_nodes`` are the ceilings a brand-new drive is
    born with. They default to the deployment's configured allowance rather
    than to a literal, because a drive is created by the org's *first* request
    and there is no provisioning step between that request and the upload it
    came to make: a drive born with no room refuses that upload with a ``507``.
    A caller that knows the org's plan (a paid tier, a teardown) passes the
    ceilings it wants; nothing else needs to know they exist.
    """
    if org_team_id != repo.scope.org_team_id:
        raise InvalidRequest("ensure_org_drive must run in the org it is creating a drive for")
    # Every Files request comes through here, and for all but an org's first
    # one the drive is already whole. That answer is two plain reads: no insert,
    # no row lock, no inode block. The drive row is the row every writer in the
    # org advances, so a build path that touched it on every request would park
    # every reader behind whichever write is holding it — and the connections
    # they wait on are the ones every other tenant shares.
    whole = await _whole_drive(repo)
    if whole is not None:
        return whole

    cp = checkpoints or NoopCheckpoints()
    allocator = ino_allocator or InoAllocator(repo)

    domain_id, domain_created = await _ensure_domain(repo, store_id=store_id)
    if domain_created and on_domain_created is not None:
        await on_domain_created(domain_id)
    # Between the two inserts a second session must be able to arrive and race
    # for the same drive; this is where a test parks it.
    await cp.reach("drives.after_domain_insert")
    drive = await _ensure_drive_row(
        repo,
        store_id=store_id,
        domain_id=domain_id,
        quota_bytes=(
            quota_bytes if quota_bytes is not None else default_settings.files_quota_default_bytes
        ),
        quota_nodes=(
            quota_nodes if quota_nodes is not None else default_settings.files_quota_default_nodes
        ),
    )

    # The drive row is the first lock in the fixed order, and taking it here is
    # what makes the skeleton below built exactly once even if the ON CONFLICT
    # nets were not there.
    locked = await repo.lock_drive(DriveId(drive.id))
    if locked is None:  # pragma: no cover - the row was read a statement ago
        raise NotFound(f"drive {drive.id} vanished")
    drive = locked

    root = await _ensure_root(repo, ctx, drive, allocator)
    await cp.reach("drives.after_root")

    shared_acl = await acl_intern.intern(
        repo,
        default_acl(
            DriveFolder.SHARED,
            org_policy=org_policy,
            org_team_id=org_team_id,
        ),
    )
    await _ensure_child(
        repo, ctx, drive, root, SHARED_NAME, allocator, acl_id=shared_acl, traversal_only=False
    )
    await _ensure_child(repo, ctx, drive, root, HOME_NAME, allocator, traversal_only=True)
    await _ensure_child(repo, ctx, drive, root, TEAMS_NAME, allocator, traversal_only=True)
    return drive


async def ensure_home_folder(
    repo: FilesRepo,
    ctx: ActingContext,
    user_id: uuid.UUID,
    *,
    ino_allocator: InoAllocator | None = None,
    checkpoints: Checkpoints | None = None,
) -> FileNode:
    """``/home/<id>/`` for one member, granted ``{user: owner}`` and nothing else.

    Called on membership and again on first visit, so it must be idempotent: it
    is, by the partial unique index on ``(parent_id, name)``. Two sessions
    racing it produce one folder and both return it.

    The folder's name is :func:`home_folder_name` — the member's id, never
    anything read off their account. What a person reads for a home is its
    owner's current display name, resolved by whoever renders it
    (:func:`home_owner` says which member a node is the home of); the stored
    name is an address, so an email or a name change leaves nothing behind in
    the tree.
    """
    drive, container = await _skeleton(repo, HOME_NAME)
    acl_id = await acl_intern.intern(
        repo,
        default_acl(
            DriveFolder.HOME,
            org_policy=ORG_POLICY_RESTRICTED,
            org_team_id=repo.scope.org_team_id,
            subject_id=user_id,
        ),
    )
    cp = checkpoints or NoopCheckpoints()
    await cp.reach("drives.before_home_insert")
    return await _ensure_child(
        repo,
        ctx,
        drive,
        container,
        home_folder_name(user_id).encode("utf-8"),
        ino_allocator or InoAllocator(repo),
        acl_id=acl_id,
        traversal_only=False,
        created_by=user_id,
        subtype=HOME_SUBTYPE,
    )


def home_folder_name(user_id: uuid.UUID) -> str:
    """The name a member's home folder is stored under: their user id.

    Names under ``home/`` only have to be unique within one org's drive, and a
    user id already is. Nothing about the member's address or name goes in:
    the stored name is never what a person reads (they read the owner's display
    name, resolved at read time), so it carries nothing that could outlive a
    change to either.
    """
    return str(user_id)


#: The ``subtype`` a member's home folder is stamped with, so a renderer can
#: tell a home from an ordinary folder off the row itself.
HOME_SUBTYPE: Final = "home"

#: Depth of a member's home: the root is 0 and ``home/`` is 1.
HOME_DEPTH: Final = 2


def home_owner(node: FileNode) -> uuid.UUID | None:
    """The member ``node`` is the home folder of, or ``None`` for any other node.

    A home is a folder stamped :data:`HOME_SUBTYPE`, directly under ``home/``,
    named :func:`home_folder_name` of the member who created it — all three,
    so a copy of a home elsewhere, or a folder somebody named after an id, is
    an ordinary folder. Pure: it reads only the row.
    """
    if (
        node.kind != "folder"
        or node.subtype != HOME_SUBTYPE
        or node.depth != HOME_DEPTH
        or node.created_by is None
    ):
        return None
    if bytes(node.name) != home_folder_name(node.created_by).encode("utf-8"):
        return None
    return node.created_by


@dataclass(frozen=True, slots=True)
class HomeLookup:
    """What one member has directly under ``home/``.

    ``home`` is their home folder, or ``None`` when nothing there carries a
    home's identity. ``extras`` are other folders that ARE a home of theirs by
    the owner grant (:func:`_owns_as_home`) but not by name — a home ensured
    under the address spelling an earlier release derived — whose contents
    belong in ``home`` and which must never be nested inside it. ``strays`` is
    everything else they created straight into the container — a folder with
    some other name, a file — which a signpost is not meant to hold and which
    their home is the place for.
    """

    home: FileNode | None
    strays: tuple[FileNode, ...]
    extras: tuple[FileNode, ...] = ()


async def lookup_home(
    repo: FilesRepo, user_id: uuid.UUID, *, drive: FileDrive | None = None
) -> HomeLookup:
    """``/home/<them>`` for one member, and whatever else of theirs sits beside it.

    A home is identified, never guessed: a live **folder** under ``home/`` that
    ``created_by`` stamps with the member's own id — what
    :func:`ensure_home_folder` writes — **and** whose name is
    :func:`home_folder_name` of that id. Matching on the creator alone took the
    first folder a founder had made under the container for her home.

    One statement, and ``drive`` is a parameter because the caller that asks
    this on every drive read has already resolved the drive. The strays ride
    the same statement, so the steady state — one folder, nothing beside it —
    costs no second read.
    """
    known = drive if drive is not None else await repo.drive_for_org()
    if known is None or known.root_node_id is None:
        raise NotFound("this org has no drive; call ensure_org_drive first")
    rows = await repo.children_of_container(
        NodeId(known.root_node_id), HOME_NAME, created_by=user_id
    )
    wanted = home_folder_name(user_id).encode("utf-8")
    homes = [row for row in rows if row.kind == "folder" and bytes(row.name) == wanted]
    others = [row for row in rows if row.kind != "folder" or bytes(row.name) != wanted]
    # Only a folder with an ACL of its own can carry the owner grant, so the
    # steady state — one home, nothing beside it — reads no ACL at all.
    extras: list[FileNode] = []
    for row in others:
        if row.kind == "folder" and row.acl_id is not None:
            acl = await repo.acl(AclId(row.acl_id))
            if acl is not None and _owns_as_home(acl.body, user_id):
                extras.append(row)
    strays = tuple(row for row in others if row not in extras)
    return HomeLookup(home=homes[0] if homes else None, strays=strays, extras=tuple(extras))


def _owns_as_home(body: Iterable[Any], user_id: uuid.UUID) -> bool:
    """Whether an interned ACL body carries ``user_id``'s home grant.

    The drive-default ``{user: owner}`` ACE that :func:`ensure_home_folder`
    mints for the member (``default_acl(DriveFolder.HOME, …)``). Nothing else
    writes a drive-default ACE naming a user — a folder a member makes or
    shares carries direct or inherited ACEs — so this identifies a home by its
    owner whatever it is called. Revision 0171 renames homes by the same rule.
    """
    for ace in body:
        if (
            isinstance(ace, dict)
            and ace.get("origin") == ORIGIN_DRIVE_DEFAULT
            and ace.get("principal_kind") == "user"
            and ace.get("principal_id") == str(user_id)
        ):
            return True
    return False


async def find_home_folder(
    repo: FilesRepo, user_id: uuid.UUID, *, drive: FileDrive | None = None
) -> FileNode | None:
    """``/home/<them>`` for one member, or ``None`` when they have no home yet.

    :func:`lookup_home`'s answer without the strays — same identity, same one
    statement.
    """
    return (await lookup_home(repo, user_id, drive=drive)).home


async def ensure_team_folder(
    repo: FilesRepo,
    ctx: ActingContext,
    team_id: uuid.UUID,
    *,
    team_name: str,
    ino_allocator: InoAllocator | None = None,
    org_policy: str = ORG_POLICY_RESTRICTED,
    checkpoints: Checkpoints | None = None,
) -> FileNode:
    """``/Teams/<team name>/`` for one non-root team.

    Team folders are **flat** siblings, never nested, because a parent-team
    member is not a member of a sub-team in the membership model: nesting them
    would let the tree's trickle-down hand a parent team reach it does not have.

    ``team_name`` is resolved by the caller for the same reason ``username`` is
    on :func:`ensure_home_folder`: the Files role cannot read ``teams``. The
    caller is also where "is this team in my org, and is it the root team?" is
    already known, which is why both checks are the caller's to make.
    """
    if not team_name:
        raise InvalidRequest("a team folder needs a team name")
    drive, container = await _skeleton(repo, TEAMS_NAME)
    # A team name is display text (``R/D`` is a fine name for a team), so its
    # folder takes the same escape a home's address does.
    name = escape_to_name(team_name.encode("utf-8"))
    acl_id = await acl_intern.intern(
        repo,
        default_acl(
            DriveFolder.TEAM,
            org_policy=org_policy,
            org_team_id=repo.scope.org_team_id,
            subject_id=team_id,
        ),
    )
    cp = checkpoints or NoopCheckpoints()
    await cp.reach("drives.before_team_insert")
    return await _ensure_child(
        repo,
        ctx,
        drive,
        container,
        name,
        ino_allocator or InoAllocator(repo),
        acl_id=acl_id,
        traversal_only=False,
    )


async def traversal_children(
    repo: FilesRepo,
    ctx: ActingContext,
    node: FileNode,
    *,
    facts: AccessFacts,
    grant_source: GrantSource | None = None,
    decider: AccessDecider = DEFAULT_DECIDER,
) -> Sequence[FileNode]:
    """The children of a traversal-only container this caller may actually read.

    This is what keeps ``/``, ``home/`` and ``Teams/`` from leaking: the folders
    themselves are listable by everyone, so a member's root listing shows
    ``Shared``, their own home and their own teams — and a listing of ``home/``
    shows only their own home, because every other home is filtered here. The
    filter is the same ``effective_role`` the policy uses, so a folder that is
    invisible in a listing is also refused when addressed directly.
    """
    drive = await repo.drive(DriveId(node.drive_id))
    if drive is None:
        raise NotFound(f"drive {node.drive_id}")
    source = grant_source or FilesGrantSource()
    visible: list[FileNode] = []
    for child in await repo.siblings(NodeId(node.id)):
        chain = await repo.chain(child)
        grants = await source.grants_for(repo, child, chain)
        access = effective_role(ctx, child, chain, grants, drive, facts, decider=decider)
        if access.allows(FilesAction.READ):
            visible.append(child)
    visible.sort(key=lambda row: (row.name_key, row.id))
    return visible


def assert_traversal_rules(node: FileNode) -> None:
    """Refuse a direct write into a traversal-only container.

    A container is a signpost: what stands under it is the set of folders
    access is granted on — one per member, one per team — and each of those is
    born from :func:`ensure_home_folder` / :func:`ensure_team_folder` with the
    grant that makes it reachable. A node a *caller* puts there has no grant of
    its own and inherits none, because the container carries none to inherit:
    it is reachable by nobody but an org admin, grantable by nobody, and its
    owner's Files surface never shows it.

    So the answer is the same for every kind and every caller, an org admin
    included — the rule belongs to the container, not to the role. Only the
    ensure writes there, and it does not ask this.
    """
    if node.traversal_only:
        raise ContainerReadOnly(CONTAINER_READONLY_MESSAGE)


def assert_grantable(node: FileNode) -> None:
    """Refuse a grant on a traversal-only container.

    Grants trickle **down**, so a grant on ``/`` or ``home/`` would hand its
    holder every node beneath it — every member's home included. There is no
    way to reduce a grant once made, so the only safe answer is to refuse it at
    the top.
    """
    if node.traversal_only:
        raise InvalidRequest("a traversal-only container carries no grants")


# ---- the pieces -----------------------------------------------------------

#: The folders a drive is not whole without.
_SKELETON: Final = (SHARED_NAME, HOME_NAME, TEAMS_NAME)


async def _whole_drive(repo: FilesRepo) -> FileDrive | None:
    """The org's drive when its root and all three skeleton folders are live.

    ``None`` for anything less — no drive, a drive with no root, a skeleton
    folder missing — which sends the caller down the building path, where the
    inserts and the drive lock decide the races. Reads only, so it can never
    wait on a writer.
    """
    drive = await repo.drive_for_org()
    if drive is None or drive.root_node_id is None:
        return None
    live = await repo.live_children_named(NodeId(drive.root_node_id), _SKELETON)
    return drive if live == len(_SKELETON) else None


async def _ensure_domain(repo: FilesRepo, *, store_id: uuid.UUID) -> tuple[uuid.UUID, bool]:
    """The org's dedup domain for this store, and whether this call created it."""
    org_id = repo.scope.org_team_id
    inserted = await repo.session.execute(
        text(
            "INSERT INTO dedup_domains "
            "(id, org_team_id, region, store_id, chunker_seed, hmac_key_id) "
            "VALUES (:id, :org, '', :store, ''::bytea, '') "
            "ON CONFLICT (org_team_id, region, store_id) DO NOTHING"
        ),
        {"id": uuid.uuid4(), "org": org_id, "store": store_id},
    )
    found = (
        await repo.session.execute(
            text(
                "SELECT id FROM dedup_domains "
                "WHERE org_team_id = :org AND region = '' AND store_id = :store"
            ),
            {"org": org_id, "store": store_id},
        )
    ).scalar_one_or_none()
    if found is None:  # pragma: no cover - the insert above just guaranteed it
        raise RuntimeError("dedup domain is not readable in this transaction")
    created = cast("CursorResult[Any]", inserted).rowcount == 1
    return uuid.UUID(str(found)), created


async def _ensure_drive_row(
    repo: FilesRepo,
    *,
    store_id: uuid.UUID,
    domain_id: uuid.UUID,
    quota_bytes: int,
    quota_nodes: int,
) -> FileDrive:
    """The org's one drive row. ``next_ino`` starts at 1: no ino is ever reused.

    The ceilings are bound parameters, never literals: this INSERT runs on the
    org's first Files request, so whatever it writes is what that request's own
    upload is measured against.
    """
    org_id = repo.scope.org_team_id
    await repo.session.execute(
        text(
            "INSERT INTO file_drives "
            "(id, org_team_id, kind, store_id, dedup_domain_id, "
            " quota_bytes, quota_nodes, next_ino) "
            "VALUES (:id, :org, 'org', :store, :domain, :quota_bytes, :quota_nodes, 1) "
            "ON CONFLICT (org_team_id, kind) DO NOTHING"
        ),
        {
            "id": uuid.uuid4(),
            "org": org_id,
            "store": store_id,
            "domain": domain_id,
            "quota_bytes": quota_bytes,
            "quota_nodes": quota_nodes,
        },
    )
    drive = await repo.drive_for_org()
    if drive is None:  # pragma: no cover - the insert above just guaranteed it
        raise RuntimeError("drive is not readable in this transaction")
    return drive


async def _ensure_root(
    repo: FilesRepo, ctx: ActingContext, drive: FileDrive, allocator: InoAllocator
) -> FileNode:
    """The drive's root: traversal-only, no grants, ``path_ids`` its own label."""
    if drive.root_node_id is not None:
        existing = await repo.node(NodeId(drive.root_node_id))
        if existing is not None:
            return existing
    node_id = uuid.uuid4()
    ino = await allocator.allocate(DriveId(drive.id))
    root = FileNode(
        id=node_id,
        ino=ino,
        drive_id=drive.id,
        org_team_id=drive.org_team_id,
        parent_id=None,
        kind="folder",
        name=b"",
        name_display="",
        name_key="",
        path_ids=ino_label(ino),
        depth=0,
        traversal_only=True,
        created_by=_actor(ctx),
    )
    await repo.add(root)
    await repo.flush()
    # Only the transaction that just wrote the root may claim it, so a
    # concurrent builder cannot repoint an established drive at a second root.
    await repo.session.execute(
        text(
            "UPDATE file_drives SET root_node_id = :root "
            "WHERE id = :drive AND org_team_id = :org AND root_node_id IS NULL"
        ),
        {"root": node_id, "drive": drive.id, "org": drive.org_team_id},
    )
    drive.root_node_id = node_id
    await _announce(repo, ctx, root, kind="create")
    return root


async def _ensure_child(
    repo: FilesRepo,
    ctx: ActingContext,
    drive: FileDrive,
    parent: FileNode,
    name: bytes,
    allocator: InoAllocator,
    *,
    acl_id: uuid.UUID | None = None,
    traversal_only: bool = False,
    created_by: uuid.UUID | None = None,
    subtype: str | None = None,
) -> FileNode:
    """One skeleton folder under ``parent``, created once.

    The idempotency is the partial unique index on ``(parent_id, name)``: the
    insert is attempted and lets the index decide, rather than checking first
    and creating in a window where a racer has already inserted.
    """
    # No traversal check here on purpose: this is the system path, and under a
    # signpost it is the only writer left. The name IS checked: every caller
    # derives it from something that was never proposed as a name, and one
    # that slipped past its derivation must fail here rather than become a node
    # every path walker splits in two.
    try:
        validate(name)
    except InvalidName as exc:
        raise InvalidRequest(f"a system folder name is not a valid name: {exc}") from exc
    node_id = uuid.uuid4()
    ino = await allocator.allocate(DriveId(drive.id))
    decoded = name.decode("utf-8", errors="replace")
    inserted = (
        await repo.session.execute(
            text(
                "INSERT INTO file_nodes "
                "(id, ino, drive_id, org_team_id, parent_id, kind, subtype, name, name_display, "
                " name_key, path_ids, depth, traversal_only, acl_id, default_acl_id, "
                " created_by, mode, uid, gid, nlink, size, rdev, atime_ns, mtime_ns, "
                " ctime_ns, birthtime_ns, etag, flags, flags_names, xattrs, metadata) "
                "VALUES (:id, :ino, :drive, :org, :parent, 'folder', :subtype, :name, :display, "
                " :key, CAST(:path AS ltree), :depth, :traversal, :acl, :acl, :by, "
                # A folder's POSIX bits and counters, spelled rather than left to
                # the ORM: this INSERT is hand-written so that ON CONFLICT can
                # decide the race, and a hand-written INSERT gets no Python-side
                # column defaults.
                " 493, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, "
                " '{}'::jsonb, '{}'::jsonb, '{}'::jsonb) "
                "ON CONFLICT (parent_id, name) WHERE trashed_at IS NULL DO NOTHING "
                "RETURNING id"
            ),
            {
                "id": node_id,
                "ino": ino,
                "drive": drive.id,
                "org": drive.org_team_id,
                "parent": parent.id,
                "subtype": subtype,
                "name": name,
                "display": decoded,
                "key": name_key(name),
                "path": f"{parent.path_ids}.{ino_label(ino)}",
                "depth": parent.depth + 1,
                "traversal": traversal_only,
                "acl": acl_id,
                "by": created_by if created_by is not None else _actor(ctx),
            },
        )
    ).scalar_one_or_none()
    if inserted is None:
        existing = await _child_named(repo, parent, name)
        if existing is None:  # pragma: no cover - DO NOTHING fired, so a row exists
            raise RuntimeError(f"child {name!r} of {parent.id} is neither inserted nor readable")
        return existing
    node = await repo.node(NodeId(node_id))
    if node is None:  # pragma: no cover - RETURNING said the row is there
        raise RuntimeError(f"node {node_id} is not readable after its own insert")
    await _announce(repo, ctx, node, kind="create")
    return node


async def _announce(repo: FilesRepo, ctx: ActingContext, node: FileNode, *, kind: str) -> None:
    """The history row and the outbox row every mutation owes, in this transaction.

    The outbox payload is ids only — no name and no path — because the event
    stream is read by principals who may not be allowed to know a folder is
    called ``Acquisition``.
    """
    await record(
        repo,
        ctx,
        node_id=NodeId(node.id),
        kind="create" if kind == "create" else "attrs",
        before=None,
        after={"kind": node.kind, "traversal_only": node.traversal_only},
    )
    await emit_node_changed(
        repo,
        ctx,
        node_id=NodeId(node.id),
        drive_id=DriveId(node.drive_id),
        version=node.etag,
    )


async def _skeleton(repo: FilesRepo, container: bytes) -> tuple[FileDrive, FileNode]:
    """The org's drive and one of its top-level containers.

    Raises rather than building the drive: a home folder for an org with no
    drive is a caller that skipped ``ensure_org_drive``, and silently creating
    one here would hide that in a path where ``store_id`` is not known.
    """
    drive = await repo.drive_for_org()
    if drive is None or drive.root_node_id is None:
        raise NotFound("this org has no drive; call ensure_org_drive first")
    root = await repo.node(NodeId(drive.root_node_id))
    if root is None:  # pragma: no cover - the drive points at it
        raise NotFound(f"drive {drive.id} has no root node")
    found = await _child_named(repo, root, container)
    if found is None:
        raise NotFound(f"drive {drive.id} has no {container.decode()}/ container")
    return drive, found


async def _child_named(repo: FilesRepo, parent: FileNode, name: bytes) -> FileNode | None:
    """The live child of ``parent`` whose name is exactly ``name`` (byte-exact)."""
    for child in await repo.siblings(NodeId(parent.id)):
        if bytes(child.name) == name:
            return child
    return None


def _actor(ctx: ActingContext) -> uuid.UUID:
    """Who ensured this drive, for any principal kind."""
    return actor_ref(ctx)


__all__ = [
    "CHATS_NAME",
    "CHAT_TEMPLATES_NAME",
    "CONTAINER_READONLY_MESSAGE",
    "HOME_DEPTH",
    "HOME_NAME",
    "HOME_SUBTYPE",
    "SHARED_NAME",
    "TEAMS_NAME",
    "HomeLookup",
    "assert_grantable",
    "assert_traversal_rules",
    "ensure_home_folder",
    "ensure_org_drive",
    "ensure_team_folder",
    "find_home_folder",
    "home_folder_name",
    "home_owner",
    "lookup_home",
    "traversal_children",
]
