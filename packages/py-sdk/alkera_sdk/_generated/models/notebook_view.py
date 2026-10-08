from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.cell_notice import CellNotice
    from ..models.cell_state import CellState
    from ..models.kernel_info import KernelInfo
    from ..models.presence import Presence
    from ..models.settings import Settings


T = TypeVar("T", bound="NotebookView")


@_attrs_define
class NotebookView:
    """A notebook as a reader sees it: its document, its kernel and who is
    where. The one definition: the engine's client, the agent tools, the
    platform's notebook route and the web client (through the generated API
    types) all use this shape.

        Attributes:
            path (str):
            token (str):
            settings (Settings):
            kernel (KernelInfo):
            cells (list[CellState]):
            presence (list[Presence] | Unset):
            read_only_reason (None | str | Unset):
            notices (list[CellNotice] | Unset):
            output_frame_url (None | str | Unset):
    """

    path: str
    token: str
    settings: Settings
    kernel: KernelInfo
    cells: list[CellState]
    presence: list[Presence] | Unset = UNSET
    read_only_reason: None | str | Unset = UNSET
    notices: list[CellNotice] | Unset = UNSET
    output_frame_url: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        path = self.path

        token = self.token

        settings = self.settings.to_dict()

        kernel = self.kernel.to_dict()

        cells = []
        for cells_item_data in self.cells:
            cells_item = cells_item_data.to_dict()
            cells.append(cells_item)

        presence: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.presence, Unset):
            presence = []
            for presence_item_data in self.presence:
                presence_item = presence_item_data.to_dict()
                presence.append(presence_item)

        read_only_reason: None | str | Unset
        if isinstance(self.read_only_reason, Unset):
            read_only_reason = UNSET
        else:
            read_only_reason = self.read_only_reason

        notices: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.notices, Unset):
            notices = []
            for notices_item_data in self.notices:
                notices_item = notices_item_data.to_dict()
                notices.append(notices_item)

        output_frame_url: None | str | Unset
        if isinstance(self.output_frame_url, Unset):
            output_frame_url = UNSET
        else:
            output_frame_url = self.output_frame_url

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "path": path,
                "token": token,
                "settings": settings,
                "kernel": kernel,
                "cells": cells,
            }
        )
        if presence is not UNSET:
            field_dict["presence"] = presence
        if read_only_reason is not UNSET:
            field_dict["read_only_reason"] = read_only_reason
        if notices is not UNSET:
            field_dict["notices"] = notices
        if output_frame_url is not UNSET:
            field_dict["output_frame_url"] = output_frame_url

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.cell_notice import CellNotice
        from ..models.cell_state import CellState
        from ..models.kernel_info import KernelInfo
        from ..models.presence import Presence
        from ..models.settings import Settings

        d = dict(src_dict)
        path = d.pop("path")

        token = d.pop("token")

        settings = Settings.from_dict(d.pop("settings"))

        kernel = KernelInfo.from_dict(d.pop("kernel"))

        cells = []
        _cells = d.pop("cells")
        for cells_item_data in _cells:
            cells_item = CellState.from_dict(cells_item_data)

            cells.append(cells_item)

        _presence = d.pop("presence", UNSET)
        presence: list[Presence] | Unset = UNSET
        if _presence is not UNSET:
            presence = []
            for presence_item_data in _presence:
                presence_item = Presence.from_dict(presence_item_data)

                presence.append(presence_item)

        def _parse_read_only_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        read_only_reason = _parse_read_only_reason(d.pop("read_only_reason", UNSET))

        _notices = d.pop("notices", UNSET)
        notices: list[CellNotice] | Unset = UNSET
        if _notices is not UNSET:
            notices = []
            for notices_item_data in _notices:
                notices_item = CellNotice.from_dict(notices_item_data)

                notices.append(notices_item)

        def _parse_output_frame_url(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        output_frame_url = _parse_output_frame_url(d.pop("output_frame_url", UNSET))

        notebook_view = cls(
            path=path,
            token=token,
            settings=settings,
            kernel=kernel,
            cells=cells,
            presence=presence,
            read_only_reason=read_only_reason,
            notices=notices,
            output_frame_url=output_frame_url,
        )

        notebook_view.additional_properties = d
        return notebook_view

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
