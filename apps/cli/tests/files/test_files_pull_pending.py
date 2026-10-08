"""A file the drive lists whose bytes have not landed waits; the rest of the pull lands.

The content route answers ``409 files.live_pending`` for a file whose writer —
the machine holding the folder's live lease — has not synced its bytes. A box
that took a chat folder with one such file in it was refused the whole folder
for it, and the chat ran no turn. One unsynced file is that file's problem: the
pull names it, leaves its place empty, and lands everything else; the next pull
picks it up once the bytes are there.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from alkera_cli.files.pull import pull

DRIVE = "drive-1"
HOME = "home-1"
DOWNLOADABLE: dict[str, Any] = {"capabilities": {"can_read": True, "can_download": True}}
BODIES = {"n-early": b"early bytes", "n-late": b"late bytes"}
PENDING = {"code": "files.live_pending", "message": "not synced yet"}


def _hash(payload: bytes) -> str:
    from blake3 import blake3

    return str(blake3(payload).hexdigest())


def _file(node_id: str, name: str) -> dict[str, Any]:
    payload = BODIES[node_id]
    return {
        "id": node_id,
        "kind": "file",
        "name": name,
        "file": {"size": len(payload), "content_hash": _hash(payload)},
        "attrs": {"mode": 0o644},
        **DOWNLOADABLE,
    }


ROWS = [_file("n-early", "early.txt"), _file("n-late", "late.txt")]


class _Files:
    """The slice of ``client.files`` a pull drives, over a fixed two-file folder."""

    def drive(self) -> dict[str, Any]:
        return {"id": DRIVE}

    def item(self, drive_id: str, item_id: str, **_: Any) -> dict[str, Any]:
        raise AssertionError("the pull under test resolves its root by path")

    def item_by_path(self, drive_id: str, item_path: str) -> dict[str, Any]:
        return {"id": HOME, "kind": "folder", "name": item_path, **DOWNLOADABLE}

    def children(self, drive_id: str, item_id: str, **_: Any) -> Iterator[dict[str, Any]]:
        if item_id == HOME:
            yield from ROWS


class _ContentRoute:
    """The content route: a redirect to the bytes, or the drive's own refusal
    for the nodes in ``refusals`` (status and body, as the API answers)."""

    def __init__(self) -> None:
        self.refusals: dict[str, tuple[int, dict[str, Any]]] = {}
        self.fetched: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/content"):
            node_id = path.rsplit("/", 2)[-2]
            self.fetched.append(node_id)
            refused = self.refusals.get(node_id)
            if refused is not None:
                status, body = refused
                return httpx.Response(status, json=body)
            return httpx.Response(302, headers={"location": f"https://content.test/c/{node_id}"})
        return httpx.Response(200, content=BODIES[path.rsplit("/", 1)[-1]])


@pytest.fixture
def route() -> _ContentRoute:
    return _ContentRoute()


@pytest.fixture
def http(route: _ContentRoute) -> Iterator[httpx.Client]:
    with httpx.Client(transport=httpx.MockTransport(route), base_url="https://api.test") as client:
        yield client


def test_a_file_whose_bytes_have_not_landed_waits_and_the_rest_lands(
    tmp_path: Path, route: _ContentRoute, http: httpx.Client
) -> None:
    route.refusals["n-late"] = (409, PENDING)
    root = tmp_path / "root"

    summary = pull(files=_Files(), http=http, root=root, source="home")  # type: ignore[arg-type]

    assert (root / "early.txt").read_bytes() == b"early bytes"
    assert not (root / "late.txt").exists(), "nothing stands in for bytes that never came"
    assert not list(root.rglob("*.alkera-part")), "no half-file is left behind either"
    assert (summary.files, summary.pending, summary.pending_paths) == (1, 1, [b"late.txt"])
    (warning,) = summary.warnings
    assert "late.txt" in warning and "not landed" in warning
    assert "could not restore attributes" not in warning, (
        "a file that is not there has no attributes to restore, and is not said to"
    )

    # The bytes land on the drive; the next pull takes only what it lacks.
    route.refusals.clear()
    route.fetched.clear()
    again = pull(files=_Files(), http=http, root=root, source="home")  # type: ignore[arg-type]

    assert (root / "late.txt").read_bytes() == b"late bytes"
    assert route.fetched == ["n-late"], "the file that landed earlier is not fetched twice"
    assert (again.files, again.unchanged, again.pending, again.warnings) == (1, 1, 0, [])


@pytest.mark.parametrize(
    ("status", "body"),
    [
        pytest.param(409, {"code": "files.leased", "message": "held"}, id="another-409"),
        pytest.param(409, {"message": "no code at all"}, id="a-409-naming-no-code"),
        pytest.param(403, PENDING, id="a-403-wearing-the-pending-code"),
        pytest.param(500, {"code": "internal", "message": "boom"}, id="a-500"),
    ],
)
def test_any_other_refusal_of_a_files_bytes_still_ends_the_pull(
    tmp_path: Path, route: _ContentRoute, http: httpx.Client, status: int, body: dict[str, Any]
) -> None:
    """The tolerance is for the one answer that means "not yet", not for
    refusals: those still stop the pull rather than leave a silent hole."""
    route.refusals["n-late"] = (status, body)

    with pytest.raises(httpx.HTTPStatusError) as raised:
        pull(files=_Files(), http=http, root=tmp_path / "root", source="home")  # type: ignore[arg-type]
    assert raised.value.response.status_code == status


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a file whatever its mode")
@pytest.mark.parametrize(
    ("local", "listed_hash"),
    [
        pytest.param(b"x" * 4096, True, id="a-local-file-of-another-size"),
        pytest.param(b"same size!", False, id="a-node-with-no-hash-to-compare"),
    ],
)
def test_a_local_file_the_listing_already_says_differs_is_never_read(
    tmp_path: Path,
    route: _ContentRoute,
    http: httpx.Client,
    monkeypatch: pytest.MonkeyPatch,
    local: bytes,
    listed_hash: bool,
) -> None:
    """A take of the storm workspace spent 35 s hashing a 20 GB sparse file the
    drive had no bytes for, only to learn what its size already said. The
    local file here cannot be read at all, so a pull that reads it fails."""
    late = _file("n-late", "late.txt")
    if not listed_hash:
        late["file"] = {"size": len(local)}
    monkeypatch.setattr(sys.modules[__name__], "ROWS", [ROWS[0], late])
    route.refusals["n-late"] = (409, PENDING)
    root = tmp_path / "root"
    root.mkdir()
    (root / "late.txt").write_bytes(local)
    (root / "late.txt").chmod(0)

    summary = pull(files=_Files(), http=http, root=root, source="home")  # type: ignore[arg-type]

    assert summary.pending_paths == [b"late.txt"]
    assert (root / "early.txt").read_bytes() == b"early bytes"
