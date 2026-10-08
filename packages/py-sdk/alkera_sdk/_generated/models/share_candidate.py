from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.principal_ref import PrincipalRef


T = TypeVar("T", bound="ShareCandidate")


@_attrs_define
class ShareCandidate:
    """Someone the caller may name on a share of this node.

    Attributes:
        principal (PrincipalRef): Who a grant is for: a ``user``, a ``team`` or the ``org`` itself.

            The kind is an open registry, so a kind the server has not registered is a
            422 rather than a row nothing can resolve. An ``org`` grant names the
            caller's own org; any other id is a 422 too. A ``user`` or ``team`` id that
            is not a principal of the caller's org (another org's, a stranger, an id
            never issued) is a 404, the same answer for every one of them, so the route
            never confirms who exists outside the org.
        name (str):
        email (None | str | Unset):
        is_org (bool | Unset):  Default: False.
    """

    principal: PrincipalRef
    name: str
    email: None | str | Unset = UNSET
    is_org: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        principal = self.principal.to_dict()

        name = self.name

        email: None | str | Unset
        if isinstance(self.email, Unset):
            email = UNSET
        else:
            email = self.email

        is_org = self.is_org

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "principal": principal,
                "name": name,
            }
        )
        if email is not UNSET:
            field_dict["email"] = email
        if is_org is not UNSET:
            field_dict["isOrg"] = is_org

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.principal_ref import PrincipalRef

        d = dict(src_dict)
        principal = PrincipalRef.from_dict(d.pop("principal"))

        name = d.pop("name")

        def _parse_email(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        email = _parse_email(d.pop("email", UNSET))

        is_org = d.pop("isOrg", UNSET)

        share_candidate = cls(
            principal=principal,
            name=name,
            email=email,
            is_org=is_org,
        )

        share_candidate.additional_properties = d
        return share_candidate

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
