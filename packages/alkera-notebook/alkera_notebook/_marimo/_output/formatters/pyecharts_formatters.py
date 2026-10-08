# Copyright 2026 Marimo. All rights reserved.
# Modified by Alkera: import paths rewritten; see vendor/marimo/README.alkera.md
from __future__ import annotations

from alkera_notebook._marimo._messaging.mimetypes import KnownMimeType
from alkera_notebook._marimo._output.builder import h
from alkera_notebook._marimo._output.formatters.formatter_factory import FormatterFactory
from alkera_notebook._marimo._output.formatters.utils import src_or_src_doc
from alkera_notebook._marimo._output.utils import flatten_string


class PyechartsFormatter(FormatterFactory):
    @staticmethod
    def package_name() -> str:
        return "pyecharts"

    def register(self) -> None:
        from pyecharts.charts.base import (  # type: ignore[import-not-found,import-untyped,unused-ignore]
            Base,
        )

        from alkera_notebook._marimo._output import formatting

        @formatting.formatter(Base)
        def _show(chart: Base) -> tuple[KnownMimeType, str]:
            html_content = chart.render_embed()
            return (
                "text/html",
                flatten_string(
                    h.iframe(
                        **src_or_src_doc(html_content),
                        onload="__resizeIframe(this)",
                        style="width: 100%",
                    )
                ),
            )
