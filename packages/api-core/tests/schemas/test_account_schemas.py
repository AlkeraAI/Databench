"""The persisted account lifecycle documents: the deletion plan and the
erasure certificate.

Both live in JSONB on ``account_deletion_requests`` and are read back long
after the process that wrote them, so every historical fixture under
``packages/api-core/tests/fixtures/account/<document>/v*.json`` must still load with today's
reader, and the reader must refuse a shape it cannot honour.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from alkera_core.schemas.account import (
    DeletionPlan,
    ErasureCertificate,
    PlanBlocker,
    PlannedOrg,
)
from alkera_core.versioning import VersionedModel
from pydantic import ValidationError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "account"
DOCUMENTS: dict[str, type[VersionedModel]] = {
    "deletion_plan": DeletionPlan,
    "erasure_certificate": ErasureCertificate,
}


def _fixtures() -> list[object]:
    return [
        pytest.param(model, path, id=f"{name}/{path.name}")
        for name, model in DOCUMENTS.items()
        for path in sorted((FIXTURES / name).glob("v*.json"))
    ]


def test_every_document_has_a_fixture() -> None:
    for name in DOCUMENTS:
        assert list((FIXTURES / name).glob("v*.json")), name


@pytest.mark.parametrize(("model", "path"), _fixtures())
def test_every_historical_fixture_loads_with_the_current_reader(
    model: type[VersionedModel], path: Path
) -> None:
    raw = json.loads(path.read_text())
    loaded = model.model_validate(raw)
    assert loaded.model_dump(mode="json")["schema_version"] == model.SCHEMA_VERSION


def test_plan_round_trips_and_reads_its_fixture_facts() -> None:
    raw = json.loads((FIXTURES / "deletion_plan" / "v1_0_0.json").read_text())
    plan = DeletionPlan.model_validate(raw)
    assert [o.fate for o in plan.orgs] == ["leave", "close"]
    assert plan.orgs[0].transfer_to_user_id == uuid.UUID("00000000-0000-4000-8000-000000000002")
    assert plan.forfeited_credit_nanos == 1_500_000_000
    assert DeletionPlan.model_validate(plan.model_dump(mode="json")) == plan


@pytest.mark.parametrize(
    ("blockers", "can_proceed"),
    [
        pytest.param([], True, id="none"),
        pytest.param([PlanBlocker(code="paid_plan")], False, id="one"),
    ],
)
def test_a_plan_can_proceed_only_without_blockers(
    blockers: list[PlanBlocker], can_proceed: bool
) -> None:
    assert DeletionPlan(blockers=blockers).can_proceed is can_proceed


def test_an_unknown_fate_is_refused() -> None:
    with pytest.raises(ValidationError):
        PlannedOrg(org_id=uuid.uuid4(), fate="archive")  # type: ignore[arg-type]


def test_an_unknown_blocker_code_is_refused() -> None:
    with pytest.raises(ValidationError):
        PlanBlocker(code="moon_phase")  # type: ignore[arg-type]


def test_unknown_fields_from_a_newer_writer_survive_a_round_trip() -> None:
    raw = {"schema_version": "1.0.0", "orgs": [], "blockers": [], "added_later": {"x": 1}}
    plan = DeletionPlan.model_validate(raw)
    assert plan.model_dump(mode="json")["added_later"] == {"x": 1}


def test_certificate_round_trips() -> None:
    raw = json.loads((FIXTURES / "erasure_certificate" / "v1_0_0.json").read_text())
    cert = ErasureCertificate.model_validate(raw)
    assert cert.rows["workspace_objects.owner_user_id:transfer"] == 3
    assert cert.resealed[0].rows_rewritten == 7
    assert ErasureCertificate.model_validate(cert.model_dump(mode="json")) == cert
