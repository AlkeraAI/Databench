from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.ip_info import IpInfo


T = TypeVar("T", bound="AdminUserIpInfoRead")


@_attrs_define
class AdminUserIpInfoRead:
    """
    Attributes:
        signup (IpInfo | None | Unset):
        last_login (IpInfo | None | Unset):
    """

    signup: IpInfo | None | Unset = UNSET
    last_login: IpInfo | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.ip_info import IpInfo

        signup: dict[str, Any] | None | Unset
        if isinstance(self.signup, Unset):
            signup = UNSET
        elif isinstance(self.signup, IpInfo):
            signup = self.signup.to_dict()
        else:
            signup = self.signup

        last_login: dict[str, Any] | None | Unset
        if isinstance(self.last_login, Unset):
            last_login = UNSET
        elif isinstance(self.last_login, IpInfo):
            last_login = self.last_login.to_dict()
        else:
            last_login = self.last_login

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if signup is not UNSET:
            field_dict["signup"] = signup
        if last_login is not UNSET:
            field_dict["last_login"] = last_login

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.ip_info import IpInfo

        d = dict(src_dict)

        def _parse_signup(data: object) -> IpInfo | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                signup_type_0 = IpInfo.from_dict(data)

                return signup_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(IpInfo | None | Unset, data)

        signup = _parse_signup(d.pop("signup", UNSET))

        def _parse_last_login(data: object) -> IpInfo | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                last_login_type_0 = IpInfo.from_dict(data)

                return last_login_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(IpInfo | None | Unset, data)

        last_login = _parse_last_login(d.pop("last_login", UNSET))

        admin_user_ip_info_read = cls(
            signup=signup,
            last_login=last_login,
        )

        admin_user_ip_info_read.additional_properties = d
        return admin_user_ip_info_read

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
