"""The chat row's sandbox limits reach the harness bag only when they are
well-formed; anything else leaves the box default in force. The sandbox MODE is
a node property, not a chat's, so a chat row never carries one."""

from __future__ import annotations

from typing import Any

import pytest
from alkera_cli.cloud.mirror import sandbox_fields_of


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        pytest.param({}, {}, id="absent"),
        pytest.param({"sandbox_vcpu": None, "sandbox_memory_mb": None}, {}, id="nulls"),
        pytest.param(
            {"sandbox_vcpu": 2, "sandbox_memory_mb": 8192},
            {"sandbox_vcpu": 2, "sandbox_memory_mb": 8192},
            id="both",
        ),
        pytest.param({"sandbox_vcpu": 0, "sandbox_memory_mb": -1}, {}, id="non-positive"),
        pytest.param({"sandbox_vcpu": True, "sandbox_memory_mb": "2048"}, {}, id="mistyped"),
        pytest.param({"sandbox_vcpu": 1}, {"sandbox_vcpu": 1}, id="partial"),
        # A stray sandbox MODE on the row is never a per-chat field — it is
        # dropped, not copied into the harness bag.
        pytest.param(
            {"sandbox": "none", "sandbox_required": "full", "sandbox_vcpu": 1},
            {"sandbox_vcpu": 1},
            id="node-mode-and-dead-required-are-dropped",
        ),
    ],
)
def test_sandbox_fields_of(row: dict[str, Any], expected: dict[str, Any]) -> None:
    assert sandbox_fields_of(row) == expected
