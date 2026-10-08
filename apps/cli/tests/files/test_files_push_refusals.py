"""What one file's refusal costs the rest of the push, and how an empty file travels.

Both were measured on a node. A chat's push hit a zero-byte ``__init__.py``:
the push opened a session for it, sent no part, and asked the drive to commit
— ``409 files.parts_mismatch — the session holds no parts`` — and that one
answer failed the whole push, then the hand-back three times over, and the
chat was reported as one whose files could not be saved, every other file
still on the box. A zero-byte file is one empty part, and a file the drive
refuses for a reason of the file's own is named and left behind while the
rest of the tree lands.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from alkera_cli.files.mount import MountRecord, save_record, unmount
from alkera_cli.files.push import PushSummary, push
from alkera_sdk.client import AlkeraHTTPError

DRIVE = "11111111-1111-1111-1111-111111111111"
DEST = "home/ana/Chats/Kickoff.alkerachat"
DEST_ID = "22222222-2222-2222-2222-222222222222"


def _refusal(code: str, message: str, status: int = 409) -> AlkeraHTTPError:
    return AlkeraHTTPError(
        label="files/upload-complete", status=status, code=code, message=message, trace_id=None
    )


class _Drive:
    """The slice of the Files API a push drives, answering as the server does
    about parts: a commit with no parts is refused, an empty part on a
    zero-byte session is taken."""

    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}
        self.parts: dict[str, list[tuple[int, bytes]]] = {}
        self.committed: list[str] = []
        #: A refusal the commit of this NAME answers with, in place of success.
        self.refuse: dict[str, Callable[[], Exception]] = {}

    def drive(self) -> dict[str, Any]:
        return {"id": DRIVE, "rootId": "root"}

    def item(self, drive_id: str, item_id: str, **_kwargs: Any) -> dict[str, Any]:
        return {"id": item_id, "etag": "1", "pathBytes": "/" + DEST}

    def item_by_path(self, drive_id: str, item_path: str) -> dict[str, Any]:
        relative = item_path.removeprefix(f"{DEST}/")
        if item_path == DEST or "." not in relative.rsplit("/", 1)[-1]:
            return {"id": f"folder:{item_path}", "etag": "1"}
        raise _refusal("not_found", item_path, status=404)

    def item_under(self, drive_id: str, item_id: str, item_path: str) -> dict[str, Any]:
        return self.item_by_path(drive_id, f"{DEST}/{item_path}".rstrip("/"))

    def open_upload(self, *, parent_id: str, name: str, declared_size: int, **_kw: Any) -> Any:
        upload = f"upload-{len(self.sessions)}"
        self.sessions[upload] = {"name": name, "declared": declared_size}
        self.parts[upload] = []
        return {"uploadId": upload, "partSize": 1 << 20}

    def put_part(self, session_id: str, part_no: int, data: bytes, *, checksum: str) -> Any:
        declared = self.sessions[session_id]["declared"]
        if len(data) == 0 and declared != 0:
            raise _refusal("files.empty_part", "a part may not be empty", status=400)
        self.parts[session_id].append((part_no, data))
        return {"partNo": part_no}

    def complete_upload(self, session_id: str, parts: Any, **_kwargs: Any) -> dict[str, Any]:
        name = self.sessions[session_id]["name"]
        refusal = self.refuse.get(name)
        if refusal is not None:
            raise refusal()
        if not self.parts[session_id]:
            raise _refusal("files.parts_mismatch", "the session holds no parts")
        self.committed.append(name)
        return {"id": f"operation:{name}"}

    def upload_status(self, session_id: str) -> dict[str, Any]:
        return {"acceptedParts": [part_no for part_no, _ in self.parts[session_id]]}

    def await_operation(self, drive_id: str, operation_id: str, **_kwargs: Any) -> dict[str, Any]:
        return {"state": "done"}

    def http(self) -> httpx.Client:
        def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/tree"):
                return httpx.Response(200, json=[])
            if request.method == "PATCH":
                return httpx.Response(200, json={})
            return httpx.Response(404, json={"code": "not_found"})

        return httpx.Client(transport=httpx.MockTransport(handle), base_url="http://files.test")


def _tree(root: Path, files: dict[str, bytes]) -> None:
    for relative, payload in files.items():
        leaf = root / relative
        leaf.parent.mkdir(parents=True, exist_ok=True)
        leaf.write_bytes(payload)


def _push(drive: _Drive, root: Path, home: Path) -> PushSummary:
    with drive.http() as http:
        return push(
            files=drive,
            http=http,
            root=root,
            dest=DEST,
            node_id=DEST_ID,
            drive_id=DRIVE,
            home=home,
            respect_gitignore=False,
        )


def test_an_empty_file_travels_as_one_empty_part(tmp_path: Path) -> None:
    root, home = tmp_path / "work", tmp_path / "home"
    _tree(root, {"pkg/__init__.py": b"", "pkg/mod.py": b"x = 1\n"})
    drive = _Drive()

    summary = _push(drive, root, home)

    assert summary.failed == [] and summary.warnings == []
    assert sorted(drive.committed) == ["__init__.py", "mod.py"]
    empty = next(s for s, meta in drive.sessions.items() if meta["name"] == "__init__.py")
    assert drive.sessions[empty]["declared"] == 0
    assert drive.parts[empty] == [(1, b"")], "one part of no bytes, never no parts"
    assert summary.uploaded == 2 and summary.parts_sent == 2


def test_a_file_the_drive_refuses_for_its_own_reason_is_left_behind_and_the_rest_lands(
    tmp_path: Path,
) -> None:
    root, home = tmp_path / "work", tmp_path / "home"
    _tree(root, {"a.txt": b"a", "broken.txt": b"b", "deep/c.txt": b"c"})
    drive = _Drive()
    drive.refuse["broken.txt"] = lambda: _refusal(
        "files.parts_mismatch", "the session holds no parts"
    )

    summary = _push(drive, root, home)

    assert sorted(drive.committed) == ["a.txt", "c.txt"]
    assert summary.failed == ["broken.txt"]
    assert summary.uploaded == 2
    (warning,) = summary.warnings
    assert warning.startswith("broken.txt: not saved (files.parts_mismatch")


@pytest.mark.parametrize(
    ("code", "status"),
    [
        pytest.param("files.lease_fenced", 409, id="the-fence"),
        pytest.param("files.leased", 409, id="somebody-elses-lease"),
        # Out of nodes is every file's answer; out of bytes is a file's own
        # (see the next case), since a smaller file may still fit.
        pytest.param("files.quota_nodes", 507, id="the-node-quota"),
        pytest.param("internal_error", 500, id="the-server"),
    ],
)
def test_a_refusal_that_is_not_the_files_own_still_ends_the_push(
    tmp_path: Path, code: str, status: int
) -> None:
    root, home = tmp_path / "work", tmp_path / "home"
    _tree(root, {"a.txt": b"a", "b.txt": b"b"})
    drive = _Drive()
    drive.refuse["a.txt"] = lambda: _refusal(code, "no", status=status)

    with pytest.raises(AlkeraHTTPError) as raised:
        _push(drive, root, home)
    assert raised.value.code == code


def test_the_release_names_what_the_final_push_left_behind(tmp_path: Path) -> None:
    """A hand-back that lost one file must not read as a clean one: the
    release the drive receives carries the count and the paths."""
    root, home = tmp_path / "work", tmp_path / "home"
    root.mkdir()
    record = MountRecord(
        instance_id="box:1",
        drive_id=DRIVE,
        node_id=DEST_ID,
        org_path=DEST,
        local_root=str(root),
        epoch=3,
    )
    save_record(record, home=home)
    releases: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/lease/release"):
            releases.append(json.loads(request.content))
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"code": "not_found"})

    def pushed(**_kwargs: Any) -> PushSummary:
        return PushSummary(uploaded=1, failed=["broken.txt"], warnings=["broken.txt: not saved"])

    with httpx.Client(transport=httpx.MockTransport(handle), base_url="http://files.test") as http:
        unmount(files=_Drive(), http=http, root=root, home=home, push_tree=pushed, unsynced=[])

    (release,) = releases
    body = json.dumps(release)
    assert "broken.txt" in body
    count = release.get("unsyncedCount", release.get("unsynced_count"))
    assert count == 1, release


class _SlowCommits(_Drive):
    """A drive whose commits never land inside the push's wait."""

    def __init__(self) -> None:
        super().__init__()
        self.waited: list[str] = []

    def await_operation(self, drive_id: str, operation_id: str, **_kwargs: Any) -> dict[str, Any]:
        self.waited.append(operation_id)
        raise TimeoutError(f"operation {operation_id} still 'running'")


class _CountedCommits(_Drive):
    def __init__(self) -> None:
        super().__init__()
        self.waited: list[str] = []

    def await_operation(self, drive_id: str, operation_id: str, **_kwargs: Any) -> dict[str, Any]:
        self.waited.append(operation_id)
        return {"state": "done"}


def test_once_one_commit_outlasts_its_wait_the_push_waits_on_no_other(tmp_path: Path) -> None:
    """Waiting on a commit is a courtesy. A hand-back of seven hundred files
    against a drive whose commits were slow waited on every one and held the
    box for most of an hour; the first commit that does not land in time says
    the drive is behind, and the rest of the push goes on without waiting."""
    root, home = tmp_path / "work", tmp_path / "home"
    _tree(root, {f"f{n}.txt": b"x" for n in range(5)})
    drive = _SlowCommits()

    summary = _push(drive, root, home)

    assert len(drive.waited) == 1, "one wait, not one per file"
    assert sorted(drive.committed) == [f"f{n}.txt" for n in range(5)]
    assert summary.uploaded == 5
    assert summary.commits_unconfirmed == 5
    assert summary.failed == []


def test_commits_that_land_are_each_waited_on(tmp_path: Path) -> None:
    root, home = tmp_path / "work", tmp_path / "home"
    _tree(root, {f"f{n}.txt": b"x" for n in range(3)})
    drive = _CountedCommits()

    summary = _push(drive, root, home)

    assert len(drive.waited) == 3
    assert summary.commits_unconfirmed == 0


def test_a_name_the_drive_does_not_file_stays_behind_and_the_rest_lands(tmp_path: Path) -> None:
    """A leading space is a name the drive refuses (``files.invalid_name.
    surrounding_space``). One such file used to fail the whole folder push, so
    none of the other files reached the drive. It is now said and left on the
    machine, with everything under a folder whose own name is refused, and the
    rest of the tree lands."""
    root, home = tmp_path / "work", tmp_path / "home"
    _tree(
        root,
        {
            "a.txt": b"a",
            " leading-space.txt": b"b",
            "trailing /inner.txt": b"c",
            "deep/d.txt": b"d",
        },
    )
    drive = _Drive()

    summary = _push(drive, root, home)

    assert sorted(drive.committed) == ["a.txt", "d.txt"]
    assert sorted(summary.failed) == [" leading-space.txt", "trailing "]
    assert all("does not file this name" in warning for warning in summary.warnings)
    assert all(meta["name"] != "inner.txt" for meta in drive.sessions.values())


def test_a_name_refusal_from_the_drive_itself_is_per_file(tmp_path: Path) -> None:
    """A box and a server of different versions can disagree about a naming
    rule. When the drive refuses a name the box let through, that file stays
    behind and the push goes on."""
    root, home = tmp_path / "work", tmp_path / "home"
    _tree(root, {"a.txt": b"a", "server-refuses.txt": b"b"})
    drive = _Drive()
    drive.refuse["server-refuses.txt"] = lambda: _refusal(
        "files.invalid_name.windows_reserved", "that name is not filed", status=422
    )

    summary = _push(drive, root, home)

    assert drive.committed == ["a.txt"]
    assert summary.failed == ["server-refuses.txt"]


class _NearlyFullDrive(_Drive):
    """A drive with ``room`` bytes left: a file larger than that is refused
    507 ``files.quota_bytes`` when its upload opens, as the server does."""

    def __init__(self, room: int) -> None:
        super().__init__()
        self.room = room
        self.asked: list[str] = []

    def open_upload(self, *, parent_id: str, name: str, declared_size: int, **kw: Any) -> Any:
        self.asked.append(name)
        if declared_size > self.room:
            raise AlkeraHTTPError(
                label="files/upload-open",
                status=507,
                code="files.quota_bytes",
                message="files.quota_bytes",
                trace_id=None,
            )
        return super().open_upload(
            parent_id=parent_id, name=name, declared_size=declared_size, **kw
        )


def test_a_file_the_drive_has_no_room_for_is_left_behind_and_the_rest_lands(
    tmp_path: Path,
) -> None:
    """A 20 GB scratch file over the drive's room failed every checkpoint push
    of its workspace, so nothing else the agent wrote was saved. The file the
    drive cannot hold is named and left behind; the rest lands; and a file at
    least as large is not even read, since the drive would refuse it too."""
    root, home = tmp_path / "work", tmp_path / "home"
    _tree(
        root,
        {
            "a-big.bin": b"x" * 500,
            "b-bigger.bin": b"y" * 800,
            "c-mid.txt": b"m" * 300,
            "d-small.txt": b"s",
        },
    )
    drive = _NearlyFullDrive(room=400)

    summary = _push(drive, root, home)

    assert sorted(drive.committed) == ["c-mid.txt", "d-small.txt"]
    assert sorted(summary.failed) == ["a-big.bin", "b-bigger.bin"]
    assert "b-bigger.bin" not in drive.asked, "a file larger than one refused is not asked for"
    assert sorted(summary.warnings) == [
        "a-big.bin: not saved (the drive has no room for it)",
        "b-bigger.bin: not saved (the drive has no room for it)",
    ]
