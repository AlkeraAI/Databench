"""The sandbox a machine row carries: the two modes a box can report, the
fail-closed ``none`` when it has reported nothing, and nothing else."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from alkera_core.schemas.compute_machines import MachineDetail, MachineRow
from pydantic import ValidationError

CREATED = datetime(2026, 9, 1, tzinfo=UTC)


@pytest.mark.parametrize("model", [MachineRow, MachineDetail])
@pytest.mark.parametrize("sandbox", ["gvisor", "none"])
def test_each_reported_sandbox_round_trips(model: type[MachineRow], sandbox: str) -> None:
    row = model.model_validate({"id": "m-1", "created_at": CREATED, "sandbox": sandbox})
    again = model.model_validate_json(row.model_dump_json())
    assert again.sandbox == sandbox
    assert again.model_dump()["sandbox"] == sandbox


@pytest.mark.parametrize("model", [MachineRow, MachineDetail])
def test_a_row_that_names_no_sandbox_reads_none(model: type[MachineRow]) -> None:
    assert model(id="m-1", created_at=CREATED).sandbox == "none"


@pytest.mark.parametrize(
    "sandbox",
    [
        pytest.param("runsc", id="engine-name"),
        pytest.param("GVISOR", id="wrong-case"),
        pytest.param("", id="empty"),
        pytest.param(None, id="null"),
    ],
)
def test_any_other_sandbox_is_refused(sandbox: object) -> None:
    with pytest.raises(ValidationError):
        MachineRow.model_validate({"id": "m-1", "created_at": CREATED, "sandbox": sandbox})
