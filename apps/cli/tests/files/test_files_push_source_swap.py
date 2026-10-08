"""A push reads the file the walk found, never what a writer swaps in for it.

The push of a chat's folder runs as root in the box daemon while the chat's
agent, which owns that folder, keeps writing to it. Between the walk that
classified an entry as a regular file and the opens that hash and upload it,
the agent can replace the file -- or any directory above it, or the folder
itself -- with a symlink to somewhere only root may read. A push that reopened
by path followed that link and uploaded the target into the chat's own drive
folder, where the chat reads it back.

Every case here builds a real tree in ``tmp_path``, performs the swap from
inside the fake Files API at the call the push makes at that moment (the drive
read comes after the walk and before any hash; the per-file lookup comes after
the hash and before the upload), and asserts on what reached the "server": the
outside file's bytes never do, the swapped path is named in a warning, and the
rest of the tree still lands.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from alkera_cli.files import push as push_module
from alkera_cli.files.push import PushSummary, SourceUnreadableError, push

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the swaps need POSIX symlinks and the descriptor-relative open; "
    "the Windows open is covered by the unanchored case below",
)

DRIVE = "11111111-1111-1111-1111-111111111111"
DEST_ID = "33333333-3333-3333-3333-333333333333"
DEST = "chat"

SECRET = b"platform-token-that-must-never-leave\n"


class _Server:
    """The slice of the Files API a push drives, holding what it was sent.

    ``uploaded`` is every byte string that reached it -- session parts and
    content PUT bodies alike -- which is what "the outside file was never
    uploaded" is checked against. ``existing`` names files the server already
    holds (so the push replaces them through the content PUT), and the two
    hooks run once, at the moments the module docstring names.
    """

    def __init__(self) -> None:
        self.uploaded: list[bytes] = []
        self.committed: list[str] = []
        self.existing: dict[str, int] = {}
        self.after_walk: Callable[[], None] | None = None
        self.after_hash: dict[str, Callable[[], None]] = {}
        self._open: dict[str, str] = {}

    def drive(self) -> dict[str, Any]:
        hook, self.after_walk = self.after_walk, None
        if hook is not None:
            hook()
        return {"id": DRIVE, "rootId": DEST_ID}

    def item(self, drive_id: str, item_id: str, **_kwargs: Any) -> dict[str, Any]:
        return {"id": item_id}

    def item_by_path(self, drive_id: str, item_path: str) -> dict[str, Any]:
        relative = item_path.removeprefix(f"{DEST}/")
        hook = self.after_hash.pop(relative, None)
        if hook is not None:
            hook()
        if item_path == DEST or "." not in relative.rsplit("/", 1)[-1]:
            return {"id": f"folder:{item_path}", "etag": "1"}
        if relative in self.existing:
            return {"id": f"node:{relative}", "etag": "7", "file": {"size": 1}}
        raise _NotFoundError(f"alkera api: files/item-by-path returned 404 — {item_path}")

    def open_upload(self, *, parent_id: str, name: str, declared_size: int, **_kw: Any) -> Any:
        upload = f"upload-{len(self._open)}"
        self._open[upload] = name
        return {"uploadId": upload, "partSize": 1 << 20}

    def put_part(self, session_id: str, part_no: int, data: bytes, *, checksum: str) -> Any:
        self.uploaded.append(data)
        return {"partNo": part_no}

    def complete_upload(self, session_id: str, parts: Any, **_kwargs: Any) -> dict[str, Any]:
        self.committed.append(self._open[session_id])
        return {"id": "operation-1"}

    def upload_status(self, session_id: str) -> dict[str, Any]:
        return {"acceptedParts": []}

    def await_operation(self, drive_id: str, operation_id: str, **_kwargs: Any) -> dict[str, Any]:
        return {"state": "succeeded"}

    def http(self) -> httpx.Client:
        """The raw routes ``_Gap`` drives: the tree, the content PUT, attrs."""

        def handle(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/tree"):
                return httpx.Response(200, json=[])
            if request.method == "PUT" and path.endswith("/content"):
                self.uploaded.append(request.content)
                self.committed.append(path.split("/items/node:", 1)[1].rsplit("/", 1)[0])
                return httpx.Response(200, json={})
            if request.method == "PATCH":
                return httpx.Response(200, json={})
            return httpx.Response(404, json={"code": "files.notFound"})

        return httpx.Client(transport=httpx.MockTransport(handle), base_url="http://files.invalid")


class _NotFoundError(RuntimeError):
    status_code = 404


def _write(root: Path, relative: str, payload: bytes) -> Path:
    leaf = root / relative
    leaf.parent.mkdir(parents=True, exist_ok=True)
    leaf.write_bytes(payload)
    return leaf


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    """A directory the push must never read from: the daemon's own home."""
    home = tmp_path / "alkera-home"
    _write(home, "auth.yml", SECRET)
    return home


def _push(server: _Server, root: Path, home: Path) -> PushSummary:
    with server.http() as http:
        return push(
            files=server, http=http, root=root, dest=DEST, home=home, respect_gitignore=False
        )


def _swap_dir_for_link(directory: Path, target: Path) -> Callable[[], None]:
    def swap() -> None:
        directory.rename(directory.with_name(directory.name + ".old"))
        directory.symlink_to(target, target_is_directory=True)

    return swap


def _leaked(server: _Server) -> bool:
    return any(SECRET in body for body in server.uploaded)


@pytest.mark.parametrize("existing", [False, True], ids=["new-file", "replaced-file"])
@pytest.mark.parametrize("moment", ["after-walk", "after-hash"])
def test_a_parent_directory_swapped_for_a_link_is_never_followed(
    tmp_path: Path, outside: Path, moment: str, existing: bool
) -> None:
    """The finding's own attack: ``mv d d.old; ln -s /opt/alkera-home d``.

    Run at both moments, and through both upload routes (a new file travels an
    upload session, one the server already holds the content PUT), because each
    reopened the source by path.
    """
    root = tmp_path / "chat"
    _write(root, "d/auth.yml", b"a note the chat wrote\n")
    _write(root, "kept.txt", b"kept\n")
    server = _Server()
    if existing:
        server.existing["d/auth.yml"] = 1
    swap = _swap_dir_for_link(root / "d", outside)
    if moment == "after-walk":
        server.after_walk = swap
    else:
        server.after_hash["d/auth.yml"] = swap

    summary = _push(server, root, tmp_path / "state")

    assert not _leaked(server)
    assert b"kept\n" in server.uploaded
    assert summary.uploaded == 1
    assert len(summary.warnings) == 1
    assert summary.warnings[0].startswith("skipped d/auth.yml: ")
    assert "d/auth.yml" in summary.warnings[0].split(": ", 1)[1]


@pytest.mark.parametrize("moment", ["after-walk", "after-hash"])
def test_a_file_swapped_for_a_link_is_never_followed(
    tmp_path: Path, outside: Path, moment: str
) -> None:
    root = tmp_path / "chat"
    leaf = _write(root, "auth.yml", b"a note the chat wrote\n")
    _write(root, "kept.txt", b"kept\n")
    server = _Server()

    def swap() -> None:
        leaf.unlink()
        leaf.symlink_to(outside / "auth.yml")

    if moment == "after-walk":
        server.after_walk = swap
    else:
        server.after_hash["auth.yml"] = swap

    summary = _push(server, root, tmp_path / "state")

    assert not _leaked(server)
    assert summary.uploaded == 1
    assert [w.split(": ", 1)[0] for w in summary.warnings] == ["skipped auth.yml"]


def test_the_folder_itself_swapped_for_a_link_sends_nothing(tmp_path: Path, outside: Path) -> None:
    """The root is followed as the caller named it, but only to the directory
    the push began in: replaced by a link after the walk, every file below it
    is refused rather than read out of whatever the link names."""
    root = tmp_path / "chat"
    _write(root, "auth.yml", b"a note the chat wrote\n")
    server = _Server()
    server.after_walk = _swap_dir_for_link(root, outside)

    summary = _push(server, root, tmp_path / "state")

    assert not _leaked(server)
    assert server.uploaded == []
    assert summary.uploaded == 0
    assert "the push root was replaced" in summary.warnings[0]


def test_a_file_replaced_after_its_hash_is_not_uploaded_under_that_hash(
    tmp_path: Path,
) -> None:
    """Same name, same size, another inode. Reopening by name would send the
    new bytes on a session and a dedup decision made for the old ones."""
    root = tmp_path / "chat"
    leaf = _write(root, "notes.txt", b"aaaa\n")
    _write(root, "kept.txt", b"kept\n")
    server = _Server()

    def replace() -> None:
        fresh = root / "notes.txt.new"
        fresh.write_bytes(b"bbbb\n")
        os.replace(fresh, leaf)

    server.after_hash["notes.txt"] = replace

    summary = _push(server, root, tmp_path / "state")

    assert all(b"bbbb" not in body for body in server.uploaded)
    assert server.committed == ["kept.txt"]
    assert "replaced by another file" in summary.warnings[0]


@pytest.mark.timeout(20)
def test_a_fifo_swapped_in_for_a_file_is_refused_without_waiting_for_a_writer(
    tmp_path: Path,
) -> None:
    """Opening a fifo for reading blocks until something writes to it; a push
    that opened one would hang the daemon's whole folder push."""
    root = tmp_path / "chat"
    leaf = _write(root, "pipe.txt", b"a file at walk time\n")
    _write(root, "kept.txt", b"kept\n")
    server = _Server()

    def swap() -> None:
        leaf.unlink()
        os.mkfifo(leaf)

    server.after_walk = swap

    summary = _push(server, root, tmp_path / "state")

    assert server.committed == ["kept.txt"]
    assert "not a regular file" in summary.warnings[0]


def test_the_unanchored_open_refuses_a_link_already_below_the_root(
    tmp_path: Path, outside: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The open Windows takes, which has no descriptor-relative open: a link
    present below the root when the file is opened is refused, not followed."""
    monkeypatch.setattr(push_module, "_ANCHORED_OPEN", False)
    root = tmp_path / "chat"
    _write(root, "d/auth.yml", b"a note the chat wrote\n")
    _write(root, "kept.txt", b"kept\n")
    server = _Server()
    server.after_walk = _swap_dir_for_link(root / "d", outside)

    summary = _push(server, root, tmp_path / "state")

    assert not _leaked(server)
    assert server.committed == ["kept.txt"]
    assert "link or a reparse point" in summary.warnings[0]


@pytest.mark.skipif(
    getattr(os, "geteuid", lambda: 1)() == 0,
    reason="a file the process may not read needs a non-root reader",
)
def test_a_refusal_to_read_is_still_a_failure_not_a_skip(tmp_path: Path) -> None:
    """Only a swapped-in shape is passed by. A regular file the push may not
    read still fails the push by name."""
    root = tmp_path / "chat"
    sealed = _write(root, "sealed.txt", b"owed\n")
    sealed.chmod(0)
    try:
        with pytest.raises(SourceUnreadableError, match=r"sealed\.txt"):
            _push(_Server(), root, tmp_path / "state")
    finally:
        sealed.chmod(0o600)


@pytest.mark.parametrize("existing", [False, True], ids=["new-file", "replaced-file"])
def test_a_file_deleted_between_hash_and_upload_is_skipped_and_the_rest_lands(
    tmp_path: Path, existing: bool
) -> None:
    """The agent's log rotates while a checkpoint push runs. A file gone once
    the push has hashed it has nothing left to send; it used to fail the whole
    folder, taking every other file's checkpoint with it."""
    root = tmp_path / "chat"
    leaf = _write(root, "log/old.log", b"rotated away\n")
    _write(root, "kept.txt", b"kept\n")
    _write(root, "z-also-kept.txt", b"also kept\n")
    server = _Server()
    if existing:
        server.existing["log/old.log"] = 1
    server.after_hash["log/old.log"] = leaf.unlink

    summary = _push(server, root, tmp_path / "state")

    assert summary.warnings == ["skipped log/old.log: it was gone before the push read it"]
    assert sorted(server.committed) == ["kept.txt", "z-also-kept.txt"]
    assert summary.uploaded == 2
    assert all(b"rotated away" not in body for body in server.uploaded)


@pytest.mark.skipif(
    getattr(os, "geteuid", lambda: 1)() == 0,
    reason="a file the process may not read needs a non-root reader",
)
def test_a_file_sealed_between_hash_and_upload_still_fails_the_push(tmp_path: Path) -> None:
    """Gone is the only reason to pass a file by at upload time too: one that
    is there but may not be read is still owed to the folder."""
    root = tmp_path / "chat"
    leaf = _write(root, "sealed.txt", b"owed\n")
    server = _Server()
    server.after_hash["sealed.txt"] = lambda: leaf.chmod(0)
    try:
        with pytest.raises(SourceUnreadableError, match=r"sealed\.txt"):
            _push(server, root, tmp_path / "state")
    finally:
        leaf.chmod(0o600)
    assert server.committed == []
