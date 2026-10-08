"""``keep_edits``: a re-pull into a copy that is not the only writer.

A reader's working copy is re-pulled while other people and a machine keep
writing the folder, so "differs from the drive" no longer means "edited here".
The policy keeps exactly the files the caller changed since it last agreed them
with the drive, and lets the drive's newer bytes replace the rest. Every case is
read off the disk the pull wrote and the bytes the fake content route served.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from alkera_cli.files.pull import pull
from blake3 import blake3

DRIVE = "11111111-1111-1111-1111-111111111111"
ROOT = "dddddddd-0000-0000-0000-000000000001"
NOTE = "dddddddd-0000-0000-0000-000000000002"
SUB = "dddddddd-0000-0000-0000-000000000003"

OLD = b"version one\n"
NEW = b"version two, from somebody else\n"
MINE = b"my unsaved-to-the-drive edit\n"


def _hash(payload: bytes) -> str:
    return str(blake3(payload).hexdigest())


@dataclass(frozen=True)
class Agreed:
    node_id: str
    content_hash: str
    size: int


class Tree:
    """A drive folder holding ``note.txt`` and an empty ``sub/``, whose bytes a
    test can move between pulls."""

    def __init__(self, body: bytes) -> None:
        self.body = body

    def drive(self) -> dict[str, Any]:
        return {"id": DRIVE}

    def item(self, drive_id: str, item_id: str, **_: Any) -> dict[str, Any]:
        assert item_id == ROOT
        return {"id": ROOT, "kind": "folder", "name": "work"}

    def item_by_path(self, drive_id: str, item_path: str) -> dict[str, Any]:
        raise AssertionError("the pull under test names its root by id")

    def children(self, drive_id: str, item_id: str, **_: Any) -> Iterator[dict[str, Any]]:
        if item_id != ROOT:
            return
        yield {
            "id": NOTE,
            "kind": "file",
            "name": "note.txt",
            "etag": f"e-{_hash(self.body)[:8]}",
            "file": {"size": len(self.body), "content_hash": _hash(self.body)},
        }
        yield {"id": SUB, "kind": "folder", "name": "sub"}


@pytest.fixture
def tree() -> Tree:
    return Tree(OLD)


@pytest.fixture
def http(tree: Tree) -> Iterator[httpx.Client]:
    def serve(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/content"):
            return httpx.Response(302, headers={"location": "https://content.test/note"})
        return httpx.Response(200, content=tree.body)

    with httpx.Client(transport=httpx.MockTransport(serve), base_url="https://api.test") as client:
        yield client


def _pull(tree: Tree, http: httpx.Client, root: Path, known: dict[bytes, Agreed]) -> Any:
    return pull(
        files=tree,
        http=http,
        root=root,
        source="",
        node_id=ROOT,
        drive_id=DRIVE,
        local_changes="keep_edits",
        known=known,
    )


def test_a_file_still_holding_its_agreed_bytes_takes_the_drives_newer_ones(
    tmp_path: Path, tree: Tree, http: httpx.Client
) -> None:
    root = tmp_path / "copy"
    first = _pull(tree, http, root, {})
    agreed = first.agreed[b"note.txt"]
    known = {b"note.txt": Agreed(agreed.node_id, agreed.content_hash, agreed.size)}

    tree.body = NEW
    second = _pull(tree, http, root, known)

    assert (root / "note.txt").read_bytes() == NEW
    assert second.kept_paths == []
    assert second.agreed[b"note.txt"].content_hash == _hash(NEW)


def test_a_file_edited_since_its_agreement_is_kept(
    tmp_path: Path, tree: Tree, http: httpx.Client
) -> None:
    root = tmp_path / "copy"
    first = _pull(tree, http, root, {})
    agreed = first.agreed[b"note.txt"]
    known = {b"note.txt": Agreed(agreed.node_id, agreed.content_hash, agreed.size)}
    (root / "note.txt").write_bytes(MINE)

    tree.body = NEW
    second = _pull(tree, http, root, known)

    assert (root / "note.txt").read_bytes() == MINE
    assert second.kept_paths == [b"note.txt"]
    assert b"note.txt" not in second.agreed


def test_a_differing_file_the_caller_never_agreed_is_kept(
    tmp_path: Path, tree: Tree, http: httpx.Client
) -> None:
    """A file made here under a name the drive also holds is somebody's work on
    both sides, so neither copy may silently win."""
    root = tmp_path / "copy"
    root.mkdir()
    (root / "note.txt").write_bytes(MINE)

    summary = _pull(tree, http, root, {})

    assert (root / "note.txt").read_bytes() == MINE
    assert summary.kept_paths == [b"note.txt"]


def test_plain_keep_still_keeps_a_stale_file(
    tmp_path: Path, tree: Tree, http: httpx.Client
) -> None:
    """The mount's policy is unchanged: with a single writer, any difference is an edit."""
    root = tmp_path / "copy"
    first = _pull(tree, http, root, {})
    agreed = first.agreed[b"note.txt"]
    known = {b"note.txt": Agreed(agreed.node_id, agreed.content_hash, agreed.size)}

    tree.body = NEW
    summary = pull(
        files=tree,
        http=http,
        root=root,
        source="",
        node_id=ROOT,
        drive_id=DRIVE,
        local_changes="keep",
        known=known,
    )

    assert (root / "note.txt").read_bytes() == OLD
    assert summary.kept_paths == [b"note.txt"]


def test_the_summary_names_every_folder_by_its_node(
    tmp_path: Path, tree: Tree, http: httpx.Client
) -> None:
    summary = _pull(tree, http, tmp_path / "copy", {})

    assert summary.folder_ids == {b"sub": SUB}
