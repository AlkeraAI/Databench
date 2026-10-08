"""The folder digest, held to the vector file the holder's copy is held to.

The drive and the machine compute the same digest independently and compare
them; a disagreement that is not a real difference costs a folder's worth of
entries on every walk, and an agreement that hides one loses a change for good.
So both copies answer to one file of vectors, and the properties the walk leans
on -- order does not matter, a rename is seen, a grandchild is not -- are
pinned on top.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import pytest
from alkera_core.files.digest import DigestChild, child_hash, directory_digest
from blake3 import blake3

VECTORS: dict[str, Any] = json.loads(
    (Path(__file__).parent / "fixtures" / "digest_vectors.json").read_text()
)


def _child(entry: dict[str, Any]) -> DigestChild:
    return DigestChild(
        kind=entry["kind"],
        name=bytes.fromhex(entry["name_hex"]),
        size=entry["size"],
        mtime_ns=entry["mtime_ns"],
    )


@pytest.mark.parametrize(
    "entry", [pytest.param(e, id=f"{e['kind']}-{e['name_hex']}") for e in VECTORS["entries"]]
)
def test_each_child_hashes_to_its_vector(entry: dict[str, Any]) -> None:
    child = _child(entry)
    assert f"{child_hash(child.kind, child.name, child.size, child.mtime_ns):016x}" == entry["hash"]


@pytest.mark.parametrize(
    "entry", [pytest.param(e, id=f"{e['kind']}-{e['name_hex']}") for e in VECTORS["entries"]]
)
def test_the_vectors_are_the_written_rule(entry: dict[str, Any]) -> None:
    """The vector file is the rule as its prose states it, recomputed here from
    the prose alone -- so the vectors cannot quietly follow a module that
    drifted from what the holder was told."""
    kind = entry["kind"]
    size, mtime = (0, 0) if kind == "dir" else (entry["size"], entry["mtime_ns"])
    encoded = (
        {"file": b"f", "dir": b"d"}[kind]
        + bytes.fromhex(entry["name_hex"])
        + size.to_bytes(8, "big")
        + mtime.to_bytes(8, "big", signed=True)
    )
    assert blake3(encoded).digest()[:8].hex() == entry["hash"]


@pytest.mark.parametrize("folder", [pytest.param(d, id=d["name"]) for d in VECTORS["directories"]])
def test_each_folder_digests_to_its_vector(folder: dict[str, Any]) -> None:
    children = [_child(VECTORS["entries"][i]) for i in folder["children"]]
    digest = directory_digest(children)
    assert digest.wire() == {"count": folder["count"], "xor": folder["xor"]}
    assert len(digest.hex) == 16


def test_the_order_children_are_visited_in_does_not_matter() -> None:
    children = [_child(entry) for entry in VECTORS["entries"]]
    expected = directory_digest(children)
    rng = random.Random(7)
    for _ in range(20):
        shuffled = children[:]
        rng.shuffle(shuffled)
        assert directory_digest(shuffled) == expected


@pytest.mark.parametrize(
    "changed",
    [
        pytest.param(DigestChild("file", b"b.txt", 5, 10), id="a-renamed-child"),
        pytest.param(DigestChild("file", b"a.txt", 6, 10), id="a-resized-child"),
        pytest.param(DigestChild("file", b"a.txt", 5, 11), id="a-touched-child"),
        pytest.param(DigestChild("dir", b"a.txt"), id="a-file-become-a-folder"),
    ],
)
def test_a_change_to_one_child_changes_the_digest(changed: DigestChild) -> None:
    sibling = DigestChild("dir", b"src")
    before = directory_digest([DigestChild("file", b"a.txt", 5, 10), sibling])
    after = directory_digest([changed, sibling])
    assert after.count == before.count
    assert after.xor != before.xor


def test_a_folder_digest_ignores_its_grandchildren() -> None:
    """A folder's own entry hashes no size and no time, so what moves inside a
    subfolder is that subfolder's digest to show, never its parent's."""
    quiet = directory_digest([DigestChild("dir", b"src", 0, 0)])
    busy = directory_digest([DigestChild("dir", b"src", 4096, 1_758_625_000_000_000_000)])
    assert quiet == busy


def test_an_empty_folder_is_zero_children_and_a_zero_xor() -> None:
    assert directory_digest([]).wire() == {"count": 0, "xor": "0000000000000000"}


@pytest.mark.parametrize(
    ("kind", "size"),
    [pytest.param("link", 0, id="an-unknown-kind"), pytest.param("file", -1, id="a-negative-size")],
)
def test_facts_the_rule_cannot_encode_are_refused(kind: Any, size: int) -> None:
    with pytest.raises(ValueError):
        child_hash(kind, b"x", size, 0)
