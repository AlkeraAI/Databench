"""The retention-policy seam: a label the library never named prunes correctly.

Future caller this stands for: an org's ``legal-hold`` (and the compliance
labels behind it — ``compliance-7y``, ``audit``) created as a **row** by an
admin or a policy engine, never as a constant in ``retention.py``. The claim is
that registering that row is the whole change: the pruner reads the policy out
of the label document, so a name the library has never heard of governs a
subtree the first time the janitor runs.

Driven through the real entry point, :func:`retention.prune_versions`, with the
labelled node's unlabelled sibling in the same call as the control — so the
assertion is the label talking and not a fixture that happened to sit inside
the global rule.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.files import retention
from alkera_core.files.clock import FakeClock
from alkera_core.files.ids import NodeId
from alkera_core.files.repo import FilesRepo
from alkera_core.models.files.history import FileRetentionLabel
from alkera_core.models.files.tree import FileNode
from alkera_core.models.files.versions import FileVersion
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files._kit.factory import EPOCH, FilesFactory, FilesOrg

pytestmark = pytest.mark.asyncio

#: Big enough that the global count rule has to drop some of it, so a test that
#: "passes" without the label is impossible.
HISTORY = retention.KEEP_NEWEST + 50
#: Every version is older than the global window, so the age half of the rule
#: keeps nothing either.
OLDEST_AGE = retention.KEEP_WINDOW + timedelta(days=1)

#: The name an admin picks. It is deliberately NOT ``retention.HEAD_ONLY`` and
#: deliberately not seeded: the library must never have heard of it.
LEGAL_HOLD = "legal-hold"


async def _history(
    session: AsyncSession, node: FileNode, *, newest_at: datetime
) -> list[FileVersion]:
    """``HISTORY`` versions on ``node``, one a day, the newest ``newest_at``."""
    made: list[FileVersion] = []
    for index in range(HISTORY):
        seq = index + 1
        made.append(
            FileVersion(
                id=uuid.uuid4(),
                org_team_id=node.org_team_id,
                node_id=node.id,
                seq=seq,
                size_bytes=8,
                content_hash=f"hash-{node.id}-{seq}",
                source="upload",
                created_at=newest_at - timedelta(days=HISTORY - seq),
            )
        )
    session.add_all(made)
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


async def _register(
    session: AsyncSession, org: FilesOrg, *, name: str, policy: dict[str, object]
) -> FileRetentionLabel:
    """The registration point: a label row, created the way an admin would."""
    label = FileRetentionLabel(
        id=uuid.uuid4(), org_team_id=org.org_team_id, name=name, policy=dict(policy)
    )
    session.add(label)
    await session.commit()
    return label


async def test_a_registered_legal_hold_label_keeps_every_version(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    repo: FilesRepo,
    clock: FakeClock,
) -> None:
    """A label the library never named, added as a row, prunes nothing under it.

    The sibling is the control: identical history, no label, and the global
    rule takes it down to its count in the *same* ``prune_versions`` call. So
    the whole chain surviving under ``legal-hold`` is the label's doing.
    """
    drive = await files_factory.drive()
    tree = await files_factory.tree("hold/ hold/case.pdf open/ open/notes.txt", drive=drive)
    held, sibling = tree["hold/case.pdf"], tree["open/notes.txt"]
    clock.move_to(EPOCH + timedelta(days=400))
    newest = clock.now() - OLDEST_AGE
    kept_ids = {version.id for version in await _history(files_session, held, newest_at=newest)}
    await _history(files_session, sibling, newest_at=newest)

    await _register(files_session, files_org, name=LEGAL_HOLD, policy=retention.KEEP_ALL_POLICY)
    async with repo.transaction():
        folder = await repo.node(NodeId(tree["hold"].id))
        assert folder is not None
        # Applied to the FOLDER: the policy has to descend the chain, which is
        # how a hold is actually placed.
        await retention.apply_label(repo, _ctx(files_org), folder, LEGAL_HOLD)

    async with repo.transaction():
        deleted = await retention.prune_versions(repo, now=clock.now())

    assert await _surviving(files_session, held) == kept_ids
    assert len(kept_ids) == HISTORY
    # The control fell to the global rule in the same pass.
    assert len(await _surviving(files_session, sibling)) == retention.KEEP_NEWEST
    assert deleted == HISTORY - retention.KEEP_NEWEST


async def test_removing_the_label_lets_the_global_rule_prune_the_same_history(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    repo: FilesRepo,
    clock: FakeClock,
) -> None:
    """Clearing the hold is enough to make the identical history prunable.

    This is the inversion of the case above run against one node: the first
    pass under the label deletes nothing, the second pass with the label
    cleared deletes exactly what the global rule always would have.
    """
    drive = await files_factory.drive()
    tree = await files_factory.tree("hold/ hold/case.pdf", drive=drive)
    node = tree["hold/case.pdf"]
    clock.move_to(EPOCH + timedelta(days=400))
    await _history(files_session, node, newest_at=clock.now() - OLDEST_AGE)

    await _register(files_session, files_org, name=LEGAL_HOLD, policy=retention.KEEP_ALL_POLICY)
    ctx = _ctx(files_org)
    async with repo.transaction():
        folder = await repo.node(NodeId(tree["hold"].id))
        assert folder is not None
        await retention.apply_label(repo, ctx, folder, LEGAL_HOLD)
    async with repo.transaction():
        assert await retention.prune_versions(repo, now=clock.now()) == 0
    assert len(await _surviving(files_session, node)) == HISTORY

    async with repo.transaction():
        folder = await repo.node(NodeId(tree["hold"].id))
        assert folder is not None
        await retention.apply_label(repo, ctx, folder, None)
    async with repo.transaction():
        assert (
            await retention.prune_versions(repo, now=clock.now()) == HISTORY - retention.KEEP_NEWEST
        )
    assert len(await _surviving(files_session, node)) == retention.KEEP_NEWEST


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        pytest.param(retention.KEEP_ALL_POLICY, HISTORY, id="explicit-null-bound-keeps-all"),
        pytest.param(retention.HEAD_ONLY_POLICY, 1, id="zero-bound-keeps-only-the-head"),
        pytest.param(
            {"keep_versions": 7, "keep_for_days": 7},
            7,
            id="a-numeric-bound-is-the-labels-own-count",
        ),
        pytest.param(
            {"keep_versions": "seven"},
            retention.KEEP_NEWEST,
            id="a-bound-this-reader-cannot-read-falls-back-to-the-global-rule",
        ),
        pytest.param(
            {"retain": "forever"},
            retention.KEEP_NEWEST,
            id="a-newer-writers-vocabulary-falls-back-to-the-global-rule",
        ),
    ],
)
async def test_the_policy_document_alone_decides_what_a_label_means(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    repo: FilesRepo,
    clock: FakeClock,
    policy: dict[str, object],
    expected: int,
) -> None:
    """One label name, five documents, five outcomes.

    The name is identical in every case, so nothing here can be the library
    special-casing a string; a document naming a number gets that number; and
    the two documents this reader cannot read keep history rather than shedding
    it, which is the fail-safe direction for a reader that meets a policy a
    newer writer invented.
    """
    drive = await files_factory.drive()
    tree = await files_factory.tree("hold/ hold/case.pdf", drive=drive)
    node = tree["hold/case.pdf"]
    clock.move_to(EPOCH + timedelta(days=400))
    await _history(files_session, node, newest_at=clock.now() - OLDEST_AGE)

    await _register(files_session, files_org, name=LEGAL_HOLD, policy=policy)
    async with repo.transaction():
        folder = await repo.node(NodeId(tree["hold"].id))
        assert folder is not None
        await retention.apply_label(repo, _ctx(files_org), folder, LEGAL_HOLD)
    async with repo.transaction():
        await retention.prune_versions(repo, now=clock.now())

    assert len(await _surviving(files_session, node)) == expected


async def test_a_nearer_label_overrides_a_hold_set_on_an_ancestor(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    repo: FilesRepo,
    clock: FakeClock,
) -> None:
    """The three policies compose through the chain the way two already did.

    A ``keep_all`` hold on the root with a ``head-only`` label on one leaf: the
    nearer label wins, which is what makes a registered policy a real peer of
    the seeded one rather than a special case bolted above it.
    """
    drive = await files_factory.drive()
    tree = await files_factory.tree("root/ root/kept.pdf root/churn.idx", drive=drive)
    kept, churn = tree["root/kept.pdf"], tree["root/churn.idx"]
    clock.move_to(EPOCH + timedelta(days=400))
    newest = clock.now() - OLDEST_AGE
    await _history(files_session, kept, newest_at=newest)
    churn_versions = await _history(files_session, churn, newest_at=newest)

    await _register(files_session, files_org, name=LEGAL_HOLD, policy=retention.KEEP_ALL_POLICY)
    ctx = _ctx(files_org)
    async with repo.transaction():
        root = await repo.node(NodeId(tree["root"].id))
        leaf = await repo.node(NodeId(churn.id))
        assert root is not None and leaf is not None
        await retention.apply_label(repo, ctx, root, LEGAL_HOLD)
        await retention.apply_label(repo, ctx, leaf, retention.HEAD_ONLY)

    async with repo.transaction():
        await retention.prune_versions(repo, now=clock.now())

    assert len(await _surviving(files_session, kept)) == HISTORY
    assert await _surviving(files_session, churn) == {churn_versions[-1].id}
