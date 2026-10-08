"""The ``alkera.ui`` elements. A cell that reads an element's value re-runs
when someone changes it."""

from __future__ import annotations

import datetime as dt
import math
import typing
from collections.abc import Callable, Iterable, Mapping
from typing import Any, ClassVar

from alkera.ui._model import WidgetModel

Options = typing.Union[typing.Iterable[Any], typing.Mapping[str, Any]]


def _options(options: Options) -> dict[str, Any]:
    """Labels to values: a mapping as given, anything else labelled by ``str``."""
    if isinstance(options, Mapping):
        return {str(k): v for k, v in options.items()}
    out: dict[str, Any] = {}
    for item in options:
        out[str(item)] = item
    return out


def _label_of(choices: dict[str, Any], value: Any) -> str | None:
    if value is None:
        return None
    for label, candidate in choices.items():
        if candidate is value or candidate == value:
            return label
    raise ValueError(f"{value!r} is not one of the options")


def _snap(wire: Any, bounds: tuple[float | None, float | None, float | None]) -> int | float | None:
    """A number clamped to ``[start, stop]`` and snapped to ``step``; ``None``
    for anything that is not a number."""
    if (
        isinstance(wire, bool)
        or not isinstance(wire, (int, float))
        or (isinstance(wire, float) and math.isnan(wire))
    ):
        return None
    start, stop, step = bounds
    number = float(wire)
    if start is not None:
        number = max(number, float(start))
    if stop is not None:
        number = min(number, float(stop))
    if step and start is not None:
        number = float(start) + round((number - float(start)) / float(step)) * float(step)
        if stop is not None:
            number = min(number, float(stop))
    integral = all(isinstance(b, int) or b is None for b in bounds) and number.is_integer()
    return int(number) if integral else round(number, 12)


class _Number(WidgetModel):
    def __init__(
        self,
        *,
        value: float | None,
        start: float | None,
        stop: float | None,
        step: float | None,
        label: str,
        disabled: bool,
    ) -> None:
        self._bounds = (start, stop, step)
        initial = _snap(
            value if value is not None else (start if start is not None else 0), self._bounds
        )
        super().__init__(
            {
                "value": initial,
                "min": start,
                "max": stop,
                "step": step,
                "label": label,
                "disabled": disabled,
            }
        )

    def _coerce_wire(self, wire: Any) -> Any:
        snapped = _snap(wire, self._bounds)
        return self._state.get("value") if snapped is None else snapped


class slider(_Number):  # noqa: N801 - the public spelling is alkera.ui.slider
    """A slider from ``start`` to ``stop``; the value is clamped to the range
    and snapped to ``step``."""

    _view_name: ClassVar[str] = "SliderView"

    def __init__(
        self,
        start: float = 0,
        stop: float = 100,
        step: float = 1,
        value: float | None = None,
        *,
        label: str = "",
        disabled: bool = False,
    ) -> None:
        super().__init__(
            value=value, start=start, stop=stop, step=step, label=label, disabled=disabled
        )


class number(_Number):  # noqa: N801
    """A number entry, optionally bounded."""

    _view_name: ClassVar[str] = "NumberView"

    def __init__(
        self,
        start: float | None = None,
        stop: float | None = None,
        step: float | None = None,
        value: float | None = None,
        *,
        label: str = "",
        disabled: bool = False,
    ) -> None:
        super().__init__(
            value=value, start=start, stop=stop, step=step, label=label, disabled=disabled
        )


class text(WidgetModel):  # noqa: N801
    """A line of text. ``kind="password"`` hides what is typed and keeps the
    value to the person typing it."""

    _view_name: ClassVar[str] = "TextView"

    def __init__(
        self,
        value: str = "",
        *,
        placeholder: str = "",
        kind: str = "text",
        label: str = "",
        disabled: bool = False,
    ) -> None:
        if kind not in ("text", "password"):
            raise ValueError('kind must be "text" or "password"')
        self._sensitive = kind == "password"
        super().__init__(
            {
                "value": str(value),
                "placeholder": placeholder,
                "kind": kind,
                "label": label,
                "disabled": disabled,
            }
        )

    def _coerce_wire(self, wire: Any) -> Any:
        return wire if isinstance(wire, str) else self._state.get("value", "")


class text_area(text):  # noqa: N801
    """Several lines of text."""

    _view_name: ClassVar[str] = "TextAreaView"

    def __init__(
        self,
        value: str = "",
        *,
        placeholder: str = "",
        rows: int = 4,
        label: str = "",
        disabled: bool = False,
    ) -> None:
        super().__init__(value, placeholder=placeholder, label=label, disabled=disabled)
        self._state["rows"] = rows


class checkbox(WidgetModel):  # noqa: N801
    _view_name: ClassVar[str] = "CheckboxView"

    def __init__(self, value: bool = False, *, label: str = "", disabled: bool = False) -> None:
        super().__init__({"value": bool(value), "label": label, "disabled": disabled})

    def _coerce_wire(self, wire: Any) -> Any:
        return wire if isinstance(wire, bool) else self._state.get("value", False)


class switch(checkbox):  # noqa: N801
    _view_name: ClassVar[str] = "SwitchView"


class _Choice(WidgetModel):
    def __init__(self, options: Options, state: dict[str, Any]) -> None:
        self._choices = _options(options)
        super().__init__({**state, "options": list(self._choices)})


class dropdown(_Choice):  # noqa: N801
    """One of ``options`` (a list, or a mapping of labels to values)."""

    _view_name: ClassVar[str] = "DropdownView"

    def __init__(
        self,
        options: Options,
        value: Any = None,
        *,
        allow_select_none: bool = False,
        label: str = "",
        disabled: bool = False,
    ) -> None:
        choices = _options(options)
        chosen = _label_of(choices, value)
        if chosen is None and not allow_select_none and choices:
            chosen = next(iter(choices))
        super().__init__(
            choices,
            {
                "value": chosen,
                "allow_select_none": allow_select_none,
                "label": label,
                "disabled": disabled,
            },
        )

    def _to_wire(self, value: Any) -> Any:
        return _label_of(self._choices, value)

    def _from_wire(self, wire: Any) -> Any:
        return self._choices.get(wire) if wire is not None else None

    def _coerce_wire(self, wire: Any) -> Any:
        if wire is None and self._state.get("allow_select_none"):
            return None
        return wire if wire in self._choices else self._state.get("value")


class radio(dropdown):  # noqa: N801
    _view_name: ClassVar[str] = "RadioView"


class multiselect(_Choice):  # noqa: N801
    """Any of ``options``; the value is a list."""

    _view_name: ClassVar[str] = "MultiselectView"

    def __init__(
        self,
        options: Options,
        value: Iterable[Any] = (),
        *,
        label: str = "",
        disabled: bool = False,
    ) -> None:
        choices = _options(options)
        labels = [_label_of(choices, v) for v in value]
        super().__init__(choices, {"value": labels, "label": label, "disabled": disabled})

    def _to_wire(self, value: Any) -> Any:
        return [_label_of(self._choices, v) for v in value]

    def _from_wire(self, wire: Any) -> Any:
        return [self._choices[w] for w in wire or [] if w in self._choices]

    def _coerce_wire(self, wire: Any) -> Any:
        if not isinstance(wire, list):
            return self._state.get("value", [])
        # Option order, each once.
        return [label for label in self._choices if label in wire]


class date(WidgetModel):  # noqa: N801
    """A calendar date, optionally bounded."""

    _view_name: ClassVar[str] = "DateView"

    def __init__(
        self,
        value: dt.date | None = None,
        *,
        start: dt.date | None = None,
        stop: dt.date | None = None,
        label: str = "",
        disabled: bool = False,
    ) -> None:
        self._start, self._stop = start, stop
        super().__init__(
            {
                "value": value.isoformat() if value else None,
                "min": start.isoformat() if start else None,
                "max": stop.isoformat() if stop else None,
                "label": label,
                "disabled": disabled,
            }
        )

    def _to_wire(self, value: Any) -> Any:
        return value.isoformat() if isinstance(value, dt.date) else None

    def _from_wire(self, wire: Any) -> Any:
        return dt.date.fromisoformat(wire) if isinstance(wire, str) else None

    def _coerce_wire(self, wire: Any) -> Any:
        if wire is None:
            return None
        try:
            day = dt.date.fromisoformat(str(wire))
        except ValueError:
            return self._state.get("value")
        if self._start and day < self._start:
            day = self._start
        if self._stop and day > self._stop:
            day = self._stop
        return day.isoformat()


class button(WidgetModel):  # noqa: N801
    """A button. Its value counts clicks; ``on_click`` runs on each one."""

    _view_name: ClassVar[str] = "ButtonView"

    def __init__(
        self,
        label: str = "Click",
        *,
        on_click: Callable[[button], Any] | None = None,
        disabled: bool = False,
    ) -> None:
        super().__init__({"value": 0, "label": label, "disabled": disabled})
        if on_click is not None:
            self.on_change(lambda _count: on_click(self))

    def _coerce_wire(self, wire: Any) -> Any:
        current = int(self._state.get("value") or 0)
        # One click at a time: a frontend may only add one.
        return (
            current + 1
            if isinstance(wire, int) and not isinstance(wire, bool) and wire > current
            else current
        )


class run_button(button):  # noqa: N801
    """A button that runs the cells reading it; nothing runs until it is clicked."""

    _view_name: ClassVar[str] = "RunButtonView"

    def __init__(self, label: str = "Run", *, disabled: bool = False) -> None:
        super().__init__(label, disabled=disabled)
