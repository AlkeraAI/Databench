"""``alkera.status``: a progress bar and a spinner shown as the cell's
output while work runs.

Both replace the cell's whole output through the host, at most ten times a
second, and always once more at the end so the last state is exact.
"""

from __future__ import annotations

import html
import time
from collections.abc import Callable, Iterable, Iterator, Sized
from types import TracebackType
from typing import Generic, TypeVar

from . import _host
from ._outputs import Output

__all__ = ["ProgressBar", "Spinner", "progress_bar", "spinner"]

T = TypeVar("T")

#: Seconds between two updates of the same bar.
UPDATE_INTERVAL_S = 0.1


def _duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


class ProgressView(Output):
    """One frame of a progress bar."""

    def __init__(
        self, title: str | None, done: int, total: int | None, elapsed: float, *, finished: bool
    ) -> None:
        self.title = title
        self.done = done
        self.total = total
        self.elapsed = elapsed
        self.finished = finished

    def _summary(self) -> str:
        parts = []
        if self.total:
            parts.append(f"{self.done}/{self.total} ({self.done * 100 // self.total}%)")
        else:
            parts.append(f"{self.done}")
        parts.append(_duration(self.elapsed))
        if self.elapsed > 0 and self.done:
            parts.append(f"{self.done / self.elapsed:.1f}/s")
        return "  ".join(parts)

    def _html(self) -> str:
        title = f"<strong>{html.escape(self.title)}</strong> " if self.title else ""
        bar = ""
        if self.total:
            style = "width: 16rem; vertical-align: middle;"
            bar = f'<progress value="{self.done}" max="{self.total}" style="{style}"></progress> '
        summary = html.escape(self._summary())
        return (
            f'<div class="alkera-progress" role="status">{title}{bar}<span>{summary}</span></div>'
        )

    def _plain(self) -> str:
        return f"{self.title}: {self._summary()}" if self.title else self._summary()


class ProgressBar(Generic[T]):
    """Iterate ``iterable``, showing how far along it is."""

    def __init__(
        self,
        iterable: Iterable[T],
        *,
        title: str | None = None,
        total: int | None = None,
        clock: Callable[[], float] = time.monotonic,
        interval: float = UPDATE_INTERVAL_S,
    ) -> None:
        if total is None and isinstance(iterable, Sized):
            total = len(iterable)
        self._iterable = iterable
        self.title = title
        self.total = total
        self.done = 0
        self._clock = clock
        self._interval = interval
        self._started = 0.0
        self._last: float | None = None

    def __len__(self) -> int:
        if self.total is None:
            raise TypeError("this progress bar has no total")
        return self.total

    def _show(self, *, finished: bool) -> None:
        now = self._clock()
        if not finished and self._last is not None and now - self._last < self._interval:
            return
        self._last = now
        view = ProgressView(
            self.title, self.done, self.total, now - self._started, finished=finished
        )
        _host.current().replace(view)

    def __iter__(self) -> Iterator[T]:
        self._started = self._clock()
        self._last = None
        self.done = 0
        self._show(finished=False)
        for item in self._iterable:
            yield item
            self.done += 1
            self._show(finished=False)
        self._show(finished=True)


def progress_bar(
    iterable: Iterable[T], *, title: str | None = None, total: int | None = None
) -> ProgressBar[T]:
    """Wrap ``iterable`` in a progress bar shown as the cell's output."""
    return ProgressBar(iterable, title=title, total=total)


class SpinnerView(Output):
    def __init__(self, title: str) -> None:
        self.title = title

    def _html(self) -> str:
        # A rotating ring made with CSS only; the animation is local to the element.
        ring = (
            '<span aria-hidden="true" style="display: inline-block; width: 0.9em; height: 0.9em; '
            "border: 2px solid rgba(127, 127, 127, 0.35); border-top-color: currentColor; "
            "border-radius: 50%; "
            'vertical-align: -0.1em; animation: alkera-spin 0.8s linear infinite;"></span>'
        )
        keyframes = "<style>@keyframes alkera-spin { to { transform: rotate(360deg); } }</style>"
        title = html.escape(self.title)
        return f'<div class="alkera-spinner" role="status">{keyframes}{ring} {title}</div>'

    def _plain(self) -> str:
        return f"{self.title}..."


class Spinner:
    """A spinner shown while the ``with`` block runs; cleared at its end."""

    def __init__(self, title: str = "Loading") -> None:
        self.title = title

    def update(self, title: str) -> None:
        """Change the spinner's text."""
        self.title = title
        _host.current().replace(SpinnerView(title))

    def __enter__(self) -> Spinner:
        _host.current().replace(SpinnerView(self.title))
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        _host.current().clear()


def spinner(title: str = "Loading") -> Spinner:
    """``with alkera.status.spinner("Loading"):`` shows a spinner while the
    block runs."""
    return Spinner(title)
