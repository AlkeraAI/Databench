"""The byte ceiling in safety mode, pinned on both sides at once.

``QuotaModel`` is the contract the stateful machine holds ``QuotaService`` to,
so a rule the model never learned is a rule the machine cannot catch — it can
only mis-report as a service bug. That is what happened here: a reserve which
adds no bytes but does add nodes, on a drive whose bytes sit exactly on the
ceiling, is refused (safety mode refuses anything that *adds* once usage is at
the limit, not only past it), and the model said it fit. The machine surfaced it
only when Hypothesis happened to draw reserves summing to exactly the quota.

These pins hit that corner every run, and they run each step through BOTH sides:
the same reserve goes to the model and to the real service on real Postgres, and
both must come back with the same verdict. A model that drifts from the
documented rule fails here deterministically instead of once every few hundred
random draws.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta

import pytest
from alkera_core.authz.principal import ActingContext, Principal, PrincipalKind
from alkera_core.files.clock import FakeClock
from alkera_core.files.errors import QuotaExceeded
from alkera_core.files.ids import DriveId, SessionId
from alkera_core.files.quota import QuotaService
from alkera_core.files.repo import FilesRepo
from alkera_core.models.files.history import FileDirStats
from alkera_core.models.files.stores import FileDrive
from alkera_core.models.files.uploads import FileUploadSession
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files._kit.factory import FilesFactory, FilesOrg
from tests.files.stateful._models import ModelRefusal, QuotaModel
from tests.files.stateful.conftest import refusal_code

QUOTA_BYTES = 1_000
QUOTA_NODES = 20
TTL = timedelta(minutes=30)


@dataclass(frozen=True, slots=True)
class Reserve:
    """One reserve and the verdict the contract owes it."""

    size: int
    nodes: int
    #: The refusal code, or ``None`` when the reserve must be accepted.
    refusal: str | None


@dataclass(frozen=True, slots=True)
class BoundaryRig:
    """A drive seeded to a known usage, its model twin, and the service."""

    repo: FilesRepo
    quota: QuotaService
    model: QuotaModel
    drive: FileDrive
    clock: FakeClock

    @property
    def drive_id(self) -> DriveId:
        return DriveId(self.drive.id)


async def _build(
    files_session: AsyncSession,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    *,
    used_bytes: int,
    used_nodes: int,
) -> BoundaryRig:
    """A drive whose committed usage is exactly ``used_bytes`` / ``used_nodes``."""
    drive = await files_factory.drive()
    await files_session.execute(
        update(FileDrive.__table__)
        .where(FileDrive.id == drive.id)
        .values(quota_bytes=QUOTA_BYTES, quota_nodes=QUOTA_NODES)
    )
    assert drive.root_node_id is not None
    seeded = await files_session.execute(
        update(FileDirStats.__table__)
        .where(FileDirStats.node_id == drive.root_node_id)
        .values(bytes=used_bytes, files=used_nodes)
    )
    if seeded.rowcount == 0:
        files_session.add(
            FileDirStats(
                node_id=drive.root_node_id,
                org_team_id=drive.org_team_id,
                bytes=used_bytes,
                files=used_nodes,
                direct_children=0,
            )
        )
    await files_session.commit()
    repo = FilesRepo(files_session, files_org.scope)
    context = ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER, id=str(uuid.uuid4()), org_id=files_org.org_team_id
        )
    )
    return BoundaryRig(
        repo=repo,
        quota=QuotaService(repo, context, clock, None),
        model=QuotaModel(
            quota_bytes=QUOTA_BYTES,
            quota_nodes=QUOTA_NODES,
            used_bytes=used_bytes,
            used_nodes=used_nodes,
        ),
        drive=drive,
        clock=clock,
    )


async def _service_reserve(rig: BoundaryRig, *, size: int, nodes: int) -> str | None:
    """Reserve through the real service; ``None`` when it was accepted.

    An accepted reserve leaves a live session holding the room, so the next step
    in a sequence sees it exactly as the model's ``holds`` does; a refused one
    leaves nothing behind.
    """
    session_id = uuid.uuid4()
    async with rig.repo.transaction():
        row = FileUploadSession(
            id=session_id,
            org_team_id=rig.drive.org_team_id,
            drive_id=rig.drive.id,
            parent_id=rig.drive.root_node_id,
            name=b"payload.bin",
            dedup_domain_id=rig.drive.dedup_domain_id,
            state="open",
            declared_size=size,
            expires_at=rig.clock.now() + TTL,
        )
        await rig.repo.add(row)
        await rig.repo.flush()
        try:
            await rig.quota.reserve(
                rig.drive_id,
                bytes=size,
                nodes=nodes,
                session_id=SessionId(session_id),
            )
        except QuotaExceeded as refused:
            await rig.repo.session.execute(
                update(FileUploadSession.__table__)
                .where(FileUploadSession.id == session_id)
                .values(state="aborted")
            )
            return refusal_code(refused)
        await rig.repo.session.execute(
            update(FileUploadSession.__table__)
            .where(FileUploadSession.id == session_id)
            .values(quota_hold_bytes=size, quota_hold_nodes=nodes)
        )
        return None


def _model_reserve(rig: BoundaryRig, handle: str, *, size: int, nodes: int) -> str | None:
    try:
        rig.model.reserve(handle, size=size, nodes=nodes)
    except ModelRefusal as refused:
        return refused.code
    return None


async def _walk(rig: BoundaryRig, steps: tuple[Reserve, ...]) -> None:
    """Apply every step to both sides and hold each to the contract's verdict."""
    for index, step in enumerate(steps):
        # Read before either side is mutated: the machine asks ``fits`` first
        # and only then applies the step, so the two must agree on the SAME
        # state or the machine asserts against a prediction nothing made.
        predicted = rig.model.fits(size=step.size, nodes=step.nodes)
        service = await _service_reserve(rig, size=step.size, nodes=step.nodes)
        model = _model_reserve(rig, f"s{index}", size=step.size, nodes=step.nodes)
        assert service == step.refusal, (
            f"step {index} ({step.size}B/{step.nodes} nodes): the service answered "
            f"{service}; the contract says {step.refusal}"
        )
        assert model == step.refusal, (
            f"step {index} ({step.size}B/{step.nodes} nodes): the reference model answered "
            f"{model}; the contract says {step.refusal}"
        )
        assert predicted is (step.refusal is None), (
            f"step {index}: ``fits`` said {predicted}; the contract says {step.refusal is None}"
        )


@pytest.mark.parametrize(
    ("used_bytes", "used_nodes", "steps"),
    [
        pytest.param(
            QUOTA_BYTES,
            0,
            (Reserve(size=0, nodes=3, refusal="files.quota_bytes"),),
            id="at-the-byte-ceiling-a-nodes-only-reserve-is-refused-for-bytes",
        ),
        pytest.param(
            QUOTA_BYTES,
            0,
            (Reserve(size=0, nodes=0, refusal=None),),
            id="at-the-byte-ceiling-a-reserve-that-adds-nothing-is-accepted",
        ),
        pytest.param(
            QUOTA_BYTES - 1,
            0,
            (
                Reserve(size=1, nodes=0, refusal=None),
                Reserve(size=0, nodes=1, refusal="files.quota_bytes"),
            ),
            id="the-last-free-byte-is-taken-and-then-a-node-cannot-be-added",
        ),
        pytest.param(
            0,
            QUOTA_NODES,
            (Reserve(size=1, nodes=0, refusal=None),),
            id="at-the-node-ceiling-a-bytes-only-reserve-is-accepted",
        ),
        pytest.param(
            0,
            0,
            (
                *(
                    Reserve(size=size, nodes=0, refusal=None)
                    for size in (344, 36, 97, 249, 0, 38, 200, 0, 0, 0, 36)
                ),
                Reserve(size=0, nodes=3, refusal="files.quota_bytes"),
            ),
            id="eleven-holds-summing-to-the-quota-then-a-nodes-only-reserve",
        ),
    ],
)
async def test_safety_mode_at_a_ceiling_is_the_same_on_both_sides(
    files_session: AsyncSession,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    used_bytes: int,
    used_nodes: int,
    steps: tuple[Reserve, ...],
) -> None:
    """A reserve that adds anything is refused AT the byte ceiling, not past it.

    The node ceiling has no such rule — it refuses only what lands past it — so
    the last case is the asymmetry that stops a model (or a service) from
    "fixing" one axis by copying the other.
    """
    rig = await _build(
        files_session,
        files_factory,
        files_org,
        clock,
        used_bytes=used_bytes,
        used_nodes=used_nodes,
    )
    await _walk(rig, steps)
