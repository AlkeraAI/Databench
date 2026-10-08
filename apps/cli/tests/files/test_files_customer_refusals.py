"""What a customer meets: a re-mount that saves their work, and sentences.

Every case here was reached by a person running the real CLI against the live
stack, and every one of them ended in something a person could not act on — an
appended line silently gone, an httpx traceback with an MDN link, a raw uuid
where a colleague's name belongs, a mount reported "stale" seconds after it
succeeded. The mechanisms that fix them are pinned here through the same door
the person used: ``CliRunner`` over the real command, and the library itself
where the decision is the library's.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from _profiles import ORG_A, make_jwt, store
from alkera_cli.commands import files as files_cli
from alkera_cli.files.mount import MountRecord, MountStatus, mount, save_record
from alkera_cli.files.progress import TransferProgress
from alkera_cli.files.pull import LocalChangesError, PullSummary, pull
from alkera_cli.main import app
from alkera_sdk.client import AlkeraAuthError, AlkeraHTTPError
from rich.console import Console
from typer.testing import CliRunner

runner = CliRunner()

DRIVE = "11111111-1111-1111-1111-111111111111"
NODE = "22222222-2222-2222-2222-222222222222"


def _blake3_of(payload: bytes) -> str:
    from blake3 import blake3

    return str(blake3(payload).hexdigest())


class OneFileDrive:
    """A folder holding exactly one file whose bytes the server knows."""

    def __init__(self, name: str = "README.md", payload: bytes = b"the cloud copy\n") -> None:
        self.name = name
        self.payload = payload
        self.content_requests: list[str] = []

    def drive(self) -> dict[str, Any]:
        return {"id": DRIVE}

    def item_by_path(self, drive_id: str, item_path: str) -> dict[str, Any]:
        return {"id": NODE, "etag": "7", "kind": "folder", "name": item_path}

    def children(self, drive_id: str, item_id: str, **_: Any) -> list[dict[str, Any]]:
        if item_id != NODE:
            return []
        return [
            {
                "id": "33333333-3333-3333-3333-333333333333",
                "name": self.name,
                "kind": "file",
                "file": {"contentHash": _blake3_of(self.payload), "size": len(self.payload)},
            }
        ]

    def client(self, *, epoch: int = 5) -> httpx.Client:
        """The wire: the lease routes answer JSON, everything else is bytes."""

        def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith(("/lease", "/lease/heartbeat")):
                return httpx.Response(
                    200, json={"epoch": epoch, "expiresAt": "", "heartbeatEvery": 15.0}
                )
            if request.url.path.endswith("/content"):
                self.content_requests.append(request.url.path)
                return httpx.Response(302, headers={"Location": "http://files.test/signed"})
            return httpx.Response(200, content=self.payload)

        return httpx.Client(transport=httpx.MockTransport(handle), base_url="http://files.test")


# ---------------------------------------------------------------------------
# F-491 — a re-mount never writes over an edit nobody has saved
# ---------------------------------------------------------------------------


def _mounted(root: Path, home: Path, *, epoch: int) -> MountRecord:
    record = MountRecord(
        instance_id="an-instance",
        drive_id=DRIVE,
        node_id=NODE,
        org_path="team/notes",
        local_root=str(root.resolve()),
        machine="this-machine",
        epoch=epoch,
        pid=1,
    )
    save_record(record, home=home)
    return record


def test_resuming_a_mount_leaves_an_unsaved_local_edit_exactly_where_it_is(
    tmp_path: Path,
) -> None:
    """The data-loss case: the same holder resumes and the edit survives."""
    home = tmp_path / "home"
    root = tmp_path / "work"
    root.mkdir()
    drive = OneFileDrive()
    edited = root / "README.md"
    edited.write_bytes(drive.payload + b"a line nobody has saved yet\n")

    _mounted(root, home, epoch=5)
    record, summary = mount(
        files=drive,
        http=drive.client(epoch=5),
        source="team/notes",
        root=root,
        home=home,
        pull_tree=pull,
    )

    assert edited.read_bytes() == drive.payload + b"a line nobody has saved yet\n"
    assert drive.content_requests == []
    assert summary.kept == 1
    assert summary.kept_paths == [b"README.md"]
    assert record.epoch == 5


def test_a_first_mount_materializes_the_org_copy_over_whatever_is_there(
    tmp_path: Path,
) -> None:
    """The asymmetry: with no record, the org copy is the only truth there is."""
    home = tmp_path / "home"
    root = tmp_path / "work"
    root.mkdir()
    drive = OneFileDrive()
    (root / "README.md").write_bytes(b"a leftover from something else\n")

    _, summary = mount(
        files=drive,
        http=drive.client(),
        source="team/notes",
        root=root,
        home=home,
        pull_tree=pull,
    )

    assert (root / "README.md").read_bytes() == drive.payload
    assert summary.kept == 0
    assert drive.content_requests


def test_a_resume_whose_lease_lapsed_refuses_and_names_the_modified_file(
    tmp_path: Path,
) -> None:
    """A new epoch means somebody else may have written it: nothing is guessed."""
    home = tmp_path / "home"
    root = tmp_path / "work"
    root.mkdir()
    drive = OneFileDrive()
    edited = root / "README.md"
    edited.write_bytes(b"my unsaved work\n")
    _mounted(root, home, epoch=5)

    with pytest.raises(LocalChangesError) as refused:
        mount(
            files=drive,
            http=drive.client(epoch=9),
            source="team/notes",
            root=root,
            home=home,
            pull_tree=pull,
        )

    assert refused.value.paths == (b"README.md",)
    assert edited.read_bytes() == b"my unsaved work\n"
    assert drive.content_requests == []


def test_a_refused_pull_writes_nothing_at_all_before_it_refuses(tmp_path: Path) -> None:
    """The refusal costs nothing: it is decided before the first byte lands."""
    root = tmp_path / "work"
    root.mkdir()
    drive = OneFileDrive()
    (root / "README.md").write_bytes(b"mine\n")

    with pytest.raises(LocalChangesError):
        pull(
            files=drive,
            http=drive.client(),
            root=root,
            source="team/notes",
            local_changes="refuse",
        )

    assert sorted(entry.name for entry in root.iterdir()) == ["README.md"]
    assert (root / "README.md").read_bytes() == b"mine\n"
    assert drive.content_requests == []


def test_the_mount_command_names_the_unsaved_files_and_the_way_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _signed_in(monkeypatch)

    def _boom(**_: Any) -> None:
        raise LocalChangesError("local edits", paths=[b"notes/README.md"])

    monkeypatch.setattr(files_cli, "mount", _boom)
    directory = tmp_path / "work"
    directory.mkdir()

    result = runner.invoke(app, ["files", "mount", "team/notes", str(directory)])

    assert result.exit_code == 2
    assert "notes/README.md" in result.output
    assert f"alkera files unmount {directory}" in result.output
    assert "Traceback" not in result.output


# ---------------------------------------------------------------------------
# F-492 / F-493 / F-494 — every refusal is one sentence
# ---------------------------------------------------------------------------


def _signed_in(monkeypatch: pytest.MonkeyPatch, *, expires_at: datetime | None = None) -> None:
    class _Auth:
        api_url = "http://127.0.0.1:1"
        token = "a-token"

    _Auth.expires_at = expires_at or datetime.now(UTC) + timedelta(days=30)  # type: ignore[attr-defined]

    class _Client:
        files = object()

        class _Raw:
            @staticmethod
            def get_httpx_client() -> object:
                return object()

        raw_client = _Raw()

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *_: object) -> None:
            return None

    # The stored expiry is the token's own `exp`, as a real sign-in records it.
    expiry = _Auth.expires_at  # type: ignore[attr-defined]
    store(
        ORG_A,
        current=True,
        api_url=_Auth.api_url,
        token=make_jwt(org=ORG_A, exp=int(expiry.timestamp())),
    )
    monkeypatch.setattr(files_cli, "AlkeraClient", lambda **_: _Client())
    # A refusal is one sentence; rich would fold it at the captured terminal's
    # 80 columns and the assertion would be about the wrap, not the words.
    monkeypatch.setattr(files_cli, "_console", Console(width=400))


def _refusal(
    status: int, code: str | None, *, detail: dict[str, Any] | None = None
) -> AlkeraHTTPError:
    kind = AlkeraAuthError if status in (401, 403) else AlkeraHTTPError
    return kind(
        label="files item-by-path",
        status=status,
        code=code,
        message="the server's own words",
        trace_id="t-1",
        detail=detail,
    )


@pytest.mark.parametrize(
    ("status", "code", "detail", "expected", "exit_code"),
    [
        pytest.param(
            409,
            "files.leased",
            {"holder": {"displayName": "Dana Okafor"}},
            "checked out by Dana Okafor",
            1,
            id="a-leased-folder-names-the-person",
        ),
        pytest.param(404, "not_found", None, "No file or folder at", 2, id="a-mistyped-path"),
        pytest.param(401, None, None, "run `alkera login`", 2, id="a-stale-session"),
        pytest.param(403, "forbidden", None, "do not have permission", 1, id="no-permission"),
        pytest.param(507, "files.quota", None, "out of storage", 1, id="a-full-drive"),
        pytest.param(422, "invalid_name", None, "would not accept that name", 2, id="a-bad-name"),
    ],
)
def test_a_refusal_a_customer_caused_is_one_sentence_and_never_a_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    code: str | None,
    detail: dict[str, Any] | None,
    expected: str,
    exit_code: int,
) -> None:
    _signed_in(monkeypatch)

    def _boom(**_: Any) -> None:
        raise _refusal(status, code, detail=detail)

    monkeypatch.setattr(files_cli, "pull", _boom)
    directory = tmp_path / "into"
    directory.mkdir()

    result = runner.invoke(app, ["files", "pull", "team/typo", str(directory)])

    assert result.exit_code == exit_code
    assert expected in result.output
    assert "Traceback" not in result.output
    assert "alkera report" not in result.output
    assert "developer.mozilla.org" not in result.output


def test_a_missing_path_is_named_in_the_sentence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _signed_in(monkeypatch)

    def _boom(**_: Any) -> None:
        raise _refusal(404, "not_found")

    monkeypatch.setattr(files_cli, "pull", _boom)
    directory = tmp_path / "into"
    directory.mkdir()

    result = runner.invoke(app, ["files", "pull", "team/nope", str(directory)])

    assert "No file or folder at team/nope" in result.output


def test_a_credential_the_file_says_has_lapsed_never_leaves_the_machine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F-494: ``expires_at`` is honoured before the call, not after PyJWT sees it."""
    _signed_in(monkeypatch, expires_at=datetime(2020, 1, 1, tzinfo=UTC))
    called: list[str] = []
    monkeypatch.setattr(files_cli, "pull", lambda **_: called.append("called"))
    directory = tmp_path / "into"
    directory.mkdir()

    result = runner.invoke(app, ["files", "pull", "team/notes", str(directory)])

    assert result.exit_code == 2
    assert "Your sign-in has expired" in result.output
    assert called == []


def test_no_sign_in_at_all_is_the_same_kind_of_sentence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Nothing is stored in the isolated home: no sign-in at all.
    directory = tmp_path / "into"
    directory.mkdir()

    result = runner.invoke(app, ["files", "pull", "team/notes", str(directory)])

    assert result.exit_code == 2
    assert "not signed in" in result.output
    assert "Traceback" not in result.output


def test_a_network_that_never_answers_is_a_sentence_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _signed_in(monkeypatch)

    def _boom(**_: Any) -> None:
        raise httpx.ConnectError("nope")

    monkeypatch.setattr(files_cli, "pull", _boom)
    directory = tmp_path / "into"
    directory.mkdir()

    result = runner.invoke(app, ["files", "pull", "team/notes", str(directory)])

    assert result.exit_code == 1
    assert "Could not reach Alkera" in result.output
    assert "Traceback" not in result.output


# ---------------------------------------------------------------------------
# F-495 — the staleness rule is the lease's liveness, not a local process
# ---------------------------------------------------------------------------


def _status(*, held: bool, running: bool, expires_at: str | None) -> MountStatus:
    return MountStatus(
        record=MountRecord(org_path="team/notes", local_root="/tmp/work", epoch=5, pid=999999),
        held=held,
        running=running,
        expires_at=expires_at,
    )


def test_a_no_hold_mount_whose_lease_is_alive_is_not_stale() -> None:
    """The customer case exactly: mounted --no-hold, process gone, lease alive."""
    alive = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
    assert _status(held=True, running=False, expires_at=alive).state == "live"


def test_a_lease_the_server_no_longer_lists_is_stale_even_while_a_process_runs() -> None:
    assert _status(held=False, running=True, expires_at=None).state == "stale"


def test_a_lease_whose_own_expiry_has_passed_is_stale() -> None:
    gone = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    assert _status(held=True, running=True, expires_at=gone).state == "stale"


def test_the_mounts_listing_says_checked_out_and_hides_the_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _signed_in(monkeypatch)
    alive = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
    monkeypatch.setattr(
        files_cli, "mounts", lambda **_: [_status(held=True, running=False, expires_at=alive)]
    )

    plain = runner.invoke(app, ["files", "mounts"])
    verbose = runner.invoke(app, ["files", "mounts", "--verbose"])

    assert "checked out to you" in plain.output
    assert "epoch" not in plain.output
    assert "instance" not in plain.output
    assert "epoch 5" in verbose.output


# ---------------------------------------------------------------------------
# F-496 / F-490 — a person, and the customer's words
# ---------------------------------------------------------------------------


def test_a_summary_drops_the_words_only_the_library_knows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _signed_in(monkeypatch)
    summary = PullSummary(files=3, folders=2, pointers=4, specials=1, symlinks=1)
    monkeypatch.setattr(files_cli, "pull", lambda **_: summary)
    directory = tmp_path / "into"
    directory.mkdir()

    plain = runner.invoke(app, ["files", "pull", "team/notes", str(directory)])
    verbose = runner.invoke(app, ["files", "pull", "team/notes", str(directory), "--verbose"])

    assert "shortcuts to chats and results" in plain.output
    assert "specials" not in plain.output
    assert "pointers=" not in plain.output
    assert "pointers=4" in verbose.output


def test_a_holder_the_api_named_reaches_the_terminal_as_a_person(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _signed_in(monkeypatch)

    def _boom(**_: Any) -> None:
        raise _refusal(
            409,
            "files.leased",
            detail={"holder": {"id": "0c0b5192-1f5e-4c0e-9f2f-19d6b7a0c2aa", "email": "ada@x.io"}},
        )

    monkeypatch.setattr(files_cli, "push", _boom)
    directory = tmp_path / "work"
    directory.mkdir()

    result = runner.invoke(app, ["files", "push", str(directory), "team/notes"])

    assert "checked out by ada@x.io" in result.output
    assert "0c0b5192" not in result.output
    assert "Traceback" not in result.output


# ---------------------------------------------------------------------------
# F-497 — progress on a terminal, silence everywhere else
# ---------------------------------------------------------------------------


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_the_counter_redraws_one_line_while_someone_is_watching() -> None:
    stream = _Tty()
    counter = TransferProgress(total=3, stream=stream)

    counter(b"one.txt")
    counter(b"two.txt")
    counter.finish()

    written = stream.getvalue()
    assert written.count("\r") == 3
    assert "1/3" in written
    assert "2/3  two.txt" in written
    assert "\n" not in written


def test_the_counter_prints_nothing_at_all_when_stdout_is_not_a_terminal() -> None:
    stream = io.StringIO()
    counter = TransferProgress(total=2, stream=stream)

    counter(b"one.txt")
    counter.finish()

    assert stream.getvalue() == ""
    assert counter.done == 1


def test_a_captured_pull_prints_the_summary_and_only_the_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """CliRunner's stdout is not a TTY — the non-TTY path, end to end."""
    _signed_in(monkeypatch)
    seen: list[bytes] = []

    def _pull(**kwargs: Any) -> PullSummary:
        kwargs["progress"](b"one.txt")
        kwargs["progress"](b"two.txt")
        seen.append(b"ran")
        return PullSummary(files=2, folders=1)

    monkeypatch.setattr(files_cli, "pull", _pull)
    directory = tmp_path / "into"
    directory.mkdir()

    result = runner.invoke(app, ["files", "pull", "team/notes", str(directory)])

    assert seen == [b"ran"]
    assert result.output.strip().splitlines() == [
        "2 files (0 bytes), 0 already up to date, 1 folders, 0 shortcuts"
    ]


def test_the_push_hands_the_library_a_counter_that_sees_every_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _signed_in(monkeypatch)
    captured: list[Any] = []

    def _push(**kwargs: Any) -> Any:
        captured.append(kwargs["progress"])
        return files_cli.PushSummary(uploaded=1)

    monkeypatch.setattr(files_cli, "push", _push)
    directory = tmp_path / "work"
    directory.mkdir()

    runner.invoke(app, ["files", "push", str(directory), "team/notes"])

    assert isinstance(captured[0], TransferProgress)
