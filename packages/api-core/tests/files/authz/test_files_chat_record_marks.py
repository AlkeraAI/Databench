"""A chat's records are records from birth, and only the holding box changes one.

Three seams, each pinned on its own:

* the NAME rule — :func:`is_chat_record_name` and the birth of the mark in
  :func:`flags_for_child` — is pure and exhaustive over the six names, the
  same names elsewhere, and what a record folder passes down;
* the DECIDER takes write, delete and restore off a record for every person,
  the owner and the org admin included, and leaves them to the one agent that
  proved it holds the live lease over the node;
* the TREE SEAM (``create_tree``), which mints a folder skeleton in one
  statement per level, marks a runtime directory raised under a chat folder
  and everything it makes beneath it.

The nodes are real rows built by ``files_factory``; the flags and subtypes a
case needs are set on the loaded objects (the decision is pure) or by an
update the seam reads back through the repo.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext
from alkera_core.authz.principal import Principal as ActingPrincipal
from alkera_core.chat_records import CHAT_RECORD_NAMES, is_chat_record_name
from alkera_core.files import tree
from alkera_core.files.authz.actions import FilesAction as A
from alkera_core.files.authz.decider import (
    CHAT_SUBTYPE,
    NO_DOWNLOAD_BIT,
    RECORD_BIT,
    RECORD_BLOCKS,
    SEAL_SELF_ONLY_BIT,
    AccessFacts,
    effective_role,
    flags_for_child,
)
from alkera_core.files.authz.grants import Grant
from alkera_core.files.authz.grants import Principal as GrantPrincipal
from alkera_core.files.ids import DriveId, NodeId
from alkera_core.files.repo import FilesRepo
from alkera_core.models.files.stores import FileDrive
from alkera_core.models.files.tree import FileNode
from sqlalchemy import select, text
from tests.files._kit.factory import FilesFactory, FilesOrg

#: A chat folder's own bits, as the objects bridge stamps them.
CHAT_FOLDER_FLAGS = NO_DOWNLOAD_BIT | SEAL_SELF_ONLY_BIT

RECORD_NAMES = sorted(CHAT_RECORD_NAMES)


# ---------------------------------------------------------------- the name


@pytest.mark.parametrize("name", RECORD_NAMES)
def test_every_record_name_is_recognised_as_text_and_as_the_bytes_the_drive_stores(
    name: str,
) -> None:
    assert is_chat_record_name(name) is True
    assert is_chat_record_name(name.encode()) is True


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("scratch", id="the-working-directory"),
        pytest.param("notes.md", id="a-working-file"),
        pytest.param("chat.jsonl.bak", id="a-record-name-with-a-suffix"),
        pytest.param("Manifest.json", id="a-record-name-in-another-case"),
        pytest.param(b"\xff\xfe.jsonl", id="bytes-that-are-not-utf-8"),
    ],
)
def test_anything_else_is_no_record(name: str | bytes) -> None:
    assert is_chat_record_name(name) is False


def test_the_set_names_the_manifest_the_three_logs_the_digest_and_the_runtime_dir() -> None:
    """The membership itself, spelled out: a name dropped from here silently
    drops out of the fence, the watcher and the drive's mark at once."""
    assert CHAT_RECORD_NAMES == {
        "manifest.json",
        "chat.jsonl",
        "decisions.jsonl",
        "cost_ledger.jsonl",
        "trace.digest.json",
        ".runtime",
    }


# --------------------------------------------------------------- the birth


@pytest.mark.parametrize("name", RECORD_NAMES)
def test_a_record_name_directly_under_a_chat_folder_is_born_a_record(name: str) -> None:
    born = flags_for_child(CHAT_FOLDER_FLAGS, parent_subtype=CHAT_SUBTYPE, name=name.encode())
    assert born & RECORD_BIT
    # The chat folder's seal is self-only, so its records are readable rather
    # than sealed: what the folder passed down before is unchanged.
    assert not born & NO_DOWNLOAD_BIT


@pytest.mark.parametrize(
    ("parent_flags", "parent_subtype", "name"),
    [
        pytest.param(CHAT_FOLDER_FLAGS, CHAT_SUBTYPE, b"scratch", id="the-working-dir"),
        pytest.param(CHAT_FOLDER_FLAGS, CHAT_SUBTYPE, b"report.html", id="a-working-file"),
        pytest.param(0, None, b"chat.jsonl", id="a-record-name-in-a-plain-folder"),
        pytest.param(0, "chat_template", b"manifest.json", id="a-record-name-in-a-template"),
        pytest.param(CHAT_FOLDER_FLAGS, None, b".runtime", id="a-caller-that-named-no-subtype"),
        pytest.param(CHAT_FOLDER_FLAGS, CHAT_SUBTYPE, None, id="a-caller-that-named-no-name"),
        pytest.param(NO_DOWNLOAD_BIT, None, b"chat.jsonl", id="under-a-sealed-plain-folder"),
    ],
)
def test_nothing_else_is_born_a_record(
    parent_flags: int, parent_subtype: str | None, name: bytes | None
) -> None:
    born = flags_for_child(parent_flags, parent_subtype=parent_subtype, name=name)
    assert not born & RECORD_BIT


def test_the_working_directorys_children_are_never_records_whatever_their_name() -> None:
    """``scratch/`` is born without the mark, so what a person or the agent
    writes inside it under a record's name is working material."""
    scratch = flags_for_child(CHAT_FOLDER_FLAGS, parent_subtype=CHAT_SUBTYPE, name=b"scratch")
    inside = flags_for_child(scratch, parent_subtype=None, name=b"chat.jsonl")
    assert not scratch & RECORD_BIT
    assert not inside & RECORD_BIT


def test_the_mark_descends_with_the_runtime_directory() -> None:
    """The harness writes its database several levels inside ``.runtime``;
    every level is born a record because its parent is one, with no name
    rule read below the top."""
    runtime = flags_for_child(CHAT_FOLDER_FLAGS, parent_subtype=CHAT_SUBTYPE, name=b".runtime")
    agent = flags_for_child(runtime, parent_subtype=None, name=b"agent")
    database = flags_for_child(agent, parent_subtype=None, name=b"agent.db")
    assert runtime & RECORD_BIT and agent & RECORD_BIT and database & RECORD_BIT


def test_the_birth_rule_follows_the_name_and_not_the_artifact_exception() -> None:
    """A deliverable folder is downloadable; a record under a chat is still a
    record when the caller also asks for the artifact mark."""
    born = flags_for_child(
        CHAT_FOLDER_FLAGS, artifact=True, parent_subtype=CHAT_SUBTYPE, name=b"manifest.json"
    )
    assert born & RECORD_BIT


# ------------------------------------------------------------- the decider


def _ctx(org_id: uuid.UUID, user_id: uuid.UUID) -> ActingContext:
    return ActingContext(
        acting_principal=ActingPrincipal(
            kind=PrincipalKind.USER, id=str(user_id), org_id=org_id, credential=CredentialKind.JWT
        )
    )


def _agent_ctx(org_id: uuid.UUID, user_id: uuid.UUID) -> ActingContext:
    user = ActingPrincipal(kind=PrincipalKind.USER, id=str(user_id), org_id=org_id)
    agent = ActingPrincipal(
        kind=PrincipalKind.AGENT,
        id="session-1",
        org_id=org_id,
        credential=CredentialKind.AGENT_HEADER,
    )
    return ActingContext(
        acting_principal=agent, delegating_user=user, delegation_chain=(user, agent)
    )


@pytest.fixture
async def record_scene(
    files_factory: FilesFactory, files_org: FilesOrg
) -> tuple[FileDrive, FileNode, Sequence[FileNode]]:
    """``chat/chat.jsonl``: a chat folder and its transcript, the record as
    the birth rule leaves it."""
    drive = await files_factory.drive()
    made = await files_factory.tree("chat/ chat/chat.jsonl", drive=drive)
    chat, record = made["chat"], made["chat/chat.jsonl"]
    chat.subtype = CHAT_SUBTYPE
    chat.flags |= CHAT_FOLDER_FLAGS
    record.flags = flags_for_child(chat.flags, parent_subtype=chat.subtype, name=record.name)
    assert record.flags & RECORD_BIT
    return drive, record, (chat, record)


BLOCKED = sorted(a.value for a in RECORD_BLOCKS)


def _owner_grant(user_id: uuid.UUID) -> Grant:
    return Grant(principal=GrantPrincipal(kind="user", id=user_id), role="owner")


@pytest.mark.parametrize(
    "facts",
    [
        pytest.param(AccessFacts(), id="the-owner-of-the-chat"),
        pytest.param(
            AccessFacts(org_admin=True, org_admin_reaches_chats=True), id="an-org-admin-let-in"
        ),
    ],
)
async def test_a_person_may_read_a_record_and_may_not_write_delete_or_restore_it(
    record_scene: tuple[FileDrive, FileNode, Sequence[FileNode]],
    files_org: FilesOrg,
    facts: AccessFacts,
) -> None:
    drive, record, chain = record_scene
    ctx = _ctx(files_org.org_team_id, files_org.member_id)

    access = effective_role(ctx, record, chain, [_owner_grant(files_org.member_id)], drive, facts)

    assert access.role == "owner"
    assert "record" in access.flags
    assert access.holds_lease is False
    assert not access.allowed_actions & set(BLOCKED)
    assert {A.READ.value, A.EXPORT.value, A.COPY.value, A.COMMENT.value} <= access.allowed_actions


async def test_the_proven_holder_of_the_lease_over_the_chat_writes_its_record(
    record_scene: tuple[FileDrive, FileNode, Sequence[FileNode]], files_org: FilesOrg
) -> None:
    drive, record, chain = record_scene
    chat = chain[0]
    box = _agent_ctx(files_org.org_team_id, files_org.member_id)
    facts = AccessFacts(
        is_agent=True,
        agent_machine_id="box-a",
        leased_subtree=chat.id,
        held_leases=frozenset({chat.id}),
        chats_run_here=frozenset({chat.id}),
    )

    access = effective_role(box, record, chain, [_owner_grant(files_org.member_id)], drive, facts)

    assert access.holds_lease is True
    assert set(BLOCKED) <= access.allowed_actions


@pytest.mark.parametrize(
    ("facts", "why"),
    [
        pytest.param(
            AccessFacts(
                is_agent=True,
                agent_machine_id="box-a",
                leased_subtree=None,
                held_leases=frozenset(),
                chats_run_here=frozenset(),
            ),
            "the box did not send the lease it holds",
            id="a-proven-box-without-its-fence",
        ),
        pytest.param(
            AccessFacts(is_agent=True, agent_machine_id=None, held_leases=frozenset()),
            "the assertion was never verified",
            id="an-unverified-agent",
        ),
        pytest.param(
            AccessFacts(is_agent=False, agent_machine_id="box-a"),
            "a person is not a box, whatever facts ride the request",
            id="a-person-with-a-machine-id-on-the-facts",
        ),
    ],
)
async def test_being_the_chats_machine_is_not_enough_the_lease_must_be_held(
    record_scene: tuple[FileDrive, FileNode, Sequence[FileNode]],
    files_org: FilesOrg,
    facts: AccessFacts,
    why: str,
) -> None:
    drive, record, chain = record_scene
    chat = chain[0]
    ctx = (
        _agent_ctx(files_org.org_team_id, files_org.member_id)
        if facts.is_agent
        else _ctx(files_org.org_team_id, files_org.member_id)
    )
    if facts.is_agent:
        facts = AccessFacts(
            is_agent=True,
            agent_machine_id=facts.agent_machine_id,
            leased_subtree=chat.id,
            held_leases=facts.held_leases,
            chats_run_here=frozenset({chat.id}) if facts.agent_machine_id else frozenset(),
        )
    else:
        facts = AccessFacts(held_leases=frozenset({chat.id}), agent_machine_id="box-a")

    access = effective_role(ctx, record, chain, [_owner_grant(files_org.member_id)], drive, facts)

    assert access.holds_lease is False, why
    assert not access.allowed_actions & set(BLOCKED), why


async def test_a_held_lease_covers_the_record_only_inside_the_leased_subtree(
    files_factory: FilesFactory, files_org: FilesOrg
) -> None:
    """The lease the box holds is over ONE chat; a record of another chat is
    not written on the strength of it."""
    drive = await files_factory.drive()
    made = await files_factory.tree("a/ a/chat.jsonl b/ b/chat.jsonl", drive=drive)
    for folder in (made["a"], made["b"]):
        folder.subtype = CHAT_SUBTYPE
    for record, folder in ((made["a/chat.jsonl"], made["a"]), (made["b/chat.jsonl"], made["b"])):
        record.flags = flags_for_child(folder.flags, parent_subtype=CHAT_SUBTYPE, name=record.name)
    box = _agent_ctx(files_org.org_team_id, files_org.member_id)
    grants = [_owner_grant(files_org.member_id)]

    def facts(inside: FileNode) -> AccessFacts:
        # Confined to the chat it is asked about, so the only question left
        # is whose lease it holds: chat ``a``'s, on both requests.
        return AccessFacts(
            is_agent=True,
            agent_machine_id="box-a",
            leased_subtree=inside.id,
            held_leases=frozenset({made["a"].id}),
            chats_run_here=frozenset({made["a"].id, made["b"].id}),
        )

    own = effective_role(box, made["a/chat.jsonl"], (made["a"],), grants, drive, facts(made["a"]))
    other = effective_role(box, made["b/chat.jsonl"], (made["b"],), grants, drive, facts(made["b"]))

    assert own.holds_lease is True and A.WRITE.value in own.allowed_actions
    assert other.holds_lease is False and A.WRITE.value not in other.allowed_actions
    assert A.READ.value in other.allowed_actions, "it still runs the chat; it reads"


# ------------------------------------------------------------ the tree seam


def _tree_ctx(org: FilesOrg) -> ActingContext:
    return _ctx(org.org_team_id, org.admin_id)


async def _make_chat(repo: FilesRepo, files_factory: FilesFactory) -> tuple[FileDrive, FileNode]:
    drive = await files_factory.drive()
    made = await files_factory.tree("chat/", drive=drive)
    chat = made["chat"]
    async with repo.transaction():
        await repo.session.execute(
            text("UPDATE file_nodes SET subtype = :subtype, flags = :flags WHERE id = :id"),
            {"subtype": CHAT_SUBTYPE, "flags": CHAT_FOLDER_FLAGS, "id": chat.id},
        )
    return drive, chat


async def _flags_by_path(
    repo: FilesRepo, drive: FileDrive, nodes: dict[str, NodeId]
) -> dict[str, int]:
    async with repo.transaction():
        rows = (
            await repo.execute_scoped(
                select(FileNode.id, FileNode.flags).where(
                    FileNode.drive_id == drive.id, FileNode.id.in_(list(nodes.values()))
                )
            )
        ).all()
    by_id = {row.id: int(row.flags) for row in rows}
    return {path: by_id[node_id] for path, node_id in nodes.items()}


async def test_a_skeleton_raised_under_a_chat_marks_its_runtime_directory_and_nothing_else(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory
) -> None:
    """The one-statement-per-level skeleton is a birth like any other: the
    runtime directory at the chat's top is a record, every level it makes
    inside is one, and a working folder beside it — and a runtime-named
    folder inside the working directory — is not."""
    drive, chat = await _make_chat(repo, files_factory)
    async with repo.transaction():
        result = await tree.create_tree(
            repo,
            _tree_ctx(files_org),
            DriveId(drive.id),
            NodeId(chat.id),
            [".runtime/agent/agent.db", "scratch/.runtime/x.txt", "scratch/report.html"],
            idempotency_key="records-1",
        )

    flags = await _flags_by_path(repo, drive, result.nodes)
    assert set(flags) == {".runtime", ".runtime/agent", "scratch", "scratch/.runtime"}
    assert flags[".runtime"] & RECORD_BIT
    assert flags[".runtime/agent"] & RECORD_BIT
    assert not flags["scratch"] & RECORD_BIT
    assert not flags["scratch/.runtime"] & RECORD_BIT


async def test_a_skeleton_that_walks_into_an_existing_runtime_directory_keeps_descending_the_mark(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory
) -> None:
    """A replay, or a second drop, walks into the folder the first made rather
    than re-creating it: the level statement reads the existing row's flags
    back, so what is made beneath it is still born a record."""
    drive, chat = await _make_chat(repo, files_factory)
    async with repo.transaction():
        first = await tree.create_tree(
            repo,
            _tree_ctx(files_org),
            DriveId(drive.id),
            NodeId(chat.id),
            [".runtime/agent/agent.db"],
            idempotency_key="records-2",
        )
    async with repo.transaction():
        second = await tree.create_tree(
            repo,
            _tree_ctx(files_org),
            DriveId(drive.id),
            NodeId(chat.id),
            [".runtime/envs/py/bin/python"],
            idempotency_key="records-3",
        )

    assert second.nodes[".runtime"] == first.nodes[".runtime"]
    assert ".runtime" not in second.created
    flags = await _flags_by_path(repo, drive, second.nodes)
    assert all(flags[path] & RECORD_BIT for path in second.created), flags
