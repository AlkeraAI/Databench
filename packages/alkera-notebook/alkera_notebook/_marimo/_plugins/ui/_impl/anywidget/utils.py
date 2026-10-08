# Copyright 2026 Marimo. All rights reserved.
# Modified by Alkera: import paths rewritten; see vendor/marimo/README.alkera.md
from __future__ import annotations

from alkera_notebook._marimo._dependencies.dependencies import DependencyManager
from alkera_notebook._marimo._plugins.ui._impl.anywidget.types import (
    BufferPaths,
    WidgetModelState,
    WidgetModelStateWithoutBuffers,
)


def extract_buffer_paths(
    message: WidgetModelState,
) -> tuple[WidgetModelStateWithoutBuffers, BufferPaths, list[bytes]]:
    """
    Extract buffer paths from a message.
    """
    DependencyManager.ipywidgets.require("for anywidget support.")
    import ipywidgets  # type: ignore

    _remove_buffers = ipywidgets.widgets.widget._remove_buffers  # type: ignore

    # Get the initial state of the widget
    state, buffer_paths, buffers = _remove_buffers(message)  # type: ignore

    return state, buffer_paths, buffers  # type: ignore
