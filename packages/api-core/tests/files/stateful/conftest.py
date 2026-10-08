"""The rig the reference-model state machines drive.

Hypothesis' ``RuleBasedStateMachine`` rules are synchronous, and every Files
service is asynchronous against real Postgres, so each machine drives its work
through an event loop and holds one connection for the whole run. This module
builds that rig — a fresh tenant and a fresh drive per run, so one machine's
tree can never be another's — and prints the reproduction line on failure.

The loop and the engine behind that connection are the process's, not the
example's: see ``HypothesisRunner`` in ``packages/api-core/tests/files/_kit/engine.py`` for why an
example that built its own could not reuse either.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from typing import TypeVar

import pytest
from alkera_core.authz.principal import ActingContext, Principal, PrincipalKind
from alkera_core.config import settings
from alkera_core.files.clock import FakeClock
from alkera_core.files.ids import DriveId, OrgScope
from alkera_core.files.repo import FilesRepo
from alkera_core.models.files.stores import FileDrive
from hypothesis import settings as hypothesis_settings
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files._kit.engine import (
    HypothesisRunner,
    close_hypothesis_runner,
    hypothesis_runner,
)
from tests.files._kit.factory import EPOCH, FilesFactory, FilesOrg

T = TypeVar("T")

#: The nightly budget for the reference-model machines. The committed
#: per-machine budgets are sized for a gate that runs on every push; the ACL
#: machine's own harness bug needed 150 examples of 12 steps to surface, which
#: is a nightly cost, not a per-PR one. Selected with
#: ``--hypothesis-profile wide``; the default profile is untouched.
WIDE_PROFILE = "wide"
hypothesis_settings.register_profile(WIDE_PROFILE, max_examples=150, stateful_step_count=12)


def widen_to_profile(committed: hypothesis_settings) -> hypothesis_settings:
    """The budget a machine runs at once the active profile is taken into account.

    Each machine pins its own budget on its ``TestCase``, and an explicitly set
    Hypothesis setting always beats the active profile — so
    ``--hypothesis-profile wide`` on its own would change nothing at all. Only
    the nightly profile widens anything, and it is a floor rather than a
    ceiling: a machine already exploring more examples or longer runs than the
    nightly budget asks for keeps its own, larger, number, and under every other
    profile the committed budget stands exactly as written.
    """
    nightly = hypothesis_settings.get_profile(WIDE_PROFILE)
    if hypothesis_settings.default is not nightly:
        return committed
    return hypothesis_settings(
        committed,
        max_examples=max(committed.max_examples, nightly.max_examples),
        stateful_step_count=max(committed.stateful_step_count, nightly.stateful_step_count),
    )


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Apply the active profile's budget as a floor to every machine collected here."""
    for item in items:
        machine = getattr(item, "cls", None)
        committed = getattr(machine, "settings", None)
        if machine is None or not isinstance(committed, hypothesis_settings):
            continue
        machine.settings = widen_to_profile(committed)


def acting_context() -> ActingContext:
    """A user principal; the namespace and quota decisions carry it, not consult it."""
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER,
            id=str(uuid.uuid4()),
            org_id=uuid.uuid4(),
        )
    )


@dataclass(slots=True)
class StatefulRig:
    """One connection, one tenant, one drive, on the shared loop."""

    loop: asyncio.AbstractEventLoop
    session: AsyncSession
    org: FilesOrg
    repo: FilesRepo
    clock: FakeClock
    #: Plain values, not ORM attributes: a rolled-back transaction expires the
    #: instance, and re-loading an expired attribute from inside a coroutine is
    #: the ``MissingGreenlet`` that async SQLAlchemy refuses.
    drive_uuid: uuid.UUID
    root_uuid: uuid.UUID
    dedup_domain_uuid: uuid.UUID

    @property
    def drive_id(self) -> DriveId:
        return DriveId(self.drive_uuid)

    @property
    def root_id(self) -> uuid.UUID:
        return self.root_uuid

    def run(self, coroutine: Awaitable[T]) -> T:
        """Drive one unit of work, starting from an empty identity map.

        In the product every request and every worker batch opens its own
        session, so nothing it reads was loaded before another unit's writes.
        The rig holds ONE session for the whole run, and the Files mutations
        are ``text()`` statements the mapper never sees: a ``FileNode`` a
        previous step loaded keeps the ``path_ids`` and ``depth`` it had
        before a move rewrote them, and a later plain ``select`` hands that
        instance back rather than the row. Whether such an instance is still
        in the (weak) identity map depends on when the garbage collector last
        ran, so a run read the moved subtree's old chain only sometimes — and
        Hypothesis, replaying a failure that no longer reproduced, reported it
        as inconsistent data generation. Expunging here gives every step what
        a request gets: rows read from the database.
        """
        self.session.expunge_all()
        return self.loop.run_until_complete(coroutine)

    def close(self) -> None:
        """Give this run's connection back to the pool.

        Hypothesis builds a fresh machine per example, so a rig that leaked its
        connection would hold one per example against a lane Postgres several
        lanes share. The loop and the engine outlive the example and are closed
        once, at the end of the session.
        """
        self.loop.run_until_complete(self.session.close())


async def _build(
    runner: HypothesisRunner,
    *,
    label: str,
    quota_bytes: int | None,
    quota_nodes: int | None,
) -> StatefulRig:
    session = AsyncSession(bind=runner.engine, expire_on_commit=False)
    org_id = uuid.uuid4()
    await session.execute(
        text("INSERT INTO teams (id, name, created_at) VALUES (:id, :name, now())"),
        {"id": org_id, "name": f"{label}-{org_id.hex[:8]}"},
    )
    await session.commit()
    org = FilesOrg(org_team_id=org_id, admin_id=uuid.uuid4(), member_id=uuid.uuid4())
    drive = await FilesFactory(session, org).drive()
    if quota_bytes is not None and quota_nodes is not None:
        await session.execute(
            update(FileDrive.__table__)
            .where(FileDrive.id == drive.id)
            .values(quota_bytes=quota_bytes, quota_nodes=quota_nodes)
        )
        await session.commit()
    assert drive.root_node_id is not None
    return StatefulRig(
        loop=runner.loop,
        session=session,
        org=org,
        repo=FilesRepo(session, OrgScope(org_team_id=org_id)),
        clock=FakeClock(now=EPOCH),
        drive_uuid=drive.id,
        root_uuid=drive.root_node_id,
        dedup_domain_uuid=drive.dedup_domain_id,
    )


def open_rig(
    *,
    label: str,
    quota_bytes: int | None = None,
    quota_nodes: int | None = None,
) -> StatefulRig:
    """A fresh tenant and drive for one state-machine run, on the shared loop."""
    runner = hypothesis_runner()
    return runner.run(_build(runner, label=label, quota_bytes=quota_bytes, quota_nodes=quota_nodes))


@pytest.fixture(scope="session", autouse=True)
def _close_the_shared_runner() -> Iterator[None]:
    """Close the loop and engine the machines here share, once, at the end.

    A state machine builds its rig from ``__init__``, where no fixture is in
    reach, so the runner is a process-wide singleton rather than a fixture
    value; this is what gives it a finaliser. Closing it is idempotent, so the
    other suites that share it register the same thing without coordinating.
    """
    yield
    close_hypothesis_runner()


def refusal_code(error: BaseException) -> str:
    """The wire code a Files refusal carries, whatever exception class it is."""
    code = getattr(error, "code", None)
    return code if isinstance(code, str) else type(error).__name__


def print_seed_on_failure(check: Callable[[], None]) -> None:
    """Run ``check``; on failure print the Hypothesis replay line before re-raising.

    Hypothesis prints the falsifying *example* itself; what a reader still needs
    is the database and the derandomization switch, because a stateful failure
    against real Postgres is only replayable with both.
    """
    try:
        check()
    except AssertionError:
        print(
            "replay: HYPOTHESIS_SEED=<seed from the report above> "
            f"DATABASE_URL={settings.database_url} uv run --frozen pytest "
            "packages/api-core/tests/files/stateful -p no:randomly"
        )
        raise
