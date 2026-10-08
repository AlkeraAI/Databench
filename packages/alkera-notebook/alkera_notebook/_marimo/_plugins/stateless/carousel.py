# Copyright 2026 Marimo. All rights reserved.
# Modified by Alkera: import paths rewritten; see vendor/marimo/README.alkera.md
from __future__ import annotations

from typing import TYPE_CHECKING

from alkera_notebook._marimo._output.formatting import as_html
from alkera_notebook._marimo._output.hypertext import ContainerHtml
from alkera_notebook._marimo._output.md import md
from alkera_notebook._marimo._output.rich_help import mddoc
from alkera_notebook._marimo._plugins.core.web_component import build_stateless_plugin

if TYPE_CHECKING:
    from collections.abc import Sequence


@mddoc
class carousel(ContainerHtml):
    """An `Html` object representing a carousel of items.

    Args:
        items: A list of items.

    Example:
        ```python3
        mo.carousel([mo.md("..."), mo.ui.text_area()])
        ```
    """

    def __init__(self, items: Sequence[object]) -> None:
        super().__init__(
            [
                md(item) if isinstance(item, str) else as_html(item)
                for item in items
            ]
        )

    def _build_text(self) -> str:
        return build_stateless_plugin(
            component_name="marimo-carousel",
            args={},
            slotted_html="".join(c.text for c in self._children),
        )
