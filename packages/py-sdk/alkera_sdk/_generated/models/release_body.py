from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.release_body_ending_type_0 import ReleaseBodyEndingType0
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.release_body_final_type_0_item import ReleaseBodyFinalType0Item


T = TypeVar("T", bound="ReleaseBody")


@_attrs_define
class ReleaseBody:
    """The hand-back. ``unsyncedCount`` is how many files the holder still had
    queued when its drain ran out, and ``unsyncedPaths`` names up to
    :data:`RELEASE_MAX_UNSYNCED_PATHS` of them; the count is recorded on the
    lease, the paths are the holder's report and are not stored.

        Attributes:
            epoch (int):
            instance_id (str):
            final (list[ReleaseBodyFinalType0Item] | None | Unset):
            unsynced_count (int | None | Unset):
            unsynced_paths (list[str] | Unset):
            ending (None | ReleaseBodyEndingType0 | Unset):
    """

    epoch: int
    instance_id: str
    final: list[ReleaseBodyFinalType0Item] | None | Unset = UNSET
    unsynced_count: int | None | Unset = UNSET
    unsynced_paths: list[str] | Unset = UNSET
    ending: None | ReleaseBodyEndingType0 | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        epoch = self.epoch

        instance_id = self.instance_id

        final: list[dict[str, Any]] | None | Unset
        if isinstance(self.final, Unset):
            final = UNSET
        elif isinstance(self.final, list):
            final = []
            for final_type_0_item_data in self.final:
                final_type_0_item = final_type_0_item_data.to_dict()
                final.append(final_type_0_item)

        else:
            final = self.final

        unsynced_count: int | None | Unset
        if isinstance(self.unsynced_count, Unset):
            unsynced_count = UNSET
        else:
            unsynced_count = self.unsynced_count

        unsynced_paths: list[str] | Unset = UNSET
        if not isinstance(self.unsynced_paths, Unset):
            unsynced_paths = self.unsynced_paths

        ending: None | str | Unset
        if isinstance(self.ending, Unset):
            ending = UNSET
        elif isinstance(self.ending, ReleaseBodyEndingType0):
            ending = self.ending.value
        else:
            ending = self.ending

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "epoch": epoch,
                "instanceId": instance_id,
            }
        )
        if final is not UNSET:
            field_dict["final"] = final
        if unsynced_count is not UNSET:
            field_dict["unsyncedCount"] = unsynced_count
        if unsynced_paths is not UNSET:
            field_dict["unsyncedPaths"] = unsynced_paths
        if ending is not UNSET:
            field_dict["ending"] = ending

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.release_body_final_type_0_item import (
            ReleaseBodyFinalType0Item,
        )

        d = dict(src_dict)
        epoch = d.pop("epoch")

        instance_id = d.pop("instanceId")

        def _parse_final(data: object) -> list[ReleaseBodyFinalType0Item] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                final_type_0 = []
                _final_type_0 = data
                for final_type_0_item_data in _final_type_0:
                    final_type_0_item = ReleaseBodyFinalType0Item.from_dict(final_type_0_item_data)

                    final_type_0.append(final_type_0_item)

                return final_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[ReleaseBodyFinalType0Item] | None | Unset, data)

        final = _parse_final(d.pop("final", UNSET))

        def _parse_unsynced_count(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        unsynced_count = _parse_unsynced_count(d.pop("unsyncedCount", UNSET))

        unsynced_paths = cast(list[str], d.pop("unsyncedPaths", UNSET))

        def _parse_ending(data: object) -> None | ReleaseBodyEndingType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                ending_type_0 = ReleaseBodyEndingType0(data)

                return ending_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | ReleaseBodyEndingType0 | Unset, data)

        ending = _parse_ending(d.pop("ending", UNSET))

        release_body = cls(
            epoch=epoch,
            instance_id=instance_id,
            final=final,
            unsynced_count=unsynced_count,
            unsynced_paths=unsynced_paths,
            ending=ending,
        )

        release_body.additional_properties = d
        return release_body

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
