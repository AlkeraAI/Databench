"""The bridge that gives every ``WorkspaceObject`` a node in the file tree.

A chat, a saved query, a promoted result and a board are rows in
``workspace_objects``; the object page stays their editor. They are *also*
files, because a member who opens the drive expects to find their chats where
they left them, to share one the way they share a spreadsheet, and to see it in
a mount. That is what an ``object`` node is: a row-backed node whose bytes are
served by the ``rows`` content provider and which materializes as a pointer
file, never a copy of the object.

Three rules shape this module.

**The node is part of the object's own transaction.** :func:`node_for_object`
is awaited by the object service inside the transaction that inserts the row,
so there is no window in which an object exists without a node and no
after-the-fact reconciler to write. A test proves it by querying the node in
the same uncommitted session and then rolling both back.

**The audience follows the object.** The node gets one direct grant derived
from ``visibility_scope`` — ``org`` becomes an org-wide reader ACE,
``team:<id>`` a reader ACE for that team, ``private`` nothing beyond the owner's
home grant — so a chat's node has exactly the audience the chat has today,
answered by the same decider as every other node.

**Deletion has one direction.** Deleting the object trashes its node
(:func:`tombstone_for_object`); trashing the node never touches the object row.
A file view must not be able to destroy a chat, and the node is a projection of
the object, not the other way round.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Iterable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Final, NamedTuple, Protocol

from sqlalchemy import and_, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from alkera_core.authz.principal import ActingContext
from alkera_core.db.tenant_session import stepped_out
from alkera_core.files import acl_intern, drives, history
from alkera_core.files.authz.decider import NO_DOWNLOAD_BIT, SEAL_SELF_ONLY_BIT
from alkera_core.files.authz.grants import FilesGrantSource, Grant, GrantOrigin, Principal
from alkera_core.files.authz.ladder import ROLE_READER
from alkera_core.files.checkpoints import Checkpoints, NoopCheckpoints
from alkera_core.files.clock import Clock, SystemClock
from alkera_core.files.errors import Conflict, InvalidRequest, NotFound
from alkera_core.files.ids import DriveId, NodeId
from alkera_core.files.ino import InoAllocator
from alkera_core.files.names import refused_in_a_name
from alkera_core.files.namespace import Namespace, NodeAttrs
from alkera_core.files.providers.derived_members import DERIVED_MEMBERS
from alkera_core.files.providers.registry import CONTEXT_MEMBER_SEPARATOR, context_member_type
from alkera_core.files.providers.registry import POINTER_EXTENSIONS as MOUNT_POINTER_EXTENSIONS
from alkera_core.files.repo import FilesRepo
from alkera_core.models.files.acl import FileShare
from alkera_core.models.files.tree import FileNode
from alkera_core.models.workspace_object import OBJECT_TYPES, WorkspaceObject

#: The pointer-file extension per object type. A registry rather than a format
#: string, so a new object type is one row here plus its content provider; it is
#: checked against ``OBJECT_TYPES`` at import so a type added to the model
#: without an extension fails loudly instead of producing unnamed nodes. (It
#: belongs beside the rest of the pointer contract in ``schemas/files/pointer``
#: once that module exists; the constant moves, its contents do not.)
#:
#: The spelling is the product's: a chat is a ``.alkerachat``, a saved query a
#: ``.alkeraquery``, a replication-context report a ``.alkerareport``. Values
#: are unique because :func:`alkera_core.files.providers.registry.object_type_of`
#: inverts this map to pick a node's renderer and its app route — two types
#: sharing one extension would serve the wrong one — so the promoted *result*
#: (a frozen table, not a re-runnable context) keeps its own ``.alkeraresult``.
POINTER_EXTENSIONS: Final[Mapping[str, str]] = {
    "chat": "alkerachat",
    "query": "alkeraquery",
    "result": "alkeraresult",
    "report": "alkerareport",
    "board": "alkeraboard",
    "app": "alkeraapp",
    "chat_template": "alkerachat.template",
    "workspace": "alkeraworkspace",
}

#: The ``mime_class`` an object node is filtered by in a listing. A chat and a
#: board read as documents, a saved query as code, a promoted result as a table.
OBJECT_MIME_CLASSES: Final[Mapping[str, str]] = {
    "chat": "document",
    "query": "code",
    "result": "tabular",
    "report": "document",
    "board": "document",
    "app": "code",
    "chat_template": "document",
    "workspace": "document",
}

#: The object type whose node is a folder rather than a pointer file.
#:
#: The population is deliberately mixed: chats created before this shipped keep
#: the ``object`` pointer node they were given, under their old ``.chat`` name.
#: No back-fill was written, because converting one in place means renaming it
#: (which recomputes the fold key a listing is indexed on), flipping its kind,
#: and allocating inos for three children inside a migration — and a chat that
#: is still a pointer is *correct*, only older: it opens, it is shared, its
#: lease is valid. What it does not have is the NO_DOWNLOAD bit, so an old chat
#: is still downloadable. That is the one behaviour this leaves on the table.
CHAT_TYPE: Final = "chat"

#: The object type a chat is started FROM: a folder holding the files the new
#: chat begins with and the brief its author wrote.
CHAT_TEMPLATE_TYPE: Final = "chat_template"

#: The object type that holds chats: one working tree several conversations
#: share, and the records of each.
WORKSPACE_TYPE: Final = "workspace"

#: A workspace folder's shared working tree, what every chat in it and every
#: person reads and writes.
WORKSPACE_FILES_FOLDER: Final = b"files"

#: Where a workspace keeps each of its chats' own folders (``<chat>.alkerachat``):
#: records a chat owns alone, kept out of the shared tree so a lease on
#: ``files/`` never nests with a chat's.
WORKSPACE_CHATS_FOLDER: Final = b".chats"

#: The chat folder child that IS the chat's working directory: where the agent
#: runs, the one directory a write is admitted into in every permission mode,
#: where everything a run produces lands — its working files and the report or
#: chart the user keeps, side by side at the top level — and therefore where a
#: file a person moves or uploads into the chat lands, so the agent finds it
#: without an ask. A person never sees the name: Files opens a chat's files AT
#: this node, and the trail names the conversation instead.
#:
#: The write fence is the whole chat folder, one level up, and the mode decides
#: a write there; the chat's own records beside this child (``manifest.json``,
#: the trace digest, ``.runtime/``) are refused in every mode. The constant
#: exists so the agent's tool binding, the brief that names its directory, the
#: destination a move or a copy onto a chat resolves to, the node Files opens
#: and the folder a produced file is looked up in are one folder by
#: construction. ``None`` names no child: a drop then lands at the chat's root,
#: and the box has no free scratch inside the folder to hand the agent.
CHAT_SANDBOX_FOLDER: Final[bytes | None] = b"scratch"

#: What a chat folder holds the moment it is created: its working directory
#: and nothing else — no folder for deliverables, none for what a person hands
#: it. Every one of those was a second name for the one place the agent reads
#: and writes, and a second answer to "where does my file go". Chats created
#: while an ``outputs/`` or an ``attachments/`` existed keep it as an ordinary
#: folder; nothing back-fills or removes it, and nothing looks there.
CHAT_FOLDER_CHILDREN: Final[tuple[bytes, ...]] = (
    () if CHAT_SANDBOX_FOLDER is None else (CHAT_SANDBOX_FOLDER,)
)

#: The ``mime_class`` a derived member is listed under, by the MIME it renders
#: as. A listing filters on the class, so a member has to land in the same
#: bucket an ordinary file of those bytes would.
_MEMBER_MIME_CLASSES: Final[Mapping[str, str]] = {
    "text/markdown": "document",
    "application/json": "code",
}


class FolderObjectKind(NamedTuple):
    """What an object type's folder is born holding, and where it is filed.

    An object whose node is a folder differs from the next one only in these
    four answers, so they are written once per type here and read by everything
    that creates, fills, files, copies or resolves one. A fifth kind is a line
    in :data:`FOLDER_OBJECT_KINDS`, not a branch added to six functions.
    """

    #: The flags stamped on the folder itself. A chat is sealed — its bytes
    #: never leave as a file — and a template is not: it is material a member
    #: is meant to copy and take away.
    root_flags: int
    #: The directories the folder is born with. The first is the working
    #: directory: where a run reads and writes, where a drop lands, and what a
    #: new chat copies from a template.
    working_folders: tuple[bytes, ...]
    #: The members whose bytes are rendered from the row on every read, as
    #: ``{name: mime}``. Nothing is stored for them, so nothing can drift.
    derived_members: Mapping[str, str]
    #: The folder inside the owner's home the node is filed in when the caller
    #: names no parent. ``None`` files it directly in the home.
    home_folder_name: bytes | None
    #: Directories the folder is born with that are NOT working directories:
    #: never a drop target, never an artifact, never copied from a template. A
    #: workspace keeps its chats' records here, beside the shared tree.
    record_folders: tuple[bytes, ...] = ()


#: Every object type whose node is a folder. Read by the create, the fill, the
#: filing, the drop target, the working-directory lookup and the adopt, so the
#: shape a type gets is one registration rather than a branch per call site.
FOLDER_OBJECT_KINDS: Final[Mapping[str, FolderObjectKind]] = {
    CHAT_TYPE: FolderObjectKind(
        root_flags=NO_DOWNLOAD_BIT | SEAL_SELF_ONLY_BIT,
        working_folders=CHAT_FOLDER_CHILDREN,
        derived_members={},
        home_folder_name=drives.CHATS_NAME,
    ),
    CHAT_TEMPLATE_TYPE: FolderObjectKind(
        root_flags=0,
        working_folders=CHAT_FOLDER_CHILDREN,
        derived_members=DERIVED_MEMBERS[CHAT_TEMPLATE_TYPE],
        home_folder_name=drives.CHAT_TEMPLATES_NAME,
    ),
    # Not sealed: the shared tree is ordinary material people open and take
    # away, and each chat folder under ``.chats/`` carries its own seal.
    WORKSPACE_TYPE: FolderObjectKind(
        root_flags=0,
        working_folders=(WORKSPACE_FILES_FOLDER,),
        derived_members={},
        home_folder_name=drives.CHATS_NAME,
        record_folders=(WORKSPACE_CHATS_FOLDER,),
    ),
}


def folder_object_kind(node: FileNode) -> FolderObjectKind | None:
    """The kind behind this node, or ``None`` when it is not one.

    The object behind the node decides, never its name: ``.alkerachat`` is a
    filesystem name minted once, and a person who renames a folder to end in it
    has not made a conversation.
    """
    if node.kind != "folder" or node.target_object_id is None:
        return None
    return FOLDER_OBJECT_KINDS.get(node.subtype or "")


#: The role an object's audience gets on its node: they may read it and reach it
#: from the drive, while the owner's home grant is what makes anyone a writer —
#: so a chat shared with the org cannot be renamed out from under its author.
AUDIENCE_ROLE: Final = ROLE_READER

#: The ``visibility_scope`` grammar this module reads (``alkera_core.authz`` owns
#: it; these are the three forms an object row can carry today).
PRIVATE_SCOPE: Final = "private"
ORG_SCOPE: Final = "org"
TEAM_SCOPE_PREFIX: Final = "team:"

#: The reason a node trashed by an object deletion carries in its history, so a
#: reader can tell it from a member trashing the node themselves.
REASON_OBJECT_DELETED: Final = "object_deleted"

#: The reason the node of a retired object type carries when the conversion to
#: a chat template trashes it: not a member's delete, and not the object's own.
REASON_OBJECT_RETIRED: Final = "object_retired"

#: Characters a title may not contribute to a name: a title is user prose and
#: may hold anything, a name is one path component. The separators are listed;
#: every control character is caught by :func:`_sanitized` instead, because a
#: name carrying one is refused by the naming contract and an auto-generated
#: title with a stray tab in it would otherwise fail the whole create.
_UNSAFE_IN_NAME: Final = ("/", "\\")
#: What the stem is truncated to, leaving room inside
#: :data:`~alkera_core.files.names.NAME_MAX_BYTES` for the extension and for
#: the ``ensure_unique`` suffix a collision adds.
_MAX_STEM_BYTES: Final = 180


def _assert_every_type_has_an_extension() -> None:
    missing = set(OBJECT_TYPES) - set(POINTER_EXTENSIONS)
    if missing:  # pragma: no cover - a guard against a model change landing alone
        raise RuntimeError(f"object types with no pointer extension: {sorted(missing)}")


def _assert_the_two_registries_agree() -> None:
    """The tree name and the mount name are the same name.

    This module names the node a member sees in the drive; the provider
    registry names the pointer a mount writes on disk and inverts it to resolve
    a node back to its object type. Two spellings of one extension mean the
    same object has two names and ``object_type_of`` answers ``None`` for every
    node this module created, so the disagreement is refused at import rather
    than discovered the first time someone mounts a chat.
    """
    disagree = {
        object_type: (f".{extension}", MOUNT_POINTER_EXTENSIONS.get(object_type))
        for object_type, extension in POINTER_EXTENSIONS.items()
        if MOUNT_POINTER_EXTENSIONS.get(object_type) != f".{extension}"
    }
    if disagree:  # pragma: no cover - a guard against one registry moving alone
        raise RuntimeError(f"pointer extension registries disagree: {disagree}")


_assert_every_type_has_an_extension()
_assert_the_two_registries_agree()


class AttachmentRecorder(Protocol):
    """How a chat records that a node is attached to it.

    The chat service owns ``workspace_objects.spec`` and its schema version, so
    the reference is written by a callback it provides rather than by this
    module reaching into another domain's JSON. Files' half of the contract is
    the node check and the history the link leaves behind.
    """

    def __call__(self, chat_id: uuid.UUID, node_id: NodeId) -> Awaitable[None]: ...


def pointer_name(workspace_object: WorkspaceObject) -> bytes:
    """The node name for an object: its title, or its logical id when untitled.

    A title is prose — it can be empty, hold a slash, or be longer than a path
    component — so it is sanitized and truncated here, and the caller creates
    the node with ``conflict="rename"`` because two chats may legitimately be
    called the same thing.
    """
    extension = POINTER_EXTENSIONS.get(workspace_object.type)
    if extension is None:
        raise InvalidRequest(f"no pointer extension for object type {workspace_object.type!r}")
    stem = _sanitized(workspace_object.title)
    if not stem:
        stem = _sanitized(workspace_object.logical_id)
    encoded = _truncate_utf8(stem.encode("utf-8"))
    if not encoded:
        encoded = _truncate_utf8(_sanitized(workspace_object.logical_id).encode("utf-8"))
    return encoded + b"." + extension.encode("ascii")


def _sanitized(title: str) -> str:
    """``title`` reduced to something one path component may hold.

    Repaired, never refused: a title is prose a person or a model wrote, and
    the naming contract refuses a separator, a NUL, a control character, a bidi
    control and a surrounding space. A chat whose auto-generated title happens to carry a tab
    would otherwise fail to create at all, with nothing the person could act
    on.

    Whitespace at the ends is trimmed BEFORE the sweep, so a pasted title
    wrapped in tabs is named for what it says rather than for its padding; an
    interior control becomes its own ``_``, so two titles that differed only
    there still differ here.
    """
    swept = "".join("_" if refused_in_a_name(ord(ch)) else ch for ch in title.strip())
    for char in _UNSAFE_IN_NAME:
        swept = swept.replace(char, "_")
    return swept.strip(". ").strip()


def _truncate_utf8(raw: bytes) -> bytes:
    """``raw`` cut to the stem budget without splitting a codepoint.

    A name is bytes on the wire, but its display form still has to decode, so a
    truncation that lands mid-sequence walks back rather than storing a name no
    client can render.
    """
    cut = raw[:_MAX_STEM_BYTES]
    while cut:
        try:
            cut.decode("utf-8")
        except UnicodeDecodeError:
            cut = cut[:-1]
            continue
        return cut
    return cut


def audience_grant(workspace_object: WorkspaceObject) -> Grant | None:
    """The one direct grant an object's ``visibility_scope`` implies.

    ``private`` returns ``None``: the node lives in the creator's home, whose
    drive default already makes them owner, and a redundant grant for the owner
    would only make a later revoke ambiguous about which one it names.
    """
    scope = workspace_object.visibility_scope
    if scope == ORG_SCOPE:
        return Grant(
            principal=Principal(kind="org", id=workspace_object.org_team_id),
            role=AUDIENCE_ROLE,
            origin=GrantOrigin.direct(),
        )
    if scope.startswith(TEAM_SCOPE_PREFIX):
        try:
            team_id = uuid.UUID(scope[len(TEAM_SCOPE_PREFIX) :])
        except ValueError as exc:
            raise InvalidRequest(f"malformed visibility scope {scope!r}") from exc
        return Grant(
            principal=Principal(kind="team", id=team_id),
            role=AUDIENCE_ROLE,
            origin=GrantOrigin.direct(),
        )
    if scope == PRIVATE_SCOPE:
        return None
    raise InvalidRequest(f"unknown visibility scope {scope!r}")


async def node_for_object(
    repo: FilesRepo,
    ctx: ActingContext,
    workspace_object: WorkspaceObject,
    *,
    parent_id: NodeId | None = None,
    clock: Clock | None = None,
    ino_allocator: InoAllocator | None = None,
    checkpoints: Checkpoints | None = None,
) -> FileNode:
    """Create the node for ``workspace_object`` in the caller's txn.

    A chat gets a **folder** — ``<Title>.alkerachat`` holding its working
    directory (:data:`CHAT_SANDBOX_FOLDER`) — because a chat owns a directory a
    run leases, writes into and hands back. Every other object type still gets
    the single ``object`` pointer node it always had.

    With no ``parent_id`` the node lands in the creator's home — a chat one
    level further down, in the home's ``Chats`` folder — created on the way if
    this is their first object.
    """
    if workspace_object.org_team_id != repo.scope.org_team_id:
        raise InvalidRequest("that object belongs to another org")
    grant = audience_grant(workspace_object)
    cp = checkpoints or NoopCheckpoints()
    allocator = ino_allocator or InoAllocator(repo)
    namespace = Namespace(repo, ctx, clock or SystemClock(), ino=allocator)
    parent = await _parent_for(repo, ctx, workspace_object, parent_id, allocator, namespace)
    kind = FOLDER_OBJECT_KINDS.get(workspace_object.type)
    is_folder = kind is not None

    node = await namespace.create(
        DriveId(parent.drive_id),
        NodeId(parent.id),
        "folder" if is_folder else "object",
        pointer_name(workspace_object),
        attrs=NodeAttrs(mode=0o755 if is_folder else 0o644),
        subtype=workspace_object.type,
        conflict="rename",
    )
    await cp.reach("objects_bridge.before_object_link")
    await repo.session.execute(
        text(
            "UPDATE file_nodes SET target_object_id = :object, mime_class = :mime, "
            "flags = flags | :flags WHERE id = :node AND org_team_id = :org"
        ),
        {
            "object": workspace_object.id,
            "mime": OBJECT_MIME_CLASSES.get(workspace_object.type, "other"),
            # A chat's bytes never leave as a file: NO_DOWNLOAD drops EXPORT for
            # everyone, owner and org admin included, so the content route, the
            # single-file serve, the download operation and the archive walk all
            # refuse it at the one place capability is computed. The seal stops
            # at the folder itself — what the conversation worked on, in its
            # working directory, is ordinary material a person opens, edits
            # and takes away. A template is not sealed at all: it exists to be
            # copied, and its registration says so rather than a branch here.
            "flags": 0 if kind is None else kind.root_flags,
            "node": node.id,
            "org": repo.scope.org_team_id,
        },
    )
    # Re-read before the children are created: the statement above is raw SQL,
    # so the session still holds the pre-update instance, and a child created
    # from it would inherit `flags = 0` — which is exactly the NO_DOWNLOAD hole
    # this closes.
    node = await _reload(repo, node)
    if kind is not None:
        await ensure_working_folders(repo, ctx, namespace, node, kind=kind)
        await _record_folders(namespace, node, kind)
        await _derived_members(repo, ctx, namespace, node, workspace_object, kind)
    await cp.reach("objects_bridge.before_audience_grant")
    await _write_acl(repo, node, parent, grant, granted_by=workspace_object.owner_user_id)
    return await _reload(repo, node)


async def ensure_working_folders(
    repo: FilesRepo,
    ctx: ActingContext,
    namespace: Namespace,
    folder_node: FileNode,
    *,
    kind: FolderObjectKind | None = None,
    only: bytes | None = None,
) -> Mapping[bytes, FileNode]:
    """The object folder's working folders, created here if they are not there yet.

    They exist from the start rather than on first write so a run that leases a
    chat finds the same shape every time, so Files has a node to open a chat's
    files at before it has produced any, and so a template hands a new chat the
    directory it was copied from. One function mints them at create time and
    re-mints a missing one later, so the folder an older chat gains the first
    time somebody drops a file on it carries the same flags as the one a chat
    created today was born with. A second creator would be a second place for
    those rules to drift. They carry no ACL of their own: the chain already
    answers for them, and a second grant here would be a second thing to revoke.

    ``kind`` is the registration to build from; it is read off the node when the
    caller does not already hold it. ``only`` narrows the work to one name, so a
    caller that needs one folder pays for that one rather than back-filling a
    shape the object never asked for.
    """
    resolved = kind or folder_object_kind(folder_node) or FOLDER_OBJECT_KINDS[CHAT_TYPE]
    wanted = resolved.working_folders if only is None else (only,)
    existing = {
        row.name: row for row in await repo.siblings(NodeId(folder_node.id)) if row.kind == "folder"
    }
    made: dict[bytes, FileNode] = {}
    for name in wanted:
        found = existing.get(name)
        if found is None:
            found = await namespace.create(
                DriveId(folder_node.drive_id),
                NodeId(folder_node.id),
                "folder",
                name,
                attrs=NodeAttrs(mode=0o755),
                # What a run produces lives here beside its working files: the
                # report a member asked for has to be a report they can take.
                artifact=name in resolved.working_folders,
                conflict="rename",
            )
        made[name] = found
    return made


def chat_sandbox_path(chat_folder: Path) -> Path:
    """The chat's working directory inside one chat folder, as a path on a box.

    The cloud mirror runs the agent here and hands this to the harness binding
    as the session's sandbox instead of joining a name of its own, so the
    directory the agent runs in, the directory it may write without an ask, the
    directory its brief names and the directory a dropped file lands in cannot
    drift apart: they are all this function of the same chat folder. The write
    fence stays the chat folder itself, one level up.
    """
    name = working_folder_name(CHAT_TYPE)
    if name is None:
        return chat_folder
    return chat_folder / name.decode("utf-8")


def is_chat_folder(node: FileNode) -> bool:
    """True when the node IS a chat and is also a real folder.

    The object behind the node decides, never its name: ``.alkerachat`` is a
    filesystem name minted once, and a person who renames a folder to end in it
    has not made a conversation.
    """
    return node.kind == "folder" and node.subtype == CHAT_TYPE and node.target_object_id is not None


def is_folder_object(node: FileNode) -> bool:
    """True when the node is the folder of any object type that owns one.

    A chat and a chat template both own a directory; what differs is what is in
    it and what may leave it, never whether the node is one.
    """
    return folder_object_kind(node) is not None


def working_folder_name(object_type: str) -> bytes | None:
    """The name of the working directory an object type's folder is born with.

    ``None`` for a type that owns no folder, or one whose files live at the
    folder itself. Read from :data:`FOLDER_OBJECT_KINDS`, so a chat's
    ``scratch``, a template's copy of it and a workspace's shared ``files``
    tree are each answered by their own registration.
    """
    kind = FOLDER_OBJECT_KINDS.get(object_type)
    if kind is None or not kind.working_folders:
        return None
    return kind.working_folders[0]


async def working_folder_nodes(
    session: AsyncSession, folder_node_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, uuid.UUID]:
    """Each object folder's working directory, as ``{folder node: working node}``.

    This is the one lookup for where an object's files ARE: the node Files
    opens a chat's, a template's or a workspace's files at, the folder a
    produced file is looked for in, and the directory a lease admits writes
    into. Each folder is answered by its own kind's working folder (a chat's
    ``scratch``, a workspace's ``files``), never by a name another kind uses,
    so a workspace that holds a child called ``scratch`` still answers with its
    shared tree. A folder with no such child (one created before the child
    existed, and never dropped on since) is simply absent: nothing is minted by
    a read. A trashed child is not a working directory either, and a folder
    that merely carries the name under something that is not an object folder
    is never asked for. A kind whose files live at the folder itself maps the
    folder to its own id.

    One statement for however many folders a page holds, because a listing of
    a home full of conversations must not cost a statement per row.
    """
    wanted = list(dict.fromkeys(folder_node_ids))
    if not wanted:
        return {}
    named = {
        subtype: name
        for subtype in FOLDER_OBJECT_KINDS
        if (name := working_folder_name(subtype)) is not None
    }
    at_self = tuple(subtype for subtype in FOLDER_OBJECT_KINDS if subtype not in named)
    owner = aliased(FileNode)
    # The parent has to BE an object that owns a directory: the object behind
    # it, never its name.
    is_object_folder = and_(
        owner.id.in_(wanted),
        owner.kind == "folder",
        owner.target_object_id.is_not(None),
    )
    found: dict[uuid.UUID, uuid.UUID] = {}
    if named:
        rows = await session.execute(
            select(FileNode.parent_id, FileNode.id)
            .join(owner, owner.id == FileNode.parent_id)
            .where(
                is_object_folder,
                FileNode.kind == "folder",
                FileNode.trashed_at.is_(None),
                or_(
                    *(
                        and_(owner.subtype == subtype, FileNode.name == name)
                        for subtype, name in named.items()
                    )
                ),
            )
        )
        found.update({parent_id: node_id for parent_id, node_id in rows.all()})
    if at_self:
        rows = await session.execute(
            select(owner.id).where(is_object_folder, owner.subtype.in_(at_self))
        )
        found.update({node_id: node_id for (node_id,) in rows.all()})
    return found


async def working_folder_node(repo: FilesRepo, folder_node: FileNode) -> FileNode | None:
    """The working directory of one object folder, or ``None`` when it has none.

    :func:`working_folder_nodes` for one node, read back as the row so a caller
    that goes on to list or write under it holds the same shape every other
    node comes in. Not an object folder: ``None``, because a folder that merely
    holds a child by that name is not a conversation or a workspace.
    """
    if not is_folder_object(folder_node):
        return None
    found = await working_folder_nodes(repo.session, [folder_node.id])
    working_id = found.get(folder_node.id)
    if working_id is None:
        return None
    if working_id == folder_node.id:
        return folder_node
    return await repo.node(NodeId(working_id))


#: The refusal a move gets when it would take a chat out of its workspace or
#: into another one.
CHAT_WORKSPACE_MOVE: Final = "files.chat_workspace_move"


async def _workspace_holding(repo: FilesRepo, folder_id: uuid.UUID | None) -> uuid.UUID | None:
    """The workspace folder whose ``.chats`` folder ``folder_id`` is, or ``None``."""
    if folder_id is None:
        return None
    folder = await repo.node(NodeId(folder_id))
    if folder is None or folder.parent_id is None or bytes(folder.name) != WORKSPACE_CHATS_FOLDER:
        return None
    parent = await repo.node(NodeId(folder.parent_id))
    if parent is None or parent.subtype != WORKSPACE_TYPE or folder_object_kind(parent) is None:
        return None
    return uuid.UUID(str(parent.id))


async def assert_chat_stays_in_workspace(
    repo: FilesRepo, node: FileNode, new_parent: FileNode
) -> None:
    """Refuse a move that would take a chat's folder out of its workspace's
    ``.chats`` folder or into another workspace's.

    A chat in a workspace is served under the box's lease on the workspace's
    folder and runs with the workspace's shared tree as its home; moving its
    records elsewhere would leave the box serving it from a folder it no longer
    leases and the chat's spec naming a workspace its bytes are not in. Moving
    chats between workspaces is a later feature; until then it is refused
    plainly, not half done. A move within one ``.chats`` folder, and every move
    of a folder that is not a chat, is untouched.
    """
    if node.subtype != CHAT_TYPE or folder_object_kind(node) is None:
        return
    if node.parent_id is not None and uuid.UUID(str(node.parent_id)) == uuid.UUID(
        str(new_parent.id)
    ):
        return
    leaving = await _workspace_holding(
        repo, uuid.UUID(str(node.parent_id)) if node.parent_id else None
    )
    entering = await _workspace_holding(repo, uuid.UUID(str(new_parent.id)))
    if leaving is None and entering is None:
        return
    raise Conflict(
        CHAT_WORKSPACE_MOVE,
        "A chat cannot be moved to another workspace yet; start a new chat there instead",
    )


async def lease_working_node(repo: FilesRepo, lease_node: FileNode) -> FileNode | None:
    """The one directory under a leased object folder that writes may be
    admitted into while a machine holds it, or ``None`` when there is none.

    A chat's folder admits its working directory; a workspace's folder admits
    its shared ``files/`` tree and never its ``.chats/`` records. Dispatched on
    the object behind the folder, never its name, so a workspace folder that
    happens to hold a child called like a chat's working directory is still
    answered by its own rule.
    """
    return await working_folder_node(repo, lease_node)


async def drop_target_for(
    repo: FilesRepo,
    ctx: ActingContext,
    namespace: Namespace,
    parent: FileNode,
) -> FileNode:
    """Where a write aimed AT ``parent`` should actually land.

    An ordinary container answers with itself. A chat answers with its working
    directory — the one directory the agent runs in and reads from — because
    "put this in the chat" means "give it to the conversation", and a file the
    conversation cannot see was not given to it. A template answers the same
    way, because a file left at its root reaches none of the chats started from
    it. The kind's first working folder names that child, and an object created
    before it existed gets it minted on the drop.
    """
    kind = folder_object_kind(parent)
    if kind is None or not kind.working_folders:
        return parent
    working = kind.working_folders[0]
    made = await ensure_working_folders(repo, ctx, namespace, parent, kind=kind, only=working)
    return made[working]


async def _record_folders(
    namespace: Namespace, folder_node: FileNode, kind: FolderObjectKind
) -> None:
    """The folder's record directories: plain folders, minted with it.

    Created here rather than by :func:`ensure_working_folders` because they are
    not working directories: a drop never lands in one and nothing a run makes
    is filed there as an artifact.
    """
    for name in kind.record_folders:
        await namespace.create(
            DriveId(folder_node.drive_id),
            NodeId(folder_node.id),
            "folder",
            name,
            attrs=NodeAttrs(mode=0o755),
            conflict="rename",
        )


async def workspace_chats_folder(repo: FilesRepo, workspace_node: FileNode) -> FileNode | None:
    """The ``.chats`` folder of a workspace's own folder, or ``None``.

    ``None`` for a node that is not a workspace folder, or one whose records
    folder somebody trashed: the caller then refuses rather than filing a chat
    in a folder that merely carries the name.
    """
    if folder_object_kind(workspace_node) is None or workspace_node.subtype != WORKSPACE_TYPE:
        return None
    for child in await repo.siblings(NodeId(workspace_node.id)):
        if child.kind == "folder" and bytes(child.name) == WORKSPACE_CHATS_FOLDER:
            return child
    return None


async def _derived_members(
    repo: FilesRepo,
    ctx: ActingContext,
    namespace: Namespace,
    folder_node: FileNode,
    workspace_object: WorkspaceObject,
    kind: FolderObjectKind,
) -> None:
    """The members of the folder whose bytes are rendered, not stored.

    Each one is an ``object`` node pointing back at the same row the folder
    does, so its bytes are RENDERED on every read by the one function a mount
    and a pull already use. That is what makes "re-render on an update without
    churning unchanged content" true absolutely: no byte is ever stored, so no
    byte is ever rewritten.
    """
    for member, mime in kind.derived_members.items():
        child = await namespace.create(
            DriveId(folder_node.drive_id),
            NodeId(folder_node.id),
            "object",
            member.encode("utf-8"),
            attrs=NodeAttrs(mode=0o644),
            subtype=context_member_type(workspace_object.type, member),
            conflict="rename",
        )
        await repo.session.execute(
            text(
                "UPDATE file_nodes SET target_object_id = :object, mime_class = :mime "
                "WHERE id = :node AND org_team_id = :org"
            ),
            {
                "object": workspace_object.id,
                "mime": _MEMBER_MIME_CLASSES.get(mime, "other"),
                "node": child.id,
                "org": repo.scope.org_team_id,
            },
        )


async def _parent_for(
    repo: FilesRepo,
    ctx: ActingContext,
    workspace_object: WorkspaceObject,
    parent_id: NodeId | None,
    allocator: InoAllocator,
    namespace: Namespace,
) -> FileNode:
    """Where the object's node goes: the given folder, the place its kind names
    inside the creator's home, or the creator's home for everything else."""
    if parent_id is not None:
        found = await repo.node(parent_id)
        if found is None:
            raise NotFound(f"no node {parent_id}")
        return found
    home = await drives.ensure_home_folder(
        repo, ctx, workspace_object.owner_user_id, ino_allocator=allocator
    )
    kind = FOLDER_OBJECT_KINDS.get(workspace_object.type)
    if kind is None or kind.home_folder_name is None:
        return home
    return await _home_child_folder(repo, namespace, home, kind.home_folder_name)


async def chats_folder(repo: FilesRepo, namespace: Namespace, home: FileNode) -> FileNode:
    """``<home>/Chats/`` for a caller who already holds their home — the folder
    a chat copied into their drive lands in, created on the way when missing,
    by the same rule a brand-new chat's folder is placed by."""
    return await _home_child_folder(repo, namespace, home, drives.CHATS_NAME)


async def chat_templates_folder(repo: FilesRepo, namespace: Namespace, home: FileNode) -> FileNode:
    """``<home>/Chat Templates/`` — where a template a member saves is filed,
    and where one copied into their drive lands, created on the way when
    missing, by the same rule the chats' place is."""
    return await _home_child_folder(repo, namespace, home, drives.CHAT_TEMPLATES_NAME)


async def adopt_copied_folder_node(
    repo: FilesRepo,
    ctx: ActingContext,
    node: FileNode,
    workspace_object: WorkspaceObject,
    *,
    clock: Clock | None = None,
) -> FileNode:
    """Make the copied folder ``node`` the node of ``workspace_object``.

    A subtree copy carries what was inside the folder — a chat's manifest, its
    transcript, the harness's own session storage, a template's files — but not
    the object link, the mime class or the flags: those are the object's, and
    the copy is a NEW object. Stamped here in the copy's own transaction: the
    link that makes the folder open as that object, whatever its kind seals (a
    conversation's bytes never leave as a file; a template's are meant to), and
    the working directory re-minted if the source predates it.
    """
    kind = FOLDER_OBJECT_KINDS.get(workspace_object.type)
    if kind is None:
        raise InvalidRequest("only an object that owns a folder adopts a copied one")
    if workspace_object.org_team_id != repo.scope.org_team_id:
        raise InvalidRequest("that object belongs to another org")
    await repo.session.execute(
        text(
            "UPDATE file_nodes SET target_object_id = :object, mime_class = :mime, "
            "subtype = :subtype, flags = flags | :flags WHERE id = :node AND org_team_id = :org"
        ),
        {
            "object": workspace_object.id,
            "mime": OBJECT_MIME_CLASSES[workspace_object.type],
            "subtype": workspace_object.type,
            "flags": kind.root_flags,
            "node": node.id,
            "org": repo.scope.org_team_id,
        },
    )
    node = await _reload(repo, node)
    namespace = Namespace(repo, ctx, clock or SystemClock())
    await ensure_working_folders(repo, ctx, namespace, node, kind=kind)
    return await _reload(repo, node)


async def _home_child_folder(
    repo: FilesRepo, namespace: Namespace, home: FileNode, name: bytes
) -> FileNode:
    """``<home>/<name>/`` — the place a new object folder of some kind goes.

    An ordinary folder and nothing more: no marker, no traversal bit, no flags
    of its own, created through the same namespace call a member's own "new
    folder" makes. So its owner may rename it, move it, put their own files in
    it, or share it, and an object inside it is an ordinary child of an
    ordinary folder — which is why every surface that reaches one by node id
    needed no change to find it here.

    It is found by name and only *directly* under the home, so a place the
    owner renamed or moved elsewhere keeps what is already in it and the next
    object is created in a fresh one. Following the folder wherever it went
    would make the rename silently permanent and the move impossible to undo;
    what a member has already filed away stays filed away either way, because
    nothing here moves an existing node.

    The create runs in the object's own transaction, so a place that cannot be
    made — the org is at its node ceiling, something that is not a folder
    already holds the name — takes the object's folder down with it rather than
    quietly putting the object somewhere else. A concurrent first object is the
    one case where the name is taken between the read and the insert: the index
    refuses the loser inside its savepoint, and the loser then reads the
    winner's folder rather than failing a create that only raced.
    """
    found = await _home_child(repo, home, name)
    if found is not None:
        return found
    try:
        return await namespace.create(
            DriveId(home.drive_id),
            NodeId(home.id),
            "folder",
            name,
            attrs=NodeAttrs(mode=0o755),
        )
    except Conflict:
        raced = await _home_child(repo, home, name)
        if raced is None:
            raise
        return raced


async def _home_child(repo: FilesRepo, home: FileNode, name: bytes) -> FileNode | None:
    """The live folder with exactly this name directly under ``home``, or ``None``.

    Byte-exact and folders only, the same identity test a home folder gets:
    a file called ``Chats`` is not a place a chat can be created in, and a
    folder called ``chats`` is a different name the member chose.
    """
    for child in await repo.siblings(NodeId(home.id)):
        if child.kind == "folder" and bytes(child.name) == name:
            return child
    return None


async def _write_acl(
    repo: FilesRepo,
    node: FileNode,
    parent: FileNode,
    grant: Grant | None,
    *,
    granted_by: uuid.UUID,
) -> None:
    """The node's effective ACL: its parent's chain, plus the audience grant.

    Two halves are written when there is a grant, because they answer different
    questions: ``file_shares`` is the queryable, audited truth a revoke names,
    and ``acl_id`` is the interned body a listing joins against. The parent's
    grants are folded in as ``inherited`` — a home folder's owner ACE lives in
    its interned body, not in ``file_shares``, so a node that carried only its
    own direct grant would be a node its own creator could not read.
    """
    if grant is not None:
        async with _writing_a_grant(repo):
            await repo.add(
                FileShare(
                    id=uuid.uuid4(),
                    org_team_id=repo.scope.org_team_id,
                    node_id=node.id,
                    principal_kind=grant.principal.kind,
                    principal_id=grant.principal.id,
                    role=grant.role,
                    granted_by=granted_by,
                )
            )
            await repo.flush()
    chain = await repo.chain(parent)
    inherited = [
        Grant(
            principal=ace.principal,
            role=ace.role,
            origin=GrantOrigin.inherited(parent.id),
            expires_at=ace.expires_at,
            conditions=ace.conditions,
        )
        for ace in await FilesGrantSource().grants_for(repo, parent, chain)
    ]
    body = inherited if grant is None else [*inherited, grant]
    if not body:
        return
    acl_id = await acl_intern.intern(repo, body)
    await repo.session.execute(
        text("UPDATE file_nodes SET acl_id = :acl WHERE id = :node AND org_team_id = :org"),
        {"acl": acl_id, "node": node.id, "org": repo.scope.org_team_id},
    )


@asynccontextmanager
async def _writing_a_grant(repo: FilesRepo) -> AsyncIterator[None]:
    """Step out of the Files role for a ``file_shares`` INSERT, then take it back.

    The table's guard trigger resolves the principal against ``teams`` and
    ``users`` — the second net that stops a grant naming someone outside the org
    — and ``alkera_files_app`` deliberately cannot read either. The window goes
    to the session's outer role (the tenant role on a bound request, see
    :func:`alkera_core.db.tenant_session.stepped_out`) and is
    transaction-scoped both ways, so the grant is still in the caller's
    transaction and every statement around it still runs under RLS.
    """
    session = repo.session
    async with stepped_out(session):
        yield


async def tombstone_for_object(
    repo: FilesRepo,
    ctx: ActingContext,
    object_id: uuid.UUID,
    *,
    checkpoints: Checkpoints | None = None,
) -> FileNode | None:
    """Trash the node of a deleted object. Never the reverse.

    A real deletion, through the Trash service: the member who deleted the chat
    finds its folder in their Trash, with the chat's own name on it and the
    purge window running, and can restore it from there. A bare ``trashed_at``
    stamp on the root would produce a node the Trash listing cannot show (that
    listing joins through the trash op row), with no purge window, and
    everything the object held would stay live under a trashed parent.

    The node also stops pointing at the object. The object is a tombstone and is
    not coming back, so a restore has to yield an ordinary folder: a node that
    still named a deleted chat would be a door onto rows the product retired,
    and the Files page would offer a conversation nobody can open.

    Returns ``None`` when the object has no live node — one created before this
    bridge existed, or one a member already trashed — because a missing
    projection must not make deleting the object fail.
    """
    node = await live_node_for(repo, object_id)
    if node is None:
        return None
    cp = checkpoints or NoopCheckpoints()
    await cp.reach("objects_bridge.before_tombstone")
    # Deferred: ``trash`` imports this module for the object hooks a purge runs.
    from alkera_core.files.trash import Trash

    # The reason rides into the trash's own history row rather than beside it:
    # one deletion is one row, and a reader counting ``trash`` rows for a node
    # would otherwise see an object's deletion as two.
    binned = await Trash(repo, ctx, SystemClock()).trash_for_deleted_object(
        NodeId(node.id), reason=REASON_OBJECT_DELETED
    )
    if binned is None:  # a racer trashed it between the read and the write
        return None
    etag = (
        await repo.session.execute(
            text(
                "UPDATE file_nodes SET target_object_id = NULL, etag = etag + 1 "
                "WHERE id = :node AND org_team_id = :org RETURNING etag"
            ),
            {"node": node.id, "org": repo.scope.org_team_id},
        )
    ).scalar_one()
    await history.emit_node_changed(
        repo,
        ctx,
        node_id=NodeId(node.id),
        drive_id=DriveId(node.drive_id),
        version=int(etag),
    )
    return await _reload(repo, node)


async def bump_for_object_update(
    repo: FilesRepo,
    ctx: ActingContext,
    object_id: uuid.UUID,
) -> FileNode | None:
    """Move the node's change token after the object's own write.

    Awaited by the object service inside its transaction, so a client watching
    the file surface sees the edit at the same instant one watching the object
    does. Exactly one outbox row is emitted: a sync client treats each row as a
    change to fetch, and two rows for one edit would make it fetch twice.
    """
    node = await live_node_for(repo, object_id)
    if node is None:
        return None
    etag = (
        await repo.session.execute(
            text(
                "UPDATE file_nodes SET etag = etag + 1 "
                "WHERE id = :node AND org_team_id = :org AND trashed_at IS NULL "
                "RETURNING etag"
            ),
            {"node": node.id, "org": repo.scope.org_team_id},
        )
    ).scalar_one_or_none()
    if etag is None:  # pragma: no cover - trashed between the read and the write
        return None
    await _bump_context_members(repo, object_id)
    await history.record(
        repo,
        ctx,
        node_id=NodeId(node.id),
        kind="attrs",
        before={"etag": node.etag},
        after={"etag": int(etag)},
    )
    await history.emit_node_changed(
        repo,
        ctx,
        node_id=NodeId(node.id),
        drive_id=DriveId(node.drive_id),
        version=int(etag),
    )
    return await _reload(repo, node)


@asynccontextmanager
async def _recording_the_reference(repo: FilesRepo) -> AsyncIterator[None]:
    """Step out of the Files role while the chat's own writer runs, then take
    it back.

    The reference lives on ``workspace_objects`` — the chat service locks the
    row, rewrites its spec and records the change on the org's audit trail —
    and ``alkera_files_app`` deliberately holds no grant on any of those
    tables; granting one would widen the role every Files statement runs as.
    The window goes to the session's outer role, so on a bound request the
    chat's write is still held to the tenant policy. Both role changes are
    transaction-scoped, so the chat's
    write stays in the caller's unit of work and every Files statement around
    it still runs under RLS.
    """
    repo._require_open()
    session = repo.session
    async with stepped_out(session):
        yield


async def link_attachment(
    repo: FilesRepo,
    ctx: ActingContext,
    chat_id: uuid.UUID,
    node_id: NodeId,
    *,
    record_reference: AttachmentRecorder,
) -> None:
    """Record that ``node_id`` is attached to ``chat_id``.

    Appearing in a transcript grants nothing: every read of an attachment is
    authorized per request through ``enforce()`` against the node itself, so a
    chat shared more widely than the file does not widen the file. This function
    only proves the node is live in the caller's org and hands the reference to
    the chat service's own writer.

    Nothing is written to the node's history. ``file_history`` is the node's
    WRITE vocabulary — an ``attrs`` row says the node's metadata changed and is
    earned by a WRITE decision — while attaching is decided as a READ and
    changes no field of the node. The reference is recorded where it lives: on
    the chat, by ``record_reference``.
    """
    node = await repo.node(node_id)
    if node is None:
        raise NotFound(f"no node {node_id}")
    if node.trashed_at is not None:
        raise InvalidRequest("a trashed node cannot be attached")
    async with _recording_the_reference(repo):
        await record_reference(chat_id, node_id)


async def unlink_attachment(
    chat_id: uuid.UUID,
    node_id: NodeId,
    *,
    forget_reference: AttachmentRecorder,
) -> None:
    """Remove the reference to ``node_id`` from ``chat_id``.

    The inverse of :func:`link_attachment`, and deliberately asymmetric with
    it: attaching proves the node is live in the caller's org, detaching proves
    nothing about the node at all. A reference outlives what it points at — the
    node may since have been trashed, purged, or moved out of reach — and a
    reference that cannot be removed once its node is gone is a file that stays
    attached to a chat forever. So there is no repo, no node read and no
    existence check here; the caller has already been decided against the CHAT
    (removing an attachment is speaking in it), which is the only thing this
    removal touches.

    Nothing is written to the node's history for the same reason the link
    writes none: ``file_history`` is the node's WRITE vocabulary, and no field
    of the node changes. The chat service's own writer removes the reference
    where it lives.
    """
    await forget_reference(chat_id, node_id)


async def live_node_for(repo: FilesRepo, object_id: uuid.UUID) -> FileNode | None:
    """The live node projecting one object, through the repo's org predicate.

    Members of a replication-context folder point at the same object as the
    folder does, so "the node for this object" is no longer unambiguous from
    ``target_object_id`` alone: an unfiltered ``.first()`` would hand a caller
    an arbitrary one, and this function is what ``bump_for_object_update`` and
    ``tombstone_for_object`` name — so an object delete would trash
    ``README.md`` and leave the report standing. A member is exactly a node
    whose subtype names one, which is the same field the renderer resolves it
    by; the CONTAINER is everything else.
    """
    result: Any = await repo.execute_scoped(
        repo.select_nodes().where(
            FileNode.target_object_id == object_id,
            FileNode.trashed_at.is_(None),
            or_(
                FileNode.subtype.is_(None),
                FileNode.subtype.not_like(f"%{CONTEXT_MEMBER_SEPARATOR}%"),
            ),
        )
    )
    node: FileNode | None = result.scalars().first()
    return node


async def _bump_context_members(repo: FilesRepo, object_id: uuid.UUID) -> None:
    """Move the change token of every derived member of a context folder.

    Their bytes are rendered from the object, so an edit to the object changes
    them even though nothing wrote to them. No history row and no outbox row:
    the container's single row is what tells a sync client the report changed,
    and a second row per member would make it fetch the same edit three times.
    The ``etag`` still has to move, or a conditional GET of ``spec.json`` would
    answer 304 with the old spec.
    """
    await repo.session.execute(
        text(
            "UPDATE file_nodes SET etag = etag + 1 "
            "WHERE target_object_id = :object AND org_team_id = :org "
            "AND trashed_at IS NULL AND subtype LIKE :member"
        ),
        {
            "object": object_id,
            "org": repo.scope.org_team_id,
            "member": f"%{CONTEXT_MEMBER_SEPARATOR}%",
        },
    )


async def _reload(repo: FilesRepo, node: FileNode) -> FileNode:
    """``node`` re-read after this transaction's own UPDATE.

    The UPDATE is a hand-written statement, so the session's identity map still
    holds the pre-update instance and a plain re-read would hand the caller a
    stale ``etag``. Expiring it first is what makes the returned node the row
    that is actually in the transaction.
    """
    node_id = NodeId(node.id)
    repo.session.expire(node)
    reloaded = await repo.node(node_id)
    if reloaded is None:  # pragma: no cover - the row was written in this transaction
        raise RuntimeError(f"node {node_id} is not readable inside its own transaction")
    return reloaded


__all__ = [
    "AUDIENCE_ROLE",
    "OBJECT_MIME_CLASSES",
    "POINTER_EXTENSIONS",
    "REASON_OBJECT_DELETED",
    "AttachmentRecorder",
    "audience_grant",
    "bump_for_object_update",
    "chats_folder",
    "lease_working_node",
    "link_attachment",
    "live_node_for",
    "node_for_object",
    "pointer_name",
    "tombstone_for_object",
    "unlink_attachment",
    "workspace_chats_folder",
]
