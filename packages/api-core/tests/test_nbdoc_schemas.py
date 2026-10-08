"""The notebook routes' wire shapes: what they admit, what they refuse, and
that every operation round-trips through the discriminated union."""

from __future__ import annotations

from typing import Any

import pytest
from alkera_core.notebooks.schemas import (
    CELL_ID_RE,
    CELL_KINDS,
    MAX_EDITS_PER_OP,
    MAX_OPS_PER_BATCH,
    CommRequest,
    DeleteCell,
    EditCell,
    EnvInstallRequest,
    InsertCell,
    KernelRequest,
    MoveCell,
    NotebookOpsRequest,
    ReplaceCell,
    RestoreCell,
    RunRequest,
    SetCellConfig,
    SetCellKind,
    SetCellName,
    SetSetting,
)
from pydantic import ValidationError

CELL = "a1b2c3d4e5"

EVERY_OP: list[Any] = [
    pytest.param(
        {"op": "insert", "kind": "sql", "source": "SELECT 1", "after": CELL},
        InsertCell,
        id="insert",
    ),
    pytest.param(
        {"op": "edit", "cell_id": CELL, "edits": [{"old": "a", "new": "b", "occurrence": 2}]},
        EditCell,
        id="edit",
    ),
    pytest.param({"op": "replace", "cell_id": CELL, "source": "x = 1"}, ReplaceCell, id="replace"),
    pytest.param({"op": "delete", "cell_id": CELL}, DeleteCell, id="delete"),
    pytest.param({"op": "restore", "cell_id": CELL, "after": None}, RestoreCell, id="restore"),
    pytest.param({"op": "move", "cell_id": CELL, "before": CELL}, MoveCell, id="move"),
    pytest.param({"op": "rename", "cell_id": CELL, "name": "load"}, SetCellName, id="rename"),
    pytest.param({"op": "set_kind", "cell_id": CELL, "kind": "markdown"}, SetCellKind, id="kind"),
    pytest.param(
        {"op": "set_config", "cell_id": CELL, "config": {"hide_code": True}},
        SetCellConfig,
        id="config",
    ),
    pytest.param({"op": "set_setting", "key": "reactivity", "value": "lazy"}, SetSetting, id="set"),
]


@pytest.mark.parametrize(("body", "model"), EVERY_OP)
def test_every_op_is_routed_by_its_tag_and_round_trips(body: dict[str, Any], model: type) -> None:
    request = NotebookOpsRequest.model_validate({"ops": [body], "submit_id": "abcd1234"})
    (op,) = request.ops
    assert type(op) is model
    again = NotebookOpsRequest.model_validate(request.model_dump(mode="json"))
    assert again == request


def test_insert_defaults_to_an_empty_python_cell() -> None:
    (op,) = NotebookOpsRequest.model_validate({"ops": [{"op": "insert"}]}).ops
    assert isinstance(op, InsertCell)
    assert (op.kind, op.source, op.name, op.config, op.meta) == ("python", "", "_", {}, {})


REFUSED: list[Any] = [
    pytest.param({"ops": []}, id="no-ops"),
    pytest.param({"ops": [{"op": "insert"}] * (MAX_OPS_PER_BATCH + 1)}, id="too-many-ops"),
    pytest.param({"ops": [{"op": "teleport", "cell_id": CELL}]}, id="unknown-op"),
    pytest.param({"ops": [{"cell_id": CELL}]}, id="untagged-op"),
    pytest.param({"ops": [{"op": "edit", "cell_id": CELL, "edits": []}]}, id="zero-edits"),
    pytest.param(
        {
            "ops": [
                {
                    "op": "edit",
                    "cell_id": CELL,
                    "edits": [{"old": "a", "new": "b"}] * (MAX_EDITS_PER_OP + 1),
                }
            ]
        },
        id="too-many-edits",
    ),
    pytest.param(
        {"ops": [{"op": "edit", "cell_id": CELL, "edits": [{"old": "a", "new": "b", "x": 1}]}]},
        id="extra-field-on-an-edit",
    ),
    pytest.param(
        {
            "ops": [
                {
                    "op": "edit",
                    "cell_id": CELL,
                    "edits": [{"old": "a", "new": "b", "occurrence": 0}],
                }
            ]
        },
        id="occurrence-zero",
    ),
    pytest.param({"ops": [{"op": "delete", "cell_id": CELL, "hard": True}]}, id="extra-on-an-op"),
    pytest.param({"ops": [{"op": "insert"}], "submit_id": "short"}, id="submit-id-too-short"),
    pytest.param({"ops": [{"op": "insert"}], "submit_id": "x" * 49}, id="submit-id-too-long"),
    pytest.param({"ops": [{"op": "insert"}], "submit_id": "has space!"}, id="submit-id-chars"),
    pytest.param({"ops": [{"op": "insert"}], "extra": 1}, id="extra-on-the-batch"),
]


@pytest.mark.parametrize("body", REFUSED)
def test_a_malformed_batch_is_refused(body: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        NotebookOpsRequest.model_validate(body)


@pytest.mark.parametrize("submit_id", ["abcd1234", "A-b_C-d_" * 6], ids=["eight", "forty-eight"])
def test_submit_ids_at_the_bounds_are_admitted(submit_id: str) -> None:
    request = NotebookOpsRequest.model_validate({"ops": [{"op": "insert"}], "submit_id": submit_id})
    assert request.submit_id == submit_id


@pytest.mark.parametrize(
    ("target", "kind"),
    [
        pytest.param({"kind": "cells", "ids": [CELL]}, "cells", id="cells"),
        pytest.param({"kind": "all"}, "all", id="all"),
        pytest.param({"kind": "stale"}, "stale", id="stale"),
        pytest.param({"kind": "above", "id": CELL}, "above", id="above"),
        pytest.param({"kind": "below", "id": CELL}, "below", id="below"),
    ],
)
def test_every_run_target_kind_parses(target: dict[str, Any], kind: str) -> None:
    request = RunRequest.model_validate({"target": target, "frontier": "3.AQID"})
    assert request.target.kind == kind
    assert RunRequest.model_validate(request.model_dump(mode="json")) == request


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"target": {"kind": "cells", "ids": []}}, id="no-cells"),
        pytest.param({"target": {"kind": "above"}}, id="above-without-id"),
        pytest.param({"target": {"kind": "all", "ids": [CELL]}}, id="all-with-ids"),
        pytest.param({"target": {"kind": "everything"}}, id="unknown-kind"),
        pytest.param({"target": {"kind": "all"}, "client_run_id": "bad id"}, id="client-run-id"),
    ],
)
def test_a_malformed_run_is_refused(body: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        RunRequest.model_validate(body)


@pytest.mark.parametrize(
    ("model", "body"),
    [
        pytest.param(KernelRequest, {"action": "reboot"}, id="kernel-action"),
        pytest.param(EnvInstallRequest, {"packages": []}, id="no-packages"),
        pytest.param(EnvInstallRequest, {"packages": [""]}, id="empty-package"),
        pytest.param(EnvInstallRequest, {"packages": ["x"] * 51}, id="too-many-packages"),
        pytest.param(
            CommRequest, {"comm_id": "", "msg_id": "m", "content": {}}, id="comm-without-id"
        ),
        pytest.param(
            CommRequest,
            {"comm_id": "c", "msg_id": "m", "content": {}, "buffers": ["x"] * 65},
            id="too-many-buffers",
        ),
    ],
)
def test_malformed_kernel_env_and_comm_requests_are_refused(
    model: type, body: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(body)


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        pytest.param("a1b2c3d4e5", True, id="valid"),
        pytest.param("0123456789", True, id="digits"),
        pytest.param("a1b2c3d4ei", False, id="crockford-has-no-i"),
        pytest.param("a1b2c3d4el", False, id="no-l"),
        pytest.param("a1b2c3d4eo", False, id="no-o"),
        pytest.param("a1b2c3d4eu", False, id="no-u"),
        pytest.param("A1B2C3D4E5", False, id="upper-case"),
        pytest.param("a1b2c3d4e", False, id="nine-characters"),
        pytest.param("a1b2c3d4e5f", False, id="eleven-characters"),
    ],
)
def test_the_cell_id_pattern_is_lower_case_crockford(value: str, valid: bool) -> None:
    assert bool(CELL_ID_RE.match(value)) is valid


def test_the_kinds_are_the_formats() -> None:
    assert CELL_KINDS == ("setup", "python", "function", "class", "sql", "markdown", "unparsable")
