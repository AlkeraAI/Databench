from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.content import Content


T = TypeVar("T", bound="CommRequest")


@_attrs_define
class CommRequest:
    """A person's widget message from an output frame. The frame must be
    attached (``POST .../frames``), the sender's, and own ``comm_id``.
    ``buffers`` are standard base64.

        Attributes:
            frame_id (str):
            comm_id (str):
            msg_id (str):
            content (Content):
            buffers (list[str] | Unset):
    """

    frame_id: str
    comm_id: str
    msg_id: str
    content: Content
    buffers: list[str] | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        frame_id = self.frame_id

        comm_id = self.comm_id

        msg_id = self.msg_id

        content = self.content.to_dict()

        buffers: list[str] | Unset = UNSET
        if not isinstance(self.buffers, Unset):
            buffers = self.buffers

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "frame_id": frame_id,
                "comm_id": comm_id,
                "msg_id": msg_id,
                "content": content,
            }
        )
        if buffers is not UNSET:
            field_dict["buffers"] = buffers

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.content import Content

        d = dict(src_dict)
        frame_id = d.pop("frame_id")

        comm_id = d.pop("comm_id")

        msg_id = d.pop("msg_id")

        content = Content.from_dict(d.pop("content"))

        buffers = cast(list[str], d.pop("buffers", UNSET))

        comm_request = cls(
            frame_id=frame_id,
            comm_id=comm_id,
            msg_id=msg_id,
            content=content,
            buffers=buffers,
        )

        return comm_request
