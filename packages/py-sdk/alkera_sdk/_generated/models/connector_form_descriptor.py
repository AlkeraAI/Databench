from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.ask_groups import AskGroups
    from ..models.form import Form


T = TypeVar("T", bound="ConnectorFormDescriptor")


@_attrs_define
class ConnectorFormDescriptor:
    """One connector's portal-facing form surface, served from the registered
    connector descriptor catalog.

        Attributes:
            name (str):
            title (str):
            form (Form):
            team_capable_methods (list[str] | Unset):
            ask_groups (AskGroups | Unset):
    """

    name: str
    title: str
    form: Form
    team_capable_methods: list[str] | Unset = UNSET
    ask_groups: AskGroups | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        name = self.name

        title = self.title

        form = self.form.to_dict()

        team_capable_methods: list[str] | Unset = UNSET
        if not isinstance(self.team_capable_methods, Unset):
            team_capable_methods = self.team_capable_methods

        ask_groups: dict[str, Any] | Unset = UNSET
        if not isinstance(self.ask_groups, Unset):
            ask_groups = self.ask_groups.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "name": name,
                "title": title,
                "form": form,
            }
        )
        if team_capable_methods is not UNSET:
            field_dict["team_capable_methods"] = team_capable_methods
        if ask_groups is not UNSET:
            field_dict["ask_groups"] = ask_groups

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.ask_groups import AskGroups
        from ..models.form import Form

        d = dict(src_dict)
        name = d.pop("name")

        title = d.pop("title")

        form = Form.from_dict(d.pop("form"))

        team_capable_methods = cast(list[str], d.pop("team_capable_methods", UNSET))

        _ask_groups = d.pop("ask_groups", UNSET)
        ask_groups: AskGroups | Unset
        if isinstance(_ask_groups, Unset):
            ask_groups = UNSET
        else:
            ask_groups = AskGroups.from_dict(_ask_groups)

        connector_form_descriptor = cls(
            name=name,
            title=title,
            form=form,
            team_capable_methods=team_capable_methods,
            ask_groups=ask_groups,
        )

        connector_form_descriptor.additional_properties = d
        return connector_form_descriptor

    @property
    def additional_keys(self) -> list[str]:
        return list(self.additional_properties.keys())

    def __getitem__(self, key: str) -> Any:
        return self.additional_properties[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.additional_properties[key] = value

    def __delitem__(self, key: str) -> None:
        del self.additional_properties[key]

    def __contains__(self, key: str) -> bool:
        return key in self.additional_properties
