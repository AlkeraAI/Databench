"""The benchmark harness runs only the work the claim it serves needs.

A performance budget row is two claims: the number of SQL statements one request costs,
and the p95 over twenty timed repetitions — and only the first is asserted on
every pull request. Splitting the assertion was not enough: the harness still
ran the twenty repetitions for the statement case, so the gate paid twenty extra
round trips per measured label over the largest trees in the suite (and, on the
move row, twenty extra relocations of a ten-thousand-node subtree) to compute a
percentile nothing then read. Under the runner's fan-out that surplus is what
turned a statement budget into a wall-clock timeout.

These cases drive ``Bench`` directly with a counter for a route, so the claim's
effect on how much the harness RUNS is observable without a database: the
statement claim issues the warm-up and the one counted call and nothing else,
the wall-clock claim still issues its twenty, and the counted call is the same
iteration number in both, so a mutating row aims at the same fresh target
either way. A bench that asserts a p95 over a row it never timed is the one
combination that must raise rather than pass quietly.
"""

from __future__ import annotations

import pytest
from _files_kit import REPEATS, Bench, Sample
from httpx import Request, Response

pytestmark = pytest.mark.asyncio


def _ok() -> Response:
    return Response(200, request=Request("GET", "http://test/x"))


class _Counter:
    """Stands in for the route under measurement, remembering every call."""

    def __init__(self) -> None:
        self.iterations: list[int] = []

    async def __call__(self, iteration: int) -> Response:
        self.iterations.append(iteration)
        return _ok()


async def test_the_statement_claim_pays_only_the_warm_up_and_the_counted_call() -> None:
    calls = _Counter()
    bench = Bench(1.0, claim="statements")

    sample = await bench.measure("a route", calls)

    assert calls.iterations == [0, REPEATS + 1], (
        "the statement claim ran the timed repetitions; they produce a "
        f"percentile it never reads: {calls.iterations}"
    )
    assert not sample.timed
    assert sample.durations_ms == []


async def test_the_wallclock_claim_still_times_every_repetition() -> None:
    calls = _Counter()
    bench = Bench(1.0, claim="wallclock")

    sample = await bench.measure("a route", calls)

    assert calls.iterations == list(range(0, REPEATS + 2))
    assert sample.timed
    assert len(sample.durations_ms) == REPEATS


async def test_both_claims_count_the_same_iteration() -> None:
    """The counted call must aim at the same target in both halves.

    A mutating row picks its subject by iteration number — ``created[21]``,
    ``homes[21]`` — so a statement claim that counted a different iteration
    than the wall-clock one would be budgeting a different request.
    """
    timed, untimed = _Counter(), _Counter()
    await Bench(1.0, claim="wallclock").measure("a route", timed)
    await Bench(1.0, claim="statements").measure("a route", untimed)

    assert timed.iterations[-1] == untimed.iterations[-1] == REPEATS + 1


async def test_a_row_that_was_not_timed_reports_no_percentile() -> None:
    """``report`` prints on a pass, so an absent percentile must not raise.

    ``statistics.median`` over an empty sample is an exception, not a number,
    and it would surface as an error in the reporting line rather than as the
    budget the row is about.
    """
    sample = Sample(label="a route")

    assert sample.p95_ms != sample.p95_ms  # nan
    assert sample.median_ms != sample.median_ms

    Bench(1.0, claim="statements").report(sample, budget_ms=10, statements=1)


async def test_a_timed_row_still_asserts_its_percentile() -> None:
    sample = Sample(label="a route", durations_ms=[500.0] * REPEATS, statements=1)

    with pytest.raises(AssertionError, match="over 10ms"):
        Bench(1.0, claim="wallclock").report(sample, budget_ms=10, statements=1)


async def test_a_wallclock_claim_over_an_untimed_row_refuses_to_pass() -> None:
    """The latency budget may not go silently unasserted.

    A bench built for the wall-clock claim and handed a row with no latencies
    behind it has measured nothing; passing would hand back a green that proves
    only the statement count, under a label that says it proves the p95.
    """
    sample = Sample(label="a route", statements=1)

    with pytest.raises(AssertionError, match="built for the wrong claim"):
        Bench(1.0, claim="wallclock").report(sample, budget_ms=10, statements=1)
