"""The box's answer to every notebook request op, held to the model the
backend reads it as.

The backend waits for an ``answer`` event and validates its ``result``: the
engine's ``EnvListing`` / ``EnvPackages`` for the environment panel, its own
``TablePage`` for a table page (after adding the offset and limit it asked
for), ``FrameAttached`` for a frame, and the run's status and reason off the
engine's ``RunInfo``. An answer the box trims (the packages without their
``env_id``) fails the backend's validation and the person gets a 500. Each op
here is driven through the box's own handler with an engine that answers a
fully populated engine model, so a field the box drops or renames fails.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from alkera_cli.notebooks.box import _OPS, SERVED_OPS
from alkera_core.notebooks.schemas import FrameAttached, TablePage
from alkera_core.schemas.realtime.machine import NOTEBOOK_REQUEST_OPS
from alkera_notebook.engine.models import (
    EnvAction,
    EnvInfo,
    EnvListing,
    EnvPackages,
    EnvResult,
    InspectQuery,
    InspectResult,
    KernelInfo,
    PackageInfo,
    PlanEntry,
    QueuedRun,
    RunInfo,
)
from alkera_notebook.outputs.state import TABLE_MIME
from pydantic import BaseModel, ConfigDict

FRAME = "0123456789abcdef0123456789abcdef.frame-1"
CELL = "cell-1"

ENV = EnvInfo(
    env_id="uv_project:.",
    kind="uv_project",
    spec_root=".",
    python="3.13.1",
    state="ready",
    recorded_in_file=True,
    last_failure="Installing torch failed.",
    allowed_actions=["install", "remove"],
)
OTHER_ENV = ENV.model_copy(update={"env_id": "default:.alkera/envs/default", "state": "absent"})

RUN = RunInfo(
    run_id="run-1",
    status="refused",
    reason="needs_confirmation",
    trigger="run_all",
    plan=[PlanEntry(cell_id=CELL, name="load", reason="target", kind="python", code="x = 1")],
    estimate_s=12.5,
    queued_behind=["run-0"],
    joined=None,
)
KERNEL = KernelInfo(
    state="busy",
    env=ENV,
    reactivity="autorun",
    memory_bytes=1 << 20,
    started_at=datetime(2026, 10, 5, 21, 0, tzinfo=UTC),
    queue=[QueuedRun(run_id="run-1", by="Ada", trigger="run", status="queued")],
    kernel_id="krn_1",
    seq=7,
)
LISTING = EnvListing(current=ENV, envs=[ENV, OTHER_ENV])
PACKAGES = EnvPackages(
    env_id=ENV.env_id,
    packages=[
        PackageInfo(name="polars", version="1.9.0"),
        PackageInfo(name="numpy", version="2.1"),
    ],
    requirements=["polars>=1"],
)
INSTALLED = EnvResult(
    env=ENV,
    envs=[ENV],
    packages=[PackageInfo(name="polars", version="1.9.0")],
    spec_changed=["pyproject.toml", "uv.lock"],
    log="$ uv add polars\nResolved 1 package",
)
#: The kernel's table page: what a table output carries, 100 rows further on.
PAGE = {
    "schema": [{"name": "a", "type": "Int64"}, {"name": "day", "type": "Date"}],
    "rows": [[100, "2026-01-01"], [101, None]],
    "total_rows": 2_000,
    "offset": 100,
}
INSPECTED = InspectResult(table=PAGE, total_rows=2_000)


class _Empty(BaseModel):
    """An op the backend only needs answered."""

    model_config = ConfigDict(extra="forbid")


class _Cleared(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cleared: list[str]


class _Detached(BaseModel):
    model_config = ConfigDict(extra="forbid")
    detached: int


class _Engine:
    """The engine client a handler reaches: every answer a full engine model."""

    def __init__(self) -> None:
        self.inspected: list[InspectQuery] = []
        self.env_asked: list[str] = []
        self.actions: list[EnvAction] = []

    async def run(self, target: Any, **_: Any) -> Any:
        return SimpleNamespace(run_id=RUN.run_id, info=RUN)

    async def kernel(self, action: str) -> KernelInfo:
        return KERNEL

    async def comm_send(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def clear_outputs(self, cell_ids: list[str] | None) -> list[str]:
        return list(cell_ids or [CELL])

    def attach_frame(self, *, frame_id: str, model_ids: list[str], output_id: Any) -> Any:
        opens = [
            SimpleNamespace(
                message={"method": "comm.open", "comm_id": "m1"}, buffers=[b"\x00\x01"]
            ),
            SimpleNamespace(message={"method": "comm.open", "comm_id": "m2"}, buffers=[]),
        ]
        return SimpleNamespace(frame_id=frame_id, opens=opens)

    def detach_frame(self, frame_id: str) -> None:
        return None

    async def inspect(self, query: InspectQuery) -> InspectResult:
        self.inspected.append(query)
        return INSPECTED

    async def envs(self) -> EnvListing:
        return LISTING

    async def env_packages(self, env_id: str) -> EnvPackages:
        self.env_asked.append(env_id)
        return PACKAGES

    async def env(self, action: EnvAction) -> EnvResult:
        self.actions.append(action)
        return INSTALLED


class _Relay:
    def __init__(self) -> None:
        self.client = _Engine()
        outputs = SimpleNamespace(bundles=[{TABLE_MIME: {"source": {"name": "df"}}}])
        self.session = SimpleNamespace(
            # A kernel runs: a table page is the engine's to read.
            runtime=SimpleNamespace(
                frames={FRAME: object()}, outputs={CELL: outputs}, kernel=object()
            )
        )

    def client_for(self, requested_by: Any) -> _Engine:
        return self.client

    def owner_of_frame(self, frame_id: str, requested_by: Any = None) -> _Engine:
        return self.client

    async def snapshot(self) -> None:
        return None


def _exactly(model: BaseModel) -> Callable[[dict[str, Any]], None]:
    """The answer is the engine's model, whole: it validates as the model and
    nothing of the engine's answer is missing or renamed."""

    def check(answer: dict[str, Any]) -> None:
        assert type(model).model_validate(answer) == model
        assert answer == model.model_dump(mode="json")

    return check


def _as(model: type[BaseModel], **added: Any) -> Callable[[dict[str, Any]], None]:
    def check(answer: dict[str, Any]) -> None:
        model.model_validate({**answer, **added})

    return check


def _frame(answer: dict[str, Any]) -> None:
    attached = FrameAttached.model_validate(answer)
    assert attached.frame_id == FRAME
    assert attached.opens[0]["buffers"] == [base64.b64encode(b"\x00\x01").decode("ascii")]


def _table(answer: dict[str, Any]) -> None:
    # The page whole and untouched: the box adds, drops and renames nothing.
    assert answer == PAGE
    page = TablePage.model_validate({**answer, "limit": 50})
    assert page.model_dump(by_alias=True) == {**PAGE, "limit": 50}


CASES: dict[str, tuple[dict[str, Any], Callable[[dict[str, Any]], None]]] = {
    "run": ({"target": {"kind": "all"}, "run_id": "run-1"}, _exactly(RUN)),
    "kernel": ({"action": "status"}, _exactly(KERNEL)),
    "outputs_clear": ({"cell_ids": [CELL]}, _as(_Cleared)),
    "comm": (
        {"frame_id": FRAME, "comm_id": "m1", "msg_id": "x", "content": {}, "buffers": []},
        _as(_Empty),
    ),
    "frame_attach": ({"frame_id": FRAME, "model_ids": ["m1"]}, _frame),
    "frame_detach": ({"frame_id": FRAME}, _as(_Detached)),
    "table": ({"cell_id": CELL, "offset": 100, "limit": 50}, _table),
    "envs": ({}, _exactly(LISTING)),
    "env_packages": ({"env_id": ENV.env_id}, _exactly(PACKAGES)),
    "env_install": ({"packages": ["polars"]}, _exactly(INSTALLED)),
    "env_action": ({"action": "remove", "packages": ["polars"]}, _exactly(INSTALLED)),
    "snapshot": ({}, _as(_Empty)),
}


def test_every_op_the_backend_sends_has_a_contract_case() -> None:
    assert set(CASES) == set(NOTEBOOK_REQUEST_OPS) == set(SERVED_OPS)


@pytest.mark.parametrize("op", sorted(CASES))
async def test_the_box_answers_each_op_as_the_model_the_backend_reads(op: str) -> None:
    body, check = CASES[op]
    answer = await _OPS[op](_Relay(), dict(body))
    check(answer)


async def test_an_env_packages_answer_validates_as_the_engine_model_the_backend_uses() -> None:
    """The regression on its own: the box once answered the packages without
    the environment they belong to, and the backend's validation 500'd."""
    relay = _Relay()
    answer = await _OPS["env_packages"](relay, {"env_id": ENV.env_id})
    assert EnvPackages.model_validate(answer).env_id == ENV.env_id
    assert relay.client.env_asked == [ENV.env_id]
