"""The shared picture cases, read by the rule the Slack thread follows.

``fixtures/chat_paths/images.json`` is read here AND by the web renderer's
test (``packages/ui/src/primitives/render/Markdown/chatImageParity.test.ts``),
so a picture the chat shows is a picture the thread gets: a case that passes on
one side and fails on the other is the two rules drifting apart.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from alkera_core.chat_paths import chat_images

_CASES: dict[str, Any] = json.loads(
    (Path(__file__).parent / "fixtures" / "chat_paths" / "images.json").read_text("utf-8")
)
_CHAT_ID: str = _CASES["chat_id"]


@pytest.mark.parametrize(
    ("markdown", "expected"),
    [pytest.param(case["markdown"], case["images"], id=case["id"]) for case in _CASES["cases"]],
)
def test_the_thread_shows_the_pictures_the_chat_shows(
    markdown: str, expected: list[dict[str, str]]
) -> None:
    found = [
        {"alt": image.alt, "path": image.path.path}
        for image in chat_images(markdown, chat_id=_CHAT_ID)
    ]
    assert found == expected


def test_a_box_path_into_this_chat_is_a_picture_of_the_chat_folder() -> None:
    """The box's absolute path names the file under the CHAT folder, which is
    where the Slack relay looks it up. (Not in the shared table: the web learns
    this spelling separately.)"""
    target = f"/opt/alkera-work/.alkera/chats/{_CHAT_ID}/scratch/c.png"
    (image,) = chat_images(f"![C]({target})", chat_id=_CHAT_ID)
    assert (image.path.anchor, image.path.path, image.target) == ("chat", "scratch/c.png", target)
