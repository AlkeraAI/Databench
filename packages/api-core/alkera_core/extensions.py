"""Extension points: how private code extends the open platform by registration.

Open code never imports private code. Where the open platform needs behaviour
that only a private distribution supplies (a data plugin, a Slack router), it
declares an :class:`ExtensionPoint` and reads it; the private distribution ships
an :class:`Extension` whose ``install`` registers into the points it extends.

Composition is explicit. A product's entry point (its backend app, its CLI)
passes its list of extensions to
:func:`install_extensions` before it hands off to the open app. There is no
entry-point scanning: the compiled ``alkera`` binary cannot scan distribution
metadata, so a list a build follows statically is the only discovery that works
in every shape the product ships in. With nothing installed, every point is
empty and the open platform runs on its own defaults.

A point freezes the first time it is read. Registering after that raises
instead of leaving a reader that already ran without the late item, so every
installation has to happen during composition, before the app starts.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


class ExtensionError(RuntimeError):
    """An extension or extension point was used in a way composition forbids."""


class ExtensionPoint(Generic[T]):
    """One seam: an ordered set of items the open platform reads and private
    extensions register into."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._items: list[T] = []
        self._frozen = False

    def __repr__(self) -> str:
        return f"ExtensionPoint({self.name!r}, items={len(self._items)}, frozen={self._frozen})"

    @property
    def frozen(self) -> bool:
        return self._frozen

    def register(self, item: T) -> None:
        """Add ``item`` after everything registered before it."""
        if self._frozen:
            raise ExtensionError(
                f"extension point {self.name!r} was already read; register during "
                "composition, before the app starts"
            )
        if any(existing == item for existing in self._items):
            raise ExtensionError(f"{item!r} is already registered on {self.name!r}")
        self._items.append(item)

    def items(self) -> tuple[T, ...]:
        """Everything registered, in registration order. Freezes the point."""
        self._frozen = True
        return tuple(self._items)


@dataclass(frozen=True, slots=True)
class Extension:
    """A private distribution's contribution: ``install`` registers into the
    extension points it extends. ``name`` identifies it across installs."""

    name: str
    install: Callable[[], None]


_installed: dict[str, Extension] = {}


def install_extensions(extensions: Iterable[Extension]) -> None:
    """Install each extension once, in order.

    Installing the same extension again is a no-op, so a composition root that
    builds its app more than once (a test suite) can call this every time. A
    different extension under a name already installed raises."""
    for extension in extensions:
        current = _installed.get(extension.name)
        if current is extension:
            continue
        if current is not None:
            raise ExtensionError(f"a different extension named {extension.name!r} is installed")
        extension.install()
        _installed[extension.name] = extension


def installed_extensions() -> tuple[str, ...]:
    """The names of every installed extension, in installation order."""
    return tuple(_installed)


__all__ = [
    "Extension",
    "ExtensionError",
    "ExtensionPoint",
    "install_extensions",
    "installed_extensions",
]
