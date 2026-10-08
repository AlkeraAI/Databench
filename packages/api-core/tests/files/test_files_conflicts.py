"""Conflict naming: never taken, extension preserved, stable, and inside the byte ceiling."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone

import pytest
from alkera_core.files.conflicts import (
    MAX_CONFLICT_INDEX,
    NAME_MAX_BYTES,
    NoAvailableNameError,
    conflict_rename,
    conflicted_copy_name,
    free_conflicted_copy_name,
)


def taken_set(*names: bytes) -> Callable[[bytes], bool]:
    """A `taken` predicate over an explicit set, so ordering cannot influence it."""
    frozen = frozenset(names)
    return lambda candidate: candidate in frozen


# --- the enumeration, the extension rule and its negative twins -----------------


@pytest.mark.parametrize(
    ("name", "taken", "expected"),
    [
        pytest.param(
            b"report.pdf",
            (b"report.pdf",),
            b"report (1).pdf",
            id="first-conflict-becomes-(1)",
        ),
        pytest.param(
            b"report.pdf",
            (b"report.pdf", b"report (1).pdf"),
            b"report (2).pdf",
            id="second-conflict-becomes-(2)",
        ),
        pytest.param(
            b"report.pdf",
            (b"report.pdf", b"report (1).pdf", b"report (3).pdf"),
            b"report (2).pdf",
            id="the-lowest-free-index-wins-not-the-highest-taken-plus-one",
        ),
        pytest.param(
            b"report.pdf",
            (),
            b"report.pdf",
            id="a-free-name-is-returned-unchanged",
        ),
        pytest.param(
            b"a.tar.gz",
            (b"a.tar.gz",),
            b"a.tar (1).gz",
            id="extension-is-the-LAST-suffix-so-a.tar.gz-splits-at-.gz",
        ),
        pytest.param(
            b"report",
            (b"report",),
            b"report (1)",
            id="a-name-with-no-dot-has-no-suffix",
        ),
        pytest.param(
            b".bashrc",
            (b".bashrc",),
            b".bashrc (1)",
            id="a-dotfile-has-an-empty-stem-so-the-whole-name-is-the-stem",
        ),
        pytest.param(
            b"report.",
            (b"report.",),
            b"report (1).",
            id="a-trailing-dot-is-a-suffix-Linux-accepts-it",
        ),
        pytest.param(
            b"\xff\xfe.bin",
            (b"\xff\xfe.bin",),
            b"\xff\xfe (1).bin",
            id="a-non-utf8-stem-is-untouched",
        ),
    ],
)
def test_conflict_rename_enumerates_and_keeps_the_extension(
    name: bytes, taken: tuple[bytes, ...], expected: bytes
) -> None:
    assert conflict_rename(name, taken_set(*taken)) == expected


@pytest.mark.parametrize(
    ("name", "taken", "expected"),
    [
        pytest.param(
            b"report (2).pdf",
            (b"report (2).pdf",),
            b"report (3).pdf",
            id="an-existing-(n)-continues-at-n+1",
        ),
        pytest.param(
            b"report (2).pdf",
            (b"report (2).pdf", b"report (3).pdf"),
            b"report (4).pdf",
            id="an-existing-(n)-skips-the-taken-successors",
        ),
        pytest.param(
            b"report (10)",
            (b"report (10)",),
            b"report (11)",
            id="multi-digit-(n)-continues-correctly",
        ),
        pytest.param(
            b"report (0).pdf",
            (b"report (0).pdf",),
            b"report (0) (1).pdf",
            id="NEGATIVE-(0)-is-not-a-conflict-marker",
        ),
        pytest.param(
            b"report (007).pdf",
            (b"report (007).pdf",),
            b"report (007) (1).pdf",
            id="NEGATIVE-a-leading-zero-is-not-a-conflict-marker",
        ),
        pytest.param(
            b"report (1a).pdf",
            (b"report (1a).pdf",),
            b"report (1a) (1).pdf",
            id="NEGATIVE-a-non-numeric-parenthetical-is-not-a-conflict-marker",
        ),
        pytest.param(
            b"report(1).pdf",
            (b"report(1).pdf",),
            b"report(1) (1).pdf",
            id="NEGATIVE-no-space-before-the-parenthesis-is-not-a-conflict-marker",
        ),
        pytest.param(
            b" (3).pdf",
            (b" (3).pdf",),
            b" (4).pdf",
            id="an-empty-base-still-continues-from-the-marker",
        ),
    ],
)
def test_an_existing_conflict_marker_continues_rather_than_nesting(
    name: bytes, taken: tuple[bytes, ...], expected: bytes
) -> None:
    assert conflict_rename(name, taken_set(*taken)) == expected


# --- never taken, stable -------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        pytest.param(b"report.pdf", id="with-suffix"),
        pytest.param(b"report", id="without-suffix"),
        pytest.param(b"report (4).pdf", id="already-marked"),
        pytest.param(b"a" * (NAME_MAX_BYTES - 5) + b".pdf", id="near-the-byte-bound"),
    ],
)
def test_conflict_rename_never_returns_a_taken_name(name: bytes) -> None:
    chosen: set[bytes] = {name}
    for _ in range(12):
        picked = conflict_rename(name, taken_set(*chosen))
        assert picked not in chosen
        assert len(picked) <= NAME_MAX_BYTES
        chosen.add(picked)
    # Twelve distinct names came out of twelve calls.
    assert len(chosen) == 13


def test_conflict_rename_is_stable_for_the_same_name_and_taken_set() -> None:
    taken = (b"report.pdf", b"report (1).pdf")
    first = conflict_rename(b"report.pdf", taken_set(*taken))
    second = conflict_rename(b"report.pdf", taken_set(*reversed(taken)))
    assert first == second == b"report (2).pdf"


# --- the byte ceiling ----------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "suffix_kept"),
    [
        pytest.param(b"a" * (NAME_MAX_BYTES - 4) + b".pdf", True, id="at-the-ceiling-exactly"),
        pytest.param(b"a" * (NAME_MAX_BYTES - 5) + b".pdf", True, id="one-under-the-ceiling"),
        pytest.param(b"a" * NAME_MAX_BYTES, False, id="at-the-ceiling-with-no-suffix"),
    ],
)
def test_conflict_rename_truncates_the_stem_and_never_the_suffix(
    name: bytes, suffix_kept: bool
) -> None:
    picked = conflict_rename(name, taken_set(name))
    assert len(picked) <= NAME_MAX_BYTES
    assert picked != name
    assert picked.endswith(b" (1).pdf" if suffix_kept else b" (1)")


def test_a_suffix_too_long_to_keep_is_dropped_rather_than_overflowing() -> None:
    # A 1-byte stem and a suffix filling the rest: no marker fits while keeping it.
    name = b"a." + b"x" * (NAME_MAX_BYTES - 2)
    assert len(name) == NAME_MAX_BYTES
    picked = conflict_rename(name, taken_set(name))
    assert len(picked) == NAME_MAX_BYTES
    assert picked.endswith(b" (1)")
    assert picked != name


def test_an_exhausted_index_range_raises_rather_than_looping_forever() -> None:
    with pytest.raises(NoAvailableNameError):
        conflict_rename(b"report.pdf", lambda _candidate: True)
    assert MAX_CONFLICT_INDEX >= 1000


# --- conflicted copies ---------------------------------------------------------


def test_conflicted_copy_name_is_pinned() -> None:
    at = datetime(2026, 9, 8, 23, 11, 45, tzinfo=UTC)
    assert conflicted_copy_name(b"report.pdf", "alice@example.com", at) == (
        b"report (conflicted copy from alice@example.com, 2026-09-08 23.11 UTC).pdf"
    )


def test_conflicted_copy_name_converts_to_utc_and_may_cross_the_day_boundary() -> None:
    # 2026-09-08 17:11 at UTC-8 is 2026-09-09 01:11 UTC.
    at = datetime(2026, 9, 8, 17, 11, tzinfo=timezone(timedelta(hours=-8)))
    assert conflicted_copy_name(b"report.pdf", "bob", at).endswith(b", 2026-09-09 01.11 UTC).pdf")


def test_copies_of_one_file_sort_by_when_they_were_made() -> None:
    """The stamp keeps a fixed-width order: a list sorted by name lists one
    file's copies oldest first, across hours, days and months."""
    times = [
        datetime(2026, 10, 5, 15, 2, tzinfo=UTC),
        datetime(2026, 9, 8, 23, 11, tzinfo=UTC),
        datetime(2026, 10, 5, 5, 21, tzinfo=UTC),
        datetime(2026, 10, 12, 0, 0, tzinfo=UTC),
    ]
    names = {conflicted_copy_name(b"shared.md", "Alkera Dev Admin", at): at for at in times}
    assert [names[name] for name in sorted(names)] == sorted(times)


def test_a_naive_datetime_is_read_as_utc() -> None:
    naive = datetime(2026, 9, 8, 23, 11)
    aware = datetime(2026, 9, 8, 23, 11, tzinfo=UTC)
    assert conflicted_copy_name(b"x", "bob", naive) == conflicted_copy_name(b"x", "bob", aware)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        pytest.param(
            b"a.tar.gz",
            b"a.tar (conflicted copy from bob, 2026-09-08 23.11 UTC).gz",
            id="last-suffix-rule-matches-conflict_rename",
        ),
        pytest.param(
            b"report",
            b"report (conflicted copy from bob, 2026-09-08 23.11 UTC)",
            id="no-suffix",
        ),
        pytest.param(
            b".bashrc",
            b".bashrc (conflicted copy from bob, 2026-09-08 23.11 UTC)",
            id="dotfile-keeps-its-whole-name-as-the-stem",
        ),
    ],
)
def test_conflicted_copy_name_splits_the_extension_the_same_way(
    name: bytes, expected: bytes
) -> None:
    at = datetime(2026, 9, 8, 23, 11, tzinfo=UTC)
    assert conflicted_copy_name(name, "bob", at) == expected


@pytest.mark.parametrize(
    ("actor", "forbidden"),
    [
        pytest.param("a/b", b"/", id="a-separator-can-never-reach-the-name"),
        pytest.param("a\x00b", b"\x00", id="a-NUL-can-never-reach-the-name"),
        pytest.param("a\nb", b"\n", id="a-control-character-is-escaped"),
        pytest.param("a‮b", "‮".encode(), id="a-bidi-override-is-escaped"),
    ],
)
def test_a_hostile_actor_cannot_inject_bytes_into_the_name(actor: str, forbidden: bytes) -> None:
    produced = conflicted_copy_name(b"report.pdf", actor, datetime(2026, 9, 8, 23, 11, tzinfo=UTC))
    assert forbidden not in produced
    assert len(produced) <= NAME_MAX_BYTES


def test_actor_escaping_stays_injective() -> None:
    at = datetime(2026, 9, 8, 23, 11, tzinfo=UTC)
    # The escape character itself is escaped, so these two actors cannot collide.
    assert conflicted_copy_name(b"x", "a\\x2fb", at) != conflicted_copy_name(b"x", "a/b", at)


def test_a_long_actor_cannot_push_the_name_past_the_byte_bound() -> None:
    produced = conflicted_copy_name(
        b"report.pdf", "z" * 400, datetime(2026, 9, 8, 23, 11, tzinfo=UTC)
    )
    assert len(produced) == NAME_MAX_BYTES
    assert produced.endswith(b").pdf")


# --- the drive's copy namer: the unnumbered name first, then (2), (3) ----------

_AT = datetime(2026, 9, 8, 23, 11, tzinfo=UTC)
_COPY = b"report (conflicted copy from alice, 2026-09-08 23.11 UTC).pdf"


@pytest.mark.parametrize(
    ("taken", "expected"),
    [
        pytest.param((), _COPY, id="the-first-copy-in-a-minute-is-unnumbered"),
        pytest.param(
            (_COPY,),
            b"report (conflicted copy from alice, 2026-09-08 23.11 UTC) (2).pdf",
            id="the-second-is-(2)-never-(1)",
        ),
        pytest.param(
            (_COPY, b"report (conflicted copy from alice, 2026-09-08 23.11 UTC) (2).pdf"),
            b"report (conflicted copy from alice, 2026-09-08 23.11 UTC) (3).pdf",
            id="the-third-is-(3)",
        ),
        pytest.param(
            (b"report (conflicted copy from alice, 2026-09-08 23.11 UTC) (1).pdf",),
            _COPY,
            id="a-(1)-somebody-made-does-not-shift-the-unnumbered-name",
        ),
    ],
)
def test_free_conflicted_copy_name_numbers_from_two(
    taken: tuple[bytes, ...], expected: bytes
) -> None:
    got = free_conflicted_copy_name(b"report.pdf", "alice", _AT, taken_set(*taken))
    assert got == expected
    assert got not in taken
