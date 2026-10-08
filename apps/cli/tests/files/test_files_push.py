"""``alkera files push`` against a real backend.

Every assertion here is about what the *server* ended up holding, or about
what the server was *asked* to do — never about what the push called on a
mock. The zero-byte re-push in particular is only meaningful as a count of
upload requests the backend actually received.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from alkera_cli.commands.files import FILES_TRANSFER_TIMEOUT
from alkera_cli.files.push import (
    AgreedBase,
    CheckpointKilled,
    SourceUnreadableError,
    push,
    push_paths,
    push_state_path,
)
from alkera_core.files.hashing import hash_bytes
from alkera_sdk import AlkeraClient
from files._live_backend import LiveBackend, home_path, live_backend

#: Every case boots its own backend on an ephemeral port, seeds its own org and
#: pushes into the drive that org owns, so no case reads what another left on the
#: server and the sixteen boots may happen on sixteen workers at once.
pytestmark = [pytest.mark.spread]

#: Path fragments that mean "bytes moved": a part PUT or an upload session.
_UPLOAD = ("/uploads",)


@pytest.fixture
def backend(tmp_path: Path) -> Iterator[LiveBackend]:
    with live_backend(tmp_path / "server") as running:
        yield running


@pytest.fixture
def client(backend: LiveBackend) -> Iterator[AlkeraClient]:
    with AlkeraClient(
        base_url=backend.base_url, token=backend.token, timeout=FILES_TRANSFER_TIMEOUT
    ) as api:
        yield api


def _tree(root: Path) -> Path:
    """A small tree that still carries every shape the push branches on."""
    root.mkdir(parents=True)
    (root / "papers").mkdir()
    (root / "papers" / "note.txt").write_bytes(b"note bytes\n")
    (root / "top.txt").write_bytes(b"top\n")
    os.symlink("../top.txt", root / "papers" / "link")
    (root / "empty").mkdir()
    return root


def _run(client: AlkeraClient, root: Path, dest: str, home: Path, **kwargs: Any) -> Any:
    return push(
        files=client.files,
        http=client.raw_client.get_httpx_client(),
        root=root,
        dest=dest,
        home=home,
        **kwargs,
    )


def test_push_creates_every_node_with_its_kind_and_attrs(
    client: AlkeraClient, backend: LiveBackend, tmp_path: Path
) -> None:
    """The tree lands: folders, bytes, and a symlink recorded as a link."""
    root = _tree(tmp_path / "tree")
    mtime = 915_148_800_000_000_000  # 1999-01-01, the corpus's old timestamp
    os.utime(root / "top.txt", ns=(mtime, mtime))
    proj = home_path(client, "proj")

    summary = _run(client, root, proj, tmp_path / "home")

    assert summary.uploaded == 2
    assert summary.symlinks == 1

    drive_id = str(client.files.drive()["id"])
    note = client.files.item_by_path(drive_id, f"{proj}/papers/note.txt")
    assert note["kind"] == "file"
    assert note["file"]["size"] == len(b"note bytes\n")

    top = client.files.item_by_path(drive_id, f"{proj}/top.txt")
    # The attrs facet reads back as a datetime (`AttrsFacet.mtime`) even though
    # the PATCH body spells it in nanoseconds (`AttrsPatch.mtime_ns`).
    restored = datetime.fromisoformat(str(top["attrs"]["mtime"]))
    assert int(restored.timestamp() * 1_000_000_000) == mtime

    link = client.files.item_by_path(drive_id, f"{proj}/papers/link")
    assert link["kind"] == "symlink"
    assert link["symlink"]["target"] == "../top.txt"

    assert client.files.item_by_path(drive_id, f"{proj}/empty")["kind"] == "folder"


def test_second_push_of_an_unchanged_tree_uploads_zero_bytes(
    client: AlkeraClient, backend: LiveBackend, tmp_path: Path
) -> None:
    """The acceptance criterion, counted at the server.

    Delete the ``contentHash``/``size`` comparison in ``push`` and the second
    run opens a session per file again, which this count catches.
    """
    root = _tree(tmp_path / "tree")
    home = tmp_path / "home"
    proj = home_path(client, "proj")

    first = _run(client, root, proj, home)
    assert first.uploaded == 2
    assert backend.log.count(*_UPLOAD) > 0

    backend.log.clear()
    second = _run(client, root, proj, home)

    assert second.uploaded == 0
    assert second.unchanged == 2
    assert second.bytes_uploaded == 0
    assert backend.log.count(*_UPLOAD) == 0


def test_changed_bytes_are_re_uploaded(
    client: AlkeraClient, backend: LiveBackend, tmp_path: Path
) -> None:
    """The negative twin: dedup must not be "skip everything that exists"."""
    root = _tree(tmp_path / "tree")
    home = tmp_path / "home"
    proj = home_path(client, "proj")
    _run(client, root, proj, home)

    (root / "top.txt").write_bytes(b"different bytes\n")
    backend.log.clear()
    again = _run(client, root, proj, home)

    assert again.uploaded == 1
    assert again.unchanged == 1
    drive_id = str(client.files.drive()["id"])
    top = client.files.item_by_path(drive_id, f"{proj}/top.txt")
    assert top["file"]["size"] == len(b"different bytes\n")


def test_a_killed_push_resumes_from_the_persisted_session(
    client: AlkeraClient, backend: LiveBackend, tmp_path: Path
) -> None:
    """A push killed after its first part finishes from the same session.

    The resumed run must not re-send the part the server already stored, and
    must reuse the session id the first run persisted — proven by reading the
    state file before the resume and counting part PUTs after it.
    """
    root = tmp_path / "big"
    root.mkdir()
    payload = os.urandom(9 * 1024 * 1024)
    (root / "blob.bin").write_bytes(payload)
    home = tmp_path / "home"
    proj = home_path(client, "proj")

    seen: list[tuple[str, int]] = []

    def kill_after_first_part(key: str, part_no: int) -> None:
        seen.append((key, part_no))
        if part_no == 1:
            raise CheckpointKilled(key)

    with pytest.raises(CheckpointKilled):
        _run(client, root, proj, home, checkpoint=kill_after_first_part)

    state_file = push_state_path(root, proj, home=home)
    remembered = json.loads(state_file.read_text(encoding="utf-8"))["sessions"]["blob.bin"]
    session_id = remembered["uploadId"]
    assert client.files.upload_status(session_id)["acceptedParts"] == [1]

    parts_before = backend.log.count("/parts/")
    summary = _run(client, root, proj, home)

    sent = backend.log.count("/parts/") - parts_before
    assert sent < 3  # the first part is not re-sent
    assert summary.uploaded == 1
    drive_id = str(client.files.drive()["id"])
    assert client.files.item_by_path(drive_id, f"{proj}/blob.bin")["file"]["size"] == len(payload)
    assert json.loads(state_file.read_text(encoding="utf-8"))["sessions"] == {}


def test_a_session_the_server_aborted_is_replaced_once_and_the_file_lands(
    client: AlkeraClient, backend: LiveBackend, tmp_path: Path
) -> None:
    """A remembered session the server aborted answers the commit ``409
    files.session_state`` on every replay, so resuming it never lands — the
    drain on the demo box replayed one until its ceiling. The push forgets it
    and goes again through one fresh session: the file lands on this pass, and
    as ONE node, not a second copy beside a first."""
    root = tmp_path / "big"
    root.mkdir()
    payload = os.urandom(9 * 1024 * 1024)
    (root / "blob.bin").write_bytes(payload)
    home = tmp_path / "home"
    proj = home_path(client, "proj")

    def kill_after_first_part(key: str, part_no: int) -> None:
        if part_no == 1:
            raise CheckpointKilled(key)

    with pytest.raises(CheckpointKilled):
        _run(client, root, proj, home, checkpoint=kill_after_first_part)
    state_file = push_state_path(root, proj, home=home)
    aborted = json.loads(state_file.read_text(encoding="utf-8"))["sessions"]["blob.bin"]
    client.files.abort_upload(aborted["uploadId"])

    summary = _run(client, root, proj, home)

    assert summary.uploaded == 1
    drive_id = str(client.files.drive()["id"])
    landed = client.files.item_by_path(drive_id, f"{proj}/blob.bin")
    assert landed["file"]["size"] == len(payload)
    parent = client.files.item_by_path(drive_id, proj)
    names = [child["name"] for child in client.files.children(drive_id, str(parent["id"]))]
    assert names == ["blob.bin"]
    assert json.loads(state_file.read_text(encoding="utf-8"))["sessions"] == {}


def test_gitignore_is_honoured_in_a_repo_and_off_outside_one(
    client: AlkeraClient, backend: LiveBackend, tmp_path: Path
) -> None:
    """Same tree, two flags: the ignored file is absent, then present."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    (root / ".gitignore").write_text("build/\n", encoding="utf-8")
    (root / "build").mkdir()
    (root / "build" / "out.o").write_bytes(b"object\n")
    (root / "keep.txt").write_bytes(b"keep\n")

    on = home_path(client, "on")
    off = home_path(client, "off")
    _run(client, root, on, tmp_path / "home-on")
    drive_id = str(client.files.drive()["id"])
    with pytest.raises(RuntimeError, match="404"):
        client.files.item_by_path(drive_id, f"{on}/build/out.o")
    assert client.files.item_by_path(drive_id, f"{on}/keep.txt")["kind"] == "file"

    _run(client, root, off, tmp_path / "home-off", respect_gitignore=False)
    assert client.files.item_by_path(drive_id, f"{off}/build/out.o")["kind"] == "file"


def test_the_exclude_preset_drops_node_modules(
    client: AlkeraClient, backend: LiveBackend, tmp_path: Path
) -> None:
    """Opt-in, and only for the preset asked for: ``src`` still lands."""
    root = tmp_path / "app"
    root.mkdir()
    (root / "node_modules").mkdir()
    (root / "node_modules" / "dep.js").write_bytes(b"dep\n")
    (root / "src").mkdir()
    (root / "src" / "index.js").write_bytes(b"src\n")

    app = home_path(client, "app")
    summary = _run(client, root, app, tmp_path / "home", exclude_presets=("node_modules",))

    assert summary.uploaded == 1
    drive_id = str(client.files.drive()["id"])
    assert client.files.item_by_path(drive_id, f"{app}/src/index.js")["kind"] == "file"
    with pytest.raises(RuntimeError, match="404"):
        client.files.item_by_path(drive_id, f"{app}/node_modules/dep.js")


def test_a_pointer_file_is_skipped_with_a_warning(
    client: AlkeraClient, backend: LiveBackend, tmp_path: Path
) -> None:
    """A modified pointer is a warning, never a version."""
    root = tmp_path / "tree"
    root.mkdir()
    (root / "design.alkerachat").write_bytes(b'{"nodeId": "edited by hand"}')
    (root / "real.txt").write_bytes(b"real\n")
    # A pointer is identified by its whole extension, not by containing the
    # word: an ordinary file whose name merely starts with one of them is the
    # user's bytes and must be uploaded.
    (root / "notes.alkerachatter").write_bytes(b"mine\n")
    proj = home_path(client, "proj")

    summary = _run(client, root, proj, tmp_path / "home")

    assert summary.pointers_skipped == 1
    assert summary.uploaded == 2
    assert any("pointer" in warning for warning in summary.warnings)
    drive_id = str(client.files.drive()["id"])
    assert client.files.item_by_path(drive_id, f"{proj}/notes.alkerachatter")["kind"] == "file"
    with pytest.raises(RuntimeError, match="404"):
        client.files.item_by_path(drive_id, f"{proj}/design.alkerachat")


class _StreamOnly:
    """A file handle that refuses to hand back a whole file at once.

    ``read()`` with no size is how a caller slurps a file into memory, and it
    is exactly what a push of a multi-gigabyte checkpoint must never do. Every
    sized read is passed straight through, so a streaming caller cannot tell
    this apart from the real handle.
    """

    def __init__(self, handle: Any) -> None:
        self._handle = handle

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            raise AssertionError("the push read a whole file into memory")
        return bytes(self._handle.read(size))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._handle, name)

    def __enter__(self) -> _StreamOnly:
        self._handle.__enter__()
        return self

    def __exit__(self, *exc: Any) -> Any:
        return self._handle.__exit__(*exc)


@pytest.fixture
def stream_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every binary read in the push prove it is bounded."""
    real_open = Path.open
    real_fdopen = os.fdopen

    def guarded(self: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        handle = real_open(self, mode, *args, **kwargs)
        return _StreamOnly(handle) if "b" in mode and "r" in mode else handle

    def guarded_fdopen(fd: int, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        # A push source is opened as a descriptor (no link followed below the
        # push root) and wrapped here, so this is where its reads start.
        handle = real_fdopen(fd, mode, *args, **kwargs)
        return _StreamOnly(handle) if "b" in mode and "r" in mode else handle

    monkeypatch.setattr(Path, "open", guarded)
    monkeypatch.setattr(os, "fdopen", guarded_fdopen)


def test_a_replace_over_the_single_put_cap_streams_through_an_upload_session(
    client: AlkeraClient, backend: LiveBackend, tmp_path: Path, stream_only: None
) -> None:
    """The bug: a file past the content-PUT cap had no way to be replaced.

    A single-call PUT of 40 MiB is refused by the server outright, so the only
    path to new bytes on an existing node is the session — committed with
    ``replace`` so it lands a version on that node rather than a second file
    beside it. The handle guard proves the 40 MiB never sat in memory.
    """
    root = tmp_path / "tree"
    root.mkdir(parents=True)
    big = root / "checkpoint.bin"
    big.write_bytes(b"a" * (40 * 1024 * 1024))
    home = tmp_path / "home"
    cap = 1 << 20
    proj = home_path(client, "proj")

    _run(client, root, proj, home, single_put_max_bytes=cap)
    drive_id = str(client.files.drive()["id"])
    before = client.files.item_by_path(drive_id, f"{proj}/checkpoint.bin")

    big.write_bytes(b"b" * (40 * 1024 * 1024))
    backend.log.clear()
    again = _run(client, root, proj, home, single_put_max_bytes=cap)

    assert again.uploaded == 1
    assert backend.log.count(*_UPLOAD) > 0, "the replacement went through the session"
    after = client.files.item_by_path(drive_id, f"{proj}/checkpoint.bin")
    assert after["id"] == before["id"], "the version landed on the node that held the name"
    assert after["file"]["size"] == 40 * 1024 * 1024
    versions = client.raw_client.get_httpx_client().get(
        f"/api/v1/files/drives/{drive_id}/items/{after['id']}/versions"
    )
    assert versions.status_code == 200, versions.text
    assert len(versions.json()["versions"]) == 2, "a second version, not a second node"


def test_a_replace_within_the_cap_still_goes_through_the_content_put(
    client: AlkeraClient, backend: LiveBackend, tmp_path: Path
) -> None:
    """The asymmetric half: the session is for the *large* case only.

    Routing every replacement through a session would cost three round trips
    where one does, so a file inside the cap must still take the single call.
    """
    root = tmp_path / "tree"
    root.mkdir(parents=True)
    (root / "small.txt").write_bytes(b"first\n")
    home = tmp_path / "home"
    proj = home_path(client, "proj")

    _run(client, root, proj, home)
    (root / "small.txt").write_bytes(b"second\n")
    backend.log.clear()
    again = _run(client, root, proj, home)

    assert again.uploaded == 1
    assert backend.log.count(*_UPLOAD) == 0, "a small replacement opened no session"
    drive_id = str(client.files.drive()["id"])
    assert client.files.item_by_path(drive_id, f"{proj}/small.txt")["file"]["size"] == 7


def _run_paths(
    client: AlkeraClient, root: Path, dest: str, home: Path, paths: list[Path], **kwargs: Any
) -> Any:
    return push_paths(
        files=client.files,
        http=client.raw_client.get_httpx_client(),
        root=root,
        dest=dest,
        home=home,
        paths=paths,
        **kwargs,
    )


def test_push_paths_moves_the_named_path_and_nothing_else(
    client: AlkeraClient, backend: LiveBackend, tmp_path: Path
) -> None:
    """One file out of a tree lands — with the folders it needs, and no sibling.

    This is the holder's single-file push: the agent wrote one file a second
    ago and the user is watching for it, so pushing the other nine is both
    slower and a lie about what changed. Drop the selection and ``top.txt``
    appears here too, which the 404 catches.
    """
    root = _tree(tmp_path / "tree")
    proj = home_path(client, "proj")

    summary = _run_paths(client, root, proj, tmp_path / "home", [root / "papers" / "note.txt"])

    assert summary.uploaded == 1
    drive_id = str(client.files.drive()["id"])
    note = client.files.item_by_path(drive_id, f"{proj}/papers/note.txt")
    assert note["file"]["size"] == len(b"note bytes\n")
    with pytest.raises(RuntimeError, match="404"):
        client.files.item_by_path(drive_id, f"{proj}/top.txt")
    with pytest.raises(RuntimeError, match="404"):
        client.files.item_by_path(drive_id, f"{proj}/empty")


def test_push_paths_takes_a_path_relative_to_the_root_and_refuses_one_outside_it(
    client: AlkeraClient, backend: LiveBackend, tmp_path: Path
) -> None:
    """A relative spelling is the same file; a path outside the root is refused.

    The holder names what its watcher reported, in whatever spelling the
    watcher uses. Accepting one outside the root would push a directory the
    caller never asked to share.
    """
    root = _tree(tmp_path / "tree")
    outside = tmp_path / "elsewhere.txt"
    outside.write_bytes(b"not yours\n")
    proj = home_path(client, "proj")

    summary = _run_paths(client, root, proj, tmp_path / "home", [Path("top.txt")])

    assert summary.uploaded == 1
    drive_id = str(client.files.drive()["id"])
    assert client.files.item_by_path(drive_id, f"{proj}/top.txt")["kind"] == "file"

    with pytest.raises(ValueError, match="is not under"):
        _run_paths(client, root, proj, tmp_path / "home", [outside])


def test_push_paths_sends_nothing_for_bytes_the_server_already_holds(
    client: AlkeraClient, backend: LiveBackend, tmp_path: Path
) -> None:
    """The dedup comparison is the shared one, counted at the server.

    A live holder re-reports a path on every ``touch``; if the narrowed push
    skipped the hash comparison it would re-upload the same bytes each time.
    """
    root = _tree(tmp_path / "tree")
    home = tmp_path / "home"
    proj = home_path(client, "proj")
    _run(client, root, proj, home)

    backend.log.clear()
    again = _run_paths(client, root, proj, home, [root / "top.txt"])

    assert again.uploaded == 0
    assert again.unchanged == 1
    assert backend.log.count(*_UPLOAD) == 0

    (root / "top.txt").write_bytes(b"second\n")
    changed = _run_paths(client, root, proj, home, [root / "top.txt"])
    assert changed.uploaded == 1
    drive_id = str(client.files.drive()["id"])
    assert client.files.item_by_path(drive_id, f"{proj}/top.txt")["file"]["size"] == 7


def test_push_paths_finishes_the_session_a_killed_push_left_open(
    client: AlkeraClient, backend: LiveBackend, tmp_path: Path
) -> None:
    """The two entry points share one resume state file.

    A push killed mid-part persists its session under the ``(root, dest)``
    digest; the narrowed push must read that same file, or it opens a second
    session and re-sends the part the server already stored. Proven by the
    session id it finishes and by the part count.
    """
    root = tmp_path / "big"
    root.mkdir()
    payload = os.urandom(9 * 1024 * 1024)
    (root / "blob.bin").write_bytes(payload)
    home = tmp_path / "home"
    proj = home_path(client, "proj")

    def kill_after_first_part(key: str, part_no: int) -> None:
        if part_no == 1:
            raise CheckpointKilled(key)

    with pytest.raises(CheckpointKilled):
        _run(client, root, proj, home, checkpoint=kill_after_first_part)

    state_file = push_state_path(root, proj, home=home)
    session_id = json.loads(state_file.read_text(encoding="utf-8"))["sessions"]["blob.bin"][
        "uploadId"
    ]
    assert client.files.upload_status(session_id)["acceptedParts"] == [1]

    parts_before = backend.log.count("/parts/")
    summary = _run_paths(client, root, proj, home, [root / "blob.bin"])

    assert backend.log.count("/parts/") - parts_before < 3, "the stored part was not re-sent"
    assert summary.uploaded == 1
    drive_id = str(client.files.drive()["id"])
    assert client.files.item_by_path(drive_id, f"{proj}/blob.bin")["file"]["size"] == len(payload)
    assert json.loads(state_file.read_text(encoding="utf-8"))["sessions"] == {}


def test_push_paths_reports_a_named_path_that_is_no_longer_there(
    client: AlkeraClient, backend: LiveBackend, tmp_path: Path
) -> None:
    """A path deleted between the change and the flush is a warning, not a crash."""
    root = _tree(tmp_path / "tree")
    proj = home_path(client, "proj")

    summary = _run_paths(
        client, root, proj, tmp_path / "home", [root / "top.txt", root / "vanished.txt"]
    )

    assert summary.uploaded == 1
    assert any("vanished.txt" in warning for warning in summary.warnings)


def test_a_skipped_path_is_never_uploaded_by_the_whole_tree_push(
    client: AlkeraClient, backend: LiveBackend, tmp_path: Path
) -> None:
    """A path the holder already trashed stays trashed.

    The bytes are still on disk when the union push walks the tree, so without
    ``skip`` the push uploads them again and the file the user deleted comes
    back. Its siblings must still land — a skip is one path, not a stop.
    """
    root = _tree(tmp_path / "tree")
    proj = home_path(client, "proj")

    summary = _run(client, root, proj, tmp_path / "home", skip={"papers/note.txt"})

    assert summary.uploaded == 1
    drive_id = str(client.files.drive()["id"])
    assert client.files.item_by_path(drive_id, f"{proj}/top.txt")["kind"] == "file"
    with pytest.raises(RuntimeError, match="404"):
        client.files.item_by_path(drive_id, f"{proj}/papers/note.txt")


def test_a_skipped_folder_takes_everything_under_it(
    client: AlkeraClient, backend: LiveBackend, tmp_path: Path
) -> None:
    """Trashing a folder trashes its children, so pushing them back resurrects it.

    The asymmetric half: a name that merely *starts* with a skipped one
    (``papers2``) is a different folder and still lands.
    """
    root = _tree(tmp_path / "tree")
    (root / "papers2").mkdir()
    (root / "papers2" / "kept.txt").write_bytes(b"kept\n")
    proj = home_path(client, "proj")

    summary = _run(client, root, proj, tmp_path / "home", skip=["papers"])

    assert summary.uploaded == 2  # top.txt and papers2/kept.txt
    assert summary.symlinks == 0  # the recorded link lived under papers/
    drive_id = str(client.files.drive()["id"])
    assert client.files.item_by_path(drive_id, f"{proj}/papers2/kept.txt")["kind"] == "file"
    for gone in ("papers", "papers/note.txt", "papers/link"):
        with pytest.raises(RuntimeError, match="404"):
            client.files.item_by_path(drive_id, f"{proj}/{gone}")


def test_a_push_anchored_on_a_folder_lands_under_the_directory_dest_names(
    client: AlkeraClient, backend: LiveBackend, tmp_path: Path
) -> None:
    """A holder that anchors on one node and pushes a directory INSIDE it.

    That pair is what a box streaming a chat sends: the lease is on the chat
    folder, so the id it anchors on is the folder's, while the bytes belong in
    the working directory one level down and ``dest`` says so. The id says
    where the walk may START; it must not decide where a file lands, or every
    file the agent writes is filed one level too high — and the per-file lookup
    that decides create-versus-replace then looks where the file is not, so the
    rewrite is sent as a create and refused because the name is taken.

    The rewrite is the half that fails loudest: a second write of a name the
    drive already holds has to land as a new VERSION of that node (the etag
    moves), not as a second node beside it or a refusal.
    """
    anchor = home_path(client, "anchor")
    seed = tmp_path / "seed"
    (seed / "work").mkdir(parents=True)
    (seed / "work" / "report.html").write_bytes(b"first")
    home = tmp_path / "home"
    _run(client, seed, anchor, home)

    drive_id = str(client.files.drive()["id"])
    anchor_id = str(client.files.item_by_path(drive_id, anchor)["id"])
    landed = client.files.item_by_path(drive_id, f"{anchor}/work/report.html")

    work = seed / "work"
    (work / "report.html").write_bytes(b"second bytes")
    again = _run_paths(
        client, work, f"{anchor}/work", home, [work / "report.html"], node_id=anchor_id
    )

    assert again.uploaded == 1, "the rewrite was never sent"
    after = client.files.item_by_path(drive_id, f"{anchor}/work/report.html")
    assert after["id"] == landed["id"], "the rewrite minted a second node instead of a version"
    assert after["etag"] != landed["etag"], "the rewrite did not move the etag"
    assert after["file"]["size"] == len(b"second bytes")
    with pytest.raises(RuntimeError, match="404"):
        client.files.item_by_path(drive_id, f"{anchor}/report.html")


class _TreeMissesAFolder(httpx.BaseTransport):
    """A drive whose FIRST ``tree`` call does not make one of the folders asked.

    Not a stub of the push: the request goes to the real server and the answer
    is the server's, with one path taken out of the body on the way. That is
    what a skeleton the route did not complete looks like from the client —
    the state the box's push met — and it is the only way to produce it
    without a second writer racing the request. Only the first call is made
    short, so the answer to a folder the push asks for again is the whole one.
    """

    def __init__(self, inner: httpx.BaseTransport, drop: str) -> None:
        self._inner = inner
        self._drop = drop
        self.dropped = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if not self.dropped and request.method == "POST" and request.url.path.endswith("/tree"):
            body = json.loads(request.read() or b"{}")
            asked = list(body.get("paths", []))
            kept = [path for path in asked if path != self._drop]
            if len(kept) != len(asked):
                self.dropped += 1
                headers = {
                    name: value
                    for name, value in request.headers.items()
                    if name.lower() not in {"content-length", "content-type"}
                }
                request = httpx.Request("POST", request.url, headers=headers, json={"paths": kept})
        return self._inner.handle_request(request)


def _trash(client: AlkeraClient, drive_id: str, item_path: str) -> None:
    """Take the node at ``item_path`` away, the way a live sync does."""
    http = client.raw_client.get_httpx_client()
    item = client.files.item_by_path(drive_id, item_path)
    response = http.request(
        "DELETE",
        f"/api/v1/files/drives/{drive_id}/items/{item['id']}",
        headers={"Idempotency-Key": os.urandom(8).hex(), "If-Match": str(item["etag"])},
    )
    response.raise_for_status()


def test_a_folder_taken_away_after_the_skeleton_is_remade_and_the_file_lands(
    client: AlkeraClient, backend: LiveBackend, tmp_path: Path
) -> None:
    """The skeleton is a snapshot, not a guarantee — the push must not abort.

    A box pushes a chat folder while its own live sync trashes the paths the
    agent deleted, so a folder the skeleton made can be gone again by the time
    the push reaches the file that needs it. The push used to read that as an
    impossible state and raise, which took the whole folder down with it: the
    live incident was ``the skeleton is missing
    'home/.../.alkerachat/.runtime/agent'`` on every checkpoint push, and the
    chat's folder was never brought up to date.
    """
    root = tmp_path / "tree"
    (root / ".runtime" / "agent").mkdir(parents=True)
    (root / ".runtime" / "agent" / "state.json").write_bytes(b'{"session":"s1"}\n')
    (root / "manifest.json").write_bytes(b"{}\n")
    proj = home_path(client, "chat")
    drive_id = str(client.files.drive()["id"])

    taken: list[bytes] = []

    def take_the_folder_away(relative: bytes) -> None:
        # The first file the push reaches is the one under that folder, so the
        # folder goes between the skeleton that made it and the upload that
        # needs it — the window the live sync writes in.
        if taken:
            return
        taken.append(relative)
        _trash(client, drive_id, f"{proj}/.runtime/agent")

    summary = _run(client, root, proj, tmp_path / "home", progress=take_the_folder_away)

    assert taken == [b".runtime/agent/state.json"], "the race never happened"
    assert summary.uploaded == 2, "a file the push had to send never landed"
    landed = client.files.item_by_path(drive_id, f"{proj}/.runtime/agent/state.json")
    assert landed["file"]["size"] == len(b'{"session":"s1"}\n')


def test_a_file_gone_between_the_walk_and_the_push_is_skipped_with_a_warning(
    client: AlkeraClient, tmp_path: Path
) -> None:
    """The walk is a snapshot too. The agent's log rotates under the harness's
    ``.runtime/agent/log`` while a checkpoint push runs, so a file the walk
    listed can be gone by the time the push reaches it; that used to fail the
    whole folder's checkpoint. There is nothing to send for it: the push says
    so in a warning that names it and sends everything else."""
    root = tmp_path / "tree"
    (root / ".runtime" / "agent" / "log").mkdir(parents=True)
    (root / ".runtime" / "agent" / "log" / "old.log").write_bytes(b"rotated away\n")
    (root / "kept.txt").write_bytes(b"kept\n")
    (root / "also-kept.txt").write_bytes(b"also kept\n")
    proj = home_path(client, "chat")
    drive_id = str(client.files.drive()["id"])

    def rotate(relative: bytes) -> None:
        if relative == b".runtime/agent/log/old.log":
            (root / ".runtime" / "agent" / "log" / "old.log").unlink()

    summary = _run(client, root, proj, tmp_path / "home", progress=rotate)

    assert summary.uploaded == 2
    assert summary.warnings == [
        "skipped .runtime/agent/log/old.log: it was gone before the push read it"
    ]
    assert client.files.item_by_path(drive_id, f"{proj}/kept.txt")["file"]["size"] == 5
    assert client.files.item_by_path(drive_id, f"{proj}/also-kept.txt")["file"]["size"] == 10
    with pytest.raises(RuntimeError, match="404"):
        client.files.item_by_path(drive_id, f"{proj}/.runtime/agent/log/old.log")


@pytest.mark.skipif(
    sys.platform == "win32" or getattr(os, "geteuid", lambda: 1)() == 0,
    reason="a file the process may not read needs POSIX modes and a non-root reader",
)
def test_a_file_the_push_may_not_read_still_fails_the_push_by_name(
    client: AlkeraClient, tmp_path: Path
) -> None:
    """Gone is the only reason to pass a file by. One the push cannot read is
    still there with bytes the folder is owed, and silently sending the rest
    would report a checkpoint that left work behind."""
    root = tmp_path / "tree"
    root.mkdir()
    (root / "kept.txt").write_bytes(b"kept\n")
    sealed = root / "sealed.txt"
    sealed.write_bytes(b"secret\n")
    sealed.chmod(0)
    try:
        with pytest.raises(SourceUnreadableError, match=r"sealed\.txt"):
            _run(client, root, home_path(client, "chat"), tmp_path / "home")
    finally:
        sealed.chmod(0o600)


def test_a_folder_the_skeleton_did_not_make_still_takes_the_files_under_it(
    client: AlkeraClient, backend: LiveBackend, tmp_path: Path
) -> None:
    """A skeleton answer short of one folder costs that folder, not the push.

    The push asks for the whole skeleton in one ``tree`` call and never reads
    which folders came back, so a path the route did not make is found only
    later, by the file that needed it. Everything the uploader would have sent
    still lands.
    """
    root = tmp_path / "tree"
    (root / ".runtime" / "agent").mkdir(parents=True)
    (root / ".runtime" / "agent" / "state.json").write_bytes(b'{"session":"s2"}\n')
    (root / "notes.txt").write_bytes(b"notes\n")
    proj = home_path(client, "chat")
    drive_id = str(client.files.drive()["id"])

    real = client.raw_client.get_httpx_client()
    transport = _TreeMissesAFolder(httpx.HTTPTransport(), drop=".runtime/agent")
    with httpx.Client(
        base_url=str(real.base_url),
        headers=real.headers,
        timeout=FILES_TRANSFER_TIMEOUT,
        transport=transport,
    ) as partial:
        summary = push(
            files=client.files, http=partial, root=root, dest=proj, home=tmp_path / "home"
        )

    assert transport.dropped == 1, "the skeleton was never made short"
    assert summary.uploaded == 2
    landed = client.files.item_by_path(drive_id, f"{proj}/.runtime/agent/state.json")
    assert landed["file"]["size"] == len(b'{"session":"s2"}\n')
    assert client.files.item_by_path(drive_id, f"{proj}/.runtime/agent")["kind"] == "folder"


def _digest(data: bytes) -> str:
    return hash_bytes(data).content_hash.hex()


def test_a_path_fenced_on_its_agreed_base_never_writes_over_a_head_it_did_not_see(
    client: AlkeraClient, backend: LiveBackend, tmp_path: Path
) -> None:
    """A holder's upload names the etag its disk's bytes were agreed at. When
    another writer moved the head since, the precondition is that older etag --
    the drive settles it for the lease's holder, and refuses it for anyone else
    -- and the retry on a refusal fences the same way rather than on the head it
    just re-read. Before the base existed the push fenced on whatever etag it
    read and wrote straight over the other writer's version."""
    root = tmp_path / "tree"
    root.mkdir()
    home = tmp_path / "home"
    proj = home_path(client, "proj")
    drive_id = str(client.files.drive()["id"])
    (root / "a.txt").write_bytes(b"agreed\n")
    first = _run_paths(client, root, proj, home, [root / "a.txt"])
    agreed = first.agreed["a.txt"]
    assert agreed == str(client.files.item_by_path(drive_id, f"{proj}/a.txt")["etag"])

    # Somebody else's write moves the head on.
    (root / "a.txt").write_bytes(b"theirs\n")
    _run_paths(client, root, proj, home, [root / "a.txt"])
    theirs = client.files.item_by_path(drive_id, f"{proj}/a.txt")
    assert str(theirs["etag"]) != agreed

    (root / "a.txt").write_bytes(b"mine, grown from agreed\n")
    with pytest.raises(httpx.HTTPStatusError) as refused:
        _run_paths(
            client,
            root,
            proj,
            home,
            [root / "a.txt"],
            bases={"a.txt": AgreedBase(etag=agreed, content_hash=_digest(b"agreed\n"))},
        )
    assert refused.value.response.status_code == 412
    after = client.files.item_by_path(drive_id, f"{proj}/a.txt")
    assert after["etag"] == theirs["etag"], "the other writer's version is still the head"


def test_a_base_whose_bytes_are_still_the_head_fences_on_the_head(
    client: AlkeraClient, backend: LiveBackend, tmp_path: Path
) -> None:
    """The asymmetric case: an etag can move without the bytes moving (a
    rename, an attribute), and that is no conflict. While the head still holds
    the agreed bytes the write fences on the head and lands, and the summary
    names the etag the new bytes were filed at."""
    root = tmp_path / "tree"
    root.mkdir()
    home = tmp_path / "home"
    proj = home_path(client, "proj")
    drive_id = str(client.files.drive()["id"])
    (root / "a.txt").write_bytes(b"agreed\n")
    _run_paths(client, root, proj, home, [root / "a.txt"])

    (root / "a.txt").write_bytes(b"next\n")
    summary = _run_paths(
        client,
        root,
        proj,
        home,
        [root / "a.txt"],
        bases={"a.txt": AgreedBase(etag="0", content_hash=_digest(b"agreed\n"))},
    )

    assert summary.uploaded == 1
    landed = client.files.item_by_path(drive_id, f"{proj}/a.txt")
    assert landed["file"]["size"] == len(b"next\n")
    assert summary.agreed == {"a.txt": str(landed["etag"])}


@pytest.mark.parametrize(
    "whole_tree", [pytest.param(False, id="push-paths"), pytest.param(True, id="push")]
)
def test_a_file_still_holding_its_agreed_bytes_is_not_sent_over_a_newer_head(
    client: AlkeraClient, backend: LiveBackend, tmp_path: Path, whole_tree: bool
) -> None:
    """A box slept holding ``a.txt`` as agreed; meanwhile a person restored an
    older version on the drive. The box's file holds no change of its own, so
    nothing is sent: sent on its agreed base it was filed as a write made
    after the restore, and the restore survived only as a conflicted copy
    (for a writer that is not the holder, a refusal that failed the push)."""
    root = tmp_path / "tree"
    root.mkdir()
    home = tmp_path / "home"
    proj = home_path(client, "proj")
    drive_id = str(client.files.drive()["id"])
    (root / "a.txt").write_bytes(b"agreed\n")
    agreed = _run_paths(client, root, proj, home, [root / "a.txt"]).agreed["a.txt"]

    (root / "a.txt").write_bytes(b"restored\n")
    _run_paths(client, root, proj, home, [root / "a.txt"])
    restored = client.files.item_by_path(drive_id, f"{proj}/a.txt")
    (root / "a.txt").write_bytes(b"agreed\n")

    backend.log.clear()
    bases = {"a.txt": AgreedBase(etag=agreed, content_hash=_digest(b"agreed\n"))}
    if whole_tree:
        summary = _run(client, root, proj, home, bases=bases)
    else:
        summary = _run_paths(client, root, proj, home, [root / "a.txt"], bases=bases)

    assert (summary.uploaded, summary.agreed) == (0, {})
    assert backend.log.count("/content", *_UPLOAD) == 0, "no byte of the stale copy was sent"
    after = client.files.item_by_path(drive_id, f"{proj}/a.txt")
    assert after["etag"] == restored["etag"], "the restored version is still the head"
