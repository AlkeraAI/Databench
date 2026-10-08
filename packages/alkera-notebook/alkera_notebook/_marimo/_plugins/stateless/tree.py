# Copyright 2026 Marimo. All rights reserved.
# Modified by Alkera: import paths rewritten; see vendor/marimo/README.alkera.md
from __future__ import annotations

from typing import Any

from alkera_notebook._marimo._output.formatters.structures import format_structure
from alkera_notebook._marimo._output.hypertext import Html
from alkera_notebook._marimo._output.rich_help import mddoc
from alkera_notebook._marimo._plugins.core.web_component import JSONType
from alkera_notebook._marimo._plugins.stateless import json_output
from alkera_notebook._marimo._utils.flatten import CyclicStructureError


@mddoc
def tree(
    items: list[Any] | tuple[Any] | dict[Any, Any],
    label: str | None = None,
) -> Html:
    """Render a nested structure of lists, tuples, or dicts as a tree.

    Example:
        ```python3
        mo.tree(
            ["entry", "another entry", {"key": [0, 1, 2]}], label="A tree."
        )
        ```

    Args:
        items: nested structure of lists, tuples, or dicts
        label: optional text label for the tree

    Returns:
        Html: `Html` object
    """
    if not isinstance(items, (list, tuple, dict)):
        raise ValueError(
            "Argument `items` must be a list, tuple, or dict, "
            + f"but got: {type(items)}"
        )

    json_data: JSONType
    try:
        json_data = format_structure(items)
    except CyclicStructureError:
        json_data = str(items)
    return json_output.json_output(
        json_data=json_data,
        name=label,
        value_types="python",
    )
