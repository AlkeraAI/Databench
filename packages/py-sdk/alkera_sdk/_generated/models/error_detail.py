from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.error_detail_details_type_0 import ErrorDetailDetailsType0


T = TypeVar("T", bound="ErrorDetail")


@_attrs_define
class ErrorDetail:
    """One problem, as the ``error`` member of :class:`ErrorEnvelope` carries it.

    Attributes:
        type_ (str): A stable URI naming the kind of problem, one per code.
        code (str): The machine-readable code a client branches on.
        status (int): The HTTP status the response carried.
        message (str): A sentence written for the person who asked.
        trace_id (str): The request's trace id, for support.
        details (ErrorDetailDetailsType0 | None | Unset): Ids and enums that explain this code; absent when empty.
    """

    type_: str
    code: str
    status: int
    message: str
    trace_id: str
    details: ErrorDetailDetailsType0 | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.error_detail_details_type_0 import ErrorDetailDetailsType0

        type_ = self.type_

        code = self.code

        status = self.status

        message = self.message

        trace_id = self.trace_id

        details: dict[str, Any] | None | Unset
        if isinstance(self.details, Unset):
            details = UNSET
        elif isinstance(self.details, ErrorDetailDetailsType0):
            details = self.details.to_dict()
        else:
            details = self.details

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "type": type_,
                "code": code,
                "status": status,
                "message": message,
                "trace_id": trace_id,
            }
        )
        if details is not UNSET:
            field_dict["details"] = details

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.error_detail_details_type_0 import ErrorDetailDetailsType0

        d = dict(src_dict)
        type_ = d.pop("type")

        code = d.pop("code")

        status = d.pop("status")

        message = d.pop("message")

        trace_id = d.pop("trace_id")

        def _parse_details(data: object) -> ErrorDetailDetailsType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                details_type_0 = ErrorDetailDetailsType0.from_dict(data)

                return details_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(ErrorDetailDetailsType0 | None | Unset, data)

        details = _parse_details(d.pop("details", UNSET))

        error_detail = cls(
            type_=type_,
            code=code,
            status=status,
            message=message,
            trace_id=trace_id,
            details=details,
        )

        error_detail.additional_properties = d
        return error_detail

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
