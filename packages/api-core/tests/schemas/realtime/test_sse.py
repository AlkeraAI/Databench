"""The event stream's ``data:`` bodies: thin, typed to the client-deliverable
vocabulary, and closed to extra fields. Pure — no database."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from alkera_core.events import EventType, RealtimeEventType
from alkera_core.schemas.realtime import SseEventData, SseResetData
from pydantic import ValidationError

_ORG = UUID("0f2b6c1e-6d1a-4a6d-9f1e-2c3b4a5d6e7f")


def _body(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "type": "kb_item.changed",
        "entity": "kb_item",
        "entity_id": "item-1",
        "version": 3,
        "org_id": str(_ORG),
    }
    base.update(overrides)
    return base


def test_event_data_dumps_exactly_the_five_thin_keys() -> None:
    """A writer that named nothing beyond the entity puts nothing beyond it on
    the wire: the narrowing ids are dropped rather than sent as nulls, so an
    ordinary frame costs a client the same bytes it always did."""
    data = SseEventData.model_validate(_body())
    dumped = data.model_dump(mode="json", exclude_none=True)
    assert dumped == {
        "type": "kb_item.changed",
        "entity": "kb_item",
        "entity_id": "item-1",
        "version": 3,
        "org_id": str(_ORG),
    }
    assert data.type is RealtimeEventType.KB_ITEM_CHANGED


def test_the_narrowing_ids_ride_along_when_the_writer_holds_them() -> None:
    """A frame may say WHERE, and only in ids.

    The folder and the drive are what let a client re-read the one listing that
    moved instead of every listing it holds, and the reason is one of a fixed
    set of words. Nothing else is admitted: the field set is closed, so a
    writer cannot widen the stream by putting a name on an outbox row.
    """
    node = "2f1c9d7a-0b2e-4a51-8c3d-6e7f8a9b0c1d"
    folder = "8a7b6c5d-4e3f-4210-9876-5a4b3c2d1e0f"
    data = SseEventData.model_validate(
        _body(
            type="file_node.changed",
            entity="file_node",
            entity_id=node,
            drive_id=str(_ORG),
            parent_id=folder,
            reason="live_saved",
        )
    )
    assert data.model_dump(mode="json", exclude_none=True) == {
        "type": "file_node.changed",
        "entity": "file_node",
        "entity_id": node,
        "version": 3,
        "org_id": str(_ORG),
        "drive_id": str(_ORG),
        "parent_id": folder,
        "reason": "live_saved",
    }
    with pytest.raises(ValidationError):
        SseEventData.model_validate(_body(name="q3.html"))


def test_event_data_defaults_version_to_zero() -> None:
    body = _body()
    del body["version"]
    assert SseEventData.model_validate(body).version == 0


@pytest.mark.parametrize("member", list(RealtimeEventType), ids=lambda m: m.value)
def test_every_client_deliverable_type_is_accepted(member: RealtimeEventType) -> None:
    assert SseEventData.model_validate(_body(type=member.value)).type is member


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(EventType.DOC_OP.value, id="doc-op-is-server-only"),
        pytest.param(EventType.AUTHZ_DECISION.value, id="authz-decision-is-server-only"),
        pytest.param("kb_item.deleted", id="unknown"),
    ],
)
def test_event_data_refuses_types_a_client_may_not_receive(value: str) -> None:
    with pytest.raises(ValidationError):
        SseEventData.model_validate(_body(type=value))


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"version": -1}, id="negative-version"),
        pytest.param({"entity": ""}, id="empty-entity"),
        pytest.param({"entity_id": ""}, id="empty-entity-id"),
        pytest.param({"entity": "e" * 65}, id="entity-too-long"),
        pytest.param({"entity_id": "e" * 256}, id="entity-id-too-long"),
        pytest.param({"org_id": "not-a-uuid"}, id="org-id-not-a-uuid"),
        pytest.param({"payload": {"secret": 1}}, id="payload-is-never-on-the-wire"),
        pytest.param({"actor": {}}, id="actor-is-never-on-the-wire"),
    ],
)
def test_event_data_refuses_malformed_or_fat_bodies(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        SseEventData.model_validate(_body(**overrides))


@pytest.mark.parametrize("reason", ["overflow", "cursor_ahead", "cursor_too_old"])
def test_reset_data_carries_only_a_reason(reason: str) -> None:
    assert SseResetData(reason=reason).model_dump() == {"reason": reason}


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"reason": ""}, id="empty-reason"),
        pytest.param({"reason": "r" * 65}, id="reason-too-long"),
        pytest.param({"reason": "overflow", "dropped": 3}, id="no-extra-fields"),
        pytest.param({}, id="reason-required"),
    ],
)
def test_reset_data_refuses_malformed_bodies(body: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        SseResetData.model_validate(body)
