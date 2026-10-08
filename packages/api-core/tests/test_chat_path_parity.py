"""The shared path cases, read by the box's rule.

``fixtures/chat_paths/cases.json`` is read here AND by the web renderer's test
(``packages/ui/src/primitives/render/Markdown/chatFiles.test.tsx``), so a
reference one surface opens is a reference every surface opens: a case that
passes on one side and fails on the other is a drift between the two rules.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from alkera_core.chat_paths import chat_path

_CASES: dict[str, Any] = json.loads(
    (Path(__file__).parent / "fixtures" / "chat_paths" / "cases.json").read_text("utf-8")
)
_CHAT_ID: str = _CASES["chat_id"]


@pytest.mark.parametrize(
    ("target", "expected"),
    [pytest.param(case["target"], case["path"], id=case["id"]) for case in _CASES["cases"]],
)
def test_the_box_rule_names_each_shared_case_as_the_web_does(
    target: str, expected: str | None
) -> None:
    found = chat_path(target, chat_id=_CHAT_ID)
    assert (found.path if found is not None else None) == expected
