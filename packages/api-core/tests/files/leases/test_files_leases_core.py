"""Folder leases against real Postgres: grant, beat, hand back, fence."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.config import settings
from alkera_core.files import errors
from alkera_core.files.clock import FakeClock
from alkera_core.files.ids import DriveId, NodeId
from alkera_core.files.leases import (
    EPOCH_SEQ_BITS,
    LeaseContext,
    LeaseService,
    assert_lease_epoch,
    covering_lease,
    epoch_for,
    lease_ttl,
    leases_held_by,
    split_epoch,
)
from alkera_core.files.namespace import Namespace
from alkera_core.files.repo import FilesRepo
from sqlalchemy import text
from tests.files._kit.factory import FilesFactory, FilesOrg
from tests.files._kit.fencing import held_by

pytestmark = pytest.mark.asyncio


def _ctx(org: FilesOrg, principal_id: uuid.UUID | None = None) -> ActingContext:
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER,
            id=str(principal_id or org.admin_id),
            org_id=org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )


def _held(org: FilesOrg, epoch: int) -> LeaseContext:
    """The holder's own fencing context: the pair AND the principal behind it,
    which is what the route builds and what the lease row is matched on."""
    return held_by(_ctx(org), epoch, "i1")


class _PerCallTransaction:
    """Every service call in its own Files transaction, as a route makes it.

    Each method here is one request: the role and the org GUC are stamped, the
    statement runs, and the transaction commits — which is exactly the window a
    lease has to be correct across, and what makes the next call see committed
    rows rather than the caller's own uncommitted ones.
    """

    def __init__(self, repo: FilesRepo, service: Any) -> None:
        self._repo = repo
        self._service = service

    def __getattr__(self, name: str) -> Any:
        async def call(*args: Any, **kwargs: Any) -> Any:
            async with self._repo.transaction():
                return await getattr(self._service, name)(*args, **kwargs)

        return call


def _leases(
    repo: FilesRepo, org: FilesOrg, clock: FakeClock, principal_id: uuid.UUID | None = None
) -> Any:
    return _PerCallTransaction(repo, LeaseService(repo, _ctx(org, principal_id), clock))


def _namespace(repo: FilesRepo, org: FilesOrg, clock: FakeClock) -> Any:
    return _PerCallTransaction(repo, Namespace(repo, _ctx(org), clock, None))


async def _name_of(repo: FilesRepo, node_id: NodeId) -> bytes:
    """The stored name, read as a plain column.

    Around the ORM on purpose: the instances a refused call loaded are expired
    by its rollback, so re-reading raw bytes is what makes a write that should
    not have happened visible rather than invisible.
    """
    return bytes(
        (
            await repo.session.execute(
                text("SELECT name FROM file_nodes WHERE id = :n"), {"n": node_id}
            )
        ).scalar_one()
    )


async def _expire(repo: FilesRepo, node_id: uuid.UUID, *, ago: float = 60.0) -> None:
    """Age a lease past its TTL the way wall time would.

    The deadline lives in Postgres ``now()``, so a test cannot move the clock
    out from under it; it moves the deadline instead, which is the same thing
    the reaper will see. ``ago`` is how long it has been lapsed: the default is
    past the grant delay, so a test that only wants "this lease is dead" does
    not have to know the delay exists.
    """
    await repo.session.execute(
        text(
            "UPDATE file_leases SET expires_at = now() - make_interval(secs => :ago), "
            "grantable_after = now() - make_interval(secs => :ago) WHERE node_id = :n"
        ),
        {"n": node_id, "ago": ago},
    )
    # Committed, because the next service call opens its own transaction and a
    # rollback there would otherwise take this back with it.
    await repo.session.commit()


async def _lapse(repo: FilesRepo, node_id: uuid.UUID, *, ago: float = 30.0) -> None:
    """Rewind the WHOLE lease so it lapsed ``ago`` seconds back.

    Every stamp moves by the same interval, which is the row wall time actually
    leaves behind: the grant is older than the deadline, and the deadline is in
    the past. :func:`_expire` drags the deadline back UNDER the grant instead,
    which is the shape a force and the reaper make — both of which mean
    something other than "this holder went quiet", so a test about a holder
    that merely went quiet must not use it.
    """
    await repo.session.execute(
        text(
            "UPDATE file_leases SET "
            "acquired_at = acquired_at - (expires_at - now() + make_interval(secs => :ago)), "
            "heartbeat_at = heartbeat_at - (expires_at - now() + make_interval(secs => :ago)), "
            "grantable_after = "
            "grantable_after - (expires_at - now() + make_interval(secs => :ago)), "
            "expires_at = expires_at - (expires_at - now() + make_interval(secs => :ago)) "
            "WHERE node_id = :n"
        ),
        {"n": node_id, "ago": ago},
    )
    await repo.session.commit()


async def _lease_row(repo: FilesRepo, node_id: uuid.UUID) -> Any:
    return (
        await repo.session.execute(
            text("SELECT * FROM file_leases WHERE node_id = :n"), {"n": node_id}
        )
    ).first()


# ---- epoch composition ---------------------------------------------------


@pytest.mark.parametrize(
    ("generation", "seq"),
    [
        pytest.param(0, 0, id="zero"),
        pytest.param(0, 1, id="first-epoch"),
        pytest.param(1, 1, id="after-one-restore"),
        pytest.param(7, (1 << EPOCH_SEQ_BITS) - 1, id="last-seq-of-a-generation"),
    ],
)
async def test_epoch_halves_round_trip(generation: int, seq: int) -> None:
    assert split_epoch(epoch_for(generation, seq)) == (generation, seq)


async def test_the_granted_epoch_carries_the_platform_generation(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    """The generation multiplier is a literal inside the acquire statement.

    This is what keeps that literal and ``EPOCH_SEQ_BITS`` in step: raise the
    platform's generation and the epoch the statement hands out must split back
    into exactly that generation.
    """
    drive = await files_factory.drive()
    tree = await files_factory.tree("work/", drive=drive)
    await repo.session.execute(
        text(
            "INSERT INTO file_platform (id, restore_generation) VALUES (1, 3) "
            "ON CONFLICT (id) DO UPDATE SET restore_generation = 3"
        )
    )
    await repo.session.commit()

    lease = await _leases(repo, files_org, clock).acquire(
        NodeId(tree["work"].id), instance_id="i1", machine_id="laptop", purpose="mount"
    )
    assert split_epoch(lease.epoch) == (3, 1)


async def test_a_later_generation_outranks_every_epoch_of_the_previous_one() -> None:
    """The whole point of the high half: a restore cannot be outrun by a seq."""
    assert epoch_for(1, 0) > epoch_for(0, (1 << EPOCH_SEQ_BITS) - 1)


# ---- acquire / heartbeat / release ---------------------------------------


async def test_acquire_heartbeat_release_round_trip(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    drive = await files_factory.drive()
    tree = await files_factory.tree("work/", drive=drive)
    node = NodeId(tree["work"].id)
    leases = _leases(repo, files_org, clock)

    first = await leases.acquire(node, instance_id="i1", machine_id="laptop", purpose="mount")
    assert first.epoch > 0
    assert first.holder_instance_id == "i1"
    assert first.released_at is None
    assert not first.forced

    beat = await leases.heartbeat(node, epoch=first.epoch, instance_id="i1")
    assert beat.epoch == first.epoch
    assert beat.heartbeat_at >= first.heartbeat_at

    await leases.release(node, epoch=first.epoch, instance_id="i1")
    assert (await _lease_row(repo, node)).released_at is not None

    # Released is immediately re-grantable, and the next epoch is strictly above.
    second = await leases.acquire(node, instance_id="i2", machine_id="desktop", purpose="mount")
    assert second.epoch > first.epoch


async def test_epochs_strictly_increase_across_re_acquires(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    drive = await files_factory.drive()
    tree = await files_factory.tree("work/", drive=drive)
    node = NodeId(tree["work"].id)
    leases = _leases(repo, files_org, clock)

    issued: list[int] = []
    for n in range(4):
        lease = await leases.acquire(node, instance_id=f"i{n}", machine_id="m", purpose="mount")
        issued.append(lease.epoch)
        await leases.release(node, epoch=lease.epoch, instance_id=f"i{n}")

    assert issued == sorted(set(issued))
    # And the mark tracks the highest one, so a lost lease row cannot re-issue it.
    hwm = (
        await repo.session.execute(
            text("SELECT hwm FROM file_lease_epoch_hwm WHERE node_id = :n"), {"n": node}
        )
    ).scalar_one()
    assert hwm == issued[-1]


async def test_a_live_lease_refuses_a_second_holder(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    drive = await files_factory.drive()
    tree = await files_factory.tree("work/", drive=drive)
    node = NodeId(tree["work"].id)
    await _leases(repo, files_org, clock).acquire(
        node, instance_id="i1", machine_id="laptop", purpose="mount"
    )

    other = uuid.uuid4()
    with pytest.raises(errors.Conflict) as refused:
        await _leases(repo, files_org, clock, other).acquire(
            node, instance_id="i2", machine_id="desktop", purpose="mount"
        )
    assert refused.value.code == "files.leased"
    assert refused.value.detail is not None
    assert refused.value.detail["machine"] == "laptop"


async def test_the_same_instance_resumes_its_own_live_lease_at_the_same_epoch(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    """A killed mount coming back takes its own lease over immediately.

    The epoch does not move, because nothing was fenced: the writes that were
    in flight when it died are still its own, and a new epoch would refuse the
    upload sessions it is about to finish. The TTL is extended, and the beat at
    the original epoch still lands — proof the row was resumed rather than
    re-issued.
    """
    drive = await files_factory.drive()
    tree = await files_factory.tree("work/", drive=drive)
    node = NodeId(tree["work"].id)
    leases = _leases(repo, files_org, clock)
    first = await leases.acquire(node, instance_id="i1", machine_id="laptop", purpose="mount")

    resumed = await leases.acquire(node, instance_id="i1", machine_id="laptop", purpose="mount")
    assert resumed.epoch == first.epoch
    assert resumed.acquired_at == first.acquired_at
    assert resumed.expires_at >= first.expires_at
    beat = await leases.heartbeat(node, epoch=first.epoch, instance_id="i1")
    assert beat.epoch == first.epoch


async def test_a_second_instance_of_the_same_holder_is_still_refused(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    """The asymmetric twin of the resume: the *instance* is what resumes, not
    the person. A second machine of the same user is another writer and waits
    for the lease like anyone else."""
    drive = await files_factory.drive()
    tree = await files_factory.tree("work/", drive=drive)
    node = NodeId(tree["work"].id)
    leases = _leases(repo, files_org, clock)
    await leases.acquire(node, instance_id="i1", machine_id="laptop", purpose="mount")

    with pytest.raises(errors.Conflict) as refused:
        await leases.acquire(node, instance_id="i2", machine_id="laptop", purpose="mount")
    assert refused.value.code == "files.leased"


async def test_a_forced_lease_cannot_be_resumed_out_of_its_grace(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    """A holder cannot answer a force-release by re-acquiring: the grace is
    ``grantable_after``, and the resume respects it exactly as a stranger's
    acquire does — otherwise a manager's force would be undone by the client
    it was aimed at."""
    drive = await files_factory.drive()
    tree = await files_factory.tree("work/", drive=drive)
    node = NodeId(tree["work"].id)
    leases = _leases(repo, files_org, clock)
    await leases.acquire(node, instance_id="i1", machine_id="laptop", purpose="mount")
    await leases.force_release(node)

    with pytest.raises(errors.Conflict) as refused:
        await leases.acquire(node, instance_id="i1", machine_id="laptop", purpose="mount")
    assert refused.value.code == "files.leased"


async def test_a_lapsed_lease_is_grantable_again_at_a_higher_epoch(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    drive = await files_factory.drive()
    tree = await files_factory.tree("work/", drive=drive)
    node = NodeId(tree["work"].id)
    leases = _leases(repo, files_org, clock)
    first = await leases.acquire(node, instance_id="i1", machine_id="laptop", purpose="mount")

    await _expire(repo, node)
    second = await _leases(repo, files_org, clock, uuid.uuid4()).acquire(
        node, instance_id="i2", machine_id="desktop", purpose="mount"
    )
    assert second.epoch > first.epoch

    # The superseded holder's beat finds zero rows: that is how it learns.
    with pytest.raises(errors.Conflict) as fenced:
        await leases.heartbeat(node, epoch=first.epoch, instance_id="i1")
    assert fenced.value.code == "files.lease_fenced"


# ---- overlap -------------------------------------------------------------


@pytest.mark.parametrize(
    ("held", "wanted"),
    [
        pytest.param("work", "work/sub", id="ancestor-is-leased"),
        pytest.param("work/sub", "work", id="descendant-is-leased"),
    ],
)
async def test_overlap_with_another_holder_is_refused_and_names_them(
    repo: FilesRepo,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    clock: FakeClock,
    held: str,
    wanted: str,
) -> None:
    drive = await files_factory.drive()
    tree = await files_factory.tree("work/ work/sub/", drive=drive)
    await _leases(repo, files_org, clock).acquire(
        NodeId(tree[held].id), instance_id="i1", machine_id="laptop", purpose="mount"
    )

    with pytest.raises(errors.Conflict) as refused:
        await _leases(repo, files_org, clock, uuid.uuid4()).acquire(
            NodeId(tree[wanted].id), instance_id="i2", machine_id="desktop", purpose="mount"
        )
    assert refused.value.code == "files.leased"
    assert refused.value.detail is not None
    assert refused.value.detail["machine"] == "laptop"


async def test_an_unreadable_leased_ancestor_refuses_without_naming_the_holder(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    """A 409 must never become an oracle for a folder the caller cannot see."""
    drive = await files_factory.drive()
    tree = await files_factory.tree("work/ work/sub/", drive=drive)
    await _leases(repo, files_org, clock).acquire(
        NodeId(tree["work"].id), instance_id="i1", machine_id="laptop", purpose="mount"
    )

    with pytest.raises(errors.Conflict) as refused:
        await _leases(repo, files_org, clock, uuid.uuid4()).acquire(
            NodeId(tree["work/sub"].id),
            instance_id="i2",
            machine_id="desktop",
            purpose="mount",
            can_read_holder=False,
        )
    assert refused.value.code == "files.leased"
    assert refused.value.detail is None


async def test_the_same_principal_may_lease_a_strict_subtree_of_its_own_lease(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    """A second machine taking part of my own mount is the allowed nesting."""
    drive = await files_factory.drive()
    tree = await files_factory.tree("work/ work/sub/", drive=drive)
    leases = _leases(repo, files_org, clock)
    await leases.acquire(
        NodeId(tree["work"].id), instance_id="laptop-1", machine_id="laptop", purpose="mount"
    )

    nested = await leases.acquire(
        NodeId(tree["work/sub"].id), instance_id="box-1", machine_id="box", purpose="box"
    )
    assert nested.machine_id == "box"


async def test_the_same_principal_may_not_lease_above_its_own_lease(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    """The negative twin: nesting is allowed downward only, never upward."""
    drive = await files_factory.drive()
    tree = await files_factory.tree("work/ work/sub/", drive=drive)
    leases = _leases(repo, files_org, clock)
    await leases.acquire(
        NodeId(tree["work/sub"].id), instance_id="box-1", machine_id="box", purpose="box"
    )

    with pytest.raises(errors.Conflict) as refused:
        await leases.acquire(
            NodeId(tree["work"].id), instance_id="laptop-1", machine_id="laptop", purpose="mount"
        )
    assert refused.value.code == "files.leased"


# ---- the fence on a real write -------------------------------------------


@pytest.mark.parametrize(
    ("carry", "expected"),
    [
        pytest.param("none", "files.leased", id="no-epoch-is-someone-elses-folder"),
        pytest.param("holder", None, id="the-holders-epoch-writes"),
        pytest.param("superseded", "files.lease_fenced", id="a-stale-epoch-is-fenced"),
    ],
)
async def test_a_rename_inside_a_leased_subtree(
    repo: FilesRepo,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    clock: FakeClock,
    carry: str,
    expected: str | None,
) -> None:
    drive = await files_factory.drive()
    tree = await files_factory.tree("work/ work/a.txt", drive=drive)
    folder = NodeId(tree["work"].id)
    # Plain values, not the ORM instance: a refused call rolls back and expires
    # every attribute it loaded, and re-reading one would be a sync query.
    target_id = NodeId(tree["work/a.txt"].id)
    target_etag = tree["work/a.txt"].etag
    leases = _leases(repo, files_org, clock)
    held = await leases.acquire(folder, instance_id="i1", machine_id="laptop", purpose="mount")

    if carry == "none":
        lease = None
    elif carry == "holder":
        lease = _held(files_org, held.epoch)
    else:
        lease = _held(files_org, held.epoch - 1)

    namespace = _namespace(repo, files_org, clock)
    if expected is None:
        renamed = await namespace.rename(target_id, b"b.txt", if_match=target_etag, lease=lease)
        assert renamed.name == b"b.txt"
        return

    with pytest.raises(errors.Conflict) as refused:
        await namespace.rename(target_id, b"b.txt", if_match=target_etag, lease=lease)
    assert refused.value.code == expected
    # The refusal changed nothing: the name is still what it was.
    assert await _name_of(repo, target_id) == b"a.txt"


async def test_a_write_outside_every_lease_is_untouched(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    """The negative twin of the fence: an unleased tree needs no epoch."""
    drive = await files_factory.drive()
    tree = await files_factory.tree("work/ other/ other/a.txt", drive=drive)
    await _leases(repo, files_org, clock).acquire(
        NodeId(tree["work"].id), instance_id="i1", machine_id="laptop", purpose="mount"
    )

    target = tree["other/a.txt"]
    renamed = await _namespace(repo, files_org, clock).rename(
        NodeId(target.id), b"b.txt", if_match=target.etag
    )
    assert renamed.name == b"b.txt"


@pytest.mark.parametrize(
    ("can_read", "expected"),
    [
        pytest.param(False, errors.NotFound, id="unreadable-target-is-not-found"),
        pytest.param(True, errors.Conflict, id="readable-target-is-a-conflict"),
    ],
)
async def test_the_policy_decides_before_the_lease_does(
    repo: FilesRepo,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    clock: FakeClock,
    can_read: bool,
    expected: type[Exception],
) -> None:
    """A non-holder who cannot read the node gets the not-found class, not 409.

    Otherwise a refusal tells an outsider that the folder exists and that
    somebody has it mounted — the oracle the whole error vocabulary avoids.
    """
    drive = await files_factory.drive()
    tree = await files_factory.tree("work/ work/a.txt", drive=drive)
    await _leases(repo, files_org, clock).acquire(
        NodeId(tree["work"].id), instance_id="i1", machine_id="laptop", purpose="mount"
    )

    target = tree["work/a.txt"]
    namespace = _namespace(repo, files_org, clock)
    with pytest.raises(expected):
        await namespace.rename(
            NodeId(target.id),
            b"b.txt",
            if_match=target.etag,
            lease=LeaseContext(can_read_target=can_read),
        )


# ---- force and request ---------------------------------------------------


async def test_forcing_a_lease_never_moves_its_deadline_later(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    """A dead holder's lease lapses when its TTL runs out. Forcing it gave it
    a fresh TTL from now, and every click on "ask for it back" added another
    one, so the folder stayed held by nobody for as long as people kept
    asking. Forcing only ever brings the deadline nearer."""
    drive = await files_factory.drive()
    tree = await files_factory.tree("dead/", drive=drive)
    node = NodeId(tree["dead"].id)
    leases = _leases(repo, files_org, clock)
    await leases.acquire(node, instance_id="i1", machine_id="gone-box", purpose="mount")
    # The holder last beat long ago: two minutes of its lease are left.
    await repo.session.execute(
        text(
            "UPDATE file_leases SET expires_at = now() + interval '120 seconds' WHERE node_id = :n"
        ),
        {"n": node},
    )
    before = (
        await repo.session.execute(
            text("SELECT expires_at FROM file_leases WHERE node_id = :n"), {"n": node}
        )
    ).scalar_one()

    first = await leases.force_release(node, ttl=timedelta(seconds=600))
    again = await leases.force_release(node, ttl=timedelta(seconds=600))

    assert first.expires_at == before, "a grace longer than what is left adds nothing"
    assert again.expires_at == before, "forcing again adds nothing"

    # A grace shorter than what is left does bring the deadline in.
    sooner = await leases.force_release(node, ttl=timedelta(seconds=30))
    assert sooner.expires_at < before


async def test_force_release_tells_the_holder_and_then_lapses(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    drive = await files_factory.drive()
    tree = await files_factory.tree("work/", drive=drive)
    node = NodeId(tree["work"].id)
    leases = _leases(repo, files_org, clock)
    held = await leases.acquire(node, instance_id="i1", machine_id="laptop", purpose="mount")
    assert not (await leases.heartbeat(node, epoch=held.epoch, instance_id="i1")).forced

    await leases.force_release(node, ttl=timedelta(seconds=30))

    # The holder is told, and its beat no longer extends the grace.
    told = await leases.heartbeat(node, epoch=held.epoch, instance_id="i1")
    assert told.forced
    assert told.expires_at == held.expires_at or told.expires_at > held.acquired_at

    # Nobody may take the folder during the grace.
    with pytest.raises(errors.Conflict) as during:
        await _leases(repo, files_org, clock, uuid.uuid4()).acquire(
            node, instance_id="i2", machine_id="desktop", purpose="mount"
        )
    assert during.value.code == "files.leased"

    # After the grace the lease lapses exactly like a crashed one.
    await _expire(repo, node)
    with pytest.raises(errors.Conflict) as after:
        await leases.heartbeat(node, epoch=held.epoch, instance_id="i1")
    assert after.value.code == "files.lease_fenced"
    taken = await _leases(repo, files_org, clock, uuid.uuid4()).acquire(
        node, instance_id="i2", machine_id="desktop", purpose="mount"
    )
    assert taken.epoch > held.epoch


async def test_request_release_returns_the_holder_and_changes_nothing(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    drive = await files_factory.drive()
    tree = await files_factory.tree("work/", drive=drive)
    node = NodeId(tree["work"].id)
    leases = _leases(repo, files_org, clock)
    held = await leases.acquire(node, instance_id="i1", machine_id="laptop", purpose="mount")
    before = await _lease_row(repo, node)

    asked = await _leases(repo, files_org, clock, uuid.uuid4()).request_release(node)
    assert asked.machine_id == "laptop"
    assert asked.epoch == held.epoch

    after = await _lease_row(repo, node)
    assert (after.epoch, after.expires_at, after.released_at, after.grantable_after) == (
        before.epoch,
        before.expires_at,
        before.released_at,
        before.grantable_after,
    )


@pytest.mark.parametrize(
    ("carry", "expected"),
    [
        pytest.param("none", "files.leased", id="create-with-no-epoch"),
        pytest.param("holder", None, id="create-with-the-holders-epoch"),
        pytest.param("superseded", "files.lease_fenced", id="create-with-a-stale-epoch"),
    ],
)
async def test_a_create_inside_a_leased_folder_is_fenced(
    repo: FilesRepo,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    clock: FakeClock,
    carry: str,
    expected: str | None,
) -> None:
    """A create is a write inside the parent, so the parent's lease governs it."""
    drive = await files_factory.drive()
    tree = await files_factory.tree("work/", drive=drive)
    folder = NodeId(tree["work"].id)
    held = await _leases(repo, files_org, clock).acquire(
        folder, instance_id="i1", machine_id="laptop", purpose="mount"
    )
    if carry == "none":
        lease = None
    elif carry == "holder":
        lease = _held(files_org, held.epoch)
    else:
        lease = _held(files_org, held.epoch - 1)

    namespace = _namespace(repo, files_org, clock)
    if expected is None:
        made = await namespace.create(DriveId(drive.id), folder, "file", b"new.txt", lease=lease)
        assert made.name == b"new.txt"
        return

    with pytest.raises(errors.Conflict) as refused:
        await namespace.create(DriveId(drive.id), folder, "file", b"new.txt", lease=lease)
    assert refused.value.code == expected
    # The refusal left the folder empty: nothing was written before the fence.
    assert await _children_of(repo, folder) == 0


async def test_only_the_release_applies_its_own_batch_past_the_drives_node_ceiling(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    """The exemption from the drive's ceilings is the release's, and the server
    is the one holding it.

    A holder writes under the same fence all day — the live plane sends one
    every few hundred milliseconds — so a fenced write that could spend the
    exemption would make every holder permanently unbounded. What still has to
    land is what the RELEASE carries: the service builds the context its hook
    runs under, stamps ``final`` on that one, and a holder cannot produce it.
    """
    drive = await files_factory.drive()
    tree = await files_factory.tree("work/", drive=drive)
    folder = NodeId(tree["work"].id)
    drive.quota_nodes = 0
    await files_factory._session.commit()
    drive_id = DriveId(drive.id)

    leases = _leases(repo, files_org, clock)
    held = await leases.acquire(folder, instance_id="i1", machine_id="laptop", purpose="mount")
    namespace = _namespace(repo, files_org, clock)

    # The holder's own create, under the fence it writes everything under: the
    # drive has no room for a node, and the fence does not make one.
    with pytest.raises(errors.QuotaExceeded) as refused:
        await namespace.create(
            drive_id,
            folder,
            "folder",
            b"outputs",
            lease=_held(files_org, held.epoch),
        )
    assert refused.value.code == "files.quota_nodes"
    assert await _children_of(repo, folder) == 0

    # The release applies its batch through the same verb, under the context
    # the lease service hands its hook — and that one lands.
    async def hook(hook_repo: FilesRepo, node_id: NodeId, *, lease: Any, changes: Any) -> None:
        await Namespace(hook_repo, _ctx(files_org), clock, None).create(
            drive_id, node_id, "folder", b"outputs", lease=lease
        )

    releasing = _PerCallTransaction(
        repo, LeaseService(repo, _ctx(files_org), clock, apply_final=hook)
    )
    await releasing.release(folder, epoch=held.epoch, instance_id="i1", final=["one batch"])

    assert await _children_of(repo, folder) == 1
    # ...and the folder is handed back in the same breath, so the exemption
    # lives exactly as long as the release that carried it.
    assert (await _lease_row(repo, folder)).released_at is not None


async def test_a_tree_raised_inside_a_leased_folder_is_fenced(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    """``create_tree`` writes every folder inside the parent, so it fences too."""
    from alkera_core.files.tree import create_tree

    drive = await files_factory.drive()
    tree = await files_factory.tree("work/", drive=drive)
    folder = NodeId(tree["work"].id)
    await _leases(repo, files_org, clock).acquire(
        folder, instance_id="i1", machine_id="laptop", purpose="mount"
    )

    with pytest.raises(errors.Conflict) as refused:
        async with repo.transaction():
            await create_tree(
                repo,
                _ctx(files_org),
                DriveId(drive.id),
                folder,
                ["a/b"],
                idempotency_key="k-1",
            )
    assert refused.value.code == "files.leased"
    assert await _children_of(repo, folder) == 0


async def test_a_move_into_a_leased_folder_is_fenced(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    """The destination has a holder too, and grafting a tree in is their write.

    The source sits outside every mount, so the source-side fence finds no
    lease and lets the write through — only fencing the destination refuses it.
    """
    drive = await files_factory.drive()
    tree = await files_factory.tree("work/ other/ other/a.txt", drive=drive)
    await _leases(repo, files_org, clock).acquire(
        NodeId(tree["work"].id), instance_id="i1", machine_id="laptop", purpose="mount"
    )

    # Plain values, not the ORM instances: the refusal rolls back and expires
    # every attribute it loaded, and re-reading one would be a sync query.
    target_id = NodeId(tree["other/a.txt"].id)
    target_etag = int(tree["other/a.txt"].etag)
    parent_before = tree["other"].id
    destination = NodeId(tree["work"].id)
    with pytest.raises(errors.Conflict) as refused:
        await _namespace(repo, files_org, clock).move(target_id, destination, if_match=target_etag)
    assert refused.value.code == "files.leased"
    assert await _parent_of(repo, target_id) == parent_before


async def _children_of(repo: FilesRepo, parent_id: NodeId) -> int:
    """Live children, read as a plain count around the ORM's identity map."""
    return int(
        (
            await repo.session.execute(
                text("SELECT count(*) FROM file_nodes WHERE parent_id = :p"),
                {"p": parent_id},
            )
        ).scalar_one()
    )


async def _parent_of(repo: FilesRepo, node_id: NodeId) -> Any:
    return (
        await repo.session.execute(
            text("SELECT parent_id FROM file_nodes WHERE id = :id"), {"id": node_id}
        )
    ).scalar_one()


# ---- a chat's folder is leased like any other ----------------------------


async def test_a_chat_lease_is_granted_beaten_forced_and_handed_back(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    """The whole life of the lease an awake chat holds on its own folder.

    The purpose is a label rather than a branch, so the proof it is a first
    class holder is that every verb — grant, beat, force, release — behaves
    exactly as it does for a desktop mount, and the row carries ``chat``.
    """
    drive = await files_factory.drive()
    tree = await files_factory.tree("Chats/ Chats/kickoff/", drive=drive)
    folder = NodeId(tree["Chats/kickoff"].id)
    leases = _leases(repo, files_org, clock)

    granted = await leases.acquire(
        folder, instance_id="mirror-1", machine_id="box-7", purpose="chat"
    )
    assert granted.purpose == "chat"
    assert granted.machine_id == "box-7"
    stored = (
        await repo.session.execute(
            text("SELECT purpose FROM file_leases WHERE node_id = :n"), {"n": folder}
        )
    ).scalar_one()
    assert stored == "chat"

    beat = await leases.heartbeat(folder, epoch=granted.epoch, instance_id="mirror-1")
    assert beat.epoch == granted.epoch
    assert beat.purpose == "chat"
    assert beat.forced is False

    forced = await leases.force_release(folder)
    assert forced.forced is True
    assert forced.purpose == "chat"
    still_beating = await leases.heartbeat(folder, epoch=granted.epoch, instance_id="mirror-1")
    assert still_beating.forced is True

    await leases.release(folder, epoch=granted.epoch, instance_id="mirror-1")
    released_at = (
        await repo.session.execute(
            text("SELECT released_at FROM file_leases WHERE node_id = :n"), {"n": folder}
        )
    ).scalar_one()
    assert released_at is not None


@pytest.mark.parametrize(
    ("purpose", "asked", "takes_drops"),
    [
        pytest.param("chat", None, True, id="a-chat-takes-drops-by-what-it-is"),
        pytest.param("mount", None, False, id="a-mount-owns-its-tree"),
        pytest.param("box", None, False, id="a-box-owns-its-tree"),
        pytest.param("share", None, False, id="a-share-owns-its-tree"),
        pytest.param("chat", False, False, id="a-chat-that-said-no"),
        pytest.param("mount", True, True, id="a-mount-that-asked"),
    ],
)
async def test_what_a_lease_does_with_a_write_that_is_not_the_holders_follows_its_purpose(
    repo: FilesRepo,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    clock: FakeClock,
    purpose: str,
    asked: bool | None,
    takes_drops: bool,
) -> None:
    """A chat's folder takes a drop; every other purpose refuses one.

    The default is the purpose because that is the thing a caller is least
    likely to get right: the box holds the folder precisely so a person can put
    a file into the conversation while the agent runs, and a desktop mount owns
    its tree outright and must not have someone else's write appear underneath
    it. An explicit answer still wins over the default in both directions, so a
    caller is never stuck with what its purpose happens to mean.
    """
    drive = await files_factory.drive()
    tree = await files_factory.tree("Chats/ Chats/kickoff/", drive=drive)
    folder = NodeId(tree["Chats/kickoff"].id)

    granted = await _leases(repo, files_org, clock).acquire(
        folder,
        instance_id="mirror-1",
        machine_id="box-7",
        purpose=purpose,
        **({} if asked is None else {"accepts_inbound": asked}),
    )

    assert granted.accepts_inbound is takes_drops
    stored = (
        await repo.session.execute(
            text("SELECT accepts_inbound FROM file_leases WHERE node_id = :n"), {"n": folder}
        )
    ).scalar_one()
    assert stored is takes_drops


@pytest.mark.parametrize(
    "other",
    [pytest.param(name, id=f"chat-blocks-{name}") for name in ("mount", "box", "share", "chat")],
)
async def test_one_live_lease_per_node_holds_across_purposes(
    repo: FilesRepo,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    clock: FakeClock,
    other: str,
) -> None:
    """A second box cannot take a chat folder a first box is holding.

    The invariant is per node, not per purpose: whatever label the second
    acquirer gives itself, the folder is already someone's.
    """
    drive = await files_factory.drive()
    tree = await files_factory.tree("Chats/ Chats/kickoff/", drive=drive)
    folder = NodeId(tree["Chats/kickoff"].id)
    first = await _leases(repo, files_org, clock).acquire(
        folder, instance_id="mirror-1", machine_id="box-7", purpose="chat"
    )

    with pytest.raises(errors.Conflict) as refused:
        await _leases(repo, files_org, clock).acquire(
            folder, instance_id="mirror-2", machine_id="box-8", purpose=other
        )
    assert refused.value.code == "files.leased"

    # And the first holder is untouched by the refusal.
    kept = await _leases(repo, files_org, clock).heartbeat(
        folder, epoch=first.epoch, instance_id="mirror-1"
    )
    assert kept.epoch == first.epoch


async def test_a_chat_lease_is_regrantable_to_the_next_box_once_released(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    """Resume on a different box: the released folder grants at a higher epoch.

    This is what makes a slept chat resumable anywhere rather than pinned to
    the box that last ran it.
    """
    drive = await files_factory.drive()
    tree = await files_factory.tree("Chats/ Chats/kickoff/", drive=drive)
    folder = NodeId(tree["Chats/kickoff"].id)
    leases = _leases(repo, files_org, clock)
    slept = await leases.acquire(folder, instance_id="mirror-1", machine_id="box-7", purpose="chat")
    await leases.release(folder, epoch=slept.epoch, instance_id="mirror-1")

    resumed = await leases.acquire(
        folder, instance_id="mirror-2", machine_id="box-8", purpose="chat"
    )
    assert resumed.epoch > slept.epoch
    assert resumed.machine_id == "box-8"

    # The box that slept cannot write behind the one that woke it.
    with pytest.raises(errors.Conflict) as fenced:
        await leases.heartbeat(folder, epoch=slept.epoch, instance_id="mirror-1")
    assert fenced.value.code == "files.lease_fenced"


# ---- a lapsed lease is not up for grabs the instant it lapses -------------


async def test_a_lease_that_lapsed_a_second_ago_is_not_grantable_to_anyone_else(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    """The grant delay: a holder whose beat was merely slow keeps the folder."""
    drive = await files_factory.drive()
    tree = await files_factory.tree("work/", drive=drive)
    node = NodeId(tree["work"].id)
    await _leases(repo, files_org, clock).acquire(
        node, instance_id="i1", machine_id="laptop", purpose="mount"
    )

    await _expire(repo, node, ago=1.0)

    with pytest.raises(errors.Conflict) as refused:
        await _leases(repo, files_org, clock, uuid.uuid4()).acquire(
            node, instance_id="i2", machine_id="desktop", purpose="mount"
        )
    assert refused.value.code == "files.leased"


async def test_the_same_instance_resumes_a_lease_that_lapsed_inside_the_delay(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    """The delay protects the holder, so it may not lock the holder out."""
    drive = await files_factory.drive()
    tree = await files_factory.tree("work/", drive=drive)
    node = NodeId(tree["work"].id)
    leases = _leases(repo, files_org, clock)
    first = await leases.acquire(node, instance_id="i1", machine_id="laptop", purpose="mount")

    await _expire(repo, node, ago=1.0)

    resumed = await leases.acquire(node, instance_id="i1", machine_id="laptop", purpose="mount")
    # A new epoch, not the old one: everything it had in flight was fenced the
    # moment the lease lapsed, so it may not keep writing under the old number.
    assert resumed.epoch > first.epoch


async def test_a_lease_lapsed_past_the_delay_is_grantable_to_the_next_asker(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    drive = await files_factory.drive()
    tree = await files_factory.tree("work/", drive=drive)
    node = NodeId(tree["work"].id)
    first = await _leases(repo, files_org, clock).acquire(
        node, instance_id="i1", machine_id="laptop", purpose="mount"
    )

    await _expire(repo, node, ago=60.0)

    second = await _leases(repo, files_org, clock, uuid.uuid4()).acquire(
        node, instance_id="i2", machine_id="desktop", purpose="mount"
    )
    assert second.epoch > first.epoch


# ---- "leased" on a listing means a lease that is live right now -----------


@pytest.mark.parametrize(
    ("ago", "still_leased"),
    [
        pytest.param(None, True, id="live"),
        pytest.param(1.0, False, id="lapsed-inside-the-grant-delay"),
        pytest.param(60.0, False, id="lapsed-past-the-grant-delay"),
    ],
)
async def test_the_leased_filter_follows_the_ttl_not_the_reaper(
    repo: FilesRepo,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    clock: FakeClock,
    ago: float | None,
    still_leased: bool,
) -> None:
    """An expired lease nobody has reaped yet is not a leased folder.

    ``reaped_at`` is a sweep, and a sweep runs late; the TTL is what actually
    fences the holder's writes. Between the two the filter must already say
    the folder is free, or a person is told it is mounted when it is not.
    """
    from alkera_core.files.filters import ListFilters
    from alkera_core.models.files.tree import FileNode
    from sqlalchemy import select

    drive = await files_factory.drive()
    tree = await files_factory.tree("work/", drive=drive)
    node = NodeId(tree["work"].id)
    await _leases(repo, files_org, clock).acquire(
        node, instance_id="i1", machine_id="laptop", purpose="mount"
    )
    if ago is not None:
        await _expire(repo, node, ago=ago)

    async with repo.transaction():
        found = (
            await repo.session.execute(
                select(FileNode.id).where(
                    FileNode.id == node,
                    *ListFilters(leased=True).predicates(FileNode),
                )
            )
        ).all()
    assert [row.id for row in found] == ([node] if still_leased else [])


# ---- a beat that arrives late ---------------------------------------------


async def test_a_beat_after_the_lease_lapsed_re_grants_it_to_the_same_holder(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    """A box that could not reach the API for longer than the TTL is still the
    only writer the folder has ever had, so its next beat takes the lease back
    rather than fencing the turn it is in the middle of."""
    drive = await files_factory.drive()
    tree = await files_factory.tree("work/", drive=drive)
    node = NodeId(tree["work"].id)
    leases = _leases(repo, files_org, clock)
    first = await leases.acquire(node, instance_id="i1", machine_id="laptop", purpose="mount")

    await _lapse(repo, node)

    beat = await leases.heartbeat(node, epoch=first.epoch, instance_id="i1")
    # The same lease, alive again for a full TTL: nothing was fenced, so the
    # writes the holder has in flight are still signed with a live epoch.
    assert beat.epoch == first.epoch
    assert beat.expires_at - beat.heartbeat_at == lease_ttl()
    assert beat.expires_at > first.expires_at
    assert not beat.forced

    # And the folder is held again, not merely un-fenced.
    with pytest.raises(errors.Conflict) as refused:
        await _leases(repo, files_org, clock, uuid.uuid4()).acquire(
            node, instance_id="i2", machine_id="desktop", purpose="mount"
        )
    assert refused.value.code == "files.leased"


async def test_a_beat_after_another_holder_took_the_lapsed_folder_is_fenced(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    """The hand-over is what the fence is for: once somebody else holds the
    folder, the lapsed holder's beat is refused however alive it is."""
    drive = await files_factory.drive()
    tree = await files_factory.tree("work/", drive=drive)
    node = NodeId(tree["work"].id)
    leases = _leases(repo, files_org, clock)
    first = await leases.acquire(node, instance_id="i1", machine_id="laptop", purpose="mount")

    await _lapse(repo, node)
    second = await _leases(repo, files_org, clock, uuid.uuid4()).acquire(
        node, instance_id="i2", machine_id="desktop", purpose="mount"
    )
    assert second.epoch > first.epoch

    with pytest.raises(errors.Conflict) as fenced:
        await leases.heartbeat(node, epoch=first.epoch, instance_id="i1")
    assert fenced.value.code == "files.lease_fenced"


async def test_a_beat_on_a_lease_the_reaper_took_apart_is_fenced(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    """The reaper aborts the holder's sessions and retracts what it said was in
    flight, so that lease is not something to extend: the holder comes back
    through ``acquire``, which mints the fresh epoch that teardown deserves."""
    drive = await files_factory.drive()
    tree = await files_factory.tree("work/", drive=drive)
    node = NodeId(tree["work"].id)
    leases = _leases(repo, files_org, clock)
    first = await leases.acquire(node, instance_id="i1", machine_id="laptop", purpose="mount")

    await _lapse(repo, node)
    async with repo.transaction():
        await repo.session.execute(
            text("UPDATE file_leases SET reaped_at = now() WHERE node_id = :n"), {"n": node}
        )

    with pytest.raises(errors.Conflict) as fenced:
        await leases.heartbeat(node, epoch=first.epoch, instance_id="i1")
    assert fenced.value.code == "files.lease_fenced"

    resumed = await leases.acquire(node, instance_id="i1", machine_id="laptop", purpose="mount")
    assert resumed.epoch > first.epoch


@pytest.mark.parametrize("seconds", [120, 900])
async def test_the_configured_ttl_is_what_a_grant_and_a_beat_live_by(
    repo: FilesRepo,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    seconds: int,
) -> None:
    """``files_lease_ttl_seconds`` is the deadline, not a literal in here: a
    deployment that lengthens the lease lengthens what the statement writes."""
    monkeypatch.setattr(settings, "files_lease_ttl_seconds", seconds)
    drive = await files_factory.drive()
    tree = await files_factory.tree("work/", drive=drive)
    node = NodeId(tree["work"].id)
    leases = _leases(repo, files_org, clock)

    granted = await leases.acquire(node, instance_id="i1", machine_id="laptop", purpose="mount")
    assert granted.expires_at - granted.acquired_at == timedelta(seconds=seconds)

    beat = await leases.heartbeat(node, epoch=granted.epoch, instance_id="i1")
    assert beat.expires_at - beat.heartbeat_at == timedelta(seconds=seconds)


@pytest.mark.parametrize(
    ("mine", "theirs"),
    [
        pytest.param("work", "work/sub", id="a-neighbour-took-a-folder-under-mine"),
        pytest.param("work/sub", "work", id="a-neighbour-took-the-folder-above-mine"),
    ],
)
async def test_a_late_beat_is_fenced_when_a_neighbour_took_an_overlapping_folder(
    repo: FilesRepo,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    clock: FakeClock,
    mine: str,
    theirs: str,
) -> None:
    """A lapse is a window, and a neighbour may legitimately take an ancestor or
    a descendant inside it. Re-granting on the strength of the row alone would
    then put two holders on the same bytes — the one thing the grant rule exists
    to forbid — so a late beat asks the same overlap question acquire asks."""
    drive = await files_factory.drive()
    tree = await files_factory.tree("work/ work/sub/", drive=drive)
    held = NodeId(tree[mine].id)
    leases = _leases(repo, files_org, clock)
    first = await leases.acquire(held, instance_id="i1", machine_id="laptop", purpose="mount")

    await _lapse(repo, held)
    neighbour = await _leases(repo, files_org, clock, uuid.uuid4()).acquire(
        NodeId(tree[theirs].id), instance_id="i2", machine_id="desktop", purpose="mount"
    )
    assert neighbour.node_id == tree[theirs].id

    with pytest.raises(errors.Conflict) as fenced:
        await leases.heartbeat(held, epoch=first.epoch, instance_id="i1")
    assert fenced.value.code == "files.lease_fenced"

    # The refusal is the whole statement: no deadline was pushed forward either.
    lapsed = (
        await repo.session.execute(
            text("SELECT expires_at <= now() AS lapsed FROM file_leases WHERE node_id = :n"),
            {"n": held},
        )
    ).scalar_one()
    assert lapsed


async def test_a_late_beat_survives_a_nested_lease_of_the_holders_own(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    """The negative twin: a second machine of my own under my mount is the
    nesting the grant rule allows, so it must not fence my own late beat."""
    drive = await files_factory.drive()
    tree = await files_factory.tree("work/ work/sub/", drive=drive)
    node = NodeId(tree["work"].id)
    leases = _leases(repo, files_org, clock)
    first = await leases.acquire(node, instance_id="laptop-1", machine_id="laptop", purpose="mount")
    await leases.acquire(
        NodeId(tree["work/sub"].id), instance_id="box-1", machine_id="box", purpose="box"
    )

    await _lapse(repo, node)

    beat = await leases.heartbeat(node, epoch=first.epoch, instance_id="laptop-1")
    assert beat.epoch == first.epoch
    assert beat.expires_at > first.expires_at


async def test_a_reaped_lease_is_dead_to_every_statement_even_with_time_left(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    """The reaper aborts the holder's sessions and retracts what it said was in
    flight. A beat that pushes the deadline forward in the same instant leaves a
    row with time left on it — and that row must not go on admitting the writes
    the teardown just undid, nor keep the folder off the next holder."""
    drive = await files_factory.drive()
    tree = await files_factory.tree("work/ work/sub/", drive=drive)
    node = NodeId(tree["work"].id)
    leases = _leases(repo, files_org, clock)
    granted = await leases.acquire(node, instance_id="i1", machine_id="laptop", purpose="mount")

    async with repo.transaction():
        await repo.session.execute(
            text("UPDATE file_leases SET reaped_at = now() WHERE node_id = :n"), {"n": node}
        )
    live = (
        await repo.session.execute(
            text("SELECT expires_at > now() AS live FROM file_leases WHERE node_id = :n"),
            {"n": node},
        )
    ).scalar_one()
    assert live, "the case is the reaped row that still has time on it"

    holder = _held(files_org, granted.epoch).holder
    async with repo.transaction():
        with pytest.raises(errors.Conflict) as fenced:
            await assert_lease_epoch(repo, node, granted.epoch, "i1", holder=holder)
        assert fenced.value.code == "files.lease_fenced"

    async with repo.transaction():
        assert (
            await leases_held_by(
                repo.session,
                org_team_id=files_org.org_team_id,
                epoch=granted.epoch,
                instance_id="i1",
                holder=holder,
            )
            == frozenset()
        )
        under = await repo.node(NodeId(tree["work/sub"].id))
        assert under is not None
        assert await covering_lease(repo, under) is None

    # The folder is free again, and the old holder cannot hand back what the
    # reaper already took from it.
    nested = await _leases(repo, files_org, clock, uuid.uuid4()).acquire(
        NodeId(tree["work/sub"].id), instance_id="i2", machine_id="desktop", purpose="mount"
    )
    assert nested.node_id == tree["work/sub"].id
    with pytest.raises(errors.Conflict) as handed_back:
        await leases.release(node, epoch=granted.epoch, instance_id="i1")
    assert handed_back.value.code == "files.lease_fenced"


async def test_a_reaped_folder_is_grantable_to_a_different_holder_with_time_left(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    """The leased FOLDER itself, not only what is under it.

    Every read predicate calls a reaped row dead: the overlap check skips it,
    the covering read returns nothing, the epoch fence refuses its own holder.
    The grant statement did not agree — its only ways past a standing row were
    released, lapsed-past-the-delay, or the same instance resuming — so a folder
    whose teardown had already retracted its live entries, aborted its sessions
    and released its quota holds stayed ungrantable to everyone but the dead
    instance until the wall clock caught up with a deadline nothing was honouring
    any more. The next holder gets it now, at a strictly higher epoch, and the
    old one is fenced.
    """
    drive = await files_factory.drive()
    tree = await files_factory.tree("work/", drive=drive)
    node = NodeId(tree["work"].id)
    leases = _leases(repo, files_org, clock)
    granted = await leases.acquire(node, instance_id="i1", machine_id="laptop", purpose="mount")

    async with repo.transaction():
        await repo.session.execute(
            text("UPDATE file_leases SET reaped_at = now() WHERE node_id = :n"), {"n": node}
        )
    live = (
        await repo.session.execute(
            text("SELECT expires_at > now() AS live FROM file_leases WHERE node_id = :n"),
            {"n": node},
        )
    ).scalar_one()
    assert live, "the case is the reaped row that still has time on it"

    taken = await _leases(repo, files_org, clock, uuid.uuid4()).acquire(
        node, instance_id="i2", machine_id="desktop", purpose="mount"
    )
    assert taken.epoch > granted.epoch
    holder = _held(files_org, granted.epoch).holder
    async with repo.transaction():
        with pytest.raises(errors.Conflict) as fenced:
            await assert_lease_epoch(repo, node, granted.epoch, "i1", holder=holder)
        assert fenced.value.code == "files.lease_fenced"
