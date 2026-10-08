"""A console stream folded the way a terminal (and Jupyter) shows it.

The kernel only ever appends to a stream; a progress bar that redraws itself
does so with carriage returns, cursor-up and erase-line sequences, and those
can be split over any number of batches. :class:`ConsoleScreen` keeps the
lines and the cursor between batches, so the stored text is what a reader
would see: a bar drawn over ten thousand batches ends as its final line.

Handled: ``\\r`` (back to the start of the line; later text overwrites),
``\\n`` (start of the next line), ``\\b`` (one cell left), ``ESC[<n>A``
(cursor up ``n`` lines, default 1), ``ESC[K``/``ESC[0K`` (erase to the end of
the line), ``ESC[1K`` (erase to the cursor) and ``ESC[2K`` (erase the line).
Any other escape sequence (colours, say) is kept as written and occupies one
cell, so overwriting never splits it.
"""

from __future__ import annotations

import re

# A whole CSI sequence, a control the screen acts on, a plain run, or a lone ESC.
_TOKEN = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|[\r\n\b]|[^\x1b\r\n\b]+|\x1b", re.DOTALL)
_CSI = re.compile(r"\x1b\[([0-9;?]*)[ -/]*([@-~])")
# An escape sequence the batch boundary cut short.
_UNFINISHED = re.compile(r"\x1b(?:\[[0-9;?]*[ -/]*)?\Z")
_CELLS = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|.", re.DOTALL)


def _cells(line: str) -> list[str]:
    return _CELLS.findall(line) if "\x1b" in line else list(line)


def _width(line: str) -> int:
    return len(_CELLS.findall(line)) if "\x1b" in line else len(line)


class ConsoleScreen:
    """The lines of one stream and the cursor, fed batch by batch.

    Lines are kept as strings; appending at the end of the cursor's line (the
    common case) is a string append, and only an overwrite splits a line into
    cells."""

    def __init__(self) -> None:
        self._lines: list[str] = [""]
        self._row = 0
        self._col = 0
        self._row_width: int | None = 0  # cells in the cursor's line, when known
        self._pending = ""

    @classmethod
    def from_text(cls, text: str) -> ConsoleScreen:
        screen = cls()
        screen.feed(text)
        return screen

    # -- feeding -------------------------------------------------------------

    def feed(self, text: str) -> None:
        """Apply one batch of appended stream text."""
        text = self._pending + text
        self._pending = ""
        cut = _UNFINISHED.search(text)
        if cut is not None:
            self._pending = text[cut.start() :]
            text = text[: cut.start()]
        for piece in _TOKEN.findall(text):
            if piece == "\n":
                self._goto(self._row + 1)
                self._col = 0
            elif piece == "\r":
                self._col = 0
            elif piece == "\b":
                self._col = max(0, self._col - 1)
            elif piece.startswith("\x1b[") and self._control(piece):
                continue
            elif piece.startswith("\x1b"):
                self._write(piece, 1)
            else:
                self._write(piece, len(piece))

    def _control(self, piece: str) -> bool:
        """Act on a cursor or erase sequence; False for any other sequence."""
        m = _CSI.fullmatch(piece)
        if m is None:
            return False
        params, final = m.group(1), m.group(2)
        if final == "A" and (params == "" or params.isdigit()):
            self._goto(max(0, self._row - max(1, int(params or "1"))))
            return True
        if final == "K" and params in ("", "0", "1", "2"):
            if params == "2":
                self._set_line("", 0)
            elif params == "1":
                cells = _cells(self._lines[self._row])
                end = min(self._col + 1, len(cells))
                cells[:end] = [" "] * end
                self._set_line("".join(cells), len(cells))
            elif self._col < self._width():
                cells = _cells(self._lines[self._row])
                del cells[self._col :]
                self._set_line("".join(cells), len(cells))
            return True
        return False

    def _goto(self, row: int) -> None:
        if row >= len(self._lines):
            self._lines.extend([""] * (row - len(self._lines) + 1))
        if row != self._row:
            self._row = row
            self._row_width = None

    def _width(self) -> int:
        if self._row_width is None:
            self._row_width = _width(self._lines[self._row])
        return self._row_width

    def _set_line(self, line: str, width: int) -> None:
        self._lines[self._row] = line
        self._row_width = width

    def _write(self, piece: str, n: int) -> None:
        width = self._width()
        line = self._lines[self._row]
        if self._col >= width:
            pad = " " * (self._col - width)
            self._set_line(line + pad + piece, self._col + n)
        else:
            cells = _cells(line)
            cells[self._col : self._col + n] = [piece] if piece.startswith("\x1b") else list(piece)
            self._set_line("".join(cells), len(cells))
        self._col += n

    def drop_head(self, chars: int) -> None:
        """Forget the first ``chars`` characters of :meth:`text` (a cap kept
        only the end). The cursor stays where it was relative to the text
        that remains."""
        if chars <= 0:
            return
        gone = 0
        while gone < len(self._lines) - 1 and chars > len(self._lines[gone]):
            chars -= len(self._lines[gone]) + 1
            gone += 1
        if gone:
            del self._lines[:gone]
            self._row = max(0, self._row - gone)
            self._row_width = None
        if chars > 0:
            first = self._lines[0]
            if self._row == 0:
                self._col = max(0, self._col - (self._width() - _width(first[chars:])))
            self._lines[0] = first[chars:]
            if self._row == 0:
                self._row_width = None

    # -- reading -------------------------------------------------------------

    def text(self) -> str:
        """What the stream shows now."""
        return "\n".join(self._lines)


def collapse(text: str) -> str:
    """``text`` as a terminal would show it (one batch, from an empty screen)."""
    return ConsoleScreen.from_text(text).text()
