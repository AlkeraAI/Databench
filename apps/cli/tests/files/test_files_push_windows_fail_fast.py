"""A push that cannot move a file's bytes says so, and says it quickly.

Three ways a transfer can stop being able to do its job, each of which used to
end as something worse than an error — a bare ``errno`` with no path on it, or
a version committed over real content from bytes that were never sent:

* the platform refuses the name. On Windows a relative path past 260
  characters is ``ENAMETOOLONG`` from ``open`` itself, which is what a deep
  corpus tree meets there and what the long-path spelling exists to avoid;
* the session offers a part size nothing can be cut into;
* the file changes under the push between the hash and the part loop.

Windows is simulated at the one seam that behaves differently there — the
``open`` the push does on a source file, made to answer ``ENAMETOOLONG`` for
one name — so the contract is provable on a platform that has no
260-character limit, and on Windows itself, where the push's own
extended-length spelling means a genuinely long path is *not* refused. Each
case is timed: a push that cannot finish has to fail, not sit there.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path
from typing import Any

import httpx
import pytest
from alkera_cli.files.push import SourceUnreadableError, UploadSessionError, push

DRIVE = "11111111-1111-1111-1111-111111111111"
ROOT_ID = "22222222-2222-2222-2222-222222222222"
DEST_ID = "33333333-3333-3333-3333-333333333333"
DEST = "corpus"

#: Long enough that Windows refuses the whole path without the extended-length
#: prefix, built the way the round-trip corpus builds its deep case.
_DEEP = "/".join("segment" * 6 for _ in range(8))


class _FakeFiles:
    """The slice of the Files API a push drives, with no server behind it.

    It records what a push actually committed, because that — not a call
    count — is what a truncated or empty version looks like from outside.
    """

    def __init__(self, *, part_size: int = 1 << 20) -> None:
        self.part_size = part_size
        self.parts: dict[int, bytes] = {}
        self.committed: dict[str, int] = {}
        self.on_open: Any = None

    def drive(self) -> dict[str, Any]:
        return {"id": DRIVE, "rootId": ROOT_ID}

    def item(self, drive_id: str, item_id: str, **_kwargs: Any) -> dict[str, Any]:
        return {"id": item_id}

    def item_by_path(self, drive_id: str, item_path: str) -> dict[str, Any]:
        if item_path == DEST:
            return {"id": DEST_ID, "etag": "1"}
        raise _NotFoundError(f"alkera api: files/item-by-path returned 404 — {item_path}")

    def open_upload(self, *, parent_id: str, name: str, declared_size: int, **_kw: Any) -> Any:
        if self.on_open is not None:
            self.on_open()
        return {"uploadId": "upload-1", "partSize": self.part_size}

    def put_part(self, session_id: str, part_no: int, data: bytes, *, checksum: str) -> Any:
        self.parts[part_no] = data
        return {"partNo": part_no}

    def complete_upload(self, session_id: str, parts: Any, **_kwargs: Any) -> dict[str, Any]:
        self.committed[session_id] = sum(int(part["size"]) for part in parts)
        return {"id": "operation-1"}

    def upload_status(self, session_id: str) -> dict[str, Any]:
        return {"acceptedParts": []}

    def await_operation(self, drive_id: str, operation_id: str, **_kwargs: Any) -> dict[str, Any]:
        return {"state": "succeeded"}


class _NotFoundError(RuntimeError):
    status_code = 404


def _http() -> httpx.Client:
    """The raw client ``_Gap`` uses. Only the tree route is ever reached here."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tree"):
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={"code": "files.notFound"})

    return httpx.Client(transport=httpx.MockTransport(handle), base_url="http://files.invalid")


def _tree(root: Path, relative: str, payload: bytes) -> Path:
    leaf = root / relative
    leaf.parent.mkdir(parents=True, exist_ok=True)
    leaf.write_bytes(payload)
    return leaf


def _push(files: _FakeFiles, root: Path, home: Path) -> Any:
    with _http() as http:
        return push(
            files=files,
            http=http,
            root=root,
            dest=DEST,
            home=home,
            respect_gitignore=False,
        )


def _refuse_one_name(monkeypatch: pytest.MonkeyPatch, refused: str) -> None:
    """Make ``open`` answer ``ENAMETOOLONG`` for one file, the way Win32 does.

    Keyed on the file's own name rather than on the length of its path,
    because the length is not the portable half of this. A real 260-character
    path is refused only by Win32, and on Windows the push's extended-length
    spelling gets that very name open — correctly — so a length-keyed guard
    never fires on the one platform whose behaviour it imitates, and the push
    runs on past the seam into the next thing that fails.

    Only the one file is refused, so the push's own state file and every other
    path in the tree keep working — which is exactly the shape of the Windows
    failure, and is what makes a push that swallows the error look like a push
    that simply found nothing to send.
    """
    original = Path.open
    original_os_open = os.open

    def guarded(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self.name == refused:
            raise OSError(errno.ENAMETOOLONG, "The filename or extension is too long", str(self))
        return original(self, *args, **kwargs)

    def guarded_os_open(path: Any, *args: Any, **kwargs: Any) -> int:
        # POSIX opens a source one component at a time from its parent's
        # descriptor, so the leaf reaches ``os.open`` as its bare name.
        if os.path.basename(os.fsdecode(path)) == refused:
            raise OSError(errno.ENAMETOOLONG, "The filename or extension is too long", path)
        return original_os_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded)
    monkeypatch.setattr(os, "open", guarded_os_open)


@pytest.mark.timeout(20)
def test_a_name_the_platform_refuses_fails_the_push_naming_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refused leaf's own path is in the error, and no version is committed.

    Before the source reads went through one opener this surfaced as a bare
    ``OSError`` from inside the transfer: an errno, no file name, and nothing
    telling an operator which of the tree's entries the push gave up on.
    """
    root = tmp_path / "corpus"
    _tree(root, "nested/deeper/leaf.txt", b"deep\n")
    _tree(root, "shallow.txt", b"short\n")
    files = _FakeFiles()
    _refuse_one_name(monkeypatch, "leaf.txt")

    with pytest.raises(SourceUnreadableError) as refused:
        _push(files, root, tmp_path / "home")

    assert "leaf.txt" in str(refused.value)
    assert "too long" in str(refused.value)
    assert files.committed == {}


@pytest.mark.timeout(20)
def test_a_session_with_no_usable_part_size_commits_nothing(tmp_path: Path) -> None:
    """A part size of zero cuts no part, so the file must not be committed.

    The read loop stops on the first empty chunk, and a zero part size makes
    the very first read empty — so an unguarded loop agreed a commit of no
    parts at all and landed an empty version over five real bytes.
    """
    root = tmp_path / "corpus"
    _tree(root, "leaf.txt", b"deep\n")
    files = _FakeFiles(part_size=0)

    with pytest.raises(UploadSessionError) as refused:
        _push(files, root, tmp_path / "home")

    assert "leaf.txt" in str(refused.value)
    assert files.parts == {}
    assert files.committed == {}


@pytest.mark.timeout(20)
def test_a_file_that_shrinks_under_the_push_is_not_committed_short(tmp_path: Path) -> None:
    """The session was opened for the hashed size; fewer bytes is a failure.

    The loop is bounded by that size rather than by end-of-file, so a file
    truncated between the hash and the transfer stops here instead of
    committing a version whose bytes nobody ever asked for.
    """
    root = tmp_path / "corpus"
    leaf = _tree(root, "leaf.txt", b"x" * 4096)
    files = _FakeFiles(part_size=1024)
    files.on_open = lambda: leaf.write_bytes(b"x" * 512)

    with pytest.raises(UploadSessionError) as refused:
        _push(files, root, tmp_path / "home")

    assert "4096" in str(refused.value)
    assert files.committed == {}


def test_the_long_path_spelling_is_only_applied_where_the_platform_needs_it(
    tmp_path: Path,
) -> None:
    """A POSIX push opens the name it was given, unprefixed.

    The prefix is a Win32 parser instruction; handing it to a POSIX ``open``
    would look for a directory called ``\\\\?\\`` and fail every read.
    """
    from alkera_cli.files.push import _extended_length

    leaf = _tree(tmp_path / "corpus", f"{_DEEP}/leaf.txt", b"deep\n")
    spelled = _extended_length(leaf)

    if os.name == "nt":
        assert str(spelled).startswith("\\\\?\\")
        assert _extended_length(spelled) == spelled
    else:
        assert spelled == leaf
    assert spelled.read_bytes() == b"deep\n"
