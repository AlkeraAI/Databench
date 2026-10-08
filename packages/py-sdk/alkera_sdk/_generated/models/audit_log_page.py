from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.audit_log_read import AuditLogRead


T = TypeVar("T", bound="AuditLogPage")


@_attrs_define
class AuditLogPage:
    """One page of audit entries, newest-first.

    Attributes:
        items (list[AuditLogRead]):
        total (int):
        page (int):
        page_size (int):
        actions (list[str] | Unset): Every action the log holds (unfiltered), for the action filter.
    """

    items: list[AuditLogRead]
    total: int
    page: int
    page_size: int
    actions: list[str] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        items = []
        for items_item_data in self.items:
            items_item = items_item_data.to_dict()
            items.append(items_item)

        total = self.total

        page = self.page

        page_size = self.page_size

        actions: list[str] | Unset = UNSET
        if not isinstance(self.actions, Unset):
            actions = self.actions

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "items": items,
                "total": total,
                "page": page,
                "page_size": page_size,
            }
        )
        if actions is not UNSET:
            field_dict["actions"] = actions

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.audit_log_read import AuditLogRead

        d = dict(src_dict)
        items = []
        _items = d.pop("items")
        for items_item_data in _items:
            items_item = AuditLogRead.from_dict(items_item_data)

            items.append(items_item)

        total = d.pop("total")

        page = d.pop("page")

        page_size = d.pop("page_size")

        actions = cast(list[str], d.pop("actions", UNSET))

        audit_log_page = cls(
            items=items,
            total=total,
            page=page,
            page_size=page_size,
            actions=actions,
        )

        audit_log_page.additional_properties = d
        return audit_log_page

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
