"""The shared file-link cases, read by the rule the Slack thread follows.

``fixtures/chat_paths/links.json`` is read here AND by the web renderer's test
(``packages/ui/src/primitives/render/Markdown/chatFileLinks.parity.test.ts``),
so a file the chat opens from a link is a file the thread attaches: a case that
passes on one side and fails on the other is the two rules drifting apart.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from alkera_core.chat_paths import chat_file_links

_CASES: dict[str, Any] = json.loads(
    (Path(__file__).parent / "fixtures" / "chat_paths" / "links.json").read_text("utf-8")
)
_CHAT_ID: str = _CASES["chat_id"]


@pytest.mark.parametrize(
    ("markdown", "expected"),
    [pytest.param(case["markdown"], case["links"], id=case["id"]) for case in _CASES["cases"]],
)
def test_the_thread_attaches_the_files_the_chat_links(
    markdown: str, expected: list[dict[str, str]]
) -> None:
    found = [
        {"label": link.label, "path": link.path.path}
        for link in chat_file_links(markdown, chat_id=_CHAT_ID)
    ]
    assert found == expected
