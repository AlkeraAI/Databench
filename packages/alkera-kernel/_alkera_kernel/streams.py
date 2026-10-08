"""stdout and stderr captured at the file-descriptor level.

File descriptors 1 and 2 are replaced by pipes that the output pump drains,
so output from C extensions, child processes and forked workers is captured
and attributed to the step running when it was read. Python-level writes go
through the same descriptors (``sys.stdout`` writes straight to fd 1), so
ordering between ``print`` and ``os.write(1, ...)`` is kept.

What is sent is bounded per cell and stream: everything up to
``LIVE_LIMIT`` bytes is sent as it arrives, batched every ``FLUSH_S``; past
that the pump keeps only the last ``TAIL_LIMIT`` bytes and sends them, after a
note naming what was left out, when the cell's output is closed.
"""

from __future__ import annotations

import codecs
import contextlib
import fcntl
import io
import os
import selectors
import struct
import sys
import termios
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

HEAD_LIMIT = 64 * 1024
TAIL_LIMIT = 960 * 1024
LIVE_LIMIT = HEAD_LIMIT + TAIL_LIMIT
FLUSH_S = 0.05

#: (target, stream name, text) -> sends one ``cell.stream`` / ``thread.output``.
Sink = Callable[["Target", str, str], None]


@dataclass(frozen=True)
class Target:
    """Where captured output belongs: a step of a run, or (after that step
    finished) the cell a background thread was started from."""

    run_id: str | None
    cell_id: str | None
    thread: bool = False


def reduce_carriage_returns(text: str) -> str:
    """``text`` with carriage-return overwrites resolved where that loses
    nothing: rendering the result after any earlier output gives what
    rendering ``text`` would (JupyterLab's overwrite semantics). A 10,000-step
    progress bar in one batch becomes one bar. Text with ANSI cursor movement
    is returned unchanged."""
    if "\r" not in text or "\x1b[" in text:
        return text
    out: list[str] = []
    for line in text.split("\n"):
        if "\r" not in line:
            out.append(line)
            continue
        parts = line.split("\r")
        prefix, pieces = parts[0], parts[1:]
        overlay = ""
        for piece in pieces:
            overlay = piece + overlay[len(piece) :]
        last = pieces[-1]
        if len(last) == len(overlay):
            out.append(f"{prefix}\r{overlay}")
        else:
            # The cursor ends inside the overlay: overwrite, return, re-place it.
            out.append(f"{prefix}\r{overlay}\r{last}")
    return "\n".join(out)


@dataclass
class _Budget:
    sent: int = 0
    omitted: int = 0
    tail: list[str] = field(default_factory=list)
    tail_size: int = 0

    def take(self, text: str) -> str:
        """The part of ``text`` to send now; the rest goes to the tail."""
        size = len(text.encode("utf-8", "surrogatepass"))
        if self.sent + size <= LIVE_LIMIT and not self.tail:
            self.sent += size
            return text
        room = max(0, LIVE_LIMIT - self.sent) if not self.tail else 0
        head = text[:room] if room else ""
        if head:
            self.sent += len(head.encode("utf-8", "surrogatepass"))
        self._keep(text[len(head) :])
        return head

    def _keep(self, text: str) -> None:
        if not text:
            return
        self.tail.append(text)
        self.tail_size += len(text)
        while self.tail_size > TAIL_LIMIT and self.tail:
            excess = self.tail_size - TAIL_LIMIT
            first = self.tail[0]
            if len(first) <= excess:
                self.tail.pop(0)
                self.tail_size -= len(first)
                self.omitted += len(first)
            else:
                self.tail[0] = first[excess:]
                self.tail_size -= excess
                self.omitted += excess

    def closing(self) -> str:
        if not self.tail:
            return ""
        note = (
            f"\n[... {self.omitted:,} characters of output omitted ...]\n" if self.omitted else ""
        )
        text = note + "".join(self.tail)
        self.tail.clear()
        self.tail_size = 0
        return text


class OutputRouter:
    """Batches captured text per (target, stream) and sends it through
    ``sink`` at most every ``FLUSH_S``, within each cell's budget."""

    def __init__(self, sink: Sink) -> None:
        self._sink = sink
        self._lock = threading.RLock()
        self._pending: dict[tuple[Target, str], list[str]] = {}
        self._budgets: dict[tuple[Target, str], _Budget] = {}
        self.target = Target(None, None)
        self._last_flush = time.monotonic()

    def write(self, name: str, text: str, target: Target | None = None) -> None:
        if not text:
            return
        with self._lock:
            key = (target or self.target, name)
            self._pending.setdefault(key, []).append(text)

    def due(self) -> bool:
        return time.monotonic() - self._last_flush >= FLUSH_S

    def flush(self) -> None:
        with self._lock:
            pending, self._pending = self._pending, {}
            self._last_flush = time.monotonic()
            for key, chunks in pending.items():
                budget = self._budgets.setdefault(key, _Budget())
                text = budget.take(reduce_carriage_returns("".join(chunks)))
                if text and key[0].cell_id is not None:
                    self._sink(key[0], key[1], text)

    def close_target(self, target: Target) -> None:
        """Send what the budget held back for ``target`` and forget it."""
        self.flush()
        with self._lock:
            for key in [k for k in self._budgets if k[0] == target]:
                rest = self._budgets.pop(key).closing()
                if rest and target.cell_id is not None:
                    self._sink(target, key[1], rest)


def _pending_bytes(fd: int) -> int:
    buf = fcntl.ioctl(fd, termios.FIONREAD, b"\0\0\0\0")
    return int(struct.unpack("i", buf)[0])


class FdCapture:
    """Pipes on fds 1 and 2, drained by one pump thread into ``router``."""

    def __init__(self, router: OutputRouter) -> None:
        self.router = router
        self._saved: dict[int, int] = {}
        self._readers: dict[int, int] = {}
        self._busy = threading.Lock()
        self._stop = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        sel = selectors.DefaultSelector()
        for fd, name in ((1, "stdout"), (2, "stderr")):
            self._saved[fd] = os.dup(fd)
            r, w = os.pipe()
            os.dup2(w, fd)
            os.close(w)
            self._readers[r] = fd
            os.set_blocking(r, False)
            sel.register(
                r, selectors.EVENT_READ, (name, codecs.getincrementaldecoder("utf-8")("replace"))
            )
        self._sel = sel
        self._thread = threading.Thread(target=self._pump, name="alkera-output-pump", daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        while not self._stop:
            events = self._sel.select(timeout=FLUSH_S)
            with self._busy:
                for key, _ in events:
                    name, decoder = key.data
                    try:
                        data = os.read(key.fd, 1 << 16)
                    except BlockingIOError:
                        continue
                    if data:
                        self.router.write(name, decoder.decode(data))
                if self.router.due():
                    self.router.flush()

    def drain(self, timeout: float = 0.5) -> None:
        """Wait until everything written so far to fds 1 and 2 was read and
        routed, then flush. Called before the target changes, so output is
        attributed to the step that wrote it."""
        for stream in (sys.stdout, sys.stderr):
            with contextlib.suppress(Exception):
                stream.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if all(_pending_bytes(r) == 0 for r in self._readers):
                with self._busy:  # the pump finished routing what it read
                    if all(_pending_bytes(r) == 0 for r in self._readers):
                        break
            time.sleep(0.001)
        self.router.flush()


class FdTextStream(io.TextIOBase):
    """``sys.stdout`` / ``sys.stderr`` in the kernel: text written straight
    to the descriptor (no buffering to lose on a crash), except from a thread
    whose cell has finished, which goes to that cell as ``thread.output``."""

    def __init__(
        self,
        fd: int,
        name: str,
        owner: Callable[[], Target | None],
        router: OutputRouter,
        divert: Callable[[str, str], bool] | None = None,
    ) -> None:
        super().__init__()
        self._divert = divert
        self._lock = threading.Lock()
        self._partial: dict[int, str] = {}
        self._fd = fd
        self._name = name
        self._owner = owner
        self._router = router

    @property
    def name(self) -> str:
        return f"<{self._name}>"

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return "utf-8"

    @property
    def errors(self) -> str:  # type: ignore[override]
        return "replace"

    def fileno(self) -> int:
        return self._fd

    def isatty(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    def write(self, s: str) -> int:
        if not isinstance(s, str):
            raise TypeError(f"write() argument must be str, not {type(s).__name__}")
        if self._divert is not None and s and self._divert(self._name, s):
            return len(s)
        target = self._owner()
        if target is not None:
            self._router.write(self._name, s, target)
            return len(s)
        # Line-buffered per thread, so ``print`` from several threads keeps
        # each line whole (it writes the text and the newline separately).
        ident = threading.get_ident()
        with self._lock:
            pending = self._partial.pop(ident, "") + s
            if "\n" not in s and "\r" not in s and len(pending) < 8192:
                self._partial[ident] = pending
                return len(s)
        self._write_fd(pending)
        return len(s)

    def _write_fd(self, text: str) -> None:
        view = memoryview(text.encode("utf-8", "replace"))
        while view:
            try:
                n = os.write(self._fd, view)
            except InterruptedError:
                continue
            view = view[n:]

    def flush(self) -> None:
        with self._lock:
            pending, self._partial = self._partial, {}
        for text in pending.values():
            self._write_fd(text)
