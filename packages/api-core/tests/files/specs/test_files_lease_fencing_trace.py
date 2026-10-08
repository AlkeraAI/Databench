"""``lease_fencing.tla`` traces, replayed through the real leases.

The spec is model-checked in CI; that proves the protocol. These tests prove the
implementation *is* that protocol: TLC simulates behaviours, and every action it
took is driven through ``LeaseService``, the reaper and a fenced rename against
the lane database, with the spec's three invariants — one live holder, epochs
that never regress, no write landing behind a newer epoch — asserted on the real
rows after every step.

Two negatives make the positive mean something: a hand-written behaviour the
spec forbids (a write at a superseded epoch, after a newer write) must be
refused by the fence, and the same behaviour against an implementation whose
fence has been removed must make the replay FAIL. Without the second, a replay
that checked nothing would pass just as green.
"""

from __future__ import annotations

import sys

import pytest
from alkera_core.files import leases
from alkera_core.files.ids import NodeId
from sqlalchemy import text
from tests.files.specs._trace import (
    ReplayServices,
    Step,
    Trace,
    hand_written,
    replay,
)

# `ops/scripts/tlc.sh` and `ops/scripts/ensure-tla-tools.sh` are bash scripts:
# handing one to `subprocess.run` on Windows is `[WinError 193] %1 is not a
# valid Win32 application`, before any spec is read. The specs themselves are
# model-checked by the dedicated `files-specs (TLA+)` pr-gate job on Linux, so
# nothing goes unproven by skipping the replay here.
# One xdist worker for this module: the directory's conftest runs TLC once per
# module, and splitting the module per test would run it once per worker instead.
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.files_specs,
    pytest.mark.skipif(
        sys.platform == "win32",
        reason=(
            "the TLC runner is a bash script; the specs are model-checked "
            "by the files-specs job on Linux"
        ),
    ),
    pytest.mark.xdist_group("files_lease_fencing_trace"),
]

#: The actions a set of simulated traces must contain before it proves anything:
#: a grant, a beat, a lapse and a fenced write. A simulation that never got past
#: acquiring would replay green while exercising almost nothing.
REQUIRED_ACTIONS = frozenset(
    {"leases_acquired", "leases_heartbeat", "leases_reaped", "leases_fenced_write"}
)


def _forbidden_trace() -> Trace:
    """h1 is reaped, h2 takes the folder and writes, then h1 writes at its old epoch.

    ``NoStaleWrite`` forbids the last step: the model would never take it,
    because ``FenceOk(h1)`` is false the moment the row names h2. The
    implementation must reach the same answer through the ``FOR SHARE`` re-read.
    """
    return hand_written(
        [
            Step("Init", (), clock=0, holder=None, epoch=0, expires=0, released=True, writes=0),
            Step(
                "leases_acquired",
                ("h1",),
                clock=0,
                holder="h1",
                epoch=1,
                expires=2,
                released=False,
                writes=0,
            ),
            Step(
                "clock_tick",
                (),
                clock=3,
                holder="h1",
                epoch=1,
                expires=2,
                released=False,
                writes=0,
            ),
            Step(
                "leases_reaped",
                (),
                clock=3,
                holder="h1",
                epoch=1,
                expires=2,
                released=True,
                writes=0,
            ),
            Step(
                "leases_acquired",
                ("h2",),
                clock=3,
                holder="h2",
                epoch=2,
                expires=5,
                released=False,
                writes=0,
            ),
            Step(
                "leases_fenced_write",
                ("h2",),
                clock=3,
                holder="h2",
                epoch=2,
                expires=5,
                released=False,
                writes=1,
            ),
            # The step the spec cannot take.
            Step(
                "leases_fenced_write",
                ("h1",),
                clock=3,
                holder="h2",
                epoch=2,
                expires=5,
                released=False,
                writes=2,
            ),
        ]
    )


async def _clear_lease(services: ReplayServices) -> None:
    """Hand the folder back between behaviours; the epoch high-water mark stays."""
    await services.repo.session.execute(
        text("DELETE FROM file_leases WHERE node_id = :n"), {"n": services.node_id}
    )
    await services.repo.session.commit()


async def test_every_simulated_trace_replays_through_the_implementation(
    tlc_traces: tuple[Trace, ...], lease_services: ReplayServices
) -> None:
    """Twenty TLC behaviours; every step driven, every invariant on the real rows."""
    assert len(tlc_traces) == 20
    seen: set[str] = set()
    replayed = 0
    for trace in tlc_traces:
        result = await replay(trace, services=lease_services)
        assert result.steps == len(trace), f"{trace.name} stopped early"
        seen.update(step.action for step in trace.steps)
        replayed += result.steps
        await _clear_lease(lease_services)
    assert REQUIRED_ACTIONS <= seen, f"the simulation never took {REQUIRED_ACTIONS - seen}"
    assert replayed > len(tlc_traces), "every behaviour was a single state"


async def test_a_write_at_a_superseded_epoch_is_fenced(lease_services: ReplayServices) -> None:
    """The negative twin: the behaviour the spec forbids is refused, by code."""
    result = await replay(_forbidden_trace(), services=lease_services, expect_refusal_at=6)

    assert result.refusals == ["files.lease_fenced"]
    assert result.landed == [result.granted[-1]], "only the newer holder's write may have landed"
    assert result.granted[0] < result.granted[-1], "the second acquire took a higher epoch"


async def test_the_replay_fails_when_the_fence_is_removed(
    lease_services: ReplayServices, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The conformance test can detect a broken implementation.

    ``assert_lease_epoch`` is the one predicate between a superseded holder and
    silent corruption. With it gone the forbidden write lands, and the replay's
    ``NoStaleWrite`` check on ``file_history`` is what must notice.
    """

    async def no_fence(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(leases, "assert_lease_epoch", no_fence)

    with pytest.raises(AssertionError, match="landed behind a newer epoch"):
        await replay(_forbidden_trace(), services=lease_services)


async def test_the_replay_reads_the_rows_it_asserts_on(lease_services: ReplayServices) -> None:
    """The invariants are read from Postgres, not from the replay's own bookkeeping."""
    result = await replay(_forbidden_trace(), services=lease_services, expect_refusal_at=6)
    node: NodeId = lease_services.node_id

    landed = (
        await lease_services.repo.session.execute(
            text(
                "SELECT (after->>'epoch')::bigint FROM file_history "
                "WHERE node_id = :n AND kind = 'rename' AND after ? 'epoch' ORDER BY seq"
            ),
            {"n": node},
        )
    ).scalars()
    assert [int(row) for row in landed] == result.landed
