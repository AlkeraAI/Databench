"""The history checker itself: it accepts what it must and rejects what it must.

A checker that never rejects would silently bless every concurrency bug in the
suite, so each case below is a hand-built history whose verdict is decided by
the definition of linearizability, not by the implementation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from alkera_core.files.clock import FakeClock
from linearizability import (
    MAX_CONCURRENT_OPS,
    History,
    NamespaceModel,
    Op,
    RegisterModel,
    TooLarge,
    check_linearizable,
    max_concurrency,
)

EPOCH_MONOTONIC = 0.0


def op(
    kind: str,
    args: tuple[object, ...],
    result: object,
    invoke_at: float,
    return_at: float,
    client: str = "c0",
) -> Op:
    return Op(
        kind=kind,
        args=args,
        result=result,
        invoke_at=invoke_at,
        return_at=return_at,
        client=client,
    )


def test_a_sequential_register_history_is_linearizable() -> None:
    history = History(
        ops=[
            op("write", (1,), None, 0.0, 1.0, "a"),
            op("read", (), 1, 2.0, 3.0, "b"),
            op("write", (2,), None, 4.0, 5.0, "a"),
            op("read", (), 2, 6.0, 7.0, "b"),
        ]
    )

    result = check_linearizable(history, RegisterModel)

    assert result.ok, result.message
    assert [o.kind for o in result.order] == ["write", "read", "write", "read"]


def test_a_read_that_misses_a_completed_write_is_rejected() -> None:
    """write(1) returned at t=1; the read starts at t=2 and says 0 — impossible."""
    offending = op("read", (), 0, 2.0, 3.0, "b")
    history = History(ops=[op("write", (1,), None, 0.0, 1.0, "a"), offending])

    result = check_linearizable(history, RegisterModel)

    assert not result.ok
    assert result.violating_op == offending
    assert "read" in result.message


def test_a_read_overlapping_the_write_may_return_either_value() -> None:
    """The read is concurrent with write(1), so both orders are legal."""
    for observed in (None, 1):
        history = History(
            ops=[
                op("write", (1,), None, 0.0, 4.0, "a"),
                op("read", (), observed, 1.0, 3.0, "b"),
            ]
        )

        result = check_linearizable(history, RegisterModel)

        assert result.ok, f"observed={observed!r}: {result.message}"


def test_two_concurrent_writes_are_pinned_by_the_read_that_follows() -> None:
    """Either write may win, but a later read must report the winner."""
    good = History(
        ops=[
            op("write", (1,), None, 0.0, 2.0, "a"),
            op("write", (2,), None, 0.5, 2.5, "b"),
            op("read", (), 1, 3.0, 4.0, "c"),
        ]
    )
    bad = History(
        ops=[
            op("write", (1,), None, 0.0, 2.0, "a"),
            op("write", (2,), None, 0.5, 2.5, "b"),
            op("read", (), 3, 3.0, 4.0, "c"),
        ]
    )

    assert check_linearizable(good, RegisterModel).ok
    assert not check_linearizable(bad, RegisterModel).ok


async def test_record_stamps_invocation_and_response_from_the_injected_clock() -> None:
    clock = FakeClock(now=datetime(2026, 1, 1, tzinfo=UTC))
    history = History(clock=clock)
    model = RegisterModel()

    async with history.record("write", (5,), client="a") as call:
        clock.advance(timedelta(seconds=1))
        call.result = model.apply(Op("write", (5,), None, 0.0, 0.0, "a"))
    clock.advance(timedelta(seconds=1))
    async with history.record("read", client="b") as call:
        call.result = model.apply(Op("read", (), None, 0.0, 0.0, "b"))

    assert [(o.kind, o.invoke_at, o.return_at) for o in history.ops] == [
        ("write", 0.0, 1.0),
        ("read", 2.0, 2.0),
    ]
    assert check_linearizable(history, RegisterModel).ok


def test_a_rename_between_two_reads_is_accepted() -> None:
    history = History(
        ops=[
            op("create", ("/a",), "/a", 0.0, 1.0, "a"),
            op("list", (), ("/a",), 2.0, 3.0, "b"),
            op("rename", ("/a", "/b"), "/b", 4.0, 5.0, "a"),
            op("list", (), ("/b",), 6.0, 7.0, "b"),
        ]
    )

    result = check_linearizable(history, NamespaceModel)

    assert result.ok, result.message


def test_a_read_of_a_name_no_order_produces_is_rejected() -> None:
    """Nothing ever created /c, so no linearization can list it."""
    offending = op("list", (), ("/c",), 6.0, 7.0, "b")
    history = History(
        ops=[
            op("create", ("/a",), "/a", 0.0, 1.0, "a"),
            op("rename", ("/a", "/b"), "/b", 4.0, 5.0, "a"),
            offending,
        ]
    )

    result = check_linearizable(history, NamespaceModel)

    assert not result.ok
    assert result.violating_op == offending


def test_a_trashed_node_reads_as_missing_until_it_is_restored() -> None:
    good = History(
        ops=[
            op("create", ("/a",), "/a", 0.0, 1.0, "a"),
            op("put_version", ("/a", "v1"), "v1", 2.0, 3.0, "a"),
            op("trash", ("/a",), "/a", 4.0, 5.0, "b"),
            op("read_version", ("/a",), None, 6.0, 7.0, "c"),
            op("restore", ("/a",), "/a", 8.0, 9.0, "b"),
            op("read_version", ("/a",), "v1", 10.0, 11.0, "c"),
        ]
    )
    bad = History(ops=[*good.ops[:3], op("read_version", ("/a",), "v1", 6.0, 7.0, "c")])

    assert check_linearizable(good, NamespaceModel).ok
    assert not check_linearizable(bad, NamespaceModel).ok


def test_a_grant_is_visible_to_a_later_read_and_gone_after_revoke() -> None:
    history = History(
        ops=[
            op("create", ("/a",), "/a", 0.0, 1.0, "a"),
            op("grant", ("/a", "p1", "editor"), "editor", 2.0, 3.0, "a"),
            op("revoke", ("/a", "p1"), "editor", 4.0, 5.0, "b"),
            op("revoke", ("/a", "p1"), None, 6.0, 7.0, "b"),
        ]
    )

    assert check_linearizable(history, NamespaceModel).ok


def test_max_concurrency_does_not_count_a_return_that_touches_an_invocation() -> None:
    touching = [op("read", (), None, 0.0, 1.0, "a"), op("read", (), None, 1.0, 2.0, "b")]
    overlapping = [op("read", (), None, 0.0, 1.5, "a"), op("read", (), None, 1.0, 2.0, "b")]

    assert max_concurrency(touching) == 1
    assert max_concurrency(overlapping) == 2


def test_a_history_wider_than_the_bound_is_refused_not_searched() -> None:
    at_bound = History(
        ops=[op("read", (), None, 0.0, 100.0, f"c{i}") for i in range(MAX_CONCURRENT_OPS)]
    )
    past_bound = History(
        ops=[op("read", (), None, 0.0, 100.0, f"c{i}") for i in range(MAX_CONCURRENT_OPS + 1)]
    )

    assert check_linearizable(at_bound, RegisterModel).ok

    with pytest.raises(TooLarge, match=f"{MAX_CONCURRENT_OPS + 1} concurrent operations"):
        check_linearizable(past_bound, RegisterModel)


async def test_record_without_a_clock_is_a_construction_error() -> None:
    history = History()

    with pytest.raises(ValueError, match="needs a clock"):
        async with history.record("read"):
            pass  # pragma: no cover — the context manager never opens
