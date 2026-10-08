"""Storage is quoted in the units it is sold in, in one place, both directions.

The bug these pin: a ceiling entered as "100 GB" was stored as 100 x 2^30 and
divided by 10^9 to display, so the admin page said 100 GB and the dashboard said
107 GB about the same number. The fix is that a GB is 10^9 on BOTH sides, so the
load-bearing assertions here are the ones that would pass under either
convention only if the two agreed — the exact byte values of the unit table, and
the round trip parse(format(x)).
"""

from __future__ import annotations

import pytest
from alkera_core.units import (
    GB,
    KB,
    MB,
    PB,
    TB,
    UNIT_BYTES,
    exact_bytes,
    format_bytes,
    parse_bytes,
)


def test_a_unit_is_a_power_of_a_thousand_bytes() -> None:
    """The whole decision, as numbers: no unit is a power of 1024."""
    assert (KB, MB, GB, TB, PB) == (10**3, 10**6, 10**9, 10**12, 10**15)
    assert UNIT_BYTES == {"B": 1, "KB": KB, "MB": MB, "GB": GB, "TB": TB, "PB": PB}
    assert GB != 1024**3


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        pytest.param(0, "0 B", id="zero-is-bytes-not-blank"),
        pytest.param(1, "1 B", id="one-byte"),
        pytest.param(999, "999 B", id="last-byte-figure"),
        pytest.param(1_000, "1 KB", id="kilobyte-boundary"),
        pytest.param(1_500, "1.5 KB", id="one-decimal-survives"),
        pytest.param(812_000_000, "812 MB", id="megabytes"),
        pytest.param(2_500_000_000, "2.5 GB", id="gigabytes-with-a-half"),
        pytest.param(10_000_000_000, "10 GB", id="the-free-plan-figure"),
        pytest.param(100_000_000_000, "100 GB", id="the-deployment-default"),
        pytest.param(1_000_000_000_000, "1 TB", id="the-plus-plan-figure"),
        pytest.param(5_000_000_000_000, "5 TB", id="the-pro-plan-figure"),
        pytest.param(10**15, "1 PB", id="petabytes"),
        pytest.param(9 * 10**17, "900 PB", id="no-unit-above-petabytes"),
    ],
)
def test_a_byte_count_reads_as_the_figure_it_was_set_as(count: int, expected: str) -> None:
    assert format_bytes(count) == expected


def test_the_limit_that_started_this_reads_as_what_the_operator_typed() -> None:
    """100 GB set on the admin page is 100 GB on the dashboard — the two
    readings that disagreed. The old binary figure is kept alongside so the
    regression is legible: it is NOT what 100 GB means any more."""
    assert format_bytes(100 * 10**9) == "100 GB"
    assert format_bytes(100 * 1024**3) == "107.4 GB"


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        pytest.param(999_999_999, "1 GB", id="rounds-up-into-the-next-unit"),
        pytest.param(999_500_000_000, "999.5 GB", id="stays-put-when-it-still-fits"),
        pytest.param(999_950_000_000, "1 TB", id="rounds-up-past-the-gigabyte-ceiling"),
        pytest.param(1_050_000_000, "1.1 GB", id="halves-round-away-from-zero"),
        pytest.param(107_374_182_400, "107.4 GB", id="one-decimal-never-two"),
    ],
)
def test_the_rounding_rule_is_one_decimal_and_a_promotion(count: int, expected: str) -> None:
    """At most one decimal, halves up, and a figure that rounds to 1000 of its
    unit is shown in the next one — nobody reads "1000 MB" or "107.37 GB"."""
    assert format_bytes(count) == expected


@pytest.mark.parametrize(
    "count",
    [
        pytest.param(None, id="none"),
        pytest.param(-1, id="negative"),
        pytest.param(float("nan"), id="nan"),
    ],
)
def test_a_thing_that_is_not_a_size_is_not_formatted_into_one(count: float | None) -> None:
    """No ceiling is not zero bytes: the surface names the absence, not this."""
    assert format_bytes(count) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("100", 100 * GB, id="a-bare-number-is-gigabytes"),
        pytest.param("100 GB", 100 * GB, id="with-a-space"),
        pytest.param("100GB", 100 * GB, id="without-a-space"),
        pytest.param("1 TB", TB, id="terabytes"),
        pytest.param("0.5 TB", 500 * GB, id="a-fraction-of-a-terabyte"),
        pytest.param("250 MB", 250 * MB, id="megabytes"),
        pytest.param("  2 tb  ", 2 * TB, id="lowercase-and-padded"),
        pytest.param("512 kb", 512 * KB, id="kilobytes"),
        pytest.param("4096 bytes", 4096, id="bytes-spelled-out"),
        pytest.param("3 t", 3 * TB, id="the-bare-letter-people-type"),
    ],
)
def test_a_typed_figure_becomes_the_bytes_it_says(text: str, expected: int) -> None:
    assert parse_bytes(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("", id="empty"),
        pytest.param("   ", id="whitespace"),
        pytest.param("abc", id="not-a-number"),
        pytest.param("100 ZB", id="not-a-unit"),
        pytest.param("1,5 GB", id="a-comma-is-not-a-decimal-point"),
        pytest.param("-4 GB", id="negative"),
        pytest.param("0", id="zero-is-a-lockout-not-an-edit"),
        pytest.param("0 GB", id="zero-with-a-unit"),
        pytest.param("100 GB extra", id="trailing-words"),
    ],
)
def test_a_figure_that_cannot_be_read_is_refused_and_says_why(text: str) -> None:
    """A ceiling that cannot be read must never be silently written as
    something else — the caller gets an exception carrying a message."""
    with pytest.raises(ValueError) as caught:
        parse_bytes(text)
    assert str(caught.value)


def test_the_default_unit_is_what_the_caller_says_it_is() -> None:
    assert parse_bytes("250", default_unit="MB") == 250 * MB


@pytest.mark.parametrize(
    "count",
    [
        pytest.param(10 * GB, id="free-plan"),
        pytest.param(100 * GB, id="deployment-default"),
        pytest.param(TB, id="plus-plan"),
        pytest.param(5 * TB, id="pro-plan"),
        pytest.param(2_500_000_000, id="a-figure-with-a-decimal"),
        pytest.param(812 * MB, id="megabytes"),
    ],
)
def test_a_formatted_figure_parses_back_to_the_same_bytes(count: int) -> None:
    """The round trip is what makes an editor honest: the figure it seeds from a
    stored ceiling, saved untouched, writes that same ceiling back."""
    rendered = format_bytes(count)
    assert rendered is not None
    assert parse_bytes(rendered) == count


def test_the_exact_count_is_available_under_a_figure_being_edited() -> None:
    assert exact_bytes(100 * GB) == "100,000,000,000 bytes"
