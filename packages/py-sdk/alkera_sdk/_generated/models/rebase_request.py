from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define

T = TypeVar("T", bound="RebaseRequest")


@_attrs_define
class RebaseRequest:
    """``POST .../rebase``: an editor's update written in ``epoch`` that never
    reached it (the document's history restarted first), standard base64 of
    the Loro update bytes. The server carries it into the current epoch cell
    by cell.

        Attributes:
            epoch (int):
            update (str):
    """

    epoch: int
    update: str

    def to_dict(self) -> dict[str, Any]:
        epoch = self.epoch

        update = self.update

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "epoch": epoch,
                "update": update,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        epoch = d.pop("epoch")

        update = d.pop("update")

        rebase_request = cls(
            epoch=epoch,
            update=update,
        )

        return rebase_request
