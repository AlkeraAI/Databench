from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.chat_promote_request_chart_spec_type_0 import ChatPromoteRequestChartSpecType0
    from ..models.promote_column import PromoteColumn


T = TypeVar("T", bound="ChatPromoteRequest")


@_attrs_define
class ChatPromoteRequest:
    """Pin a result the chat produced: the cloud creates the object now and the
    daemon uploads the payload behind it.

        Attributes:
            event_id (str):
            title (str):
            columns (list[PromoteColumn] | None | Unset):
            chart_spec (ChatPromoteRequestChartSpecType0 | None | Unset):
    """

    event_id: str
    title: str
    columns: list[PromoteColumn] | None | Unset = UNSET
    chart_spec: ChatPromoteRequestChartSpecType0 | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.chat_promote_request_chart_spec_type_0 import (
            ChatPromoteRequestChartSpecType0,
        )

        event_id = self.event_id

        title = self.title

        columns: list[dict[str, Any]] | None | Unset
        if isinstance(self.columns, Unset):
            columns = UNSET
        elif isinstance(self.columns, list):
            columns = []
            for columns_type_0_item_data in self.columns:
                columns_type_0_item = columns_type_0_item_data.to_dict()
                columns.append(columns_type_0_item)

        else:
            columns = self.columns

        chart_spec: dict[str, Any] | None | Unset
        if isinstance(self.chart_spec, Unset):
            chart_spec = UNSET
        elif isinstance(self.chart_spec, ChatPromoteRequestChartSpecType0):
            chart_spec = self.chart_spec.to_dict()
        else:
            chart_spec = self.chart_spec

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "event_id": event_id,
                "title": title,
            }
        )
        if columns is not UNSET:
            field_dict["columns"] = columns
        if chart_spec is not UNSET:
            field_dict["chart_spec"] = chart_spec

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.chat_promote_request_chart_spec_type_0 import (
            ChatPromoteRequestChartSpecType0,
        )
        from ..models.promote_column import PromoteColumn

        d = dict(src_dict)
        event_id = d.pop("event_id")

        title = d.pop("title")

        def _parse_columns(data: object) -> list[PromoteColumn] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                columns_type_0 = []
                _columns_type_0 = data
                for columns_type_0_item_data in _columns_type_0:
                    columns_type_0_item = PromoteColumn.from_dict(columns_type_0_item_data)

                    columns_type_0.append(columns_type_0_item)

                return columns_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[PromoteColumn] | None | Unset, data)

        columns = _parse_columns(d.pop("columns", UNSET))

        def _parse_chart_spec(data: object) -> ChatPromoteRequestChartSpecType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                chart_spec_type_0 = ChatPromoteRequestChartSpecType0.from_dict(data)

                return chart_spec_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(ChatPromoteRequestChartSpecType0 | None | Unset, data)

        chart_spec = _parse_chart_spec(d.pop("chart_spec", UNSET))

        chat_promote_request = cls(
            event_id=event_id,
            title=title,
            columns=columns,
            chart_spec=chart_spec,
        )

        chat_promote_request.additional_properties = d
        return chat_promote_request

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
