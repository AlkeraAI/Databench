# Copyright 2026 Marimo. All rights reserved.
# Modified by Alkera: import paths rewritten; see vendor/marimo/README.alkera.md
from __future__ import annotations

from typing import Any

from alkera_notebook._marimo._messaging.mimetypes import KnownMimeType
from alkera_notebook._marimo._output.formatters.formatter_factory import FormatterFactory
from alkera_notebook._marimo._plugins.ui._impl.from_panel import panel as from_panel


class PanelFormatter(FormatterFactory):
    @staticmethod
    def package_name() -> str:
        return "panel"

    def register(self) -> None:
        import panel  # type: ignore
        import param  # type: ignore

        from alkera_notebook._marimo._output import formatting

        @formatting.formatter(param.reactive.rx)
        @formatting.formatter(panel.viewable.Viewable)
        @formatting.formatter(panel.viewable.Viewer)
        def _from(lmap: Any) -> tuple[KnownMimeType, str]:
            return from_panel(lmap)._mime_()
