"""The daemon's status file: what it said on the last beat the server took.

A deploy rolls each compute node onto a new build and must then know, from
the node, that the new build is what serves: once a node ran a build three
labels older than the one its rig had written down. The file is that answer —
written by the serving process only after the server ACCEPTED a beat from it,
so it also says the platform's row now carries the same build — plus the
chats in flight a restart would cut (the box roll reads both).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from _mirror_service import Clock, build_service
from alkera_cli.box_status import (
    DAEMON_STATUS_FILE_NAME,
    ENV_DAEMON_STATUS_FILE,
    status_file_from_env,
)
from alkera_cli.cloud.rest import CloudApiError
from alkera_cli.cloud.service import CloudMirrorService
from alkera_cli.host import paths as alkera_paths


def _chat(chat_id: str) -> dict[str, Any]:
    return {"id": chat_id, "machine_id": "machine:x", "last_seq": 1}


async def _one_beat(service: CloudMirrorService, answer: Any) -> None:
    """Run the machine loop through exactly one heartbeat, answered by
    ``answer`` (a body, or an exception to raise)."""
    beaten = asyncio.Event()

    async def _beat(machine_id: str, **kwargs: Any) -> dict[str, Any]:
        beaten.set()
        if isinstance(answer, BaseException):
            raise answer
        return dict(answer)

    service._rest.heartbeat_machine = _beat  # type: ignore[method-assign]
    task = asyncio.ensure_future(service._machine_loop())
    await asyncio.wait_for(beaten.wait(), timeout=10.0)
    # The loop records the beat before it next awaits (its sleep); one more
    # turn of the event loop lets it get there.
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_an_accepted_beat_writes_what_the_process_reported(tmp_path: Path) -> None:
    status = tmp_path / "home" / DAEMON_STATUS_FILE_NAME
    service, built = build_service(
        tmp_path,
        clock=Clock(),
        status_file=status,
        daemon_version="0.5.0 (build 951dac943d65)",
        wall_clock=lambda: 1_790_000_000.25,
    )
    service._machine_id = "machine:x"
    for chat_id in ("chat-idle", "chat-busy", "chat-asking"):
        await service._ensure_mirror(chat_id, _chat(chat_id))
    built["chat-busy"].turn_running = True
    built["chat-asking"].turn_running = True
    built["chat-asking"].parked_only = True

    await _one_beat(service, {})

    report = json.loads(status.read_text())
    assert report == {
        "schema": 1,
        "pid": os.getpid(),
        "daemon_instance_id": service._instance_id,
        "daemon_version": "0.5.0 (build 951dac943d65)",
        "build": report["build"],
        "machine_id": "machine:x",
        "beat_at": 1_790_000_000.25,
        "chats_held": 3,
        "chats_busy": 2,
        "chats_working": 1,
        "chats_awaiting_user": 1,
        "chats_idle": 1,
        "draining": False,
        "restarting": False,
    }
    # The roll's status probe reads it as root; nobody else may.
    if sys.platform != "win32":
        assert status.stat().st_mode & 0o777 == 0o600


async def test_the_build_is_the_one_the_process_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALKERA_BUILD_ID", "951dac943d65")
    status = tmp_path / DAEMON_STATUS_FILE_NAME
    service, _built = build_service(tmp_path, clock=Clock(), status_file=status)
    service._machine_id = "machine:x"
    await _one_beat(service, {})
    assert json.loads(status.read_text())["build"] == "951dac943d65"


async def test_a_draining_box_says_so_in_its_status(tmp_path: Path) -> None:
    status = tmp_path / DAEMON_STATUS_FILE_NAME
    service, _built = build_service(tmp_path, clock=Clock(), status_file=status)
    service._machine_id = "machine:x"
    service.begin_drain()
    await _one_beat(service, {})
    report = json.loads(status.read_text())
    assert report["draining"] is True
    assert report["restarting"] is False


@pytest.mark.parametrize(
    "refusal",
    [
        pytest.param(
            CloudApiError(503, {"detail": "down"}, method="POST", path="/heartbeat"),
            id="the-server-answered-an-error",
        ),
        pytest.param(httpx.ConnectError("no route"), id="the-server-was-unreachable"),
    ],
)
async def test_a_beat_the_server_did_not_take_leaves_the_last_accepted_one(
    tmp_path: Path, refusal: BaseException
) -> None:
    """The file says what the SERVER has on record; a beat it never took must
    not move it, or a roll would count a node the platform never heard from."""
    status = tmp_path / DAEMON_STATUS_FILE_NAME
    clock_value = [1_790_000_000.0]
    service, _built = build_service(
        tmp_path, clock=Clock(), status_file=status, wall_clock=lambda: clock_value[0]
    )
    service._machine_id = "machine:x"
    await _one_beat(service, {})
    accepted = status.read_text()

    clock_value[0] = 1_790_000_600.0
    await _one_beat(service, refusal)
    assert status.read_text() == accepted


async def test_a_refused_first_beat_writes_nothing(tmp_path: Path) -> None:
    status = tmp_path / DAEMON_STATUS_FILE_NAME
    service, _built = build_service(tmp_path, clock=Clock(), status_file=status)
    service._machine_id = "machine:x"
    await _one_beat(service, httpx.ConnectError("no route"))
    assert not status.exists()


async def test_no_status_file_is_written_when_none_is_configured(tmp_path: Path) -> None:
    service, _built = build_service(tmp_path, clock=Clock())
    service._machine_id = "machine:x"
    await _one_beat(service, {})
    written = await asyncio.to_thread(lambda: list(tmp_path.rglob(DAEMON_STATUS_FILE_NAME)))
    assert written == []


async def test_a_status_file_that_cannot_be_written_never_stops_the_beats(
    tmp_path: Path,
) -> None:
    """The file is a report; a disk that refuses it must not end the service."""
    status = tmp_path / "taken"
    status.mkdir()  # a directory where the file should go: the rename fails
    service, _built = build_service(tmp_path, clock=Clock(), status_file=status)
    service._machine_id = "machine:x"
    await _one_beat(service, {})
    await _one_beat(service, {})
    assert status.is_dir()


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        pytest.param(
            {ENV_DAEMON_STATUS_FILE: "/run/alkera/status.json"},
            Path("/run/alkera/status.json"),
            id="named",
        ),
        pytest.param({ENV_DAEMON_STATUS_FILE: "   "}, None, id="blank-means-the-default"),
        pytest.param({}, None, id="unset-means-the-default"),
    ],
)
def test_the_status_file_lives_in_the_alkera_home_unless_named(
    env: dict[str, str], expected: Path | None
) -> None:
    default = alkera_paths.ALKERA_HOME / DAEMON_STATUS_FILE_NAME
    assert status_file_from_env(env) == (expected or default)
