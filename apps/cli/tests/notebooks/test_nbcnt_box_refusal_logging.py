"""A request the engine refuses in the ordinary course (a build that failed,
a kernel that could not start, a bad request) is answered
with its reason and logged as a refusal, without a traceback; anything else
the box hits is still logged as an error with one.

The box's own request handler is driven with a relay whose engine client
raises what the engine raises; the answer is what the backend receives.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import pytest
from alkera_cli.notebooks.box import BoxFolder, BoxNotebooks
from alkera_cli.notebooks.store_loro import Located
from alkera_core.schemas.realtime.machine import NotebookMachineRequest
from alkera_notebook.engine.errors import NotFoundError
from alkera_notebook.envs import (
    EnvBuildError,
    EnvNotFoundError,
    InvalidRequirementError,
)
from alkera_notebook.kernels.kernel import KernelStartError

LEASE = uuid.UUID("6b0e1c4f-0000-4000-8000-00000000aaaa")
DRIVE = uuid.UUID("6b0e1c4f-0000-4000-8000-00000000dddd")
ITEM = uuid.UUID("6b0e1c4f-0000-4000-8000-00000000eeee")


class _Client:
    def __init__(self, raises: BaseException) -> None:
        self.raises = raises

    async def env(self, action: Any) -> Any:
        raise self.raises

    async def kernel(self, action: str) -> Any:
        raise self.raises


class _Relay:
    def __init__(self, raises: BaseException) -> None:
        self.client = _Client(raises)
        self.posted: list[dict[str, Any]] = []

    def client_for(self, requested_by: Any) -> _Client:
        return self.client

    async def post(self, events: list[dict[str, Any]], *, urgent: bool = False) -> None:
        self.posted.extend(events)


class _Folders:
    def __init__(self, folder: BoxFolder) -> None:
        self.folder = folder

    def by_key(self, key: str) -> BoxFolder | None:
        return self.folder

    def by_lease(self, lease_node_id: str) -> BoxFolder | None:
        return self.folder


async def _answer(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, op: str, body: dict[str, Any], raises: Exception
) -> dict[str, Any]:
    folder = BoxFolder(
        key="chat",
        root=tmp_path,
        drive_id=str(DRIVE),
        lease_node_id=str(LEASE),
        step="",
        headers={},
        node_at=lambda _p: str(ITEM),
    )

    async def locate(key: str, path: str) -> Located:
        return Located(drive_id=str(DRIVE), item_id=str(ITEM))

    async def no_events(*_: Any, **__: Any) -> None:
        return None

    box = BoxNotebooks(
        folders=_Folders(folder),
        engine_for=lambda _t: None,  # type: ignore[arg-type,return-value]
        events=type("Sink", (), {"post": staticmethod(no_events)})(),
        locate=locate,
    )
    relay = _Relay(raises)

    async def opened(*_: Any, **__: Any) -> _Relay:
        return relay

    monkeypatch.setattr(box, "relay", opened)
    await box.handle(
        NotebookMachineRequest(
            request_id=uuid.uuid4(),
            drive_id=DRIVE,
            item_id=ITEM,
            op=op,  # type: ignore[arg-type]
            body=body,
            lease_node_id=LEASE,
            path="n.alknb.py",
        )
    )
    (answer,) = relay.posted
    return dict(answer["error"])


INSTALL = ("env_install", {"packages": ["six"]})
RESTART = ("kernel", {"action": "restart"})


@pytest.mark.parametrize(
    ("request_", "raises", "code", "says"),
    [
        pytest.param(
            INSTALL,
            EnvBuildError("add failed (exit 1)", log="$ uv add six\nerror: no solution found"),
            "env_build_failed",
            "no solution found",
            id="install-build-failed",
        ),
        pytest.param(
            INSTALL,
            EnvNotFoundError("no environment named venv:x"),
            "env_unavailable",
            "venv:x",
            id="install-env-not-found",
        ),
        pytest.param(
            INSTALL,
            InvalidRequirementError("not a requirement: '!!'"),
            "invalid_request",
            "not a requirement",
            id="install-invalid-requirement",
        ),
        pytest.param(
            RESTART,
            KernelStartError("env_unavailable", "environment uv_project:. was not found"),
            "env_unavailable",
            "was not found",
            id="restart-kernel-start-refused",
        ),
        pytest.param(
            RESTART,
            NotFoundError("no such notebook"),
            "not_found",
            "no such notebook",
            id="engine-error",
        ),
    ],
)
async def test_an_expected_refusal_is_answered_with_its_reason_and_logged_without_a_traceback(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    request_: tuple[str, dict[str, Any]],
    raises: Exception,
    code: str,
    says: str,
) -> None:
    op, body = request_
    with caplog.at_level(logging.DEBUG, logger="alkera_cli.notebooks.box"):
        error = await _answer(tmp_path, monkeypatch, op, body, raises)
    assert error["code"] == code and says in error["message"]
    mine = [r for r in caplog.records if r.name == "alkera_cli.notebooks.box"]
    assert mine, "the refusal is logged"
    assert all(r.levelno < logging.ERROR and r.exc_info is None for r in mine)
    assert any(code in r.getMessage() for r in mine)


async def test_an_unexpected_failure_is_still_an_error_with_its_traceback(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger="alkera_cli.notebooks.box"):
        error = await _answer(tmp_path, monkeypatch, *INSTALL, RuntimeError("the box broke"))
    assert error["code"] == "box_error"
    (record,) = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert record.exc_info is not None and record.exc_info[0] is RuntimeError
