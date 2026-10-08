"""``alkera.status``: the progress bar's frames under a controlled clock,
and the spinner's show-then-clear."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import alkera
import pytest
from alkera import _host
from alkera.status import ProgressBar, ProgressView, Spinner, SpinnerView, _duration


class Screen(_host.ScriptHost):
    """A host that keeps what it was asked to show, standing in for a cell's
    output area."""

    def __init__(self) -> None:
        super().__init__()
        self.frames: list[tuple[str, Any]] = []

    def display(self, obj: Any) -> None:
        self.frames.append(("append", obj))

    def replace(self, obj: Any) -> None:
        self.frames.append(("replace", obj))

    def clear(self) -> None:
        self.frames.append(("clear", None))


@pytest.fixture
def screen() -> Iterator[Screen]:
    host = Screen()
    with _host.use(host):
        yield host


class Clock:
    def __init__(self, step: float) -> None:
        self.now = 1000.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def test_frames_are_rate_limited_and_the_last_is_exact(screen: Screen) -> None:
    bar = ProgressBar(range(1000), title="Rows", clock=Clock(0.001))
    assert list(bar) == list(range(1000))
    views = [obj for mode, obj in screen.frames]
    assert all(mode == "replace" for mode, _ in screen.frames)
    # One second of work at ten frames a second, plus the first and last.
    assert 10 <= len(views) <= 13, len(views)
    last = views[-1]
    assert isinstance(last, ProgressView)
    assert (last.done, last.total, last.finished) == (1000, 1000, True)
    assert last._plain().startswith("Rows: 1000/1000 (100%)")
    assert views[0].done == 0


def test_without_the_rate_limit_every_item_would_be_a_frame(screen: Screen) -> None:
    list(ProgressBar(range(50), clock=Clock(0.001), interval=0))
    assert len(screen.frames) == 52


def test_slow_items_each_get_a_frame(screen: Screen) -> None:
    list(ProgressBar(range(5), clock=Clock(0.5)))
    assert [obj.done for _, obj in screen.frames] == [0, 1, 2, 3, 4, 5, 5]


def test_total_comes_from_len_or_the_caller(screen: Screen) -> None:
    assert len(alkera.status.progress_bar([1, 2, 3])) == 3
    gen = (i for i in range(4))
    bar = alkera.status.progress_bar(gen, total=4, title="g")
    assert len(bar) == 4
    assert list(bar) == [0, 1, 2, 3]


def test_unknown_total_shows_a_count_without_a_bar(screen: Screen) -> None:
    bar = ProgressBar((i for i in range(3)), clock=Clock(1.0))
    with pytest.raises(TypeError, match="no total"):
        len(bar)
    list(bar)
    last = screen.frames[-1][1]
    assert "<progress" not in last._html()
    assert last._plain().startswith("3  0:0")


def test_iterating_again_starts_over(screen: Screen) -> None:
    bar = ProgressBar(range(3), clock=Clock(1.0))
    list(bar)
    first = len(screen.frames)
    list(bar)
    assert screen.frames[first][1].done == 0
    assert screen.frames[-1][1].done == 3


def test_progress_html_escapes_the_title() -> None:
    view = ProgressView("<b>x</b>", 1, 2, 3.0, finished=False)
    assert "&lt;b&gt;x&lt;/b&gt;" in view._html()
    assert '<progress value="1" max="2"' in view._html()
    assert view._plain() == "<b>x</b>: 1/2 (50%)  0:03  0.3/s"


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        pytest.param(0, "0:00", id="zero"),
        pytest.param(65.9, "1:05", id="minutes"),
        pytest.param(3725, "1:02:05", id="hours"),
        pytest.param(-3, "0:00", id="negative"),
    ],
)
def test_duration(seconds: float, text: str) -> None:
    assert _duration(seconds) == text


def test_spinner_shows_updates_and_clears(screen: Screen) -> None:
    with alkera.status.spinner("Fetching") as spin:
        spin.update("Parsing")
    modes = [mode for mode, _ in screen.frames]
    assert modes == ["replace", "replace", "clear"]
    titles = [obj.title for mode, obj in screen.frames if mode == "replace"]
    assert titles == ["Fetching", "Parsing"]


def test_spinner_clears_when_the_block_raises(screen: Screen) -> None:
    with pytest.raises(RuntimeError), Spinner("Work"):
        raise RuntimeError("boom")
    assert screen.frames[-1] == ("clear", None)


def test_spinner_view_is_self_contained_and_escaped() -> None:
    view = SpinnerView("<x>")
    html = view._html()
    assert "&lt;x&gt;" in html
    assert "@keyframes alkera-spin" in html
    assert "http" not in html
    assert view._plain() == "<x>..."
