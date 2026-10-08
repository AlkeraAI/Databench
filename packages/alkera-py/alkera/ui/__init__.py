"""``alkera.ui``: inputs in a notebook's output. A cell that reads an
element's ``.value`` re-runs when someone changes it.

Each element is a Jupyter widget (module ``@alkera/ui-widgets``) on the
host's comms, with no ipywidgets dependency. Outside a notebook an element
keeps its value and prints as text.
"""

from __future__ import annotations

from alkera.ui._elements import (
    button,
    checkbox,
    date,
    dropdown,
    multiselect,
    number,
    radio,
    run_button,
    slider,
    switch,
    text,
    text_area,
)
from alkera.ui._model import WidgetModel

__all__ = [
    "WidgetModel",
    "button",
    "checkbox",
    "date",
    "dropdown",
    "multiselect",
    "number",
    "radio",
    "run_button",
    "slider",
    "switch",
    "text",
    "text_area",
]
