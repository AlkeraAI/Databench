"""`save_auth` replaces `~/.alkera/auth.yml` atomically.

A write cut short must leave the previous sign-in readable (a truncated file
reads as signed out), and on POSIX the token must never sit in a file a group or
other user can read, not even for the moment between create and chmod.
"""

from __future__ import annotations

import os
import stat
import sys
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alkera_cli.account import auth_file
from alkera_cli.account.auth_file import StoredAuth
from alkera_cli.host import paths
from alkera_core import atomic_io


def _isolate_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "alkera-home"
    monkeypatch.setattr(paths, "ALKERA_HOME", home)
    monkeypatch.setattr(paths, "AUTH_FILE_PATH", home / "auth.yml")
    monkeypatch.setattr(auth_file, "AUTH_FILE_PATH", home / "auth.yml")
    return home


def _auth(token: str) -> StoredAuth:
    return StoredAuth(
        api_url="https://api.alkera.test",
        token=token,
        expires_at=datetime(2026, 12, 1, tzinfo=UTC) + timedelta(days=90),
    )


def _leftovers(home: Path) -> list[str]:
    """What a write left in the home besides the file itself. The writers' lock
    lives in its own ``.locks/`` directory (a pid, never a token)."""
    return sorted(p.name for p in home.iterdir() if p.name not in {"auth.yml", ".locks"})


@pytest.fixture
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sharing-violation retry sleeps between attempts; a refusal that never
    clears should exhaust the budget without the test waiting it out."""
    monkeypatch.setattr(atomic_io.time, "sleep", lambda _s: None)


def _out_of_space(_src: object, _dst: object) -> None:
    raise OSError(28, "No space left on device")


def _held_forever(_src: object, _dst: object) -> None:
    raise PermissionError(13, "The process cannot access the file")


@pytest.mark.parametrize(
    "replace",
    [
        pytest.param(_out_of_space, id="replace-fails-outright"),
        pytest.param(_held_forever, id="replace-refused-past-the-retry-budget"),
    ],
)
def test_a_save_that_dies_before_the_replace_keeps_the_previous_sign_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_backoff: None,
    replace: Callable[[object, object], None],
) -> None:
    home = _isolate_home(monkeypatch, tmp_path)
    auth_file.save_auth(_auth("old-token"))
    before = paths.AUTH_FILE_PATH.read_bytes()

    monkeypatch.setattr(atomic_io.os, "replace", replace)
    with pytest.raises(OSError):
        auth_file.save_auth(_auth("new-token"))

    assert paths.AUTH_FILE_PATH.read_bytes() == before
    loaded = auth_file.load_auth()
    assert loaded is not None
    assert loaded.token == "old-token"
    assert _leftovers(home) == []


def test_a_replace_refused_a_few_times_still_lands_the_new_sign_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_backoff: None
) -> None:
    """The refusal Windows gives while a reader (the auth watcher of another
    window, an antivirus scan) has `auth.yml` open clears in milliseconds, so the
    save rides it out. Driven by an injected replace so it runs on every OS."""
    home = _isolate_home(monkeypatch, tmp_path)
    auth_file.save_auth(_auth("old-token"))
    real_replace = os.replace
    refusals = iter([True, True])

    def _held_briefly(src: str, dst: str) -> None:
        if next(refusals, False):
            raise PermissionError(13, "The process cannot access the file")
        real_replace(src, dst)

    monkeypatch.setattr(atomic_io.os, "replace", _held_briefly)
    auth_file.save_auth(_auth("new-token"))

    loaded = auth_file.load_auth()
    assert loaded is not None
    assert loaded.token == "new-token"
    assert _leftovers(home) == []


@pytest.fixture
def permissive_umask() -> Iterator[None]:
    """With umask 0, a file's mode is exactly what its creator asked for, so a
    create with the default mode would show group and other bits."""
    previous = os.umask(0)
    try:
        yield
    finally:
        os.umask(previous)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits do not apply on Windows")
@pytest.mark.parametrize("previous_mode", [None, 0o644], ids=["first-sign-in", "over-a-0644-file"])
def test_the_token_is_never_readable_by_group_or_other(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    require_effective_chmod: None,
    permissive_umask: None,
    previous_mode: int | None,
) -> None:
    home = _isolate_home(monkeypatch, tmp_path)
    if previous_mode is not None:
        home.mkdir(mode=0o700)
        paths.AUTH_FILE_PATH.write_text("api_url: x\n", encoding="utf-8")
        paths.AUTH_FILE_PATH.chmod(previous_mode)

    # The mode each file under the home has the instant it is created, read from
    # the descriptor before any later chmod could tighten it.
    created: dict[str, int] = {}
    real_open = os.open

    def _recording_open(path: str | os.PathLike[str], flags: int, *args: int, **kwargs: int) -> int:
        fd = real_open(path, flags, *args, **kwargs)
        if flags & os.O_CREAT and Path(path).parent == home:
            created[Path(path).name] = stat.S_IMODE(os.fstat(fd).st_mode)
        return fd

    monkeypatch.setattr(atomic_io.os, "open", _recording_open)
    auth_file.save_auth(_auth("secret-token"))
    monkeypatch.setattr(atomic_io.os, "open", real_open)

    assert created, "save_auth created no file through os.open"
    assert {name: mode & 0o077 for name, mode in created.items()} == dict.fromkeys(created, 0)
    assert stat.S_IMODE(paths.AUTH_FILE_PATH.stat().st_mode) == 0o600
    assert _leftovers(home) == []


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows open-handle semantics")
def test_a_save_rides_out_a_reader_holding_auth_yml_open_win32(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CPython's `open()` takes no FILE_SHARE_DELETE, so replacing `auth.yml`
    while a reader holds it genuinely fails until the handle closes, which a
    timer does well inside the retry budget."""
    home = _isolate_home(monkeypatch, tmp_path)
    auth_file.save_auth(_auth("old-token"))

    handle = paths.AUTH_FILE_PATH.open("r", encoding="utf-8")
    releaser = threading.Timer(0.08, handle.close)
    releaser.start()
    try:
        auth_file.save_auth(_auth("new-token"))
    finally:
        releaser.cancel()
        if not handle.closed:
            handle.close()

    loaded = auth_file.load_auth()
    assert loaded is not None
    assert loaded.token == "new-token"
    assert _leftovers(home) == []
