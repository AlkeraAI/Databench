"""What a second pull of an unchanged tree costs, and what dedup may fuse.

Three claims, all read off a counting fake server and the disk it writes to:

* the pull short-circuits on the spelling the **real** wire uses. `Item`
  camel-cases its own keys, but a facet is a `VersionedModel` with no alias
  generator, so the hash arrives as `content_hash`. A client hard-coded to
  `contentHash` reads the empty default, decides every file differs and
  re-downloads the whole tree — silently, because the bytes it writes are
  correct;
* a facet with no hash at all is "I cannot tell", not "it differs" — an older
  server must not make `keep` refuse to write a file nobody touched;
* two files that merely share bytes are still two files. The dedup saves the
  second download and nothing else: fusing them into one inode would make
  restoring one's mtime rewrite the other's, and an edit to either show up in
  both.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
from alkera_cli.files.pull import pull

DRIVE = "11111111-1111-1111-1111-111111111111"
HOME = "cccccccc-0000-0000-0000-000000000001"
ALPHA = "cccccccc-0000-0000-0000-000000000002"
BETA = "cccccccc-0000-0000-0000-000000000003"
TWIN = "cccccccc-0000-0000-0000-000000000004"

ALPHA_BYTES = b"the first file's bytes\n"
BETA_BYTES = b"a second, different payload\n"
BODIES = {ALPHA: ALPHA_BYTES, BETA: BETA_BYTES, TWIN: ALPHA_BYTES}


def _hash(payload: bytes) -> str:
    from blake3 import blake3

    return str(blake3(payload).hexdigest())


DOWNLOADABLE: dict[str, Any] = {"capabilities": {"can_read": True, "can_download": True}}


def _file(node_id: str, name: str, payload: bytes, facet: Mapping[str, Any]) -> dict[str, Any]:
    return {"id": node_id, "kind": "file", "name": name, "file": dict(facet), **DOWNLOADABLE}


def _snake(payload: bytes) -> dict[str, Any]:
    """The facet exactly as the items routes serialize it."""
    return {
        "schema_version": "1.0.0",
        "metadata": {},
        "mime_type": "application/octet-stream",
        "size": len(payload),
        "content_hash": _hash(payload),
        "block_hash": None,
        "scan_state": "clean",
        "provider": "bytes",
    }


def _hashless(payload: bytes) -> dict[str, Any]:
    """What a server that never joined the head version answers with."""
    return {"size": len(payload), "content_hash": ""}


class FakeFiles:
    """The slice of ``client.files`` a pull drives, over a fixed tree."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def drive(self) -> dict[str, Any]:
        return {"id": DRIVE}

    def item(self, drive_id: str, item_id: str, **_: Any) -> dict[str, Any]:
        raise AssertionError("the pull under test resolves its root by path")

    def item_by_path(self, drive_id: str, item_path: str) -> dict[str, Any]:
        return {"id": HOME, "kind": "folder", "name": item_path, **DOWNLOADABLE}

    def children(self, drive_id: str, item_id: str, **_: Any) -> Iterator[dict[str, Any]]:
        if item_id == HOME:
            yield from self._rows


class ContentCounter:
    """Serves the content route and counts every fetch it is asked for."""

    def __init__(self) -> None:
        self.fetched: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/content"):
            node_id = path.rsplit("/", 2)[-2]
            self.fetched.append(node_id)
            return httpx.Response(302, headers={"location": f"https://content.test/c/{node_id}"})
        return httpx.Response(200, content=BODIES[path.rsplit("/", 1)[-1]])


@pytest.fixture
def counter() -> ContentCounter:
    return ContentCounter()


@pytest.fixture
def http(counter: ContentCounter) -> Iterator[httpx.Client]:
    with httpx.Client(
        transport=httpx.MockTransport(counter), base_url="https://api.test"
    ) as client:
        yield client


def test_a_second_pull_of_an_unchanged_tree_asks_for_no_bytes(
    tmp_path: Path, http: httpx.Client, counter: ContentCounter
) -> None:
    """The hash on the wire is `content_hash`, and the pull must read it there."""
    files = FakeFiles(
        [
            _file(ALPHA, "alpha.txt", ALPHA_BYTES, _snake(ALPHA_BYTES)),
            _file(BETA, "beta.txt", BETA_BYTES, _snake(BETA_BYTES)),
        ]
    )
    root = tmp_path / "local"

    first = pull(files=files, http=http, root=root, source="home")
    assert sorted(counter.fetched) == sorted([ALPHA, BETA])
    assert first.files == 2

    counter.fetched.clear()
    second = pull(files=files, http=http, root=root, source="home")

    assert counter.fetched == []
    assert second.unchanged == 2
    assert second.files == 0
    assert (root / "alpha.txt").read_bytes() == ALPHA_BYTES
    assert (root / "beta.txt").read_bytes() == BETA_BYTES


def test_a_facet_with_no_hash_is_not_read_as_a_local_edit(
    tmp_path: Path, http: httpx.Client, counter: ContentCounter
) -> None:
    """ "I cannot tell" must not become "someone edited this".

    A server that does not carry the hash makes the pull re-download — there is
    no other honest answer. But `local_changes="refuse"` exists to protect
    unsaved work, and treating a missing hash as a difference would abort every
    pull against such a server on a tree nobody has touched.
    """
    files = FakeFiles([_file(ALPHA, "alpha.txt", ALPHA_BYTES, _hashless(ALPHA_BYTES))])
    root = tmp_path / "local"

    pull(files=files, http=http, root=root, source="home")
    counter.fetched.clear()

    # Not raised, and not kept: the file is rewritten with the server's bytes.
    summary = pull(files=files, http=http, root=root, source="home", local_changes="refuse")

    assert counter.fetched == [ALPHA]
    assert summary.kept == 0
    assert (root / "alpha.txt").read_bytes() == ALPHA_BYTES


def test_two_files_that_share_bytes_stay_two_files(
    tmp_path: Path, http: httpx.Client, counter: ContentCounter
) -> None:
    """One download for the pair, but never one inode.

    The dedup is a network optimisation. A hard link would make it a semantic
    one: the two nodes would share a stat block, so the later `_restore_attrs`
    would overwrite the earlier one's mtime, and a subsequent edit to either
    path would appear at both.
    """
    files = FakeFiles(
        [
            _file(ALPHA, "alpha.txt", ALPHA_BYTES, _snake(ALPHA_BYTES)),
            _file(TWIN, "twin.txt", ALPHA_BYTES, _snake(ALPHA_BYTES)),
        ]
    )
    root = tmp_path / "local"

    pull(files=files, http=http, root=root, source="home")

    # One fetch for the pair — the second came off the disk.
    assert counter.fetched == [ALPHA]
    alpha = os.lstat(root / "alpha.txt")
    twin = os.lstat(root / "twin.txt")
    assert alpha.st_ino != twin.st_ino
    assert alpha.st_nlink == 1 and twin.st_nlink == 1

    # The proof that separateness is what matters: stamping one leaves the
    # other exactly where it was, and writing one does not rewrite the other.
    os.utime(root / "twin.txt", ns=(1_000_000_000, 1_000_000_000))
    assert os.lstat(root / "alpha.txt").st_mtime_ns != 1_000_000_000
    (root / "twin.txt").write_bytes(b"edited\n")
    assert (root / "alpha.txt").read_bytes() == ALPHA_BYTES
