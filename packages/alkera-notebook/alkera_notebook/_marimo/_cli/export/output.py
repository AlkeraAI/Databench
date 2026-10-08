# Copyright 2026 Marimo. All rights reserved.
# Modified by Alkera: import paths rewritten; see vendor/marimo/README.alkera.md
from __future__ import annotations

from io import TextIOBase

from alkera_notebook._marimo._cli.print import echo


class CLIExportWriter(TextIOBase):
    def __init__(self, *, err: bool) -> None:
        self._err = err

    def write(self, value: str) -> int:
        echo(value, err=self._err, nl=False)
        return len(value)


STDOUT = CLIExportWriter(err=False)
STDERR = CLIExportWriter(err=True)
