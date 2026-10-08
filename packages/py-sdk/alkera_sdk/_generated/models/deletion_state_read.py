from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.deletion_status_read import DeletionStatusRead


T = TypeVar("T", bound="DeletionStateRead")


@_attrs_define
class DeletionStateRead:
    """The live request, or none.

    Attributes:
        request (DeletionStatusRead | None | Unset):
    """

    request: DeletionStatusRead | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.deletion_status_read import DeletionStatusRead

        request: dict[str, Any] | None | Unset
        if isinstance(self.request, Unset):
            request = UNSET
        elif isinstance(self.request, DeletionStatusRead):
            request = self.request.to_dict()
        else:
            request = self.request

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if request is not UNSET:
            field_dict["request"] = request

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.deletion_status_read import DeletionStatusRead

        d = dict(src_dict)

        def _parse_request(data: object) -> DeletionStatusRead | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                request_type_0 = DeletionStatusRead.from_dict(data)

                return request_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(DeletionStatusRead | None | Unset, data)

        request = _parse_request(d.pop("request", UNSET))

        deletion_state_read = cls(
            request=request,
        )

        deletion_state_read.additional_properties = d
        return deletion_state_read

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
