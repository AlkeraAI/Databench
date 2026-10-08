"""A folder in the trash cannot be worked on, so nobody holds it.

A lease is exclusive write on a subtree. A trashed subtree takes no writes from
anybody, so a lease on it fences nothing it should — and it blocked the two
things a person still does with a trashed folder: deleting it forever and
emptying the trash. The deleted chat whose box kept beating its folder's lease
is the case that surfaced it: "Delete forever" answered "Someone has this folder
for local use" for a folder nobody could open.

The rule, pinned from each side:

* trashing ends every lease on what it trashes, a descendant's included, and
  leaves a lease on a live folder ABOVE it alone;
* a lease a trash left live anyway (one from before the rule) is neither beaten
  nor re-granted, and does not stand between the owner and the purge;
* a lease on a live folder still fences exactly as it did.
"""

from __future__ import annotations

import uuid

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.files import errors
from alkera_core.files.clock import FakeClock
from alkera_core.files.ids import DriveId, NodeId
from alkera_core.files.leases import LeaseConflict, LeaseContext, LeaseService
from alkera_core.files.repo import FilesRepo
from alkera_core.files.trash import Trash
from alkera_core.models.files.stores import FileDrive
from sqlalchemy import text
from tests.files._kit.factory import FilesFactory, FilesOrg
from tests.files._kit.fencing import held_by

pytestmark = pytest.mark.asyncio

INSTANCE = "i1"


def _ctx(org: FilesOrg) -> ActingContext:
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER,
            id=str(org.admin_id),
            org_id=org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )


async def _seed(
    files_factory: FilesFactory, spec: str, *, drive: FileDrive | None = None
) -> dict[str, uuid.UUID]:
    """The tree ``spec`` names, by path, as ids rather than rows: a refused call
    rolls back and expires every row the session loaded."""
    tree = await files_factory.tree(spec, drive=drive or await files_factory.drive())
    return {name: node.id for name, node in tree.items()}


def _held(org: FilesOrg, epoch: int) -> LeaseContext:
    return held_by(_ctx(org), epoch, INSTANCE)


async def _acquire(
    repo: FilesRepo,
    org: FilesOrg,
    clock: FakeClock,
    node_id: uuid.UUID,
    *,
    instance: str = INSTANCE,
) -> int:
    async with repo.transaction():
        lease = await LeaseService(repo, _ctx(org), clock).acquire(
            NodeId(node_id), instance_id=instance, machine_id="box", purpose="chat"
        )
    return lease.epoch


async def _trash(
    repo: FilesRepo,
    org: FilesOrg,
    clock: FakeClock,
    node_id: uuid.UUID,
    *,
    lease: LeaseContext | None = None,
) -> None:
    async with repo.transaction():
        node = await repo.node(NodeId(node_id))
        assert node is not None
        etag = node.etag
    async with repo.transaction():
        await Trash(repo, _ctx(org), clock).trash(NodeId(node_id), if_match=etag, lease=lease)


async def _live(repo: FilesRepo, node_id: uuid.UUID) -> bool:
    """Whether the lease on ``node_id`` is live by the predicate every fence uses."""
    row = (
        await repo.session.execute(
            text(
                "SELECT released_at IS NULL AND reaped_at IS NULL AND expires_at > now() "
                "FROM file_leases WHERE node_id = :n"
            ),
            {"n": node_id},
        )
    ).scalar_one()
    await repo.session.commit()
    return bool(row)


async def _left_live(repo: FilesRepo, node_id: uuid.UUID) -> None:
    """The row a trash from before this rule left behind: still live, an hour of
    TTL ahead of it, on a folder that is in the trash."""
    await repo.session.execute(
        text(
            "UPDATE file_leases SET released_at = NULL, grantable_after = now(), "
            "expires_at = now() + interval '1 hour' WHERE node_id = :n"
        ),
        {"n": node_id},
    )
    await repo.session.commit()


async def _exists(repo: FilesRepo, node_id: uuid.UUID) -> bool:
    found = (
        await repo.session.execute(text("SELECT 1 FROM file_nodes WHERE id = :n"), {"n": node_id})
    ).first()
    await repo.session.commit()
    return found is not None


# ---- trashing ends what it trashes ------------------------------------------


async def test_trashing_a_folder_ends_the_lease_on_it(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    """The holder trashing the folder it holds gives the folder up with it."""
    ids = await _seed(files_factory, "chat/")
    epoch = await _acquire(repo, files_org, clock, ids["chat"])

    await _trash(repo, files_org, clock, ids["chat"], lease=_held(files_org, epoch))

    assert not await _live(repo, ids["chat"])


async def test_trashing_a_folder_ends_a_lease_held_on_a_folder_inside_it_and_nothing_else(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    """The lease is on a descendant, so the trash is not fenced by it — and it
    used to be left live inside a subtree nobody could write. A lease on a
    folder beside the trashed one is not the trash's business."""
    ids = await _seed(files_factory, "projects/ projects/chat/ other/")
    await _acquire(repo, files_org, clock, ids["projects/chat"])
    await _acquire(repo, files_org, clock, ids["other"], instance="i2")

    await _trash(repo, files_org, clock, ids["projects"])

    assert not await _live(repo, ids["projects/chat"])
    assert await _live(repo, ids["other"])


async def test_the_holder_trashing_a_file_in_its_folder_keeps_the_folder(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    """Only leases ON what is trashed end. A lease on a live folder ABOVE it is
    the mount the trash was written through, and it goes on holding."""
    ids = await _seed(files_factory, "work/ work/draft.txt")
    epoch = await _acquire(repo, files_org, clock, ids["work"])

    await _trash(repo, files_org, clock, ids["work/draft.txt"], lease=_held(files_org, epoch))

    assert await _live(repo, ids["work"])


# ---- a lease on a trashed folder is not kept ---------------------------------


async def test_a_beat_on_a_lease_over_a_trashed_folder_is_fenced(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    """The box that never heard: its lease survived the trash, and every beat it
    sent pushed the deadline another TTL out. The beat is fenced now, and the
    deadline it would have moved stays where it was."""
    ids = await _seed(files_factory, "chat/")
    epoch = await _acquire(repo, files_org, clock, ids["chat"])
    await _trash(repo, files_org, clock, ids["chat"], lease=_held(files_org, epoch))
    await _left_live(repo, ids["chat"])
    before = (
        await repo.session.execute(
            text("SELECT expires_at FROM file_leases WHERE node_id = :n"), {"n": ids["chat"]}
        )
    ).scalar_one()
    await repo.session.commit()

    with pytest.raises(LeaseConflict) as fenced:
        async with repo.transaction():
            await LeaseService(repo, _ctx(files_org), clock).heartbeat(
                NodeId(ids["chat"]), epoch=epoch, instance_id=INSTANCE
            )

    assert fenced.value.code == "files.lease_fenced"
    after = (
        await repo.session.execute(
            text("SELECT expires_at FROM file_leases WHERE node_id = :n"), {"n": ids["chat"]}
        )
    ).scalar_one()
    assert after == before


async def test_the_same_beat_on_a_live_folder_still_renews(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    """The asymmetric half: the trash check must not fence a folder that is not
    in the trash, or every box would lose every chat on its next beat."""
    ids = await _seed(files_factory, "chat/")
    epoch = await _acquire(repo, files_org, clock, ids["chat"])

    async with repo.transaction():
        beat = await LeaseService(repo, _ctx(files_org), clock).heartbeat(
            NodeId(ids["chat"]), epoch=epoch, instance_id=INSTANCE
        )

    assert beat.epoch == epoch
    assert await _live(repo, ids["chat"])


@pytest.mark.parametrize(
    "target",
    [pytest.param("chat", id="the-trashed-folder"), pytest.param("chat/work", id="inside-it")],
)
async def test_a_trashed_folder_cannot_be_taken(
    repo: FilesRepo,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    clock: FakeClock,
    target: str,
) -> None:
    """A box re-taking the folder of a chat deleted under it is told the folder
    is in the trash, rather than handed a fresh lease that would block its
    purge all over again."""
    ids = await _seed(files_factory, "chat/ chat/work/")
    await _trash(repo, files_org, clock, ids["chat"])

    with pytest.raises(errors.Conflict) as refused:
        await _acquire(repo, files_org, clock, ids[target])

    assert refused.value.code == "files.trashed"
    assert (
        await repo.session.execute(
            text("SELECT count(*) FROM file_leases WHERE node_id = :n"), {"n": ids[target]}
        )
    ).scalar_one() == 0


# ---- the purge is not blocked by a lease on what it removes -------------------


async def test_delete_forever_removes_a_trashed_folder_a_lease_was_left_on(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    ids = await _seed(files_factory, "chat/ chat/transcript.jsonl")
    epoch = await _acquire(repo, files_org, clock, ids["chat"])
    await _trash(repo, files_org, clock, ids["chat"], lease=_held(files_org, epoch))
    await _left_live(repo, ids["chat"])

    async with repo.transaction():
        await Trash(repo, _ctx(files_org), clock).purge(NodeId(ids["chat"]))

    assert not await _exists(repo, ids["chat"])
    assert not await _exists(repo, ids["chat/transcript.jsonl"])


async def test_emptying_the_trash_removes_a_trashed_folder_a_lease_was_left_on(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    drive = await files_factory.drive()
    drive_id = DriveId(drive.id)
    ids = await _seed(files_factory, "chat/ notes.txt", drive=drive)
    epoch = await _acquire(repo, files_org, clock, ids["chat"])
    await _trash(repo, files_org, clock, ids["chat"], lease=_held(files_org, epoch))
    await _trash(repo, files_org, clock, ids["notes.txt"])
    await _left_live(repo, ids["chat"])

    async with repo.transaction():
        removed = await Trash(repo, _ctx(files_org), clock).empty(drive_id)

    assert removed == 2
    assert not await _exists(repo, ids["chat"])


async def test_a_trashed_file_under_a_live_mount_is_still_the_mounts_to_purge(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    """The purge lets go of leases IN the trash, not of the one above it: a file
    the holder trashed inside a folder it still holds is refused to everyone
    else, and the mount keeps its lease."""
    ids = await _seed(files_factory, "work/ work/draft.txt")
    async with repo.transaction():
        lease = await LeaseService(repo, _ctx(files_org), clock).acquire(
            NodeId(ids["work"]), instance_id=INSTANCE, machine_id="laptop", purpose="mount"
        )
    await _trash(repo, files_org, clock, ids["work/draft.txt"], lease=_held(files_org, lease.epoch))

    with pytest.raises(LeaseConflict) as refused:
        async with repo.transaction():
            await Trash(repo, _ctx(files_org), clock).purge(NodeId(ids["work/draft.txt"]))

    assert refused.value.code == "files.leased"
    assert await _exists(repo, ids["work/draft.txt"])
    assert await _live(repo, ids["work"])


async def test_a_live_leased_folder_still_refuses_somebody_elses_trash(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    """No regression on the ordinary case: a folder a mount holds is not
    trashed out from under it by a write that carries no fence."""
    ids = await _seed(files_factory, "work/")
    async with repo.transaction():
        await LeaseService(repo, _ctx(files_org), clock).acquire(
            NodeId(ids["work"]), instance_id=INSTANCE, machine_id="laptop", purpose="mount"
        )

    with pytest.raises(LeaseConflict) as refused:
        await _trash(repo, files_org, clock, ids["work"])

    assert refused.value.code == "files.leased"
    assert await _live(repo, ids["work"])
