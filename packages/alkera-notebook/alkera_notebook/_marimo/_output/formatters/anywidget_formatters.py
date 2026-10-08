# Copyright 2026 Marimo. All rights reserved.
# Modified by Alkera: import paths rewritten; see vendor/marimo/README.alkera.md
from __future__ import annotations

from alkera_notebook._marimo._messaging.mimetypes import KnownMimeType
from alkera_notebook._marimo._output.formatters.formatter_factory import FormatterFactory
from alkera_notebook._marimo._plugins.ui._impl.from_anywidget import from_anywidget


class AnyWidgetFormatter(FormatterFactory):
    @staticmethod
    def package_name() -> str:
        return "anywidget"

    def register(self) -> None:
        import anywidget  # type: ignore [import-not-found]

        from alkera_notebook._marimo._output import formatting

        @formatting.formatter(anywidget.AnyWidget)
        def _from(lmap: anywidget.AnyWidget) -> tuple[KnownMimeType, str]:
            return from_anywidget(lmap)._mime_()
