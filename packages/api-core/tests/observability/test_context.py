"""Trace-id context: bind/get/reset + structlog contextvar mirroring."""

from __future__ import annotations

import re

import pytest
import structlog
from alkera_core.observability.context import (
    bind_log_context,
    bind_trace_id,
    clear_log_context,
    get_trace_id,
    new_trace_id,
    reset_trace_id,
)


@pytest.fixture(autouse=True)
def _clean_context() -> None:
    clear_log_context()
    yield
    clear_log_context()


def test_new_trace_id_is_32_hex_and_unique() -> None:
    a, b = new_trace_id(), new_trace_id()
    assert a != b
    assert re.fullmatch(r"[0-9a-f]{32}", a)


def test_get_trace_id_is_none_by_default() -> None:
    assert get_trace_id() is None


def test_bind_and_reset_roundtrip() -> None:
    token = bind_trace_id("trace-abc")
    assert get_trace_id() == "trace-abc"
    assert structlog.contextvars.get_contextvars().get("trace_id") == "trace-abc"
    reset_trace_id(token)
    assert get_trace_id() is None
    assert "trace_id" not in structlog.contextvars.get_contextvars()


def test_bind_log_context_adds_fields() -> None:
    bind_trace_id("t-1")
    bind_log_context(user_id="u-1", org_id="o-1")
    ctx = structlog.contextvars.get_contextvars()
    assert ctx["user_id"] == "u-1"
    assert ctx["org_id"] == "o-1"


def test_clear_log_context_drops_everything() -> None:
    bind_trace_id("t-1")
    bind_log_context(user_id="u-1")
    clear_log_context()
    assert structlog.contextvars.get_contextvars() == {}
