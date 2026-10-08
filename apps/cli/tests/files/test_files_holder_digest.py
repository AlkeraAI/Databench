"""The holder's per-directory digest, against the vectors the drive shares.

The drive computes the same digest from its rows (``alkera_core.files.digest``)
and the walk trusts a match to skip a directory, so the two must agree to the
bit. Both test against one vector file; until the drive's lands, the holder's
own hand-derived vectors stand in, and every vector file found is checked.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.files.digest import EMPTY, DirDigest, child_value, digest_of, from_hex

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[3]
_VECTOR_FILES = [
    path
    for path in (
        _REPO / "packages" / "api-core" / "tests" / "files" / "fixtures" / "digest_vectors.json",
        _HERE / "fixtures" / "digest_vectors.json",
    )
    if path.is_file()
]


def _vectors() -> list[Any]:
    found: list[Any] = []
    for path in _VECTOR_FILES:
        data = json.loads(path.read_text())
        # The shared file names each child once, in ``entries``, and a directory
        # lists its children by index; a file of the holder's own spells them out.
        entries = data.get("entries", [])
        for vector in data["directories"]:
            resolved = dict(vector)
            resolved["children"] = [
                entries[child] if isinstance(child, int) else child for child in vector["children"]
            ]
            found.append(
                pytest.param(
                    resolved,
                    id=f"{path.parent.parent.parent.name}-{vector.get('id', vector.get('name'))}",
                )
            )
    return found


def _children(vector: dict[str, Any]) -> list[tuple[Any, bytes, int, int]]:
    return [
        (
            child["kind"],
            bytes.fromhex(child["name_hex"])
            if "name_hex" in child
            else child["name"].encode("utf-8"),
            int(child.get("size", 0)),
            int(child.get("mtime_ns", 0)),
        )
        for child in vector["children"]
    ]


@pytest.mark.parametrize("vector", _vectors())
def test_the_digest_matches_the_shared_vectors(vector: dict[str, Any]) -> None:
    digest = digest_of(_children(vector))

    assert digest.count == vector["count"]
    assert digest.hex == vector["xor"]


def test_the_fold_is_independent_of_listing_order() -> None:
    children: list[tuple[Any, bytes, int, int]] = [
        ("file", b"a.txt", 5, 10),
        ("dir", b"src", 0, 0),
        ("file", b"b.txt", 7, 11),
    ]
    assert digest_of(children) == digest_of(list(reversed(children)))


@pytest.mark.parametrize(
    ("before", "after"),
    [
        pytest.param(("file", b"a.txt", 5, 10), ("file", b"b.txt", 5, 10), id="renamed"),
        pytest.param(("file", b"a.txt", 5, 10), ("file", b"a.txt", 6, 10), id="resized"),
        pytest.param(("file", b"a.txt", 5, 10), ("file", b"a.txt", 5, 11), id="touched"),
        pytest.param(("file", b"a", 0, 0), ("dir", b"a", 0, 0), id="file-became-dir"),
    ],
)
def test_any_fact_of_a_child_changes_the_digest(
    before: tuple[Any, bytes, int, int], after: tuple[Any, bytes, int, int]
) -> None:
    sibling: tuple[Any, bytes, int, int] = ("file", b"z", 1, 1)
    assert digest_of([before, sibling]) != digest_of([after, sibling])


def test_a_directory_child_ignores_its_own_size_and_time() -> None:
    """A change inside a subdirectory marks that subdirectory, not its parent."""
    assert child_value("dir", b"src", 4096, 123) == child_value("dir", b"src")


def test_adding_then_removing_a_child_returns_to_the_same_digest() -> None:
    one = child_value("file", b"a.txt", 5, 10)
    two = child_value("file", b"b.txt", 6, 10)
    digest = EMPTY.with_child(one).with_child(two)
    assert DirDigest(count=digest.count - 1, xor=digest.xor ^ two) == EMPTY.with_child(one)


def test_the_wire_form_round_trips() -> None:
    digest = digest_of([("file", b"a.txt", 5, 10)])
    assert from_hex(digest.count, digest.hex) == digest
    assert len(digest.hex) == 16
    assert EMPTY.hex == "0" * 16
