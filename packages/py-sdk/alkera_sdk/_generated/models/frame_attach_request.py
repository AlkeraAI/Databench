from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="FrameAttachRequest")


@_attrs_define
class FrameAttachRequest:
    """``POST .../frames``: attach an output frame at the engine's widget hub.
    ``peer_id`` (the socket's id from its ``welcome``) narrows the frame's
    events to that socket; without it they reach every socket of the person.

        Attributes:
            output_id (str):
            model_ids (list[str] | None | Unset):
            peer_id (None | str | Unset):
    """

    output_id: str
    model_ids: list[str] | None | Unset = UNSET
    peer_id: None | str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        output_id = self.output_id

        model_ids: list[str] | None | Unset
        if isinstance(self.model_ids, Unset):
            model_ids = UNSET
        elif isinstance(self.model_ids, list):
            model_ids = self.model_ids

        else:
            model_ids = self.model_ids

        peer_id: None | str | Unset
        if isinstance(self.peer_id, Unset):
            peer_id = UNSET
        else:
            peer_id = self.peer_id

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "output_id": output_id,
            }
        )
        if model_ids is not UNSET:
            field_dict["model_ids"] = model_ids
        if peer_id is not UNSET:
            field_dict["peer_id"] = peer_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        output_id = d.pop("output_id")

        def _parse_model_ids(data: object) -> list[str] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                model_ids_type_0 = cast(list[str], data)

                return model_ids_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[str] | None | Unset, data)

        model_ids = _parse_model_ids(d.pop("model_ids", UNSET))

        def _parse_peer_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        peer_id = _parse_peer_id(d.pop("peer_id", UNSET))

        frame_attach_request = cls(
            output_id=output_id,
            model_ids=model_ids,
            peer_id=peer_id,
        )

        return frame_attach_request
