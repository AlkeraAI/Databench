"""`recent` and `sharedWithMe`: two projections, both filtered before the cut."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.files import history, search
from alkera_core.files.errors import InvalidRequest
from alkera_core.files.filters import ListFilters
from alkera_core.files.ids import NodeId
from alkera_core.files.repo import FilesRepo
from alkera_core.models.files.history import FileHistory
from alkera_core.models.files.tree import FileNode
from sqlalchemy import func, select, text, update
from tests.files._kit.factory import FilesFactory, FilesOrg

pytestmark = pytest.mark.asyncio


def _ctx(org: FilesOrg, who: uuid.UUID | None = None) -> ActingContext:
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER,
            id=str(who or org.admin_id),
            org_id=org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )


async def _touch(repo: FilesRepo, ctx: ActingContext, node: FileNode) -> None:
    await history.record(repo, ctx, node_id=NodeId(node.id), kind="attrs", before=None, after={})


async def _age_history(repo: FilesRepo, node_id: uuid.UUID, days: int) -> None:
    """Backdate every history row of a node, to cross the 30-day window."""
    await repo.session.execute(
        update(FileHistory)
        .where(FileHistory.node_id == node_id)
        .values(at=func.now() - timedelta(days=days))
    )


async def test_recent_leaves_the_drive_skeleton_out(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory
) -> None:
    """Provisioning stamps the first caller's name on the root, ``home/``,
    ``Teams/`` and ``Shared/``; none of them is something the person touched. A
    home folder and a file under it are."""
    drive = await files_factory.drive()
    nodes = await files_factory.tree(
        "Shared/ home/ Teams/ home/dana/ home/dana/note.txt", drive=drive
    )
    root_id = drive.root_node_id
    assert root_id is not None
    # The three signposts, said here rather than taken from the rig: the rig's
    # root is a plain folder (a traversal-only container takes no direct write,
    # so nothing could be built under it), and it is this test that means the
    # product's skeleton.
    await repo.session.execute(
        update(FileNode)
        .where(FileNode.id.in_([root_id, nodes["home"].id, nodes["Teams"].id]))
        .values(traversal_only=True)
    )
    await repo.session.commit()
    root = await repo.session.get(FileNode, root_id)
    assert root is not None
    async with repo.transaction():
        for node in (
            root,
            nodes["Shared"],
            nodes["home"],
            nodes["Teams"],
            nodes["home/dana"],
            nodes["home/dana/note.txt"],
        ):
            await _touch(repo, _ctx(files_org), node)
    async with repo.transaction():
        page = await search.recent(repo, _ctx(files_org))
    assert sorted(row.id for row in page.items) == sorted(
        [nodes["home/dana"].id, nodes["home/dana/note.txt"].id]
    )


async def test_recent_holds_only_what_this_caller_touched(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory
) -> None:
    drive = await files_factory.drive()
    nodes = await files_factory.tree("mine.txt theirs.txt untouched.txt", drive=drive)
    async with repo.transaction():
        await _touch(repo, _ctx(files_org), nodes["mine.txt"])
        await _touch(repo, _ctx(files_org, files_org.member_id), nodes["theirs.txt"])
    async with repo.transaction():
        page = await search.recent(repo, _ctx(files_org))
    assert [row.id for row in page.items] == [nodes["mine.txt"].id]


@pytest.mark.parametrize(
    ("age_days", "visible"),
    [
        pytest.param(0, True, id="just-now"),
        pytest.param(29, True, id="one-day-inside-the-window"),
        pytest.param(31, False, id="one-day-past-the-window"),
    ],
)
async def test_recent_closes_its_window_at_thirty_days(
    repo: FilesRepo,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    age_days: int,
    visible: bool,
) -> None:
    drive = await files_factory.drive()
    nodes = await files_factory.tree("aged.txt", drive=drive)
    async with repo.transaction():
        await _touch(repo, _ctx(files_org), nodes["aged.txt"])
        await _age_history(repo, nodes["aged.txt"].id, age_days)
    async with repo.transaction():
        page = await search.recent(repo, _ctx(files_org))
    assert bool(page.items) is visible


async def test_an_unreadable_recent_node_never_shortens_the_page(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory
) -> None:
    drive = await files_factory.drive()
    nodes = await files_factory.tree("r1.txt r2.txt r3.txt secret.txt", drive=drive)
    hidden = nodes["secret.txt"].id
    async with repo.transaction():
        for node in nodes.values():
            await _touch(repo, _ctx(files_org), node)
        # Recent is newest first, so the hidden node is made the newest: a page
        # cut before the readability filter would carry it at position one.
        for offset, path in enumerate(("r3.txt", "r2.txt", "r1.txt")):
            await repo.session.execute(
                repo.update_nodes()
                .where(FileNode.id == nodes[path].id)
                .values(mtime_ns=100 - offset)
            )
        await repo.session.execute(
            repo.update_nodes().where(FileNode.id == hidden).values(mtime_ns=1000)
        )

    def hides_the_secret(_table: object) -> object:
        return FileNode.id != hidden

    async with repo.transaction():
        plain = await search.recent(repo, _ctx(files_org), limit=2)
        filtered = await search.recent(
            repo,
            _ctx(files_org),
            limit=2,
            readable_predicate=hides_the_secret,  # type: ignore[arg-type]
        )
    assert hidden in {row.id for row in plain.items}
    assert hidden not in {row.id for row in filtered.items}
    assert [row.name_key for row in filtered.items] == ["r3.txt", "r2.txt"]
    assert len(plain.items) == len(filtered.items) == 2
    assert plain.has_more is filtered.has_more is True


async def test_recent_takes_the_same_chips_as_every_other_listing(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory
) -> None:
    drive = await files_factory.drive()
    nodes = await files_factory.tree("doc.txt folder/", drive=drive)
    async with repo.transaction():
        for node in nodes.values():
            await _touch(repo, _ctx(files_org), node)
    async with repo.transaction():
        files_only = await search.recent(repo, _ctx(files_org), filters=ListFilters(kind="file"))
    assert [row.id for row in files_only.items] == [nodes["doc.txt"].id]


async def test_shared_with_me_returns_direct_grants_to_any_of_my_principals(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory
) -> None:
    drive = await files_factory.drive()
    nodes = await files_factory.tree("to-me/ to-my-team/ to-nobody/", drive=drive)
    org_principal = files_org.org_team_id
    await files_factory.grant(nodes["to-me"], "user", files_org.admin_id, "reader")
    await files_factory.grant(nodes["to-my-team"], "org", org_principal, "reader")
    async with repo.transaction():
        page = await search.shared_with_me(repo, _ctx(files_org), principal_ids=[org_principal])
    assert {row.id for row in page.items} == {nodes["to-me"].id, nodes["to-my-team"].id}


async def test_shared_with_me_shows_the_root_not_every_granted_descendant(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory
) -> None:
    """A grant inside another grant is reached by opening the first one."""
    drive = await files_factory.drive()
    nodes = await files_factory.tree("top/ top/inner/ elsewhere/", drive=drive)
    for path in ("top", "top/inner", "elsewhere"):
        await files_factory.grant(nodes[path], "user", files_org.admin_id, "reader")
    async with repo.transaction():
        page = await search.shared_with_me(repo, _ctx(files_org))
    assert {row.id for row in page.items} == {nodes["top"].id, nodes["elsewhere"].id}


async def test_a_revoked_grant_is_not_shared_with_anybody(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory
) -> None:
    drive = await files_factory.drive()
    nodes = await files_factory.tree("gone/ kept/", drive=drive)
    revoked = await files_factory.grant(nodes["gone"], "user", files_org.admin_id, "reader")
    await files_factory.grant(nodes["kept"], "user", files_org.admin_id, "reader")
    # Revoked outside the Files role: the share table's membership CHECK reads
    # team_memberships, which that role deliberately cannot see.
    await repo.session.execute(
        text("UPDATE file_shares SET revoked_at = now() WHERE id = :id"), {"id": revoked.id}
    )
    await repo.session.commit()
    async with repo.transaction():
        page = await search.shared_with_me(repo, _ctx(files_org))
    assert [row.id for row in page.items] == [nodes["kept"].id]


async def test_an_expired_grant_is_not_shared_with_anybody(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory
) -> None:
    drive = await files_factory.drive()
    nodes = await files_factory.tree("expired/ live/", drive=drive)
    expired = await files_factory.grant(nodes["expired"], "user", files_org.admin_id, "reader")
    await files_factory.grant(nodes["live"], "user", files_org.admin_id, "reader")
    await repo.session.execute(
        text("UPDATE file_shares SET expires_at = now() - interval '1 second' WHERE id = :id"),
        {"id": expired.id},
    )
    await repo.session.commit()
    async with repo.transaction():
        page = await search.shared_with_me(repo, _ctx(files_org))
    assert [row.id for row in page.items] == [nodes["live"].id]


async def test_nothing_shared_is_an_empty_page_not_an_error(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory
) -> None:
    await files_factory.drive()
    async with repo.transaction():
        page = await search.shared_with_me(repo, _ctx(files_org))
    assert page.items == [] and page.next_marker is None


async def test_a_trashed_grant_target_is_not_shared(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory
) -> None:
    drive = await files_factory.drive()
    nodes = await files_factory.tree("binned/", drive=drive)
    await files_factory.grant(nodes["binned"], "user", files_org.admin_id, "reader")
    async with repo.transaction():
        await repo.session.execute(
            repo.update_nodes()
            .where(FileNode.id == nodes["binned"].id)
            .values(trashed_at=func.now())
        )
        page = await search.shared_with_me(repo, _ctx(files_org))
    assert page.items == []


async def test_a_recent_marker_cannot_be_replayed_against_shared_with_me(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory
) -> None:
    drive = await files_factory.drive()
    nodes = await files_factory.tree("r1.txt r2.txt r3.txt", drive=drive)
    async with repo.transaction():
        for node in nodes.values():
            await _touch(repo, _ctx(files_org), node)
    async with repo.transaction():
        page = await search.recent(repo, _ctx(files_org), limit=1)
        assert page.next_marker is not None
        with pytest.raises(InvalidRequest):
            await search.shared_with_me(repo, _ctx(files_org), marker=page.next_marker)


# --------------------------------------------------------------------------
# what the feed's own statement costs the database
# --------------------------------------------------------------------------

#: The index the actor lookup has to descend. ``(org_team_id, at)`` cannot
#: serve it: its second key is the timestamp, so the actor is a filter over
#: everything the org ever wrote rather than the start of a range.
ACTOR_INDEX = "ix_file_history_org_principal_at"


async def test_the_recent_window_descends_an_index_under_the_app_role(
    repo: FilesRepo, files_org: FilesOrg
) -> None:
    """ "What did THIS person touch lately" is an index condition, not a filter.

    ``recent`` is a projection over ``file_history``, and that table only ever
    grows: every mutation the org makes writes a row and nothing deletes one.
    So the predicate has to be answered by descending to the caller's own rows.
    With the actor as a *filter* instead, one person's thirty days costs a read
    of the whole org's history — a cost that follows the table while the answer
    does not, which on a mature tenant is the page's whole latency.

    Planned as the role the routes actually run as: under ``alkera_files_app``
    ``file_history`` carries FORCE row security, and PostgreSQL will not
    evaluate a qual ahead of a pending security qual unless that qual is
    leakproof. All three keys here are uuid and timestamptz comparisons, which
    are — but only an index that LEADS with the actor can carry all three.

    The whole-table fallbacks are turned off because a test tree is small
    enough that reading all of it wins on cost whatever the predicate says.
    What is proven here is which index can serve the lookup under the role, not
    which plan this row count happens to prefer: without the actor index the
    plan falls back to ``ix_file_history_org_at`` with ``acting_principal``
    demoted to a filter, which is exactly the regression.
    """
    who = files_org.admin_id
    # The production spelling of the predicate, not a second copy of it: the
    # index exists to serve these columns in this order.
    stmt = select(FileHistory.node_id).where(
        FileHistory.org_team_id == files_org.org_team_id,
        *search.touched_within_window(who),
    )
    compiled = stmt.compile(compile_kwargs={"literal_binds": True})

    async with repo.transaction() as scoped:
        connection = await scoped.session.connection()
        await connection.exec_driver_sql("SET LOCAL enable_seqscan = off")
        await connection.exec_driver_sql("SET LOCAL enable_bitmapscan = off")
        rows = await connection.exec_driver_sql(f"EXPLAIN {compiled}")
        plan = "\n".join(str(row[0]) for row in rows.fetchall())

    assert ACTOR_INDEX in plan, plan
    assert "Seq Scan on file_history" not in plan, plan
    # The actor reaches the index rather than being re-checked after it, which
    # is the whole difference between a descent and a scan.
    index_cond = next(line for line in plan.splitlines() if "Index Cond" in line)
    assert "acting_principal" in index_cond, plan
