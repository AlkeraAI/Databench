"""A push's resumable upload sessions on disk: private, atomic, and readable
by every later build."""

from __future__ import annotations

import json
import os
import stat
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from alkera_cli.files.push_state import PushState, load_state, save_state

_FIXTURES = Path(__file__).parent / "fixtures" / "push_state"

SESSIONS: dict[str, dict[str, Any]] = {
    "data/blob.bin": {
        "uploadId": "5f0c1c55-0000-4000-8000-0000000000a1",
        "partSize": 8388608,
        "contentHash": "af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262",
        "size": 9437184,
    },
    "model.ckpt": {
        "uploadId": "5f0c1c55-0000-4000-8000-0000000000a2",
        "partSize": 8388608,
        "contentHash": "4878ca0425c739fa427f7eda20fe845f6b2e46ba5fe2a14df5b1e32f50603215",
        "size": 20971520,
    },
}


@pytest.fixture
def open_umask() -> Iterator[None]:
    """The common default umask, which leaves a plainly written file readable
    by every local user."""
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


def _fixtures() -> list[Path]:
    return sorted(_FIXTURES.glob("v*.json"))


def test_the_corpus_covers_the_version_this_build_writes() -> None:
    versions = {path.stem for path in _fixtures()}
    assert f"v{PushState.SCHEMA_VERSION.replace('.', '_')}" in versions


@pytest.mark.parametrize("fixture", _fixtures(), ids=lambda path: cast(Path, path).stem)
def test_every_state_a_past_writer_produced_still_resumes(fixture: Path, tmp_path: Path) -> None:
    """Sessions an older build left open resume after an upgrade instead of
    re-uploading. The fix for a failure here is a migration, never an edit to
    the fixture."""
    assert load_state(fixture) == SESSIONS

    # And a save by this build keeps them.
    copy = tmp_path / "state.json"
    save_state(copy, load_state(fixture))
    assert load_state(copy) == SESSIONS


def test_a_state_from_a_newer_build_resumes_and_keeps_what_it_added(tmp_path: Path) -> None:
    """A newer minor version is the same sessions plus fields this build does
    not know: they are read, not thrown away, and survive this build's save."""
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps(
            {
                "schema_version": "1.1.0",
                "sessions": {"a.bin": {**SESSIONS["model.ckpt"], "resumeToken": "t"}},
                "pushed_by": "a-newer-build",
            }
        ),
        encoding="utf-8",
    )

    sessions = load_state(state)
    assert sessions == {"a.bin": {**SESSIONS["model.ckpt"], "resumeToken": "t"}}

    save_state(state, sessions)
    assert load_state(state)["a.bin"]["resumeToken"] == "t"


def test_one_unreadable_session_does_not_cost_the_others(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps({"schema_version": "1.0.0", "sessions": {"bad": 3, **SESSIONS}}),
        encoding="utf-8",
    )
    assert load_state(state) == SESSIONS


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(None, id="missing"),
        pytest.param("{not json", id="torn"),
        pytest.param("[]", id="not-an-object"),
        pytest.param('{"schema_version": "1.0.0", "sessions": []}', id="sessions-not-a-map"),
    ],
)
def test_a_state_that_cannot_be_read_is_no_sessions(content: str | None, tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    if content is not None:
        state.write_text(content, encoding="utf-8")
    assert load_state(state) == {}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
@pytest.mark.usefixtures("open_umask")
def test_the_state_is_readable_by_this_user_alone(tmp_path: Path) -> None:
    """The sessions name what a person is uploading; the file holding them is
    not for the machine's other users, whatever the umask."""
    state = tmp_path / "home" / "files" / "pushes" / "s.json"
    save_state(state, SESSIONS)
    assert stat.S_IMODE(state.stat().st_mode) == 0o600

    # A rewrite over an existing file stays private too.
    save_state(state, {})
    assert stat.S_IMODE(state.stat().st_mode) == 0o600
    assert load_state(state) == {}


def test_a_save_leaves_no_temporary_beside_the_state(tmp_path: Path) -> None:
    state = tmp_path / "s.json"
    save_state(state, SESSIONS)
    save_state(state, {"model.ckpt": SESSIONS["model.ckpt"]})
    assert [path.name for path in tmp_path.iterdir()] == ["s.json"]
    assert load_state(state) == {"model.ckpt": SESSIONS["model.ckpt"]}
