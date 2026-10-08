"""The lease-purpose seam: a new holder kind fences exactly like ``mount``.

Future callers each purpose stands for:

* ``box`` — a RunPod / demo box that mounts an org folder for an agent to work
  in, held by the box's daemon rather than a desktop;
* ``share`` — a share-link session that takes a folder for the life of an
  external collaborator's edit;
* ``chat`` — the box that runs an awake web chat, holding that chat's folder
  until the chat sleeps;
* ``workspace`` — the box that runs a workspace's chats, holding the
  workspace's whole folder until its last chat sleeps.

``purpose`` is a label on the row, never a branch in the fence. The claim this
file makes is exactly that: the whole three-case write contract — no epoch is
refused, the holder's epoch writes, a superseded epoch is fenced — is driven
once per purpose through the real ``LeaseService`` and the real ``Namespace``,
so a fourth purpose is a new literal and no new code.
"""

from __future__ import annotations

from typing import Any, get_args

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.files import errors
from alkera_core.files.clock import FakeClock
from alkera_core.files.ids import NodeId
from alkera_core.files.leases import LeasePurpose, LeaseService
from alkera_core.files.namespace import Namespace
from alkera_core.files.repo import FilesRepo
from sqlalchemy import text
from tests.files._kit.factory import FilesFactory, FilesOrg
from tests.files._kit.fencing import held_by

pytestmark = pytest.mark.asyncio

#: Every purpose the type admits. Read back off the literal at import so a
#: fourth purpose added to ``LeasePurpose`` and not to the fence lands here as
#: a collection error rather than as a silently uncovered hole.
PURPOSES: tuple[LeasePurpose, ...] = ("mount", "box", "share", "chat", "workspace")
assert set(get_args(LeasePurpose)) == set(PURPOSES), "a purpose is not driven through the fence"


def _ctx(org: FilesOrg) -> ActingContext:
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER,
            id=str(org.admin_id),
            org_id=org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )


class _PerCallTransaction:
    """Every service call in its own Files transaction, as a route makes it."""

    def __init__(self, repo: FilesRepo, service: Any) -> None:
        self._repo = repo
        self._service = service

    def __getattr__(self, name: str) -> Any:
        async def call(*args: Any, **kwargs: Any) -> Any:
            async with self._repo.transaction():
                return await getattr(self._service, name)(*args, **kwargs)

        return call


def _leases(repo: FilesRepo, org: FilesOrg, clock: FakeClock) -> Any:
    return _PerCallTransaction(repo, LeaseService(repo, _ctx(org), clock))


def _namespace(repo: FilesRepo, org: FilesOrg, clock: FakeClock) -> Any:
    return _PerCallTransaction(repo, Namespace(repo, _ctx(org), clock, None))


async def _name_of(repo: FilesRepo, node_id: NodeId) -> bytes:
    """The stored name as a plain column: a refused call expired the instance."""
    async with repo.transaction():
        row = (
            await repo.session.execute(
                text("SELECT name FROM file_nodes WHERE id = :id"), {"id": str(node_id)}
            )
        ).one()
    return bytes(row.name)


@pytest.mark.parametrize("purpose", [pytest.param(name, id=name) for name in PURPOSES])
@pytest.mark.parametrize(
    ("carry", "expected"),
    [
        pytest.param("none", "files.leased", id="no-epoch-is-someone-elses-folder"),
        pytest.param("holder", None, id="the-holders-epoch-writes"),
        pytest.param("superseded", "files.lease_fenced", id="a-stale-epoch-is-fenced"),
    ],
)
async def test_every_purpose_fences_a_write_the_same_way(
    repo: FilesRepo,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    clock: FakeClock,
    purpose: LeasePurpose,
    carry: str,
    expected: str | None,
) -> None:
    """The three-case write contract holds under ``box`` and ``share`` too."""
    drive = await files_factory.drive()
    tree = await files_factory.tree("work/ work/a.txt", drive=drive)
    folder = NodeId(tree["work"].id)
    target_id = NodeId(tree["work/a.txt"].id)
    target_etag = tree["work/a.txt"].etag
    held = await _leases(repo, files_org, clock).acquire(
        folder, instance_id="i1", machine_id="host", purpose=purpose
    )
    assert held.purpose == purpose

    if carry == "none":
        lease = None
    elif carry == "holder":
        lease = held_by(_ctx(files_org), held.epoch, "i1")
    else:
        lease = held_by(_ctx(files_org), held.epoch - 1, "i1")

    namespace = _namespace(repo, files_org, clock)
    if expected is None:
        renamed = await namespace.rename(target_id, b"b.txt", if_match=target_etag, lease=lease)
        assert renamed.name == b"b.txt"
        return

    with pytest.raises(errors.Conflict) as refused:
        await namespace.rename(target_id, b"b.txt", if_match=target_etag, lease=lease)
    assert refused.value.code == expected
    assert await _name_of(repo, target_id) == b"a.txt"


@pytest.mark.parametrize(
    ("held_as", "wanted_as"),
    [
        pytest.param(held, wanted, id=f"{held}-blocks-{wanted}")
        for held in PURPOSES
        for wanted in PURPOSES
        if held != wanted
    ],
)
async def test_a_purpose_never_buys_a_second_lease_on_one_folder(
    repo: FilesRepo,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    clock: FakeClock,
    held_as: LeasePurpose,
    wanted_as: LeasePurpose,
) -> None:
    """One writer per folder is the invariant, and the purpose cannot buy out of it.

    Every ordered pair of distinct purposes: a box asking for a folder a
    desktop has mounted is refused, and so is the reverse. A purpose that took
    a second lease would give a folder two writers, which is the one thing the
    lease exists to prevent.
    """
    drive = await files_factory.drive()
    tree = await files_factory.tree("work/ work/a.txt", drive=drive)
    folder = NodeId(tree["work"].id)
    leases = _leases(repo, files_org, clock)
    first = await leases.acquire(folder, instance_id="i1", machine_id="host-a", purpose=held_as)

    with pytest.raises(errors.Conflict) as refused:
        await leases.acquire(folder, instance_id="i2", machine_id="host-b", purpose=wanted_as)
    assert refused.value.code == "files.leased"

    # The refusal left the first holder's grant exactly as it was — the loser
    # did not silently re-purpose the row it lost, and the winner's own
    # heartbeat still lands under its original epoch and purpose.
    still = await leases.heartbeat(folder, epoch=first.epoch, instance_id="i1")
    assert (still.purpose, still.epoch, still.holder_instance_id) == (held_as, first.epoch, "i1")


@pytest.mark.parametrize("purpose", [pytest.param(name, id=name) for name in PURPOSES])
async def test_a_reacquire_after_release_carries_the_new_purpose(
    repo: FilesRepo,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    clock: FakeClock,
    purpose: LeasePurpose,
) -> None:
    """A desktop hands a folder back and a box takes it, with a fresh epoch.

    The old epoch is fenced afterwards under every purpose, so handing a folder
    from one holder kind to another cannot leave the previous holder able to
    write through a stale context.
    """
    drive = await files_factory.drive()
    tree = await files_factory.tree("work/ work/a.txt", drive=drive)
    folder = NodeId(tree["work"].id)
    target_id = NodeId(tree["work/a.txt"].id)
    leases = _leases(repo, files_org, clock)
    first = await leases.acquire(folder, instance_id="i1", machine_id="host-a", purpose="mount")
    await leases.release(folder, instance_id="i1", epoch=first.epoch)

    second = await leases.acquire(folder, instance_id="i2", machine_id="host-b", purpose=purpose)
    assert second.purpose == purpose
    assert second.epoch > first.epoch

    namespace = _namespace(repo, files_org, clock)
    with pytest.raises(errors.Conflict) as refused:
        await namespace.rename(
            target_id,
            b"b.txt",
            if_match=tree["work/a.txt"].etag,
            lease=held_by(_ctx(files_org), first.epoch, "i1"),
        )
    assert refused.value.code == "files.lease_fenced"
    assert await _name_of(repo, target_id) == b"a.txt"


async def test_every_spelling_of_the_purpose_vocabulary_agrees() -> None:
    """The literal, the wire schema and the CHECK say the same four words.

    ``purpose`` is spelled in three places that are compiled apart — the
    service's ``Literal``, the response schema's, and the ORM enum the column
    CHECK is rendered from. A word added to one and not the others is a route
    that 422s a purpose the service would have accepted, which is exactly how
    ``chat`` first landed; this is the test that catches the next one.
    """
    from alkera_core.models.files.leases import LEASE_PURPOSES
    from alkera_core.schemas.files.lease import LeasePurpose as WireLeasePurpose

    assert set(get_args(LeasePurpose)) == set(LEASE_PURPOSES)
    assert set(get_args(LeasePurpose)) == set(get_args(WireLeasePurpose))
