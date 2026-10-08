"""The safe-slug rule for a handle that becomes a path component."""

from __future__ import annotations

import pytest
from alkera_core.naming import handle_error, is_safe_handle

MAX_LENGTH = 128


@pytest.mark.parametrize(
    "handle",
    [
        pytest.param("Analytics-Prod", id="mixed-case-and-a-dash"),
        pytest.param("x.y_z-0", id="every-permitted-punctuation"),
        pytest.param("a..b", id="dots-inside-are-not-traversal"),
        pytest.param("a" * MAX_LENGTH, id="the-longest-permitted-name"),
    ],
)
def test_a_safe_slug_is_accepted(handle: str) -> None:
    assert is_safe_handle(handle) is True


@pytest.mark.parametrize(
    "handle",
    [
        pytest.param("", id="empty"),
        pytest.param("../../etc", id="dotdot-chain"),
        pytest.param("a/b", id="posix-separator"),
        pytest.param("a\\b", id="windows-separator"),
        pytest.param("/Users/victim/Documents", id="absolute-posix"),
        pytest.param("Analytics Prod", id="a-space-is-the-name-that-used-to-save"),
        # ``$`` matches just before a trailing newline, which is why the pattern
        # anchors on ``\Z`` — and Windows refuses such a directory name outright.
        pytest.param("Analytics-Prod\n", id="an-accepted-name-plus-a-newline"),
        pytest.param(".hidden", id="a-leading-dot-hides-the-directory"),
        pytest.param("café", id="non-ascii-letter"),
        pytest.param("a" * (MAX_LENGTH + 1), id="one-over-the-length-cap"),
    ],
)
def test_an_unsafe_name_is_refused(handle: str) -> None:
    assert is_safe_handle(handle) is False


def test_the_refusal_names_the_offending_value() -> None:
    assert "Analytics Prod" in handle_error("Analytics Prod")
