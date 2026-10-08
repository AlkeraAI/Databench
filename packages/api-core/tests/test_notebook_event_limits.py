"""A notebook's events fit every hop they cross, from one budget.

The box's post, the backend's outbox row and the realtime frame each have a
ceiling; every one is derived from the outbox's payload cap in
``alkera_core.notebooks.limits``. An event that would weigh more is fitted
there: an output too large to carry becomes a marker that says so, never a
refused batch."""

from __future__ import annotations

import base64
import datetime
import decimal
import json
import uuid
from typing import Any

import pytest
from alkera_core.events.outbox import MAX_FRAME_BYTES, MAX_PAYLOAD_BYTES
from alkera_core.notebooks import limits
from alkera_core.notebooks.limits import (
    EVENT_MAX_BYTES,
    TOO_LARGE_MIME,
    batches,
    fit_event,
    json_bytes,
)

#: What the owner's kernel sent: a 7.3 MB image in one output.
IMAGE = base64.b64encode(b"\x89PNG" + b"\x00" * 5_500_000).decode("ascii")


def _output(seq: int, bundle: dict[str, Any]) -> dict[str, Any]:
    return {"type": "cell.output", "kernel_id": "k1", "seq": seq, "cell_id": "c1", "output": bundle}


def test_every_hop_s_ceiling_comes_from_the_outbox_cap() -> None:
    assert EVENT_MAX_BYTES + limits.BATCH_ENVELOPE_BYTES == MAX_PAYLOAD_BYTES
    assert limits.BATCH_MAX_BYTES <= MAX_PAYLOAD_BYTES - limits.BATCH_ENVELOPE_BYTES
    assert MAX_FRAME_BYTES >= MAX_PAYLOAD_BYTES
    assert limits.INLINE_VALUE_MAX_BYTES < EVENT_MAX_BYTES
    assert limits.POST_BODY_MAX_BYTES >= EVENT_MAX_BYTES + limits.POST_MAX_BYTES


def test_an_event_that_fits_travels_unchanged() -> None:
    small = _output(1, {"text/plain": "42", "text/html": "<b>42</b>"})
    assert fit_event(small) == small


def test_an_output_too_large_to_carry_becomes_a_marker_that_says_so() -> None:
    event = _output(7, {"image/png": IMAGE, "text/plain": "<Figure>"})
    assert json_bytes(event) > MAX_PAYLOAD_BYTES  # the owner's 7.3 MB
    fitted = fit_event(event)
    assert json_bytes(fitted) <= EVENT_MAX_BYTES
    assert (fitted["type"], fitted["seq"], fitted["cell_id"]) == ("cell.output", 7, "c1")
    bundle = fitted["output"]
    assert "image/png" not in bundle
    assert bundle[TOO_LARGE_MIME]["outputs"][0]["mime"] == "image/png"
    assert bundle["text/plain"] == "<Figure>"


def test_a_too_large_output_without_text_says_so_in_text() -> None:
    fitted = fit_event(_output(1, {"text/html": "x" * 3_000_000}))
    assert fitted["output"]["text/plain"].startswith("Output too large to show here (")


def test_a_snapshot_s_outputs_are_fitted_where_they_sit() -> None:
    view = {
        "cells": [
            {"id": "a", "outputs": [{"output_id": "a/0", "data": {"image/png": IMAGE}}]},
            {"id": "b", "output": {"text/plain": "1"}},
        ]
    }
    fitted = fit_event({"type": "snapshot", "kernel_id": "k1", "seq": 3, "view": view})
    assert json_bytes(fitted) <= EVENT_MAX_BYTES
    assert TOO_LARGE_MIME in fitted["view"]["cells"][0]["outputs"][0]["data"]
    assert fitted["view"]["cells"][1]["output"] == {"text/plain": "1"}


def test_an_event_with_nothing_left_to_mark_keeps_who_it_is() -> None:
    huge = {
        "type": "cell.variables",
        "kernel_id": "k1",
        "seq": 9,
        "cell_id": "c",
        "names": ["x" * 3_000_000],
    }
    fitted = fit_event(huge)
    assert json_bytes(fitted) <= EVENT_MAX_BYTES
    assert {k: fitted[k] for k in ("type", "kernel_id", "seq", "cell_id")} == {
        "type": "cell.variables",
        "kernel_id": "k1",
        "seq": 9,
        "cell_id": "c",
    }
    assert fitted["too_large"]["bytes"] > MAX_PAYLOAD_BYTES


@pytest.mark.parametrize("count", [1, 3, 40])
def test_a_batch_is_split_in_order_under_the_row_cap(count: int) -> None:
    events = [_output(n, {"text/plain": "y" * 200_000}) for n in range(1, count + 1)]
    groups = batches(events)
    assert [e["seq"] for g in groups for e in g] == list(range(1, count + 1))
    assert all(json_bytes(g) <= limits.BATCH_MAX_BYTES for g in groups)


@pytest.mark.parametrize(
    ("value", "carried"),
    [
        pytest.param(datetime.date(2026, 10, 6), "2026-10-06", id="date"),
        pytest.param(
            datetime.datetime(2026, 10, 6, 11, 5, tzinfo=datetime.UTC),
            "2026-10-06T11:05:00+00:00",
            id="datetime",
        ),
        pytest.param(datetime.time(11, 5), "11:05:00", id="time"),
        pytest.param(decimal.Decimal("12.50"), "12.50", id="decimal"),
        pytest.param(float("nan"), "nan", id="nan"),
        pytest.param(float("-inf"), "-inf", id="negative-infinity"),
        pytest.param(uuid.UUID(int=5), "00000000-0000-0000-0000-000000000005", id="uuid"),
        pytest.param((1, 2.5, None, True, "x"), [1, 2.5, None, True, "x"], id="plain-values-kept"),
    ],
)
def test_a_fitted_event_is_strict_json_whatever_an_answer_carries(value: Any, carried: Any) -> None:
    """A table page's rows hold the frame's own values. An event the box's
    HTTP client cannot encode fails its whole post, so the answer in it never
    arrives: fitted, every value is one strict JSON can carry."""
    answer = {
        "type": "answer",
        "request_id": "r1",
        "result": {"rows": {"columns": [{"name": "v"}], "rows": [[value]]}, "total_rows": 1},
    }
    fitted = fit_event(answer)
    wire = json.dumps(fitted, allow_nan=False)
    assert json.loads(wire)["result"]["rows"]["rows"] == [[carried]]
    assert {k: fitted[k] for k in ("type", "request_id")} == {"type": "answer", "request_id": "r1"}
