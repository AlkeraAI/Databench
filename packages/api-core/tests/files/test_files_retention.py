"""The version pruner keeps exactly what the rules promise, and nothing less."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.config import Settings
from alkera_core.files import retention
from alkera_core.files.checkpoints import PausingCheckpoints
from alkera_core.files.clock import FakeClock
from alkera_core.files.errors import InvalidRequest
from alkera_core.files.ids import DriveId, NodeId
from alkera_core.files.repo import FilesRepo
from alkera_core.files.retention import KEEP_NEWEST, KEEP_WINDOW
from alkera_core.models.files.history import (
    FileContentGrant,
    FileHistory,
    FileRetentionLabel,
)
from alkera_core.models.files.ops import FileOp
from alkera_core.models.files.stores import FileDrive
from alkera_core.models.files.tree import FileNode
from alkera_core.models.files.versions import FileVersion
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files._kit.factory import EPOCH, FilesFactory, FilesOrg


async def _versions(
    session: AsyncSession,
    node: FileNode,
    *,
    count: int,
    newest_at: datetime,
    step: timedelta,
) -> list[FileVersion]:
    """``count`` versions on ``node``, seq 1..count, newest at ``newest_at``.

    Written oldest-first so ``seq`` and ``created_at`` agree, which is what lets
    a test move one boundary (age) without disturbing the other (count).
    """
    made: list[FileVersion] = []
    for index in range(count):
        seq = index + 1
        age = step * (count - seq)
        version = FileVersion(
            id=uuid.uuid4(),
            org_team_id=node.org_team_id,
            node_id=node.id,
            seq=seq,
            size_bytes=8,
            content_hash=f"hash-{node.id}-{seq}",
            source="upload",
            created_at=newest_at - age,
        )
        session.add(version)
        made.append(version)
    await session.flush()
    node.head_version_id = made[-1].id
    await session.commit()
    return made


async def _surviving(session: AsyncSession, node: FileNode) -> set[uuid.UUID]:
    rows = await session.execute(select(FileVersion.id).where(FileVersion.node_id == node.id))
    return {row[0] for row in rows.all()}


def _ctx(org: FilesOrg) -> ActingContext:
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER,
            id=str(org.admin_id),
            org_id=org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )


@pytest.mark.parametrize(
    ("over", "expected_kept"),
    [
        pytest.param(-1, KEEP_NEWEST - 1, id="one-under-the-count-rule"),
        pytest.param(0, KEEP_NEWEST, id="exactly-the-count-rule"),
        pytest.param(1, KEEP_NEWEST, id="one-over-the-count-rule-the-oldest-falls-out"),
    ],
)
async def test_count_boundary_keeps_the_newest_configured_versions(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    repo: FilesRepo,
    clock: FakeClock,
    over: int,
    expected_kept: int,
) -> None:
    """At the count boundary the count rule alone decides.

    Every version is older than the age window, so the age rule keeps nothing
    and the count rule is the only thing standing between them and deletion.
    """
    count = KEEP_NEWEST + over
    drive = await files_factory.drive()
    node = (await files_factory.tree("f.bin", drive=drive))["f.bin"]
    clock.move_to(EPOCH + timedelta(days=4000))
    # One version a day, the newest well past the window: the whole chain is
    # outside it, so nothing is kept for being young.
    await _versions(
        files_session,
        node,
        count=count,
        newest_at=clock.now() - KEEP_WINDOW - timedelta(days=1),
        step=timedelta(days=1),
    )

    async with repo.transaction():
        deleted = await retention.prune_versions(repo, now=clock.now())

    kept = await _surviving(files_session, node)
    assert len(kept) == expected_kept
    assert deleted == count - expected_kept


@pytest.mark.parametrize(
    ("over_days", "kept_by_age"),
    [
        pytest.param(-1, True, id="one-day-inside-the-window"),
        pytest.param(0, True, id="on-the-window-edge"),
        pytest.param(1, False, id="one-day-outside-the-window"),
    ],
)
async def test_age_boundary_keeps_versions_inside_the_configured_window(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    repo: FilesRepo,
    clock: FakeClock,
    over_days: int,
    kept_by_age: bool,
) -> None:
    """A version past the newest N survives only while it is inside the window.

    The chain is long enough that the oldest are outside the count rule; the one
    under test is placed at the age boundary, so its fate is the age rule's
    answer alone.
    """
    age = KEEP_WINDOW + timedelta(days=over_days)
    drive = await files_factory.drive()
    node = (await files_factory.tree("f.bin", drive=drive))["f.bin"]
    clock.move_to(EPOCH + timedelta(days=4000))
    now = clock.now()
    made = await _versions(
        files_session,
        node,
        count=KEEP_NEWEST + 50,
        # The newest live inside one hour; the rest are spread far enough back
        # that only their own timestamps matter.
        newest_at=now - KEEP_WINDOW - timedelta(days=200),
        step=timedelta(days=1),
    )
    probe = made[0]
    probe.created_at = now - age
    await files_session.commit()

    async with repo.transaction():
        await retention.prune_versions(repo, now=clock.now())

    kept = await _surviving(files_session, node)
    assert (probe.id in kept) is kept_by_age


@pytest.mark.parametrize(
    "exempt",
    ["keep_forever", "held"],
)
async def test_pinned_and_held_versions_survive_both_rules(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    repo: FilesRepo,
    clock: FakeClock,
    exempt: str,
) -> None:
    """A pin or a legal hold outranks age and count alike."""
    drive = await files_factory.drive()
    node = (await files_factory.tree("f.bin", drive=drive))["f.bin"]
    clock.move_to(EPOCH + timedelta(days=400))
    made = await _versions(
        files_session,
        node,
        count=150,
        newest_at=clock.now() - timedelta(days=60),
        step=timedelta(days=1),
    )
    marked = made[0]
    setattr(marked, exempt, True)
    twin = made[1]
    await files_session.commit()

    async with repo.transaction():
        await retention.prune_versions(repo, now=clock.now())

    kept = await _surviving(files_session, node)
    assert marked.id in kept
    # The negative twin: the version next to it, identical in every way the
    # rules read, is gone — so the survival above is the flag, not luck.
    assert twin.id not in kept


async def test_an_open_undo_inverse_keeps_the_version_it_names(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    repo: FilesRepo,
    clock: FakeClock,
) -> None:
    """A version an undoable operation could restore is never pruned."""
    drive = await files_factory.drive()
    node = (await files_factory.tree("f.bin", drive=drive))["f.bin"]
    clock.move_to(EPOCH + timedelta(days=400))
    made = await _versions(
        files_session,
        node,
        count=150,
        newest_at=clock.now() - timedelta(days=60),
        step=timedelta(days=1),
    )
    referenced, unreferenced = made[0], made[1]
    files_session.add(
        FileOp(
            id=uuid.uuid4(),
            org_team_id=node.org_team_id,
            drive_id=drive.id,
            kind="trash",
            actor=files_org.member_id,
            inverse={"restore": {"version_id": str(referenced.id)}},
            # Postgres ``now()`` decides whether the window is open, so the
            # deadline is written far enough ahead to be open on any runner.
            undoable_until=datetime.now(UTC) + timedelta(days=1),
            state="done",
        )
    )
    await files_session.commit()

    async with repo.transaction():
        await retention.prune_versions(repo, now=clock.now())

    kept = await _surviving(files_session, node)
    assert referenced.id in kept
    assert unreferenced.id not in kept


async def test_an_expired_undo_window_no_longer_keeps_its_version(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    repo: FilesRepo,
    clock: FakeClock,
) -> None:
    """The twin of the case above: a closed window keeps nothing."""
    drive = await files_factory.drive()
    node = (await files_factory.tree("f.bin", drive=drive))["f.bin"]
    clock.move_to(EPOCH + timedelta(days=400))
    made = await _versions(
        files_session,
        node,
        count=150,
        newest_at=clock.now() - timedelta(days=60),
        step=timedelta(days=1),
    )
    files_session.add(
        FileOp(
            id=uuid.uuid4(),
            org_team_id=node.org_team_id,
            drive_id=drive.id,
            kind="trash",
            actor=files_org.member_id,
            inverse={"restore": {"version_id": str(made[0].id)}},
            undoable_until=datetime.now(UTC) - timedelta(days=1),
            state="done",
        )
    )
    await files_session.commit()

    async with repo.transaction():
        await retention.prune_versions(repo, now=clock.now())

    assert made[0].id not in await _surviving(files_session, node)


async def test_a_live_content_grant_keeps_the_version_it_points_at(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    repo: FilesRepo,
    clock: FakeClock,
) -> None:
    """An unexpired signed URL is a GC root: its bytes outlive both rules."""
    drive = await files_factory.drive()
    node = (await files_factory.tree("f.bin", drive=drive))["f.bin"]
    clock.move_to(EPOCH + timedelta(days=400))
    made = await _versions(
        files_session,
        node,
        count=150,
        newest_at=clock.now() - timedelta(days=60),
        step=timedelta(days=1),
    )
    live, twin = made[0], made[1]
    files_session.add(
        FileContentGrant(
            nonce=uuid.uuid4().hex,
            org_team_id=node.org_team_id,
            version_id=live.id,
            # Postgres ``now()`` decides liveness, so the deadline is written
            # against the wall clock the database reads, not the fake one.
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
    )
    await files_session.commit()

    async with repo.transaction():
        await retention.prune_versions(repo, now=clock.now())

    kept = await _surviving(files_session, node)
    assert live.id in kept
    # The negative twin: identical in age and position, ungranted, and gone.
    assert twin.id not in kept


async def test_a_version_is_prunable_only_once_its_grant_row_is_swept(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    repo: FilesRepo,
    clock: FakeClock,
) -> None:
    """An expired grant still pins its bytes until the janitor reaps the row.

    The row is the foreign key a redemption reads, so deleting the version under
    it would either break the download or corrupt the table. Retention hands the
    ordering to the janitor rather than racing it.
    """
    drive = await files_factory.drive()
    node = (await files_factory.tree("f.bin", drive=drive))["f.bin"]
    clock.move_to(EPOCH + timedelta(days=400))
    made = await _versions(
        files_session,
        node,
        count=150,
        newest_at=clock.now() - timedelta(days=60),
        step=timedelta(days=1),
    )
    stale = made[0]
    nonce = uuid.uuid4().hex
    files_session.add(
        FileContentGrant(
            nonce=nonce,
            org_team_id=node.org_team_id,
            version_id=stale.id,
            expires_at=datetime.now(UTC) - timedelta(minutes=5),
        )
    )
    await files_session.commit()

    async with repo.transaction():
        await retention.prune_versions(repo, now=clock.now())
    assert stale.id in await _surviving(files_session, node)

    grant = await files_session.get(FileContentGrant, nonce)
    assert grant is not None
    await files_session.delete(grant)
    await files_session.commit()

    async with repo.transaction():
        await retention.prune_versions(repo, now=clock.now())
    assert stale.id not in await _surviving(files_session, node)


async def test_the_head_is_never_removed_even_when_it_is_ancient(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    repo: FilesRepo,
    clock: FakeClock,
) -> None:
    """A file untouched for years still has its current bytes."""
    drive = await files_factory.drive()
    node = (await files_factory.tree("f.bin", drive=drive))["f.bin"]
    clock.move_to(EPOCH + timedelta(days=4000))
    made = await _versions(
        files_session,
        node,
        count=KEEP_NEWEST + 50,
        newest_at=clock.now() - timedelta(days=2000),
        step=timedelta(days=1),
    )
    head = made[-1]

    async with repo.transaction():
        await retention.prune_versions(repo, now=clock.now())

    kept = await _surviving(files_session, node)
    # The count rule floors the chain at the configured count even when every
    # version is years old, and the head is in that set.
    assert head.id in kept
    assert len(kept) == retention.KEEP_NEWEST

    # Under ``head-only`` the count rule is gone and only the head is left, so
    # the head's survival is the exemption and not the floor above.
    ctx = _ctx(files_org)
    async with repo.transaction():
        labelled = await repo.node(NodeId(node.id))
        assert labelled is not None
        await retention.apply_label(repo, ctx, labelled, retention.HEAD_ONLY)
    async with repo.transaction():
        await retention.prune_versions(repo, now=clock.now())
    assert await _surviving(files_session, node) == {head.id}


async def test_head_only_label_keeps_the_head_while_a_sibling_keeps_history(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    repo: FilesRepo,
    clock: FakeClock,
) -> None:
    """The ``.git/index`` case: a labelled path sheds history, its sibling does not."""
    drive = await files_factory.drive()
    tree = await files_factory.tree("git/ git/index other/ other/notes.txt", drive=drive)
    churn, quiet = tree["git/index"], tree["other/notes.txt"]
    clock.move_to(EPOCH + timedelta(days=400))
    # Both chains are young enough that the age rule alone would keep every
    # version, so only the label can separate them.
    churn_versions = await _versions(
        files_session, churn, count=40, newest_at=clock.now(), step=timedelta(hours=1)
    )
    quiet_versions = await _versions(
        files_session, quiet, count=40, newest_at=clock.now(), step=timedelta(hours=1)
    )

    ctx = _ctx(files_org)
    async with repo.transaction():
        node = await repo.node(NodeId(tree["git"].id))
        assert node is not None
        await retention.apply_label(repo, ctx, node, retention.HEAD_ONLY)

    async with repo.transaction():
        await retention.prune_versions(repo, now=clock.now())

    assert await _surviving(files_session, churn) == {churn_versions[-1].id}
    assert await _surviving(files_session, quiet) == {v.id for v in quiet_versions}


async def test_head_only_still_yields_to_a_hold(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    repo: FilesRepo,
    clock: FakeClock,
) -> None:
    """A label is a retention policy, not a way to destroy evidence."""
    drive = await files_factory.drive()
    tree = await files_factory.tree("git/ git/index", drive=drive)
    churn = tree["git/index"]
    clock.move_to(EPOCH + timedelta(days=400))
    made = await _versions(
        files_session, churn, count=10, newest_at=clock.now(), step=timedelta(hours=1)
    )
    made[0].held = True
    await files_session.commit()

    ctx = _ctx(files_org)
    async with repo.transaction():
        node = await repo.node(NodeId(tree["git"].id))
        assert node is not None
        await retention.apply_label(repo, ctx, node, retention.HEAD_ONLY)
    async with repo.transaction():
        await retention.prune_versions(repo, now=clock.now())

    assert await _surviving(files_session, churn) == {made[0].id, made[-1].id}


async def test_apply_label_writes_its_history_row_and_seeds_the_label(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    repo: FilesRepo,
) -> None:
    """The label, the history row and the announcement land in one transaction."""
    drive = await files_factory.drive()
    node_row = (await files_factory.tree("git/", drive=drive))["git"]

    ctx = _ctx(files_org)
    async with repo.transaction():
        node = await repo.node(NodeId(node_row.id))
        assert node is not None
        label = await retention.apply_label(repo, ctx, node, retention.HEAD_ONLY)
    assert label is not None

    rows = await files_session.execute(
        select(FileHistory.kind, FileHistory.after).where(FileHistory.node_id == node_row.id)
    )
    recorded = rows.all()
    assert [kind for kind, _ in recorded] == ["label"]
    assert recorded[0][1] == {"retention_label_id": str(label.id)}


async def test_a_labelled_subtree_can_be_overridden_closer_to_the_leaf(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    repo: FilesRepo,
    clock: FakeClock,
) -> None:
    """``label_for`` answers with the nearest label, not the outermost one."""
    drive = await files_factory.drive()
    tree = await files_factory.tree("a/ a/b/ a/b/c.txt", drive=drive)

    ctx = _ctx(files_org)
    async with repo.transaction():
        outer = await repo.node(NodeId(tree["a"].id))
        assert outer is not None
        await retention.apply_label(repo, ctx, outer, retention.HEAD_ONLY)

    async with repo.transaction():
        leaf = await repo.node(NodeId(tree["a/b/c.txt"].id))
        assert leaf is not None
        chain: Sequence[FileNode] = await repo.chain(leaf)
        inherited = await retention.label_for(repo, leaf, chain)
        assert inherited is not None
        assert inherited.name == retention.HEAD_ONLY

        await retention.apply_label(repo, ctx, leaf, None)
        cleared = await retention.label_for(repo, leaf, chain)
    # Clearing the leaf's own label leaves the ancestor's in force: the walk
    # resolves through the chain, it does not stop at the first node.
    assert cleared is not None
    assert cleared.name == retention.HEAD_ONLY


async def test_prune_batch_bounds_the_nodes_one_pass_touches(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    repo: FilesRepo,
    clock: FakeClock,
) -> None:
    """The janitor's pass is bounded work, and repeated passes finish the job."""
    drive = await files_factory.drive()
    tree = await files_factory.tree("a.bin b.bin c.bin", drive=drive)
    clock.move_to(EPOCH + timedelta(days=4000))
    for node in tree.values():
        await _versions(
            files_session,
            node,
            count=KEEP_NEWEST + 20,
            newest_at=clock.now() - KEEP_WINDOW - timedelta(days=1),
            step=timedelta(days=1),
        )

    async with repo.transaction():
        first = await retention.prune_versions(repo, now=clock.now(), batch=1)
    assert first == 20

    async with repo.transaction():
        rest = await retention.prune_versions(repo, now=clock.now(), batch=10)
    assert rest == 40

    async with repo.transaction():
        assert await retention.prune_versions(repo, now=clock.now(), batch=10) == 0


async def test_prune_is_scoped_to_the_running_org(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_org_factory: Callable[[], Awaitable[FilesOrg]],
    files_factory: FilesFactory,
    repo: FilesRepo,
    clock: FakeClock,
) -> None:
    """One org's janitor never deletes another org's versions."""
    other = await files_org_factory()
    other_drive = await files_factory.drive(org=other)
    other_node = (await files_factory.tree("f.bin", drive=other_drive))["f.bin"]
    clock.move_to(EPOCH + timedelta(days=400))
    other_versions = await _versions(
        files_session,
        other_node,
        count=KEEP_NEWEST + 50,
        newest_at=clock.now() - KEEP_WINDOW - timedelta(days=1),
        step=timedelta(days=1),
    )

    mine_drive = await files_factory.drive()
    mine = (await files_factory.tree("f.bin", drive=mine_drive))["f.bin"]
    await _versions(
        files_session,
        mine,
        count=KEEP_NEWEST + 50,
        newest_at=clock.now() - KEEP_WINDOW - timedelta(days=1),
        step=timedelta(days=1),
    )

    async with repo.transaction():
        await retention.prune_versions(repo, now=clock.now())

    assert await _surviving(files_session, other_node) == {v.id for v in other_versions}
    assert len(await _surviving(files_session, mine)) == KEEP_NEWEST


async def test_the_module_defaults_are_the_settings_defaults() -> None:
    """The hosted rule is spelled once, whichever end you read it from.

    `KEEP_NEWEST` / `KEEP_WINDOW` document the Alkera-hosted rule and the
    settings carry it into a deployment; a value changed at one end and not the
    other would make the documentation lie about what actually prunes.
    """
    defaults = Settings.model_fields
    assert defaults["files_version_keep_newest"].default == KEEP_NEWEST
    assert timedelta(days=defaults["files_version_keep_window_days"].default) == KEEP_WINDOW


@pytest.mark.parametrize(
    ("newest", "days", "expected"),
    [
        pytest.param(3, 90, retention.Policy(3, timedelta(days=90)), id="both-configured"),
        pytest.param(0, 0, retention.Policy(0, timedelta(0)), id="zero-is-a-real-bound"),
        pytest.param(-1, 90, retention.Policy(None, timedelta(days=90)), id="negative-count"),
        pytest.param(5, -1, retention.Policy(5, None), id="negative-window"),
    ],
)
def test_the_configured_rule_reads_the_settings_and_over_keeps_on_nonsense(
    newest: int,
    days: int,
    expected: retention.Policy,
) -> None:
    """A deployment's two numbers ARE the global rule; a negative one keeps everything."""
    settings = Settings(
        files_version_keep_newest=newest,
        files_version_keep_window_days=days,
    )
    assert retention.configured_policy(settings) == expected


@pytest.mark.parametrize(
    ("keep_newest", "keep_days", "expected_kept"),
    [
        pytest.param(2, 0, 2, id="a-deployment-that-keeps-two"),
        pytest.param(20, 0, 20, id="a-deployment-that-keeps-twenty"),
        pytest.param(0, 0, 1, id="a-deployment-that-keeps-only-the-head"),
    ],
)
async def test_the_configured_count_is_what_the_pruner_keeps(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    repo: FilesRepo,
    clock: FakeClock,
    keep_newest: int,
    keep_days: int,
    expected_kept: int,
) -> None:
    """A self-hosted deployment's own numbers decide, not the hosted defaults."""
    drive = await files_factory.drive()
    node = (await files_factory.tree("f.bin", drive=drive))["f.bin"]
    clock.move_to(EPOCH + timedelta(days=4000))
    made = await _versions(
        files_session,
        node,
        count=40,
        newest_at=clock.now() - timedelta(days=1),
        step=timedelta(hours=1),
    )
    policy = retention.configured_policy(
        Settings(
            files_version_keep_newest=keep_newest,
            files_version_keep_window_days=keep_days,
        )
    )

    async with repo.transaction():
        deleted = await retention.prune_versions(repo, now=clock.now(), policy=policy)

    kept = await _surviving(files_session, node)
    # The head is exempt from every rule, so a count of zero still leaves one.
    assert len(kept) == expected_kept
    assert made[-1].id in kept
    assert deleted == 40 - expected_kept


async def test_the_configured_window_keeps_what_the_count_would_have_dropped(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    repo: FilesRepo,
    clock: FakeClock,
) -> None:
    """Widening the window alone rescues versions the count rule sheds.

    Same chain, same count bound, two deployments: the one with the longer
    window keeps the older half that the other deletes. That is the "whichever
    keeps MORE" union, driven by a setting.
    """
    drive = await files_factory.drive()
    node = (await files_factory.tree("weekly.bin", drive=drive))["weekly.bin"]
    clock.move_to(EPOCH + timedelta(days=4000))
    now = clock.now()
    # Ten versions, one a week apart: inside a 90 d window, outside a 7 d one.
    await _versions(files_session, node, count=10, newest_at=now, step=timedelta(days=7))

    async with repo.transaction():
        wide = await retention.prune_versions(
            repo,
            now=now,
            policy=retention.Policy(newest=1, window=timedelta(days=90)),
        )
    assert wide == 0
    assert len(await _surviving(files_session, node)) == 10

    async with repo.transaction():
        narrow = await retention.prune_versions(
            repo,
            now=now,
            policy=retention.Policy(newest=1, window=timedelta(days=7)),
        )
    assert narrow == 8
    # The head and the one written inside the week: the window is the only
    # thing that moved.
    assert len(await _surviving(files_session, node)) == 2


async def test_a_numeric_keep_versions_label_bounds_one_file(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    repo: FilesRepo,
    clock: FakeClock,
) -> None:
    """A per-file `keep_versions` label is a number, not one of three names.

    The labelled file keeps exactly what its label says while its sibling keeps
    what the deployment says, and the label's own bound is not the head-only
    floor — so the number really is read.
    """
    drive = await files_factory.drive()
    tree = await files_factory.tree("a.bin b.bin", drive=drive)
    labelled, sibling = tree["a.bin"], tree["b.bin"]
    clock.move_to(EPOCH + timedelta(days=4000))
    now = clock.now()
    for node in (labelled, sibling):
        await _versions(files_session, node, count=12, newest_at=now, step=timedelta(hours=1))

    ctx = _ctx(files_org)
    async with repo.transaction():
        label = FileRetentionLabel(
            id=uuid.uuid4(),
            org_team_id=files_org.org_team_id,
            name="keep-three",
            policy={"keep_versions": 3, "keep_for_days": 0},
        )
        await repo.add(label)
        await repo.flush()
        node = await repo.node(NodeId(labelled.id))
        assert node is not None
        await retention.apply_label(repo, ctx, node, "keep-three")

    async with repo.transaction():
        await retention.prune_versions(
            repo,
            now=now,
            policy=retention.Policy(newest=8, window=timedelta(0)),
        )

    assert len(await _surviving(files_session, labelled)) == 3
    assert len(await _surviving(files_session, sibling)) == 8


async def test_a_label_naming_one_bound_leaves_the_other_to_the_deployment(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    repo: FilesRepo,
    clock: FakeClock,
) -> None:
    """`{"keep_versions": 1}` tightens the count and says nothing about age.

    The deployment's window still applies, so a chain written inside it survives
    in full — a label must not silently shed history it never spoke about.
    """
    drive = await files_factory.drive()
    node = (await files_factory.tree("a.bin", drive=drive))["a.bin"]
    clock.move_to(EPOCH + timedelta(days=4000))
    now = clock.now()
    await _versions(files_session, node, count=6, newest_at=now, step=timedelta(hours=1))

    ctx = _ctx(files_org)
    async with repo.transaction():
        label = FileRetentionLabel(
            id=uuid.uuid4(),
            org_team_id=files_org.org_team_id,
            name="keep-one",
            policy={"keep_versions": 1},
        )
        await repo.add(label)
        await repo.flush()
        target = await repo.node(NodeId(node.id))
        assert target is not None
        await retention.apply_label(repo, ctx, target, "keep-one")

    async with repo.transaction():
        await retention.prune_versions(
            repo,
            now=now,
            policy=retention.Policy(newest=0, window=timedelta(days=90)),
        )

    assert len(await _surviving(files_session, node)) == 6


@pytest.mark.parametrize(
    "document",
    [
        pytest.param({"keep_versions": -2}, id="a-negative-count"),
        pytest.param({"keep_versions": "none"}, id="a-string"),
        pytest.param({"keep_versions": True}, id="a-bool-is-not-a-count"),
        pytest.param({"keep_for_days": 1.5}, id="a-fractional-window"),
    ],
)
async def test_an_unreadable_label_bound_falls_back_to_the_deployment(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    repo: FilesRepo,
    clock: FakeClock,
    document: dict[str, object],
) -> None:
    """A bound this reader cannot read over-keeps; it never deletes on a guess."""
    drive = await files_factory.drive()
    node = (await files_factory.tree("a.bin", drive=drive))["a.bin"]
    clock.move_to(EPOCH + timedelta(days=4000))
    now = clock.now()
    await _versions(files_session, node, count=9, newest_at=now, step=timedelta(hours=1))

    ctx = _ctx(files_org)
    async with repo.transaction():
        label = FileRetentionLabel(
            id=uuid.uuid4(),
            org_team_id=files_org.org_team_id,
            name="unreadable",
            policy=document,
        )
        await repo.add(label)
        await repo.flush()
        target = await repo.node(NodeId(node.id))
        assert target is not None
        await retention.apply_label(repo, ctx, target, "unreadable")

    async with repo.transaction():
        await retention.prune_versions(
            repo,
            now=now,
            policy=retention.Policy(newest=4, window=timedelta(0)),
        )

    assert len(await _surviving(files_session, node)) == 4


async def test_prune_refuses_a_non_positive_batch(repo: FilesRepo, clock: FakeClock) -> None:
    """A batch of zero would be a silent no-op pass, so it is an error."""
    async with repo.transaction():
        with pytest.raises(InvalidRequest):
            await retention.prune_versions(repo, now=clock.now(), batch=0)


async def test_prune_reaches_its_checkpoints_in_order(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    repo: FilesRepo,
    clock: FakeClock,
    checkpoints: PausingCheckpoints,
) -> None:
    """The selection and the delete are separately interleavable."""
    points = checkpoints
    drive = await files_factory.drive()
    node = (await files_factory.tree("f.bin", drive=drive))["f.bin"]
    clock.move_to(EPOCH + timedelta(days=400))
    await _versions(
        files_session,
        node,
        count=110,
        newest_at=clock.now() - timedelta(days=60),
        step=timedelta(days=1),
    )

    async with repo.transaction():
        await retention.prune_versions(repo, now=clock.now(), checkpoints=points)

    assert points.reached == ("retention.prune.selected", "retention.prune.deleted")


async def test_drive_kind_is_unchanged_by_labelling(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    repo: FilesRepo,
) -> None:
    """Labelling bumps the node's etag — the surface a client polls on."""
    drive: FileDrive = await files_factory.drive()
    node_row = (await files_factory.tree("git/", drive=drive))["git"]
    before = node_row.etag

    ctx = _ctx(files_org)
    async with repo.transaction():
        node = await repo.node(NodeId(node_row.id))
        assert node is not None
        await retention.apply_label(repo, ctx, node, retention.HEAD_ONLY)

    await files_session.refresh(node_row)
    assert node_row.etag == before + 1
    assert DriveId(drive.id) == DriveId(node_row.drive_id)
