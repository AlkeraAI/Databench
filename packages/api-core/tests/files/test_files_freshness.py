"""The freshness predicate: whether the drive's bytes are the holder's, by metadata alone.

A row under a live lease is listed from what the holder reported before its
bytes arrived, and every reader is told which of five states it is in. Nothing
here touches a database: the predicate is a pure function of the head version
and the holder's report, which is what lets a read of a current file cost the
machine nothing.
"""

from __future__ import annotations

import pytest
from alkera_core.files.freshness import (
    CONTENT_STATES,
    LANDING_STATES,
    ContentState,
    HeadFacts,
    HolderFacet,
    content_state,
    current,
    head_digest,
    under_lease,
)

_A = bytes.fromhex("aa" * 32)
_B = bytes.fromhex("bb" * 32)


@pytest.mark.parametrize(
    ("head", "facet", "expected"),
    [
        pytest.param(HeadFacts(10, 5), HolderFacet(10, 5), True, id="the-pair-agrees"),
        pytest.param(HeadFacts(10, 5), HolderFacet(11, 5), False, id="the-size-differs"),
        pytest.param(HeadFacts(10, 5), HolderFacet(10, 6), False, id="the-mtime-differs"),
        pytest.param(
            HeadFacts(10, 5, _A),
            HolderFacet(10, 5, _B),
            False,
            id="both-hashes-known-and-different-beat-an-equal-pair",
        ),
        pytest.param(
            HeadFacts(10, 5, _A),
            HolderFacet(99, 1, _A),
            True,
            id="both-hashes-known-and-equal-beat-a-different-pair",
        ),
        pytest.param(
            HeadFacts(10, 5, _A), HolderFacet(10, 5), True, id="hash-on-the-drive-only-pair-decides"
        ),
        pytest.param(
            HeadFacts(10, 5),
            HolderFacet(10, 5, _A),
            True,
            id="hash-on-the-holder-only-pair-decides",
        ),
        pytest.param(
            HeadFacts(10, 5, _A),
            HolderFacet(10, 6),
            False,
            id="hash-on-one-side-and-the-pair-differs",
        ),
        pytest.param(None, HolderFacet(10, 5), False, id="no-head"),
        pytest.param(HeadFacts(10, 5), None, False, id="no-report"),
    ],
)
def test_current(head: HeadFacts | None, facet: HolderFacet | None, expected: bool) -> None:
    assert current(head, facet) is expected


@pytest.mark.parametrize(
    ("head", "facet", "lease_live", "expected"),
    [
        pytest.param(HeadFacts(1, 1), HolderFacet(1, 1), True, "on_drive", id="current"),
        pytest.param(HeadFacts(1, 1), HolderFacet(2, 1), True, "behind", id="holder-is-ahead"),
        pytest.param(None, HolderFacet(2, 1), True, "unlanded", id="no-bytes-yet"),
        pytest.param(None, HolderFacet(2, 1), False, "unsynced", id="holder-gone-before-bytes"),
        # A holder going away (a box restart, a release, a lapse) says nothing
        # about bytes the drive already has: a current file is saved, and one
        # the holder had moved past is the older copy it always was. Only a
        # file with no bytes on the drive is left on the machine.
        pytest.param(
            HeadFacts(1, 1), HolderFacet(1, 1), False, "on_drive", id="holder-gone-current"
        ),
        pytest.param(HeadFacts(1, 1), HolderFacet(2, 1), False, "behind", id="holder-gone-ahead"),
        pytest.param(HeadFacts(1, 1), None, True, "none", id="no-report-under-a-live-lease"),
        pytest.param(HeadFacts(1, 1), None, False, "none", id="no-report-and-no-lease"),
        pytest.param(None, None, False, "none", id="an-ordinary-headless-row"),
    ],
)
def test_content_state(
    head: HeadFacts | None, facet: HolderFacet | None, lease_live: bool, expected: str
) -> None:
    assert content_state(head, facet, lease_live=lease_live) == expected


@pytest.mark.parametrize(
    ("landed", "lease_live", "expected"),
    [
        pytest.param("unlanded", True, "unlanded", id="unlanded-while-held"),
        pytest.param("unlanded", False, "unsynced", id="unlanded-and-the-holder-gone"),
        pytest.param("on_drive", False, "on_drive", id="saved-stays-saved"),
        pytest.param("behind", False, "behind", id="an-older-copy-stays-an-older-copy"),
        pytest.param("behind", True, "behind", id="behind-while-held"),
        pytest.param("none", False, "none", id="nothing-reported"),
    ],
)
def test_the_lease_only_changes_a_row_with_no_bytes_on_the_drive(
    landed: ContentState, lease_live: bool, expected: str
) -> None:
    """The fold a feed applies once it learns the lease after rendering a row
    is the same rule ``content_state`` applies when it knows the lease up front."""
    assert under_lease(landed, lease_live=lease_live) == expected


def test_landing_is_exactly_the_two_states_with_bytes_on_their_way() -> None:
    assert set(CONTENT_STATES) == {"on_drive", "behind", "unlanded", "unsynced", "none"}
    assert LANDING_STATES == {"behind", "unlanded"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("aa" * 32, _A, id="hex"),
        pytest.param(_A, _A, id="raw-bytes"),
        pytest.param("", None, id="empty-hex-is-unknown"),
        pytest.param(b"", None, id="empty-bytes-is-unknown"),
        pytest.param("not-hex", None, id="malformed-is-unknown"),
        pytest.param(None, None, id="absent"),
    ],
)
def test_a_stored_hash_is_read_as_a_digest_or_as_unknown(
    raw: str | bytes | None, expected: bytes | None
) -> None:
    assert head_digest(raw) == expected
