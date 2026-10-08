from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast
from uuid import UUID

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.deletion_status_read import DeletionStatusRead


T = TypeVar("T", bound="AdminAccountRead")


@_attrs_define
class AdminAccountRead:
    """The support tool's view of one person's lifecycle requests.

    Attributes:
        user_id (UUID):
        deleted_at (datetime.datetime | None | Unset):
        deletion (DeletionStatusRead | None | Unset):
    """

    user_id: UUID
    deleted_at: datetime.datetime | None | Unset = UNSET
    deletion: DeletionStatusRead | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.deletion_status_read import DeletionStatusRead

        user_id = str(self.user_id)

        deleted_at: None | str | Unset
        if isinstance(self.deleted_at, Unset):
            deleted_at = UNSET
        elif isinstance(self.deleted_at, datetime.datetime):
            deleted_at = self.deleted_at.isoformat()
        else:
            deleted_at = self.deleted_at

        deletion: dict[str, Any] | None | Unset
        if isinstance(self.deletion, Unset):
            deletion = UNSET
        elif isinstance(self.deletion, DeletionStatusRead):
            deletion = self.deletion.to_dict()
        else:
            deletion = self.deletion

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "user_id": user_id,
            }
        )
        if deleted_at is not UNSET:
            field_dict["deleted_at"] = deleted_at
        if deletion is not UNSET:
            field_dict["deletion"] = deletion

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.deletion_status_read import DeletionStatusRead

        d = dict(src_dict)
        user_id = UUID(d.pop("user_id"))

        def _parse_deleted_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                deleted_at_type_0 = datetime.datetime.fromisoformat(data)

                return deleted_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        deleted_at = _parse_deleted_at(d.pop("deleted_at", UNSET))

        def _parse_deletion(data: object) -> DeletionStatusRead | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                deletion_type_0 = DeletionStatusRead.from_dict(data)

                return deletion_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(DeletionStatusRead | None | Unset, data)

        deletion = _parse_deletion(d.pop("deletion", UNSET))

        admin_account_read = cls(
            user_id=user_id,
            deleted_at=deleted_at,
            deletion=deletion,
        )

        admin_account_read.additional_properties = d
        return admin_account_read

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
