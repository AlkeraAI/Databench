"""The extension-point mechanism private code uses to extend the open platform."""

from __future__ import annotations

import itertools

import pytest
from alkera_core.extensions import (
    Extension,
    ExtensionError,
    ExtensionPoint,
    install_extensions,
    installed_extensions,
)

_names = itertools.count()


def _unique(prefix: str) -> str:
    """Installed names are process-wide, so every test installs under its own."""
    return f"test.{prefix}.{next(_names)}"


def _registering(point: ExtensionPoint[str], *items: str, name: str | None = None) -> Extension:
    def install() -> None:
        for item in items:
            point.register(item)

    return Extension(name=name or _unique("ext"), install=install)


def test_an_unread_point_is_empty_and_open() -> None:
    point: ExtensionPoint[str] = ExtensionPoint("p")
    assert not point.frozen
    assert point.items() == ()


def test_items_come_back_in_registration_order() -> None:
    point: ExtensionPoint[str] = ExtensionPoint("p")
    for item in ("b", "a", "c"):
        point.register(item)
    assert point.items() == ("b", "a", "c")


def test_reading_a_point_freezes_it_and_a_late_registration_raises() -> None:
    point: ExtensionPoint[str] = ExtensionPoint("routers")
    point.register("early")
    assert point.items() == ("early",)
    assert point.frozen
    with pytest.raises(ExtensionError, match="'routers' was already read"):
        point.register("late")
    assert point.items() == ("early",)


def test_the_same_item_cannot_be_registered_twice() -> None:
    point: ExtensionPoint[str] = ExtensionPoint("p")
    point.register("x")
    with pytest.raises(ExtensionError, match="already registered"):
        point.register("x")
    assert point.items() == ("x",)


def test_install_runs_each_extension_in_order() -> None:
    point: ExtensionPoint[str] = ExtensionPoint("p")
    first = _registering(point, "a1", "a2")
    second = _registering(point, "b1")
    install_extensions([first, second])
    assert point.items() == ("a1", "a2", "b1")
    assert installed_extensions()[-2:] == (first.name, second.name)


def test_installing_the_same_extension_again_is_a_no_op() -> None:
    point: ExtensionPoint[str] = ExtensionPoint("p")
    extension = _registering(point, "only")
    install_extensions([extension])
    install_extensions([extension])
    assert point.items() == ("only",)
    assert installed_extensions().count(extension.name) == 1


def test_a_different_extension_under_an_installed_name_raises_without_installing() -> None:
    point: ExtensionPoint[str] = ExtensionPoint("p")
    name = _unique("clash")
    install_extensions([_registering(point, "original", name=name)])
    impostor = _registering(point, "impostor", name=name)
    with pytest.raises(ExtensionError, match="different extension"):
        install_extensions([impostor])
    assert point.items() == ("original",)


def test_installing_nothing_installs_nothing() -> None:
    before = installed_extensions()
    install_extensions([])
    assert installed_extensions() == before
