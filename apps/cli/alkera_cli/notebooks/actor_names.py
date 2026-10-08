"""Names the notebook actors after the registered brand.

The notebook package names the system and an unnamed agent through its own
seam (:func:`alkera_notebook.actors.name_actors_with`), since it never imports
the platform. This extension hands it the brand, read each time a label is
built, so the open CLI says "Databench" and "Agent" and a product's brand says
its own words.
"""

from __future__ import annotations

from alkera_core import brand
from alkera_core.extensions import Extension
from alkera_notebook.actors import ActorNames, name_actors_with


def brand_actor_names() -> ActorNames:
    """The system and agent names the registered brand gives."""
    return ActorNames(system=brand.product_name(), agent=brand.agent_name())


def _install() -> None:
    name_actors_with(brand_actor_names)


EXTENSION = Extension(name="notebook_actor_names", install=_install)

__all__ = ["EXTENSION", "brand_actor_names"]
