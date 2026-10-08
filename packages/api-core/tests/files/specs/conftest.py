"""Fixtures for the spec-conformance replays.

TLC is run once per module and its behaviours are parsed there; each test then
replays them against its own freshly-seeded folder, so a trace can never see
rows another trace left behind.
"""

from __future__ import annotations

import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.files.clock import FakeClock
from alkera_core.files.ids import NodeId
from alkera_core.files.repo import FilesRepo
from tests.files._kit.factory import FilesFactory, FilesOrg
from tests.files.specs._trace import (
    REPO_ROOT,
    ReplayServices,
    Trace,
    generate_traces,
)

#: How many behaviours TLC simulates, and how far each one runs.
TRACE_COUNT = 20
TRACE_DEPTH = 40

#: The seed TLC draws its twenty behaviours from. Pinned, not random: without
#: ``-seed`` TLC picks a fresh one per run, so whether the set contained a
#: reap — and therefore whether ``REQUIRED_ACTIONS`` was met — was decided by a
#: coin toss, and the replay failed on ``develop`` with "the simulation never
#: took {'leases_reaped'}" while passing everywhere else. This value is one
#: whose twenty behaviours cover every required action; plenty do not (seed 1
#: takes no reap at all), which is what keeps the floor an assertion rather
#: than a formality. Change it only together with a run proving the new one
#: still covers the floor.
TRACE_SEED = 4


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "files_specs: replays a TLC-generated trace of a Files TLA+ spec through the "
        "implementation. Needs a working java or docker (the two runtimes ops/scripts/tlc.sh "
        "uses); skipped with a reason when neither is present.",
    )


@pytest.fixture(scope="module")
def tlc_traces(tla_tools_jar: Path, tlc_runtime: str) -> Iterator[tuple[Trace, ...]]:
    """The behaviours TLC simulated for ``lease_fencing.tla``, parsed.

    ``tla_tools_jar`` is what makes this module runnable on its own: TLC needs the jar
    installed before the first simulation, not whenever another module gets around to it.
    """
    assert tla_tools_jar.is_file()
    out_dir = REPO_ROOT / ".cache" / "tla" / "traces" / uuid.uuid4().hex
    out_dir.mkdir(parents=True)
    try:
        yield generate_traces(
            count=TRACE_COUNT, depth=TRACE_DEPTH, seed=TRACE_SEED, out_dir=out_dir
        )
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


@pytest.fixture
async def lease_services(
    repo: FilesRepo,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    clock: FakeClock,
) -> ReplayServices:
    """One leased folder in one org, plus the acting context a replay writes as."""
    drive = await files_factory.drive()
    tree = await files_factory.tree("work/", drive=drive)
    ctx = ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER,
            id=str(files_org.admin_id),
            org_id=files_org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )
    return ReplayServices(
        repo=repo,
        ctx=ctx,
        clock=clock,
        node_id=NodeId(tree["work"].id),
        org_team_id=files_org.org_team_id,
    )
