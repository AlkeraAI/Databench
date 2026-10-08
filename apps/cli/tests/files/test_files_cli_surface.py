"""The ``alkera files`` typer surface: what the wrapper hands the library.

The push and pull libraries are proven against a real backend elsewhere; what
is unproven there is the façade — that a flag reaches the library at all, that
an unauthenticated invocation refuses instead of calling out, and that the
operator sub-app is reachable by the name an operator types.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from _help_text import HELP_ENV, plain
from _profiles import ORG_A, store
from alkera_cli.commands import files as files_cli
from alkera_cli.files.mount import MountRecord
from alkera_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()


class _Auth:
    api_url = "http://127.0.0.1:1"
    token = "a-token"


@pytest.fixture
def signed_in(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """A signed-in session whose client never opens a socket."""
    store(ORG_A, current=True, api_url=_Auth.api_url)
    calls: list[dict[str, Any]] = []

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

    monkeypatch.setattr(files_cli, "AlkeraClient", lambda **_: _Client())

    def _pull(**kwargs: Any) -> files_cli.PullSummary:
        calls.append(kwargs)
        return files_cli.PullSummary(files=3, folders=2, bytes_downloaded=99)

    monkeypatch.setattr(files_cli, "pull", _pull)
    return calls


def test_pull_hands_the_library_the_source_the_root_and_the_flags(
    signed_in: list[dict[str, Any]], tmp_path: Path
) -> None:
    """Every argument the verb accepts reaches ``pull`` unchanged."""
    into = tmp_path / "into"
    elsewhere = tmp_path / "mounted"

    result = runner.invoke(
        app,
        [
            "files",
            "pull",
            "proj/docs",
            str(into),
            "--local-root",
            str(elsewhere),
            "--trusted",
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(signed_in) == 1
    passed = signed_in[0]
    assert passed["source"] == "proj/docs"
    assert passed["root"] == into
    assert passed["local_root"] == bytes(elsewhere)
    assert passed["trusted"] is True
    # The summary is what the operator reads back, so it is printed, not dropped.
    assert "3 files (99 bytes)" in result.output


def test_pull_defaults_the_local_root_and_stays_untrusted(
    signed_in: list[dict[str, Any]], tmp_path: Path
) -> None:
    """Without the flags a pull resolves canonical links against its own root."""
    result = runner.invoke(app, ["files", "pull", "proj", str(tmp_path / "into")])

    assert result.exit_code == 0, result.output
    assert signed_in[0]["local_root"] is None
    assert signed_in[0]["trusted"] is False


def test_pull_refuses_a_destination_that_is_a_file(
    signed_in: list[dict[str, Any]], tmp_path: Path
) -> None:
    """A pull into a regular file is an argument error, not a half-written tree."""
    occupied = tmp_path / "not-a-dir"
    occupied.write_text("bytes")

    result = runner.invoke(app, ["files", "pull", "proj", str(occupied)])

    assert result.exit_code == 2
    assert signed_in == []


def test_pull_without_a_session_refuses_instead_of_calling_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No saved token means no request: the verb refuses before building a client.

    The code is the sign-in code the whole Files group leaves — an expired
    credential and an absent one are the same thing to whoever runs the
    command, so a script branches on one number for both.
    """
    # Nothing is stored in the isolated home: no sign-in at all.
    reached: list[object] = []
    monkeypatch.setattr(files_cli, "pull", lambda **kwargs: reached.append(kwargs))

    result = runner.invoke(app, ["files", "pull", "proj", str(tmp_path / "into")])

    assert result.exit_code == files_cli.SIGN_IN_EXIT
    assert reached == []
    assert "not signed in" in result.output


def test_the_operator_verbs_are_reachable_as_files_admin() -> None:
    """`alkera files admin gc` exists under the name an operator types.

    Drop the `files_app.add_typer(admin_app, name="admin")` line and typer
    answers 2 with "No such command", which is what this catches.
    """
    result = runner.invoke(app, ["files", "admin", "gc", "--dry-run", "--help"], env=HELP_ENV)

    assert result.exit_code == 0, result.output
    assert "--dry-run" in plain(result.output)


@pytest.fixture
def mounting(
    monkeypatch: pytest.MonkeyPatch, signed_in: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """A mount verb whose library call is recorded instead of taken."""

    def _mount(**kwargs: Any) -> Any:
        signed_in.append(kwargs)
        return (
            MountRecord(instance_id="i", epoch=1),
            files_cli.PullSummary(files=0, folders=0, bytes_downloaded=0),
        )

    monkeypatch.setattr(files_cli, "mount", _mount)
    monkeypatch.setattr(
        files_cli, "watch", lambda **_kwargs: pytest.fail("--no-hold must not heartbeat")
    )
    return signed_in


def test_a_mount_asks_for_the_streaming_cadence_only_when_told_to(
    mounting: list[dict[str, Any]], tmp_path: Path
) -> None:
    """``--live`` is what opens the streaming plane; nothing else does.

    The server opens a per-change plane for a lease granted the cadence, so a
    holder that never said it would stream must not be handed one — a flag
    that reached the library regardless would cost every plain mount that
    plane.
    """
    result = runner.invoke(
        app, ["files", "mount", "proj", str(tmp_path / "here"), "--no-hold", "--live"]
    )

    assert result.exit_code == 0, result.output
    assert mounting[0]["live"] is True


def test_a_plain_mount_is_not_handed_a_streaming_cadence(
    mounting: list[dict[str, Any]], tmp_path: Path
) -> None:
    """The opt-in is an opt-in: the verb without the flag asks for no plane."""
    result = runner.invoke(app, ["files", "mount", "proj", str(tmp_path / "here"), "--no-hold"])

    assert result.exit_code == 0, result.output
    assert mounting[0]["live"] is False


def test_watch_is_described_as_running_beside_the_heartbeat() -> None:
    """The export runs on a thread of its own; the beat does not wait for it.

    Someone reading the old sentence sized their sync interval as if a long
    push delayed the next heartbeat and cost them the lease. It does not, and
    the help is the only place they would learn that.
    """
    result = runner.invoke(app, ["files", "mount", "--help"], env=HELP_ENV)

    assert result.exit_code == 0, result.output
    # The options panel draws a border down both sides and wraps inside it, so
    # the sentence is only one string once the frame and the line breaks are
    # taken back out. Whatever width the console guessed, the words are these.
    said = " ".join(plain(result.output).replace("│", " ").split())
    assert (
        "Export the folder every sync interval, beside the heartbeat that keeps the lease "
        "(default). --no-watch only heartbeats." in said
    )
