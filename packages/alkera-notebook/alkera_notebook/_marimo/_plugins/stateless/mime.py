# Copyright 2026 Marimo. All rights reserved.
# Modified by Alkera: import paths rewritten; see vendor/marimo/README.alkera.md
from __future__ import annotations

from alkera_notebook._marimo._output.hypertext import Html
from alkera_notebook._marimo._plugins.core.web_component import JSONType, build_stateless_plugin


def mime_renderer(mime: str, data: JSONType) -> Html:
    """Build a mime renderer element.

    Args:
    -----
    mime: MIME type of the data
    data: data to display

    Returns:
    --------
    A string of HTML for a mime renderer element.
    """
    return Html(
        build_stateless_plugin(
            component_name="marimo-mime-renderer",
            args=({"mime": mime, "data": data}),
        )
    )
