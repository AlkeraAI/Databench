"""A reader's working copy of a chat's files, against a real drive.

Every claim here is read off the disk the copy wrote and what the drive holds
afterwards, through the same REST surface the web portal and the box use, on
the real backend ``files._chat_backend`` serves.

The chat is a real chat, so its folder, its working directory and the facet
that names it are the product's own. "Awake" is a real ``chat`` lease on the
chat folder (the lease a box takes, which admits inbound writes under the
working directory); "asleep" is no lease at all.
"""

from __future__ import annotations

import asyncio
import shutil
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from alkera_cli.files.working_copy import (
    AccessLostError,
    Conflict,
    CopiedFile,
    CopyDetachedError,
    CopyTarget,
    HttpTargetReader,
    SyncReport,
    TargetUnavailableError,
    WorkingCopy,
    WorkingCopyStore,
    folder_name_for,
)
from alkera_cli.files.working_copy_live import STREAM_OPENED, WorkingCopyRunner
from alkera_cli.host.backoff import ReconnectBackoff
from alkera_sdk import AlkeraClient
from files._chat_backend import (
    Chat,
    chat_backend,
    http_of,
    make_chat,
    remote_bytes,
    rewrite_remote,
    upload,
)
from files._live_backend import LiveBackend

#: One backend serves the whole module, so its tests stay on one worker.
pytestmark = pytest.mark.xdist_group("working_copy")


@pytest.fixture(scope="module")
def backend(tmp_path_factory: pytest.TempPathFactory) -> Iterator[LiveBackend]:
    with chat_backend(tmp_path_factory.mktemp("server")) as served:
        yield served


@pytest.fixture
def api(backend: LiveBackend) -> Iterator[AlkeraClient]:
    with AlkeraClient(base_url=backend.base_url, token=backend.token) as client:
        yield client


@pytest.fixture
def chat(api: AlkeraClient) -> Chat:
    return make_chat(api)


def _open(api: AlkeraClient, chat: Chat, tmp_path: Path) -> tuple[WorkingCopy, Any]:
    return WorkingCopy.open(
        CopyTarget("chat", chat.id),
        location=tmp_path / "copies",
        files=api.files,
        http=http_of(api),
        reader=HttpTargetReader(http_of(api)),
        store=WorkingCopyStore(tmp_path / "home"),
    )


def _local_tree(root: Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}


# ---------------------------------------------------------------------------
# Down
# ---------------------------------------------------------------------------


def test_open_copies_the_working_directory_and_nothing_around_it(
    api: AlkeraClient, chat: Chat, tmp_path: Path
) -> None:
    upload(api, tmp_path, chat.drive_id, chat.working_id, "report.md", b"# report\n")
    data = api.files.create_folder(chat.drive_id, chat.working_id, "data")
    upload(api, tmp_path, chat.drive_id, str(data["id"]), "a.csv", b"x,y\n1,2\n")

    copy, report = _open(api, chat, tmp_path)

    assert copy.record.node_id == chat.working_id
    assert copy.root.parent == tmp_path / "copies"
    assert copy.root.name == folder_name_for("Quarterly plan", chat.id)
    assert _local_tree(copy.root) == {"report.md", "data/a.csv"}
    assert (copy.root / "data" / "a.csv").read_bytes() == b"x,y\n1,2\n"
    assert sorted(report.downloaded) == ["data/a.csv", "report.md"]
    # The bookkeeping lives under the store, never in the copied folder.
    assert not any(name.endswith(".json") for name in _local_tree(copy.root))


def test_opening_the_same_chat_again_reuses_its_directory(
    api: AlkeraClient, chat: Chat, tmp_path: Path
) -> None:
    first, _ = _open(api, chat, tmp_path)
    second, report = _open(api, chat, tmp_path)

    assert second.root == first.root
    assert not report.changed


def test_a_directory_the_store_did_not_make_is_never_adopted(
    api: AlkeraClient, chat: Chat, tmp_path: Path
) -> None:
    squatter = tmp_path / "copies" / folder_name_for("Quarterly plan", chat.id)
    squatter.mkdir(parents=True)
    (squatter / "private.txt").write_text("not the chat's")

    with pytest.raises(FileExistsError):
        _open(api, chat, tmp_path)
    assert list(api.files.children(chat.drive_id, chat.working_id)) == []


def test_a_change_on_the_drive_replaces_a_file_this_copy_did_not_touch(
    api: AlkeraClient, chat: Chat, tmp_path: Path
) -> None:
    node = upload(api, tmp_path, chat.drive_id, chat.working_id, "notes.txt", b"one\n")
    copy, _ = _open(api, chat, tmp_path)

    rewrite_remote(api, chat.drive_id, node, b"two, from the agent\n")
    report = copy.sync()

    assert (copy.root / "notes.txt").read_bytes() == b"two, from the agent\n"
    assert report.downloaded == ["notes.txt"]
    assert report.conflicts == []


def test_a_file_deleted_on_the_drive_is_deleted_here(
    api: AlkeraClient, chat: Chat, tmp_path: Path
) -> None:
    node = upload(api, tmp_path, chat.drive_id, chat.working_id, "old.txt", b"bye\n")
    copy, _ = _open(api, chat, tmp_path)

    api.files.trash(chat.drive_id, node, if_match=api.files.item(chat.drive_id, node))
    report = copy.sync()

    assert not (copy.root / "old.txt").exists()
    assert report.removed == ["old.txt"]


# ---------------------------------------------------------------------------
# Up, asleep (nobody holds the chat's lease)
# ---------------------------------------------------------------------------


def test_asleep_a_save_lands_on_the_drive(api: AlkeraClient, chat: Chat, tmp_path: Path) -> None:
    node = upload(api, tmp_path, chat.drive_id, chat.working_id, "plot.py", b"print(1)\n")
    copy, _ = _open(api, chat, tmp_path)

    (copy.root / "plot.py").write_bytes(b"print(2)\n")
    report = copy.sync()

    assert report.uploaded == ["plot.py"]
    assert remote_bytes(api, tmp_path, chat.drive_id, node) == b"print(2)\n"
    # Agreed afterwards: the next pass has nothing to do.
    again = copy.sync()
    assert not again.changed and again.conflicts == []


def test_asleep_a_save_names_its_base_so_a_stale_one_is_a_conflict_not_an_overwrite(
    api: AlkeraClient, chat: Chat, tmp_path: Path
) -> None:
    node = upload(api, tmp_path, chat.drive_id, chat.working_id, "plan.md", b"base\n")
    copy, _ = _open(api, chat, tmp_path)

    rewrite_remote(api, chat.drive_id, node, b"theirs\n")
    (copy.root / "plan.md").write_bytes(b"mine\n")
    report = copy.sync()

    assert report.conflicts == [Conflict(path="plan.md", reason="edited_both")]
    assert report.uploaded == [] and report.downloaded == []
    assert (copy.root / "plan.md").read_bytes() == b"mine\n"
    assert remote_bytes(api, tmp_path, chat.drive_id, node) == b"theirs\n"
    # It stays a conflict, pass after pass, until somebody settles it.
    assert copy.sync().conflicts == [Conflict(path="plan.md", reason="edited_both")]


def test_keeping_mine_writes_my_bytes_over_theirs(
    api: AlkeraClient, chat: Chat, tmp_path: Path
) -> None:
    node = upload(api, tmp_path, chat.drive_id, chat.working_id, "plan.md", b"base\n")
    copy, _ = _open(api, chat, tmp_path)
    rewrite_remote(api, chat.drive_id, node, b"theirs\n")
    (copy.root / "plan.md").write_bytes(b"mine\n")
    copy.sync()

    report = copy.resolve("plan.md", "mine")

    assert report.conflicts == []
    assert "plan.md" in report.uploaded
    assert remote_bytes(api, tmp_path, chat.drive_id, node) == b"mine\n"


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("../outside.txt", id="parent"),
        pytest.param("/etc/hosts", id="absolute"),
        pytest.param("a/../../b", id="climbs-out"),
        pytest.param("a\\b", id="backslash"),
        pytest.param("", id="empty"),
    ],
)
def test_settling_refuses_a_path_that_is_not_inside_the_copy(
    api: AlkeraClient, chat: Chat, tmp_path: Path, path: str
) -> None:
    copy, _ = _open(api, chat, tmp_path)
    bystander = tmp_path / "outside.txt"
    bystander.write_bytes(b"not the copy's\n")

    with pytest.raises(ValueError, match="not a path inside"):
        copy.resolve(path, "theirs")
    assert bystander.read_bytes() == b"not the copy's\n"


def test_taking_theirs_replaces_my_bytes(api: AlkeraClient, chat: Chat, tmp_path: Path) -> None:
    node = upload(api, tmp_path, chat.drive_id, chat.working_id, "plan.md", b"base\n")
    copy, _ = _open(api, chat, tmp_path)
    rewrite_remote(api, chat.drive_id, node, b"theirs\n")
    (copy.root / "plan.md").write_bytes(b"mine\n")
    copy.sync()

    report = copy.resolve("plan.md", "theirs")

    assert report.conflicts == []
    assert (copy.root / "plan.md").read_bytes() == b"theirs\n"
    assert remote_bytes(api, tmp_path, chat.drive_id, node) == b"theirs\n"
    # Mine was set aside, outside the synced folder, not deleted.
    saved = Path(report.backups["plan.md"])
    assert saved.read_bytes() == b"mine\n"
    assert copy.root not in saved.parents
    assert not copy.sync().changed


# ---------------------------------------------------------------------------
# A missing or emptied folder is never "the person deleted everything"
# ---------------------------------------------------------------------------


def _writes_seen(backend: LiveBackend) -> list[tuple[str, str]]:
    return [(m, p) for m, p in backend.log.seen if m in {"POST", "PUT", "PATCH", "DELETE"}]


def _seed_tree(api: AlkeraClient, chat: Chat, tmp_path: Path, count: int) -> list[str]:
    return [
        upload(api, tmp_path, chat.drive_id, chat.working_id, f"f{i}.md", f"{i}\n".encode())
        for i in range(count)
    ]


def test_a_missing_local_folder_stops_the_copy_and_changes_nothing_on_the_drive(
    backend: LiveBackend, api: AlkeraClient, chat: Chat, tmp_path: Path
) -> None:
    _seed_tree(api, chat, tmp_path, 3)
    copy, _ = _open(api, chat, tmp_path)
    shutil.rmtree(copy.root)

    backend.log.clear()
    with pytest.raises(CopyDetachedError):
        copy.sync()

    assert _writes_seen(backend) == []
    assert not copy.root.exists()
    names = {child["name"] for child in api.files.children(chat.drive_id, chat.working_id)}
    assert names == {"f0.md", "f1.md", "f2.md"}
    with pytest.raises(CopyDetachedError):
        copy.refresh(frozenset({"f0.md"}))


def test_an_emptied_folder_holds_its_deletions_until_the_person_answers(
    backend: LiveBackend, api: AlkeraClient, chat: Chat, tmp_path: Path
) -> None:
    _seed_tree(api, chat, tmp_path, 6)
    copy, _ = _open(api, chat, tmp_path)
    for child in list(copy.root.iterdir()):
        child.unlink()

    backend.log.clear()
    held = copy.sync()

    assert held.deletions_held == [f"f{i}.md" for i in range(6)]
    assert held.trashed == [] and _writes_seen(backend) == []
    assert list(copy.root.iterdir()) == []  # nor brought back behind the person's back
    assert copy.sync().deletions_held == held.deletions_held

    restored = copy.resolve_deletions(apply=False)

    assert restored.restored == [f"f{i}.md" for i in range(6)]
    assert sorted(p.name for p in copy.root.iterdir()) == [f"f{i}.md" for i in range(6)]


def test_held_deletions_go_through_when_the_person_confirms(
    api: AlkeraClient, chat: Chat, tmp_path: Path
) -> None:
    _seed_tree(api, chat, tmp_path, 6)
    copy, _ = _open(api, chat, tmp_path)
    for child in list(copy.root.iterdir()):
        child.unlink()
    copy.sync()

    applied = copy.resolve_deletions(apply=True)

    assert sorted(applied.trashed) == [f"f{i}.md" for i in range(6)]
    assert list(api.files.children(chat.drive_id, chat.working_id)) == []


# ---------------------------------------------------------------------------
# One ignore policy: what is never sent is never brought down or deleted
# ---------------------------------------------------------------------------


def test_ignored_paths_on_the_drive_are_neither_brought_down_nor_trashed(
    backend: LiveBackend, api: AlkeraClient, chat: Chat, tmp_path: Path
) -> None:
    venv = api.files.create_folder(chat.drive_id, chat.working_id, ".venv")
    upload(api, tmp_path, chat.drive_id, str(venv["id"]), "pyvenv.cfg", b"home = /usr\n")
    modules = api.files.create_folder(chat.drive_id, chat.working_id, "node_modules")
    upload(api, tmp_path, chat.drive_id, str(modules["id"]), "index.js", b"x\n")
    upload(api, tmp_path, chat.drive_id, chat.working_id, "notes.md~", b"old\n")
    upload(api, tmp_path, chat.drive_id, chat.working_id, "report.md", b"# report\n")

    copy, _ = _open(api, chat, tmp_path)
    backend.log.clear()
    copy.sync()
    copy.sync()

    assert sorted(p.name for p in copy.root.iterdir()) == ["report.md"]
    assert _writes_seen(backend) == []
    names = {child["name"] for child in api.files.children(chat.drive_id, chat.working_id)}
    assert names == {".venv", "node_modules", "notes.md~", "report.md"}


def test_a_record_holding_ignored_paths_from_an_older_build_trashes_none_of_them(
    backend: LiveBackend, api: AlkeraClient, chat: Chat, tmp_path: Path
) -> None:
    venv = api.files.create_folder(chat.drive_id, chat.working_id, ".venv")
    node = upload(api, tmp_path, chat.drive_id, str(venv["id"]), "pyvenv.cfg", b"home\n")
    copy, _ = _open(api, chat, tmp_path)
    item = api.files.item(chat.drive_id, node)
    copy.record.files[".venv/pyvenv.cfg"] = CopiedFile(
        node_id=node, etag=str(item["etag"]), content_hash="h", size=5
    )

    backend.log.clear()
    copy.sync()

    assert _writes_seen(backend) == []
    assert ".venv/pyvenv.cfg" not in copy.record.files
    assert not api.files.item(chat.drive_id, node).get("trashed")


def test_editor_scratch_made_here_is_never_sent(
    api: AlkeraClient, chat: Chat, tmp_path: Path
) -> None:
    copy, _ = _open(api, chat, tmp_path)
    for name in (".#notes.md", "notes.md.swp", "notes.md~", ".notes.md.1a2b.alkera-tmp"):
        (copy.root / name).write_bytes(b"scratch\n")
    (copy.root / "notes.md").write_bytes(b"work\n")

    report = copy.sync()

    assert report.uploaded == ["notes.md"]
    names = {child["name"] for child in api.files.children(chat.drive_id, chat.working_id)}
    assert names == {"notes.md"}


def test_new_files_and_folders_made_here_go_up(
    api: AlkeraClient, chat: Chat, tmp_path: Path
) -> None:
    copy, _ = _open(api, chat, tmp_path)
    (copy.root / "src" / "lib").mkdir(parents=True)
    (copy.root / "src" / "lib" / "util.py").write_bytes(b"X = 1\n")
    (copy.root / "README.md").write_bytes(b"hello\n")

    report = copy.sync()

    assert sorted(report.uploaded) == ["README.md", "src/lib/util.py"]
    util = api.files.item_under(chat.drive_id, chat.working_id, "src/lib/util.py")
    assert remote_bytes(api, tmp_path, chat.drive_id, str(util["id"])) == b"X = 1\n"
    assert not copy.sync().changed


def test_the_agents_caches_stay_here(api: AlkeraClient, chat: Chat, tmp_path: Path) -> None:
    copy, _ = _open(api, chat, tmp_path)
    (copy.root / ".venv" / "bin").mkdir(parents=True)
    (copy.root / ".venv" / "bin" / "python").write_bytes(b"#!")
    (copy.root / "keep.txt").write_bytes(b"k\n")

    report = copy.sync()

    assert report.uploaded == ["keep.txt"]
    names = {child["name"] for child in api.files.children(chat.drive_id, chat.working_id)}
    assert names == {"keep.txt"}


def test_a_name_made_on_both_sides_with_different_bytes_is_a_conflict(
    api: AlkeraClient, chat: Chat, tmp_path: Path
) -> None:
    copy, _ = _open(api, chat, tmp_path)
    node = upload(api, tmp_path, chat.drive_id, chat.working_id, "same.txt", b"theirs\n")
    (copy.root / "same.txt").write_bytes(b"mine\n")

    report = copy.sync()

    assert report.conflicts == [Conflict(path="same.txt", reason="created_both")]
    assert (copy.root / "same.txt").read_bytes() == b"mine\n"
    assert remote_bytes(api, tmp_path, chat.drive_id, node) == b"theirs\n"


def test_a_name_made_on_both_sides_with_the_same_bytes_is_one_file(
    api: AlkeraClient, chat: Chat, tmp_path: Path
) -> None:
    copy, _ = _open(api, chat, tmp_path)
    upload(api, tmp_path, chat.drive_id, chat.working_id, "same.txt", b"identical\n")
    (copy.root / "same.txt").write_bytes(b"identical\n")

    report = copy.sync()

    assert report.conflicts == []
    assert not copy.sync().changed


def test_a_file_deleted_here_is_trashed_on_the_drive(
    api: AlkeraClient, chat: Chat, tmp_path: Path
) -> None:
    node = upload(api, tmp_path, chat.drive_id, chat.working_id, "gone.txt", b"x\n")
    copy, _ = _open(api, chat, tmp_path)

    (copy.root / "gone.txt").unlink()
    report = copy.sync()

    assert report.trashed == ["gone.txt"]
    assert api.files.item(chat.drive_id, node).get("trashed")


def test_a_folder_deleted_here_goes_from_the_drive_once_empty(
    api: AlkeraClient, chat: Chat, tmp_path: Path
) -> None:
    folder = api.files.create_folder(chat.drive_id, chat.working_id, "old")
    upload(api, tmp_path, chat.drive_id, str(folder["id"]), "a.txt", b"a\n")
    copy, _ = _open(api, chat, tmp_path)

    (copy.root / "old" / "a.txt").unlink()
    (copy.root / "old").rmdir()
    report = copy.sync()

    assert sorted(report.trashed) == ["old", "old/a.txt"]
    assert list(api.files.children(chat.drive_id, chat.working_id)) == []
    assert not (copy.root / "old").exists()


def test_a_file_deleted_here_but_changed_there_comes_back(
    api: AlkeraClient, chat: Chat, tmp_path: Path
) -> None:
    node = upload(api, tmp_path, chat.drive_id, chat.working_id, "live.txt", b"v1\n")
    copy, _ = _open(api, chat, tmp_path)

    rewrite_remote(api, chat.drive_id, node, b"v2\n")
    (copy.root / "live.txt").unlink()
    report = copy.sync()

    assert report.restored == ["live.txt"]
    assert (copy.root / "live.txt").read_bytes() == b"v2\n"
    assert not api.files.item(chat.drive_id, node).get("trashed")


# ---------------------------------------------------------------------------
# Up, awake (a machine holds the chat's lease)
# ---------------------------------------------------------------------------


def _hold(api: AlkeraClient, chat: Chat, *, purpose: str) -> dict[str, Any]:
    return api.files.acquire_lease(
        chat.drive_id,
        chat.folder_id,
        instance_id=f"box-{uuid.uuid4().hex[:8]}",
        machine_id="runner-7",
        purpose=purpose,
        if_match=api.files.item(chat.drive_id, chat.folder_id),
    )


def test_awake_a_save_under_the_working_directory_is_taken_as_an_inbound_write(
    api: AlkeraClient, chat: Chat, tmp_path: Path
) -> None:
    node = upload(api, tmp_path, chat.drive_id, chat.working_id, "plot.py", b"print(1)\n")
    copy, _ = _open(api, chat, tmp_path)
    grant = _hold(api, chat, purpose="chat")

    (copy.root / "plot.py").write_bytes(b"print(3)\n")
    report = copy.sync()

    assert report.uploaded == ["plot.py"] and report.refused == []
    assert remote_bytes(api, tmp_path, chat.drive_id, node) == b"print(3)\n"
    held = api.files.my_leases(chat.drive_id)
    assert [lease["epoch"] for lease in held if lease.get("nodeId") == chat.folder_id] == [
        grant["epoch"]
    ]


def test_a_lease_that_takes_no_inbound_writes_refuses_the_save_and_says_why(
    api: AlkeraClient, chat: Chat, tmp_path: Path
) -> None:
    node = upload(api, tmp_path, chat.drive_id, chat.working_id, "plot.py", b"print(1)\n")
    copy, _ = _open(api, chat, tmp_path)
    _hold(api, chat, purpose="mount")

    (copy.root / "plot.py").write_bytes(b"print(4)\n")
    report = copy.sync()

    assert report.uploaded == []
    assert [(refusal.path, refusal.code) for refusal in report.refused] == [
        ("plot.py", "files.leased")
    ]
    assert "held by the chat's machine" in report.refused[0].message
    assert (copy.root / "plot.py").read_bytes() == b"print(4)\n"
    assert remote_bytes(api, tmp_path, chat.drive_id, node) == b"print(1)\n"


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------


class _NoFolder:
    def read_chat(self, chat_id: str) -> dict[str, Any]:
        return {"id": chat_id, "title": "Empty", "files_node_id": None, "files_drive_id": None}


@pytest.mark.parametrize(
    ("target", "reason"),
    [
        pytest.param(CopyTarget("chat", "../../etc"), "invalid", id="a-path-is-not-an-id"),
        pytest.param(CopyTarget("chat", "not-a-uuid"), "invalid", id="garbage-is-not-an-id"),
        pytest.param(
            CopyTarget("workspace", str(uuid.uuid4())),
            "not_found",
            id="a-server-without-workspaces",
        ),
        pytest.param(CopyTarget("chat", str(uuid.uuid4())), "not_found", id="a-chat-nobody-has"),
    ],
)
def test_a_target_that_cannot_be_copied_says_which_way(
    api: AlkeraClient, tmp_path: Path, target: CopyTarget, reason: str
) -> None:
    with pytest.raises(TargetUnavailableError) as refused:
        WorkingCopy.open(
            target,
            location=tmp_path / "copies",
            files=api.files,
            http=http_of(api),
            reader=HttpTargetReader(http_of(api)),
            store=WorkingCopyStore(tmp_path / "home"),
        )
    assert refused.value.reason == reason
    assert not (tmp_path / "copies").exists()


def test_a_chat_with_no_folder_has_nothing_to_copy(api: AlkeraClient, tmp_path: Path) -> None:
    with pytest.raises(TargetUnavailableError) as refused:
        WorkingCopy.open(
            CopyTarget("chat", str(uuid.uuid4())),
            location=tmp_path / "copies",
            files=api.files,
            http=http_of(api),
            reader=_NoFolder(),
            store=WorkingCopyStore(tmp_path / "home"),
        )
    assert refused.value.reason == "no_files"


class _Workspaces(HttpTargetReader):
    """The real chat read, with a workspace read scripted in the shape
    ``GET /api/v1/workspaces/{id}`` answers (this backend predates the route)."""

    def __init__(self, http: httpx.Client, workspace: dict[str, Any]) -> None:
        super().__init__(http)
        self._workspace = workspace

    def read_workspace(self, workspace_id: str) -> dict[str, Any]:
        return {"id": workspace_id, **self._workspace}


def _open_workspace(
    api: AlkeraClient, tmp_path: Path, workspace: dict[str, Any], workspace_id: str
) -> tuple[WorkingCopy, Any]:
    return WorkingCopy.open(
        CopyTarget("workspace", workspace_id),
        location=tmp_path / "copies",
        files=api.files,
        http=http_of(api),
        reader=_Workspaces(http_of(api), workspace),
        store=WorkingCopyStore(tmp_path / "home"),
    )


def test_a_workspace_of_one_copies_its_chats_working_directory_never_the_transcript(
    api: AlkeraClient, chat: Chat, tmp_path: Path
) -> None:
    """A workspace of one has the chat's folder as its folder, transcript and
    all. Its link copies the chat's working directory, and shares the copy the
    chat's own link makes."""
    upload(api, tmp_path, chat.drive_id, chat.working_id, "report.md", b"# report\n")
    # The chat's records sit beside the working directory (a member may make
    # the runtime folder but not write into it, which is all this needs).
    api.files.create_folder(chat.drive_id, chat.folder_id, ".runtime")
    beside = {child["name"] for child in api.files.children(chat.drive_id, chat.folder_id)}
    assert beside == {".runtime", "scratch"}  # the records really sit beside it
    workspace = {
        "title": "Quarterly plan",
        "adopted_chat_id": chat.id,
        "files_drive_id": chat.drive_id,
        "files_node_id": chat.folder_id,
        "working_node_id": chat.working_id,
    }

    copy, _ = _open_workspace(api, tmp_path, workspace, str(uuid.uuid4()))

    assert sorted(p.name for p in copy.root.iterdir()) == ["report.md"]
    assert copy.record.node_id == chat.working_id
    assert (copy.record.kind, copy.record.target_id) == ("chat", chat.id)
    by_chat, _ = _open(api, chat, tmp_path)
    assert by_chat.root == copy.root


@pytest.mark.parametrize(
    "workspace",
    [
        pytest.param({"working_node_id": None}, id="names-no-working-tree"),
        pytest.param({"working_node_id": "FOLDER"}, id="names-its-own-folder"),
    ],
)
def test_a_workspace_read_that_names_no_safe_tree_copies_nothing(
    api: AlkeraClient, chat: Chat, tmp_path: Path, workspace: dict[str, Any]
) -> None:
    # The chat's records sit beside the working directory (a member may make
    # the runtime folder but not write into it, which is all this needs).
    api.files.create_folder(chat.drive_id, chat.folder_id, ".runtime")
    read = {
        "title": "Plan",
        "files_drive_id": chat.drive_id,
        "files_node_id": chat.folder_id,
        "working_node_id": chat.folder_id
        if workspace["working_node_id"] == "FOLDER"
        else workspace["working_node_id"],
    }

    with pytest.raises(TargetUnavailableError) as refused:
        _open_workspace(api, tmp_path, read, str(uuid.uuid4()))
    assert refused.value.reason == "no_files"
    assert not (tmp_path / "copies").exists()


def test_a_project_workspace_copies_its_shared_files_tree(
    api: AlkeraClient, chat: Chat, tmp_path: Path
) -> None:
    """The box-side workspaces lane serves ``files/`` as the working tree; the
    copy follows ``working_node_id`` wherever it points."""
    shared = api.files.create_folder(chat.drive_id, chat.working_id, "files")
    upload(api, tmp_path, chat.drive_id, str(shared["id"]), "shared.csv", b"a,b\n")
    workspace_id = str(uuid.uuid4())
    read = {
        "title": "Analytics",
        "files_drive_id": chat.drive_id,
        "files_node_id": chat.working_id,
        "working_node_id": str(shared["id"]),
    }

    copy, _ = _open_workspace(api, tmp_path, read, workspace_id)

    assert _local_tree(copy.root) == {"shared.csv"}
    assert (copy.record.kind, copy.record.target_id) == ("workspace", workspace_id)


# ---------------------------------------------------------------------------
# The tree stops being ours
# ---------------------------------------------------------------------------


def _shared_tree_copy(
    api: AlkeraClient, chat: Chat, tmp_path: Path
) -> tuple[WorkingCopy, dict[str, Any]]:
    """A copy of a plain shared folder, whose access the test can take away."""
    shared = api.files.create_folder(chat.drive_id, chat.working_id, "shared")
    upload(api, tmp_path, chat.drive_id, str(shared["id"]), "data.csv", b"a,b\n")
    read = {
        "title": "Analytics",
        "files_drive_id": chat.drive_id,
        "files_node_id": chat.working_id,
        "working_node_id": str(shared["id"]),
    }
    copy, _ = _open_workspace(api, tmp_path, read, str(uuid.uuid4()))
    return copy, shared


def _writes(backend: LiveBackend) -> list[tuple[str, str]]:
    return [(m, p) for m, p in backend.log.seen if m in {"POST", "PUT", "PATCH", "DELETE"}]


@pytest.mark.parametrize("how", ["trashed", "no_longer_visible"])
def test_a_tree_this_account_lost_stops_the_copy_and_keeps_every_local_file(
    backend: LiveBackend, api: AlkeraClient, chat: Chat, tmp_path: Path, how: str
) -> None:
    """A revoked share answers the tree's root with an opaque 404, exactly as a
    deleted folder does. Read as "every file was deleted", the copy would
    delete local work or push it all back as new; it must do neither."""
    copy, shared = _shared_tree_copy(api, chat, tmp_path)
    (copy.root / "data.csv").write_bytes(b"edited here\n")
    (copy.root / "draft.md").write_bytes(b"made here\n")
    before = copy.record.model_dump()
    if how == "trashed":
        api.files.trash(chat.drive_id, str(shared["id"]), if_match=shared)
    else:
        # The node this account could read is no longer one it can find.
        copy.record.node_id = str(uuid.uuid4())
        before = copy.record.model_dump()

    backend.log.clear()
    with pytest.raises(AccessLostError) as lost:
        copy.sync()

    assert str(lost.value) == "You no longer have access to Analytics. Local files are kept."
    assert _writes(backend) == []
    assert (copy.root / "data.csv").read_bytes() == b"edited here\n"
    assert (copy.root / "draft.md").read_bytes() == b"made here\n"
    assert copy.record.model_dump() == before
    with pytest.raises(AccessLostError):
        copy.refresh(frozenset({"data.csv"}))
    with pytest.raises(AccessLostError):
        copy.resolve("data.csv", "theirs")
    assert (copy.root / "data.csv").read_bytes() == b"edited here\n"


def test_the_runner_stops_for_good_when_access_is_lost(
    api: AlkeraClient, chat: Chat, tmp_path: Path
) -> None:
    copy, shared = _shared_tree_copy(api, chat, tmp_path)
    api.files.trash(chat.drive_id, str(shared["id"]), if_match=shared)
    runner, reports, errors = _runner(copy)

    async def scenario() -> bool:
        runner.start()
        runner.request()
        await _until(lambda: bool(errors))
        runner.request()
        await asyncio.sleep(0.5)
        return runner.running

    still_running = asyncio.run(scenario())
    assert [type(error) for error in errors] == [AccessLostError]
    assert still_running is False
    assert reports == []


def test_attach_finds_the_copy_living_in_a_folder(
    api: AlkeraClient, chat: Chat, tmp_path: Path
) -> None:
    copy, _ = _open(api, chat, tmp_path)
    store = WorkingCopyStore(tmp_path / "home")

    found = WorkingCopy.attach(copy.root, files=api.files, http=http_of(api), store=store)
    stranger = WorkingCopy.attach(tmp_path, files=api.files, http=http_of(api), store=store)

    assert found is not None and found.record.target_id == chat.id
    assert stranger is None


# ---------------------------------------------------------------------------
# While it is open: the runner decides when a pass runs
# ---------------------------------------------------------------------------


class _Feed:
    """A realtime stream a test writes frames into."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def frames(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            yield await self.queue.get()


async def _until(check: Callable[[], bool], *, within: float = 15.0) -> None:
    deadline = asyncio.get_running_loop().time() + within
    while not check():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("the condition never held")
        await asyncio.sleep(0.05)


def _runner(
    copy: WorkingCopy, *, frames: Any = None, watch: Any = None
) -> tuple[WorkingCopyRunner, list[SyncReport], list[BaseException]]:
    reports: list[SyncReport] = []
    errors: list[BaseException] = []

    async def on_report(report: SyncReport) -> None:
        reports.append(report)

    async def on_error(exc: BaseException) -> None:
        errors.append(exc)

    runner = WorkingCopyRunner(
        copy,
        frames=frames,
        watch=watch,
        on_report=on_report,
        on_error=on_error,
        debounce=0.05,
        full_debounce=0.1,
        poll_seconds=3600,
        backoff=ReconnectBackoff(base=0.05, cap=0.05, healthy_period=60, rng=lambda: 0.5),
    )
    return runner, reports, errors


def test_a_frame_naming_the_copy_brings_the_drives_change_down_and_others_do_not(
    api: AlkeraClient, chat: Chat, tmp_path: Path
) -> None:
    node = upload(api, tmp_path, chat.drive_id, chat.working_id, "notes.txt", b"one\n")
    copy, _ = _open(api, chat, tmp_path)
    feed = _Feed()
    runner, reports, _ = _runner(copy, frames=feed.frames)

    async def scenario() -> None:
        runner.start()
        try:
            await asyncio.to_thread(rewrite_remote, api, chat.drive_id, node, b"two\n")
            stranger = str(uuid.uuid4())
            await feed.queue.put(
                {
                    "type": "file_node.changed",
                    "data": {"entity_id": stranger, "parent_id": stranger},
                }
            )
            # Long enough for a pass to have run and reported had the frame
            # started one: a pass here takes well under a second.
            await asyncio.sleep(3.0)
            assert reports == []
            assert (copy.root / "notes.txt").read_bytes() == b"one\n"
            await feed.queue.put(
                {
                    "type": "file_node.changed",
                    "data": {"entity_id": node, "parent_id": chat.working_id},
                }
            )
            await _until(lambda: (copy.root / "notes.txt").read_bytes() == b"two\n")
        finally:
            await runner.stop()

    asyncio.run(scenario())
    assert [report.downloaded for report in reports] == [["notes.txt"]]


def test_a_reopened_stream_catches_up_on_what_it_missed(
    api: AlkeraClient, chat: Chat, tmp_path: Path
) -> None:
    node = upload(api, tmp_path, chat.drive_id, chat.working_id, "notes.txt", b"one\n")
    copy, _ = _open(api, chat, tmp_path)
    rewrite_remote(api, chat.drive_id, node, b"changed while the stream was down\n")
    opened = 0

    async def frames() -> AsyncIterator[dict[str, Any]]:
        nonlocal opened
        opened += 1
        if opened == 1:
            raise httpx.ReadError("the stream dropped")
        yield {"type": STREAM_OPENED, "id": None, "data": None}
        await asyncio.Event().wait()

    runner, _, _ = _runner(copy, frames=frames)

    async def scenario() -> None:
        runner.start()
        try:
            await _until(
                lambda: (
                    (copy.root / "notes.txt").read_bytes() == b"changed while the stream was down\n"
                )
            )
        finally:
            await runner.stop()

    asyncio.run(scenario())
    assert opened == 2


def test_a_change_on_disk_goes_up_without_being_asked(
    api: AlkeraClient, chat: Chat, tmp_path: Path
) -> None:
    node = upload(api, tmp_path, chat.drive_id, chat.working_id, "plot.py", b"print(1)\n")
    copy, _ = _open(api, chat, tmp_path)
    batches: asyncio.Queue[object] = asyncio.Queue()

    async def watch(root: Path) -> AsyncIterator[object]:
        assert root == copy.root
        while True:
            yield await batches.get()

    runner, reports, _ = _runner(copy, watch=watch)

    async def scenario() -> None:
        runner.start()
        try:
            (copy.root / "plot.py").write_bytes(b"print(5)\n")
            await batches.put({"modified": "plot.py"})
            await _until(lambda: bool(reports))
        finally:
            await runner.stop()

    asyncio.run(scenario())
    assert reports[0].uploaded == ["plot.py"]
    assert remote_bytes(api, tmp_path, chat.drive_id, node) == b"print(5)\n"


def test_a_conflict_is_reported_by_the_runner_and_settled_through_it(
    api: AlkeraClient, chat: Chat, tmp_path: Path
) -> None:
    node = upload(api, tmp_path, chat.drive_id, chat.working_id, "plan.md", b"base\n")
    copy, _ = _open(api, chat, tmp_path)
    rewrite_remote(api, chat.drive_id, node, b"theirs\n")
    (copy.root / "plan.md").write_bytes(b"mine\n")
    runner, _, _ = _runner(copy)

    async def scenario() -> tuple[SyncReport, SyncReport]:
        first = await runner.sync_now()
        settled = await runner.resolve("plan.md", "mine")
        return first, settled

    first, settled = asyncio.run(scenario())
    assert first.conflicts == [Conflict(path="plan.md", reason="edited_both")]
    assert settled.conflicts == []
    assert remote_bytes(api, tmp_path, chat.drive_id, node) == b"mine\n"


# ---------------------------------------------------------------------------
# A file with unsaved edits in the editor, while the drive keeps changing it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pass_kind", ["full", "single_file"])
def test_a_dirty_file_keeps_its_base_and_its_bytes_while_the_drive_moves_on(
    api: AlkeraClient, chat: Chat, tmp_path: Path, pass_kind: str
) -> None:
    """The browser writes the file back every couple of seconds while the person
    types in VS Code. The copy must neither write the browser's bytes under the
    unsaved buffer nor move the version the buffer's save will name, or that
    save lands as a plain new version over the browser's edits."""
    node = upload(api, tmp_path, chat.drive_id, chat.working_id, "live.md", b"base\n")
    copy, _ = _open(api, chat, tmp_path)
    base = copy.record.files["live.md"].etag
    copy.set_dirty(frozenset({"live.md"}))

    rewrite_remote(api, chat.drive_id, node, b"typed in the browser\n")
    report = copy.sync() if pass_kind == "full" else copy.refresh(frozenset({"live.md"}))

    assert (copy.root / "live.md").read_bytes() == b"base\n"
    assert copy.record.files["live.md"].etag == base
    assert report.downloaded == [] and report.conflicts == []

    # The buffer is saved: the save names the base it was edited from, so the
    # drive refuses it as a conflict instead of overwriting the browser.
    (copy.root / "live.md").write_bytes(b"typed in VS Code\n")
    copy.set_dirty(frozenset())
    saved = copy.sync(pull=False)

    assert saved.conflicts == [Conflict(path="live.md", reason="edited_both")]
    assert remote_bytes(api, tmp_path, chat.drive_id, node) == b"typed in the browser\n"


def test_a_dirty_buffer_reverted_without_saving_catches_up(
    api: AlkeraClient, chat: Chat, tmp_path: Path
) -> None:
    node = upload(api, tmp_path, chat.drive_id, chat.working_id, "live.md", b"base\n")
    copy, _ = _open(api, chat, tmp_path)
    feed = _Feed()
    runner, _, _ = _runner(copy, frames=feed.frames)
    frame = {"type": "file_node.changed", "data": {"entity_id": node, "parent_id": chat.working_id}}

    async def scenario() -> None:
        runner.start()
        try:
            runner.set_dirty(frozenset({"live.md"}))
            await asyncio.to_thread(rewrite_remote, api, chat.drive_id, node, b"newer\n")
            await feed.queue.put(frame)
            await asyncio.sleep(1.5)
            assert (copy.root / "live.md").read_bytes() == b"base\n"
            runner.set_dirty(frozenset())
            await _until(lambda: (copy.root / "live.md").read_bytes() == b"newer\n")
        finally:
            await runner.stop()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Change frames read only what they name
# ---------------------------------------------------------------------------


def _tree_listings(backend: LiveBackend) -> int:
    return sum(1 for _method, path in backend.log.seen if path.endswith("/children"))


def _content_reads(backend: LiveBackend) -> int:
    return sum(
        1 for method, path in backend.log.seen if method == "GET" and path.endswith("/content")
    )


def test_a_frame_naming_one_file_reads_that_file_and_not_the_tree(
    backend: LiveBackend, api: AlkeraClient, chat: Chat, tmp_path: Path
) -> None:
    node = upload(api, tmp_path, chat.drive_id, chat.working_id, "live.md", b"one\n")
    upload(api, tmp_path, chat.drive_id, chat.working_id, "other.md", b"other\n")
    sub = api.files.create_folder(chat.drive_id, chat.working_id, "sub")
    upload(api, tmp_path, chat.drive_id, str(sub["id"]), "deep.md", b"deep\n")
    copy, _ = _open(api, chat, tmp_path)
    feed = _Feed()
    runner, reports, _ = _runner(copy, frames=feed.frames)
    frame = {"type": "file_node.changed", "data": {"entity_id": node, "parent_id": chat.working_id}}

    async def scenario() -> None:
        runner.start()
        try:
            await asyncio.to_thread(rewrite_remote, api, chat.drive_id, node, b"two\n")
            backend.log.clear()
            await feed.queue.put(frame)
            await _until(lambda: bool(reports))
        finally:
            await runner.stop()

    asyncio.run(scenario())
    assert (copy.root / "live.md").read_bytes() == b"two\n"
    assert reports[0].downloaded == ["live.md"]
    assert _tree_listings(backend) == 0
    assert _content_reads(backend) == 1


def test_a_frame_for_a_node_the_copy_does_not_know_reads_the_tree(
    api: AlkeraClient, chat: Chat, tmp_path: Path
) -> None:
    """A new file is named by its folder only, so the copy has to look."""
    copy, _ = _open(api, chat, tmp_path)
    feed = _Feed()
    runner, _, _ = _runner(copy, frames=feed.frames)

    async def scenario() -> None:
        runner.start()
        try:
            fresh = await asyncio.to_thread(
                upload, api, tmp_path, chat.drive_id, chat.working_id, "new.md", b"new\n"
            )
            await feed.queue.put(
                {
                    "type": "file_node.changed",
                    "data": {"entity_id": fresh, "parent_id": chat.working_id},
                }
            )
            await _until(lambda: (copy.root / "new.md").exists())
        finally:
            await runner.stop()

    asyncio.run(scenario())
    assert (copy.root / "new.md").read_bytes() == b"new\n"


def test_a_file_renamed_on_the_drive_falls_back_to_reading_the_tree(
    api: AlkeraClient, chat: Chat, tmp_path: Path
) -> None:
    node = upload(api, tmp_path, chat.drive_id, chat.working_id, "draft.md", b"text\n")
    copy, _ = _open(api, chat, tmp_path)
    feed = _Feed()
    runner, _, _ = _runner(copy, frames=feed.frames)

    async def scenario() -> None:
        runner.start()
        try:
            await asyncio.to_thread(
                api.files.rename,
                chat.drive_id,
                node,
                "final.md",
                if_match=api.files.item(chat.drive_id, node),
            )
            await feed.queue.put(
                {
                    "type": "file_node.changed",
                    "data": {"entity_id": node, "parent_id": chat.working_id},
                }
            )
            await _until(lambda: (copy.root / "final.md").exists())
        finally:
            await runner.stop()

    asyncio.run(scenario())
    assert not (copy.root / "draft.md").exists()
    assert (copy.root / "final.md").read_bytes() == b"text\n"


def test_a_save_sends_without_reading_the_tree(
    backend: LiveBackend, api: AlkeraClient, chat: Chat, tmp_path: Path
) -> None:
    node = upload(api, tmp_path, chat.drive_id, chat.working_id, "plot.py", b"print(1)\n")
    copy, _ = _open(api, chat, tmp_path)
    (copy.root / "plot.py").write_bytes(b"print(9)\n")

    backend.log.clear()
    report = copy.sync(pull=False)

    assert report.uploaded == ["plot.py"]
    assert _tree_listings(backend) == 0
    assert remote_bytes(api, tmp_path, chat.drive_id, node) == b"print(9)\n"


class _HashlessReads:
    """The drive, except that a single-item read carries no content hash, as a
    read that did not select the file facet's hash would."""

    def __init__(self, files: Any) -> None:
        self._files = files

    def __getattr__(self, name: str) -> Any:
        return getattr(self._files, name)

    def item(self, drive_id: str, item_id: str, **kwargs: Any) -> dict[str, Any]:
        read = dict(self._files.item(drive_id, item_id, **kwargs))
        facet = dict(read.get("file") or {})
        facet.pop("contentHash", None)
        facet.pop("content_hash", None)
        read["file"] = facet
        return read


@pytest.mark.parametrize(
    ("hash_carried", "fetched"),
    [
        pytest.param(True, False, id="hash-carried-same-bytes-not-fetched"),
        pytest.param(False, True, id="hash-unknown-fetched-and-verified"),
    ],
)
def test_a_moved_etag_with_no_hash_to_compare_is_fetched_not_assumed_the_same(
    api: AlkeraClient,
    chat: Chat,
    tmp_path: Path,
    *,
    hash_carried: bool,
    fetched: bool,
) -> None:
    """A node whose etag moved with its bytes unchanged is recognised by its
    hash. A read that carries no hash says nothing about the bytes, so the copy
    fetches them rather than conclude they match."""
    upload(api, tmp_path, chat.drive_id, chat.working_id, "notes.txt", b"one\n")
    copy, _ = WorkingCopy.open(
        CopyTarget("chat", chat.id),
        location=tmp_path / "copies",
        files=api.files if hash_carried else _HashlessReads(api.files),
        http=http_of(api),
        reader=HttpTargetReader(http_of(api)),
        store=WorkingCopyStore(tmp_path / "home"),
    )
    node = copy.record.files["notes.txt"].node_id
    before = copy.record.files["notes.txt"].etag
    # An attribute edit moves the etag without touching the bytes.
    http_of(api).patch(
        f"/api/v1/files/drives/{chat.drive_id}/items/{node}",
        json={"attrs": {"mode": 0o600}},
        headers={"Idempotency-Key": str(uuid.uuid4()), "If-Match": before},
    ).raise_for_status()
    assert api.files.item(chat.drive_id, node)["etag"] != before

    report = copy.refresh(frozenset({"notes.txt"}))

    assert report.downloaded == (["notes.txt"] if fetched else [])
    assert (copy.root / "notes.txt").read_bytes() == b"one\n"
    assert copy.record.files["notes.txt"].etag == api.files.item(chat.drive_id, node)["etag"]
