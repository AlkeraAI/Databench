from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.user_read import UserRead


T = TypeVar("T", bound="LoginResponse")


@_attrs_define
class LoginResponse:
    """
    Attributes:
        user (UserRead):
        expires_at (datetime.datetime):
        choose_org (bool | Unset):  Default: False.
    """

    user: UserRead
    expires_at: datetime.datetime
    choose_org: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        user = self.user.to_dict()

        expires_at = self.expires_at.isoformat()

        choose_org = self.choose_org

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "user": user,
                "expires_at": expires_at,
            }
        )
        if choose_org is not UNSET:
            field_dict["choose_org"] = choose_org

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.user_read import UserRead

        d = dict(src_dict)
        user = UserRead.from_dict(d.pop("user"))

        expires_at = datetime.datetime.fromisoformat(d.pop("expires_at"))

        choose_org = d.pop("choose_org", UNSET)

        login_response = cls(
            user=user,
            expires_at=expires_at,
            choose_org=choose_org,
        )

        login_response.additional_properties = d
        return login_response

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
