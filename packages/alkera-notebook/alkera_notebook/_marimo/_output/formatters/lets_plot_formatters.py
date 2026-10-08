# Copyright 2026 Marimo. All rights reserved.
# Modified by Alkera: import paths rewritten; see vendor/marimo/README.alkera.md
from __future__ import annotations

from alkera_notebook._marimo._messaging.mimetypes import KnownMimeType
from alkera_notebook._marimo._output.formatters.formatter_factory import FormatterFactory


class LetsPlotFormatter(FormatterFactory):
    @staticmethod
    def package_name() -> str:
        return "lets_plot"

    def register(self) -> None:
        import lets_plot.plot.core  # type: ignore[import-not-found,import-untyped,unused-ignore]
        import lets_plot.plot.subplots  # type: ignore[import-not-found,import-untyped,unused-ignore]

        from alkera_notebook._marimo._output import formatting

        @formatting.formatter(lets_plot.plot.core.PlotSpec)
        def _html_from_plot_spec(
            fig: lets_plot.plot.core.PlotSpec,
        ) -> tuple[KnownMimeType, str]:
            html_str: str = fig.to_html(iframe=True)
            return ("text/html", html_str)

        @formatting.formatter(lets_plot.plot.subplots.SupPlotsSpec)
        def _html_from_subplot_spec(
            fig: lets_plot.plot.subplots.SupPlotsSpec,
        ) -> tuple[KnownMimeType, str]:
            html_str: str = fig.to_html(iframe=True)
            return ("text/html", html_str)
