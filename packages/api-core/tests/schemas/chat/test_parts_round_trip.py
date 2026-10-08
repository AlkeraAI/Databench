"""Round-trip tests for every Part variant.

For each known part variant: build an instance with realistic-but-tiny
data, dump to JSON, reload via the discriminated union, assert the
reloaded instance equals the original.

Adding a NEW part variant means adding a new fixture row here AND a
fixture file under `packages/api-core/tests/fixtures/chat/v<X.Y.Z>/parts/` (the lineage
regression net — see `CLAUDE.md`).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from alkera_core.schemas.chat import (
    AgentPart,
    CompactionPart,
    FilePart,
    Part,
    PatchPart,
    RawPart,
    RawProviderPart,
    ReasoningPart,
    RetryPart,
    SnapshotPart,
    StepFinishPart,
    StepStartPart,
    SubtaskPart,
    TextPart,
    ToolCallPart,
)
from pydantic import TypeAdapter

PartAdapter = TypeAdapter(Part)
_T = datetime(2026, 5, 26, tzinfo=UTC)


@pytest.mark.parametrize(
    "build",
    [
        lambda: TextPart(part_id="p1", message_id="m1", time=_T, text="hi"),
        lambda: ReasoningPart(part_id="p1", message_id="m1", time=_T, text="thinking"),
        lambda: FilePart(
            part_id="p1",
            message_id="m1",
            time=_T,
            sha256="a" * 64,
            filename="x.png",
            mime="image/png",
            size=42,
        ),
        lambda: ToolCallPart(
            part_id="p1",
            message_id="m1",
            time=_T,
            call_id="c1",
            name="read_file",
            input={"path": "/tmp/x"},
            state="completed",
            output={"content": "hello"},
        ),
        lambda: StepStartPart(part_id="p1", message_id="m1", time=_T),
        lambda: StepFinishPart(
            part_id="p1",
            message_id="m1",
            time=_T,
            reason="stop",
            tokens={"input": 100, "output": 50},
            cost=0.01,
        ),
        lambda: SnapshotPart(part_id="p1", message_id="m1", time=_T, snapshot_hash="abc"),
        lambda: PatchPart(part_id="p1", message_id="m1", time=_T, hash="def", files=["a.py"]),
        lambda: AgentPart(part_id="p1", message_id="m1", time=_T, name="general"),
        lambda: SubtaskPart(
            part_id="p1",
            message_id="m1",
            time=_T,
            prompt="do the thing",
            agent="explorer",
        ),
        lambda: CompactionPart(part_id="p1", message_id="m1", time=_T, auto=True),
        lambda: RetryPart(part_id="p1", message_id="m1", time=_T, attempt=2, error="rate-limit"),
        lambda: RawProviderPart(
            part_id="p1",
            message_id="m1",
            time=_T,
            provider_id="anthropic",
            payload={"weird_field": 1},
        ),
    ],
)
def test_part_round_trip(build) -> None:
    original = build()
    dumped = original.model_dump(mode="json")
    reloaded = PartAdapter.validate_python(dumped)
    # The discriminated union returns the concrete subclass — assert
    # the dumped fields survived round-trip 1:1.
    assert type(reloaded) is type(original)
    assert reloaded.model_dump(mode="json") == dumped


def test_unknown_part_type_falls_back_to_raw_part() -> None:
    """A future writer's part type lands in RawPart, not a crash."""
    payload: dict[str, Any] = {
        "type": "screen_recording",  # not in known tags
        "part_id": "p1",
        "message_id": "m1",
        "schema_version": "1.0.0",
        "duration_ms": 12345,
    }
    reloaded = PartAdapter.validate_python(payload)
    assert isinstance(reloaded, RawPart)
    assert reloaded.type == "screen_recording"
    # The unknown payload field survives via extras (extra="allow")
    assert reloaded.model_dump(mode="json").get("duration_ms") == 12345


def test_unknown_fields_on_known_part_survive() -> None:
    """A newer writer added a field to TextPart — older reader keeps it."""
    payload: dict[str, Any] = {
        "type": "text",
        "part_id": "p1",
        "message_id": "m1",
        "schema_version": "1.0.0",
        "text": "hello",
        "future_field": "added in 1.1",
    }
    reloaded = PartAdapter.validate_python(payload)
    assert isinstance(reloaded, TextPart)
    assert reloaded.model_dump(mode="json")["future_field"] == "added in 1.1"


def test_tool_call_state_lifecycle() -> None:
    """A pending tool call serializes + reloads in each state without
    drama. We don't enforce a state machine at the Pydantic layer —
    transitions are managed by `PartUpdated` events at fold time."""
    for state in ("pending", "running", "completed", "error"):
        part = ToolCallPart(
            part_id="p1",
            message_id="m1",
            time=_T,
            call_id="c1",
            state=state,  # type: ignore[arg-type]
        )
        reloaded = PartAdapter.validate_python(part.model_dump(mode="json"))
        assert isinstance(reloaded, ToolCallPart)
        assert reloaded.state == state


def test_redacted_text_part_round_trips() -> None:
    """The `redacted` flag is preserved across round-trip; the text
    itself was already swapped by the redactor upstream."""
    p = TextPart(
        part_id="p1",
        message_id="m1",
        time=_T,
        text="[REDACTED: 7 chars]",
        redacted=True,
    )
    reloaded = PartAdapter.validate_python(p.model_dump(mode="json"))
    assert isinstance(reloaded, TextPart)
    assert reloaded.redacted is True
    assert reloaded.text == "[REDACTED: 7 chars]"
