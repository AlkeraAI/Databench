from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any, Literal, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.model_provider_test_result_status_type_0 import ModelProviderTestResultStatusType0

T = TypeVar("T", bound="ModelProviderTestResult")


@_attrs_define
class ModelProviderTestResult:
    """
    Attributes:
        ok (bool):
        status (Literal['not_configured'] | ModelProviderTestResultStatusType0):
        detail (None | str):
        tested_at (datetime.datetime):
    """

    ok: bool
    status: Literal["not_configured"] | ModelProviderTestResultStatusType0
    detail: None | str
    tested_at: datetime.datetime
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        ok = self.ok

        status: Literal["not_configured"] | str
        if isinstance(self.status, ModelProviderTestResultStatusType0):
            status = self.status.value
        else:
            status = self.status

        detail: None | str
        detail = self.detail

        tested_at = self.tested_at.isoformat()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "ok": ok,
                "status": status,
                "detail": detail,
                "tested_at": tested_at,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        ok = d.pop("ok")

        def _parse_status(
            data: object,
        ) -> Literal["not_configured"] | ModelProviderTestResultStatusType0:
            try:
                if not isinstance(data, str):
                    raise TypeError()
                status_type_0 = ModelProviderTestResultStatusType0(data)

                return status_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            status_type_1 = cast(Literal["not_configured"], data)
            if status_type_1 != "not_configured":
                raise ValueError(
                    f"status_type_1 must match const 'not_configured', got '{status_type_1}'"
                )
            return status_type_1

        status = _parse_status(d.pop("status"))

        def _parse_detail(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        detail = _parse_detail(d.pop("detail"))

        tested_at = datetime.datetime.fromisoformat(d.pop("tested_at"))

        model_provider_test_result = cls(
            ok=ok,
            status=status,
            detail=detail,
            tested_at=tested_at,
        )

        model_provider_test_result.additional_properties = d
        return model_provider_test_result

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
