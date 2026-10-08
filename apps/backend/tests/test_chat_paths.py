"""What a path the agent wrote names inside its chat's folder -- the pure half.

The walk against real Files is ``apps/backend/tests/files/test_chat_paths_resolve.py``.
"""

from __future__ import annotations

import pytest
from alkera_core.config import settings
from backend.services.files.chat_paths import ChatPath, chat_path, file_web_url

CHAT = "5d1c2e3f-9a8b-4c7d-8e6f-0123456789ab"
OTHER = "0a0b0c0d-1111-4222-8333-444455556666"


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        pytest.param(
            "charts/responses.png", ChatPath("charts/responses.png", "working"), id="relative"
        ),
        pytest.param(
            "./plot_responses.py", ChatPath("plot_responses.py", "working"), id="dot-slash"
        ),
        pytest.param("././a.csv", ChatPath("a.csv", "working"), id="repeated-dot-slash"),
        pytest.param(
            "out/2026/q3/sum.csv", ChatPath("out/2026/q3/sum.csv", "working"), id="nested"
        ),
        pytest.param("my%20chart.png", ChatPath("my chart.png", "working"), id="url-encoded-space"),
        pytest.param(
            f"/opt/alkera-work/.alkera/chats/{CHAT}/scratch/charts/x.png",
            ChatPath("scratch/charts/x.png", "chat"),
            id="absolute-box-path",
        ),
        pytest.param(
            f"/home/u/proj/.alkera/chats/{CHAT}/report.md",
            ChatPath("report.md", "chat"),
            id="absolute-local-chat-path",
        ),
    ],
)
def test_a_path_inside_the_chat_is_named_by_its_steps(target: str, expected: ChatPath) -> None:
    assert chat_path(target, chat_id=CHAT) == expected


@pytest.mark.parametrize(
    "target",
    [
        pytest.param("../secrets.csv", id="climbs-out"),
        pytest.param("charts/../../x.png", id="climbs-out-midway"),
        pytest.param("%2E%2E/x.png", id="encoded-climb"),
        pytest.param(f"/opt/alkera-work/.alkera/chats/{OTHER}/scratch/a.png", id="another-chat"),
        pytest.param(
            f"/opt/alkera-work/.alkera/chats/{CHAT}/../{OTHER}/a.png", id="climbs-sideways"
        ),
        pytest.param("/etc/passwd", id="system-path"),
        pytest.param("https://evil.example/a.png", id="web-url"),
        pytest.param("data:image/png;base64,AAAA", id="data-uri"),
        pytest.param("blob:0123abcd", id="blob-handle"),
        pytest.param("file:///etc/passwd", id="file-url"),
        pytest.param("a\\b.png", id="backslash"),
        pytest.param("a.png?raw=1", id="query"),
        pytest.param("#section", id="fragment"),
        pytest.param("charts//x.png", id="empty-step"),
        pytest.param("", id="empty"),
        pytest.param(" a.png", id="padded"),
    ],
)
def test_anything_outside_the_chat_folder_is_not_a_chat_file(target: str) -> None:
    assert chat_path(target, chat_id=CHAT) is None


@pytest.mark.parametrize(
    ("name", "image"),
    [
        pytest.param("a.png", True, id="png"),
        pytest.param("a.JPEG", True, id="upper-jpeg"),
        pytest.param("a.svg", True, id="svg"),
        pytest.param("a.py", False, id="code"),
        pytest.param(".png", False, id="dotfile"),
    ],
)
def test_an_image_is_what_the_web_renders_as_one(name: str, image: bool) -> None:
    assert ChatPath(name, "working").is_image is image


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        pytest.param(
            "https://app.example.com", "https://app.example.com/files/n1", id="production"
        ),
        pytest.param(
            "https://staging.example.com/", "https://staging.example.com/files/n1", id="staging"
        ),
        pytest.param("http://localhost:27173", "http://localhost:27173/files/n1", id="local-dev"),
    ],
)
def test_a_files_page_is_on_the_deployments_own_origin(
    monkeypatch: pytest.MonkeyPatch, base: str, expected: str
) -> None:
    monkeypatch.setattr(settings, "frontend_base_url", base)
    assert file_web_url("n1") == expected
    assert file_web_url("n1", base_url=base) == expected
