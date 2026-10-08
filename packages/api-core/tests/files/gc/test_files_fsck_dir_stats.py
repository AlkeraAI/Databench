"""fsck's dir-stats recount must see one folder's subtree and nothing else.

``path_ids`` labels are the node's **per-drive** ino, so the very same
label chain — ``1.2.3`` — exists in every drive that has grown three nodes.
A subtree predicate that compares paths without pinning the drive therefore
matches a stranger's files, and the recount it feeds reports drift against a
cache that was right all along. The fixture below plants exactly that
collision: a second drive whose folder and file carry byte-identical
``path_ids`` to the first drive's, with a different size, so an unscoped
recount cannot come out with the correct number by luck.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from _files_gc_factory import AdminOnlyFactory
from alkera_core.files import fsck as fsck_mod
from alkera_core.files.clock import FakeClock
from alkera_core.files.gc import Janitor
from alkera_core.files.hashing import hash_bytes
from alkera_core.files.ids import DomainId
from alkera_core.files.store.filesystem import FilesystemStore
from alkera_core.files.store.keys import object_key
from alkera_core.models.files.history import FileDirStats
from alkera_core.models.files.tree import FileNode
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files.gc.conftest import Domain, MtimeAges, Seeder

pytestmark = pytest.mark.asyncio

#: Distinct lengths, so a recount that folds the wrong drive in cannot land on
#: the right total by coincidence.
ALPHA_BYTES = b"alpha" * 11
BETA_BYTES = b"beta" * 17
STRANGER_BYTES = b"stranger" * 29


class Collision:
    """Two drives whose folders share a ``path_ids`` chain, one folder apiece."""

    def __init__(self, drive: Any, tree: dict[str, FileNode], other: dict[str, FileNode]) -> None:
        self.drive = drive
        self.tree = tree
        self.other = other

    @property
    def alpha(self) -> FileNode:
        return self.tree["alpha"]

    @property
    def beta(self) -> FileNode:
        return self.tree["beta"]


@pytest.fixture
async def drive(files_factory: Any) -> Any:
    return await files_factory.drive()


@pytest.fixture
def domain(tmp_path: Path, clock: FakeClock, drive: Any) -> Domain:
    root = tmp_path / "bucket"
    store = FilesystemStore(root, clock=clock, layout="bucket")
    return Domain(id=DomainId(drive.dedup_domain_id), root=root, store=store, ages=MtimeAges(root))


@pytest.fixture
def janitor(domain: Domain, repo_for_org: Any, clock: FakeClock) -> Janitor:
    return Janitor(repo_for_org, AdminOnlyFactory(domain.store), clock, age_source=domain.ages)


async def _place(
    seeder: Seeder,
    domain: Domain,
    session: AsyncSession,
    node: FileNode,
    payload: bytes,
) -> None:
    """Real bytes, a version that names them, and a head that points at it."""
    digests = hash_bytes(payload)
    key = object_key(digests.content_hash)
    domain.write(key, payload)
    version = await seeder.version(node, key, size=len(payload))
    version.content_hash = digests.content_hash.hex()
    node.head_version_id = version.id
    await session.commit()


@pytest.fixture
async def collision(
    files_session: AsyncSession,
    files_org: Any,
    files_factory: Any,
    files_org_factory: Any,
    domain: Domain,
    drive: Any,
) -> Collision:
    tree = await files_factory.tree(
        "alpha/ alpha/one.bin alpha/nested/ alpha/nested/deep.bin beta/ beta/two.bin",
        drive=drive,
    )
    seeder = Seeder(files_session, files_org, drive)
    await _place(seeder, domain, files_session, tree["alpha/one.bin"], ALPHA_BYTES)
    await _place(seeder, domain, files_session, tree["beta/two.bin"], BETA_BYTES)

    # The stranger: a second drive — an org gets exactly one, so it is a second
    # tenant — seeded to the same depth, so its ino labels, and therefore its
    # whole ``path_ids``, repeat drive A's exactly.
    other_org = await files_org_factory()
    other_drive = await files_factory.drive(org=other_org)
    other = await files_factory.tree("alpha/ alpha/one.bin", drive=other_drive)
    other_seeder = Seeder(files_session, other_org, other_drive)
    other_version = await other_seeder.version(
        other["alpha/one.bin"],
        object_key(hash_bytes(STRANGER_BYTES).content_hash),
        size=len(STRANGER_BYTES),
    )
    other["alpha/one.bin"].head_version_id = other_version.id

    # ``alpha`` holds one file with bytes plus the still-empty ``nested`` folder,
    # so its truthful cache is two direct children and one counted file.
    for folder, payload, kids in (
        (tree["alpha"], ALPHA_BYTES, 2),
        (tree["beta"], BETA_BYTES, 1),
    ):
        files_session.add(
            FileDirStats(
                node_id=folder.id,
                org_team_id=files_org.org_team_id,
                bytes=len(payload),
                files=1,
                direct_children=kids,
            )
        )
    await files_session.commit()
    return Collision(drive, tree, other)


async def test_the_two_drives_really_do_share_a_path_chain(
    collision: Collision, files_session: AsyncSession
) -> None:
    """The premise, asserted — otherwise the tests below could pass vacuously."""
    assert collision.alpha.path_ids == collision.other["alpha"].path_ids
    assert collision.tree["alpha/one.bin"].path_ids == collision.other["alpha/one.bin"].path_ids
    assert collision.alpha.drive_id != collision.other["alpha"].drive_id


async def test_dir_stats_recount_ignores_an_identically_pathed_other_drive(
    janitor: Janitor, collision: Collision, files_org: Any, domain: Domain
) -> None:
    """Correct caches stay correct: no drift is reported for either folder."""
    report = await fsck_mod.run_fsck(janitor, org=files_org.scope, domain_id=domain.id)
    assert report.by_code(fsck_mod.DIR_STATS_DRIFT) == ()
    assert report.clean


async def test_repair_writes_this_folders_own_totals_not_the_strangers(
    janitor: Janitor,
    collision: Collision,
    files_org: Any,
    domain: Domain,
    files_session: AsyncSession,
) -> None:
    """A genuinely stale row is repaired to the subtree's own numbers."""
    await files_session.execute(
        text("UPDATE file_dir_stats SET bytes = 1, files = 0 WHERE node_id = :n"),
        {"n": collision.alpha.id},
    )
    await files_session.commit()

    report = await fsck_mod.run_fsck(
        janitor, org=files_org.scope, domain_id=domain.id, repair_safe=True
    )
    drift = report.by_code(fsck_mod.DIR_STATS_DRIFT)
    assert [f.ref_id for f in drift] == [str(collision.alpha.id)]
    assert drift[0].detail["recomputed"] == [len(ALPHA_BYTES), 1, 2]

    row = (
        await files_session.execute(
            text("SELECT bytes, files, direct_children FROM file_dir_stats WHERE node_id = :n"),
            {"n": collision.alpha.id},
        )
    ).one()
    assert tuple(row) == (len(ALPHA_BYTES), 1, 2)


async def test_trashing_the_strangers_file_moves_no_number_here(
    janitor: Janitor,
    collision: Collision,
    files_org: Any,
    domain: Domain,
    files_session: AsyncSession,
) -> None:
    """The negative twin: the stranger is not an input, so removing it changes nothing.

    If the recount were still reaching across drives, trashing the stranger's
    file would *fix* the numbers; the absence of any change is what says it was
    never being read in the first place.
    """
    before = await fsck_mod.run_fsck(janitor, org=files_org.scope, domain_id=domain.id)
    await files_session.execute(
        text("UPDATE file_nodes SET trashed_at = now() WHERE id = :n"),
        {"n": collision.other["alpha/one.bin"].id},
    )
    await files_session.commit()
    after = await fsck_mod.run_fsck(janitor, org=files_org.scope, domain_id=domain.id)
    assert before.codes == after.codes == ()


async def test_the_recount_still_reaches_a_grandchild(
    janitor: Janitor,
    collision: Collision,
    files_factory: Any,
    files_org: Any,
    domain: Domain,
    files_session: AsyncSession,
) -> None:
    """Narrowing the predicate must not turn it into "direct children only"."""
    seeder = Seeder(files_session, files_org, collision.drive)
    payload = b"deep" * 13
    await _place(seeder, domain, files_session, collision.tree["alpha/nested/deep.bin"], payload)

    report = await fsck_mod.run_fsck(janitor, org=files_org.scope, domain_id=domain.id)
    drift = report.by_code(fsck_mod.DIR_STATS_DRIFT)
    assert [f.ref_id for f in drift] == [str(collision.alpha.id)]
    assert drift[0].detail["recomputed"] == [len(ALPHA_BYTES) + len(payload), 2, 2]
