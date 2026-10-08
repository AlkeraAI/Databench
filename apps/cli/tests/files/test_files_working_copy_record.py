"""The working copy's pure parts: its record, its folder names, and which
realtime frames concern it.

No drive here: each of these is plain data in, plain data out, and is pinned on
its own so the live suite beside it can stay about what the drive does.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from alkera_cli.files.working_copy import (
    CopiedFile,
    HttpTargetReader,
    TargetUnavailableError,
    WorkingCopyAuthError,
    WorkingCopyRecord,
    WorkingCopyStore,
    concerns,
    deletion_needs_confirmation,
    folder_name_for,
    ignored,
    registered_kinds,
)

# ---------------------------------------------------------------------------
# The workspace read: a server without workspaces is a fallback, not a miss
# ---------------------------------------------------------------------------


def _reader(status: int, body: Any) -> HttpTargetReader:
    def serve(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    return HttpTargetReader(
        httpx.Client(transport=httpx.MockTransport(serve), base_url="https://api.test")
    )


@pytest.mark.parametrize(
    ("status", "body", "reason"),
    [
        pytest.param(
            404,
            {"error": {"code": "not_found", "message": "Not Found"}},
            "not_found",
            id="a-server-without-workspaces",
        ),
        pytest.param(
            404,
            {"error": {"code": "not_found", "message": "no"}},
            "not_found",
            id="no-such-workspace",
        ),
        pytest.param(403, {"error": {"code": "forbidden"}}, "not_found", id="not-shared"),
    ],
)
def test_a_workspace_that_cannot_be_read_says_which_way(
    status: int, body: Any, reason: str
) -> None:
    with pytest.raises(TargetUnavailableError) as refused:
        _reader(status, body).read_workspace("w1")
    assert refused.value.reason == reason


def test_a_refused_sign_in_on_the_workspace_read_asks_for_a_new_one() -> None:
    with pytest.raises(WorkingCopyAuthError):
        _reader(401, {"detail": "no"}).read_workspace("w1")


_FIXTURES = Path(__file__).parent / "fixtures" / "working_copy_record"

ROOT = "aaaaaaaa-0000-4000-8000-000000000001"
CHAT = "aaaaaaaa-0000-4000-8000-000000000002"
SUB = "aaaaaaaa-0000-4000-8000-000000000003"
FILE = "aaaaaaaa-0000-4000-8000-000000000004"
ELSEWHERE = "bbbbbbbb-0000-4000-8000-000000000009"


def _record() -> WorkingCopyRecord:
    return WorkingCopyRecord(
        kind="chat",
        target_id="cccccccc-0000-4000-8000-000000000001",
        node_id=ROOT,
        watch_ids=[CHAT],
        folders={"sub": SUB},
        files={"sub/a.txt": CopiedFile(node_id=FILE, etag="1", content_hash="h", size=1)},
    )


# ---------------------------------------------------------------------------
# The record's own shape over time
# ---------------------------------------------------------------------------


def _fixtures() -> list[Path]:
    return sorted(_FIXTURES.glob("v*.json"))


def test_the_corpus_covers_the_version_this_build_writes() -> None:
    versions = {path.stem for path in _fixtures()}
    assert f"v{WorkingCopyRecord.SCHEMA_VERSION.replace('.', '_')}" in versions


@pytest.mark.parametrize("fixture", _fixtures(), ids=lambda path: cast(Path, path).stem)
def test_every_record_a_past_writer_produced_still_loads(fixture: Path) -> None:
    """A copy made by an older build keeps syncing after an upgrade. The fix for
    a failure here is a migration, never an edit to the fixture."""
    record = WorkingCopyRecord.model_validate(json.loads(fixture.read_text(encoding="utf-8")))

    assert record.kind == "chat"
    assert record.files["data/a.csv"].etag == "3"
    assert record.folders == {"data": "5f0c1c55-0000-4000-8000-000000000002"}


def test_a_field_from_a_newer_writer_survives_a_round_trip(tmp_path: Path) -> None:
    store = WorkingCopyStore(tmp_path)
    raw = _record().model_dump(mode="json") | {"workspace_id": "from-a-newer-build"}
    store.directory.mkdir(parents=True)
    store.path_for("chat", raw["target_id"]).write_text(json.dumps(raw), encoding="utf-8")

    loaded = store.load("chat", raw["target_id"])
    assert loaded is not None
    store.save(loaded)

    saved = json.loads(store.path_for("chat", raw["target_id"]).read_text(encoding="utf-8"))
    assert saved["workspace_id"] == "from-a-newer-build"


def test_an_unreadable_record_is_no_record(tmp_path: Path) -> None:
    store = WorkingCopyStore(tmp_path)
    store.directory.mkdir(parents=True)
    store.path_for("chat", "x").write_text("{not json", encoding="utf-8")

    assert store.load("chat", "x") is None
    assert store.find_by_root(tmp_path) is None


def test_chats_and_workspaces_are_both_targets() -> None:
    assert "chat" in registered_kinds()
    assert "workspace" in registered_kinds()


# ---------------------------------------------------------------------------
# Folder names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        pytest.param("Quarterly plan", "Quarterly plan 12345678", id="plain"),
        pytest.param("../../etc/passwd", "etc passwd 12345678", id="no-traversal"),
        pytest.param("a/b\\c:d", "a b c d 12345678", id="no-separators"),
        pytest.param("", "chat 12345678", id="untitled"),
        pytest.param("...", "chat 12345678", id="only-dots"),
        pytest.param("x" * 200, "x" * 60 + " 12345678", id="bounded"),
    ],
)
def test_a_folder_name_is_one_safe_component(title: str, expected: str) -> None:
    name = folder_name_for(title, "12345678-0000-4000-8000-000000000000")

    assert name == expected
    assert "/" not in name and "\\" not in name and name not in {".", ".."}


# ---------------------------------------------------------------------------
# Which frames concern a copy
# ---------------------------------------------------------------------------


def _frame(kind: str, **data: Any) -> dict[str, Any]:
    return {"id": "41", "type": kind, "data": data}


@pytest.mark.parametrize(
    ("frame", "expected"),
    [
        pytest.param(
            _frame("file_node.changed", parent_id=ROOT, entity_id=ELSEWHERE),
            True,
            id="new-file-at-the-root",
        ),
        pytest.param(
            _frame("file_node.changed", parent_id=SUB, entity_id=ELSEWHERE),
            True,
            id="new-file-in-a-known-folder",
        ),
        pytest.param(
            _frame("file_node.changed", entity_id=FILE), True, id="a-known-file-renamed-no-parent"
        ),
        pytest.param(
            _frame("file_lease.changed", lease_node_id=CHAT), True, id="the-box-took-the-chat"
        ),
        pytest.param(
            _frame("file_lease.changed", entity_id=CHAT), True, id="older-server-names-the-entity"
        ),
        pytest.param(
            _frame("file_node.changed", parent_id=ELSEWHERE, entity_id=ELSEWHERE),
            False,
            id="somebody-elses-folder",
        ),
        pytest.param(
            _frame("file_lease.changed", lease_node_id=ELSEWHERE), False, id="somebody-elses-lease"
        ),
        pytest.param(_frame("chat.updated", entity_id=CHAT), False, id="not-a-files-frame"),
        pytest.param(_frame("file_node.changed"), False, id="names-nothing"),
        pytest.param(
            {"type": "file_node.changed", "data": "garbled"}, False, id="data-not-an-object"
        ),
        pytest.param(
            _frame("file_node.changed", parent_id=""), False, id="empty-id-is-no-wildcard"
        ),
    ],
)
def test_a_frame_concerns_the_copy_only_when_it_names_something_the_copy_knows(
    frame: dict[str, Any], expected: bool
) -> None:
    assert concerns(_record(), frame) is expected


# ---------------------------------------------------------------------------
# The one ignore policy, for both directions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        pytest.param(".venv/bin/python", id="venv"),
        pytest.param("app/node_modules/left-pad/index.js", id="node-modules-deep"),
        pytest.param("src/__pycache__/m.cpython-313.pyc", id="pycache"),
        pytest.param(".cache/x", id="cache"),
        pytest.param(".DS_Store", id="finder"),
        pytest.param("._report.md", id="appledouble"),
        pytest.param("notes.md.swp", id="vim-swap"),
        pytest.param("4913", id="vim-probe"),
        pytest.param("notes.md~", id="backup"),
        pytest.param(".#notes.md", id="emacs-lock"),
        pytest.param("#notes.md#", id="emacs-autosave"),
        pytest.param("report.md___jb_tmp___", id="jetbrains-tmp"),
        pytest.param("report.md.crswap", id="chrome-swap"),
        pytest.param(".report.md.3f2a.alkera-tmp", id="alkera-tmp"),
        pytest.param("data.csv.alkera-part", id="pull-sidecar"),
    ],
)
def test_tool_and_editor_scratch_is_ignored(path: str) -> None:
    assert ignored(path) is True


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("report.md", id="plain"),
        pytest.param("src/venv-notes/readme.md", id="venv-in-a-longer-name"),
        pytest.param("swp.md", id="swp-as-a-stem"),
        pytest.param("deep/tree/plot.py", id="deep"),
        pytest.param(".gitignore", id="dotfile"),
    ],
)
def test_work_is_not_ignored(path: str) -> None:
    assert ignored(path) is False


@pytest.mark.parametrize(
    ("deleting", "known", "asks"),
    [
        pytest.param(1, 1, False, id="the-only-file"),
        pytest.param(4, 4, False, id="under-the-floor"),
        pytest.param(5, 9, True, id="most-of-a-small-tree"),
        pytest.param(5, 10, False, id="half-is-not-more-than-half"),
        pytest.param(19, 200, False, id="a-few-of-a-large-tree"),
        pytest.param(20, 1000, True, id="twenty-is-always-too-many"),
    ],
)
def test_a_large_deletion_asks_first(deleting: int, known: int, asks: bool) -> None:
    assert deletion_needs_confirmation(deleting, known) is asks
