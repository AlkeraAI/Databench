"""The parts of the org settings read that a domain states.

``GET /org/settings`` answers the stored row plus whatever server-stated facts
the editor needs and cannot guess (the CI gate's always-on rules and every
rule id it knows). Those facts belong to the domain that owns them, so it
registers an :class:`OrgSettingsSection` here and the org settings service
folds every section into the read. With nothing registered each such field
keeps its schema default.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from alkera_core.extensions import ExtensionError, ExtensionPoint
from alkera_core.models import OrgSettings
from alkera_core.schemas.tenancy.org import OrgSettingsRead


@dataclass(frozen=True, slots=True)
class OrgSettingsSection:
    """A domain's fields on the org settings read, stated from the org's row."""

    name: str
    read_fields: Callable[[OrgSettings], Mapping[str, object]]


#: The sections domains register, in registration order.
ORG_SETTINGS_SECTIONS: ExtensionPoint[OrgSettingsSection] = ExtensionPoint("org_settings_sections")


def section_fields(
    row: OrgSettings, point: ExtensionPoint[OrgSettingsSection] = ORG_SETTINGS_SECTIONS
) -> dict[str, object]:
    """Every registered section's fields for ``row``. Refuses a field the read
    does not have (it would be dropped from the response without a word) and a
    field two sections both state (one would silently win)."""
    fields: dict[str, object] = {}
    for section in point.items():
        stated = dict(section.read_fields(row))
        unknown = sorted(stated.keys() - OrgSettingsRead.model_fields.keys())
        if unknown:
            raise ExtensionError(f"{section.name} states fields the read has not: {unknown}")
        taken = sorted(stated.keys() & fields.keys())
        if taken:
            raise ExtensionError(f"{section.name} states fields already stated: {taken}")
        fields.update(stated)
    return fields


__all__ = ["ORG_SETTINGS_SECTIONS", "OrgSettingsSection", "section_fields"]
