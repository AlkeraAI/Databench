from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.content_grant_response_contentstate import ContentGrantResponseContentstate
from ..models.content_grant_response_kind import ContentGrantResponseKind
from ..types import UNSET, Unset

T = TypeVar("T", bound="ContentGrantResponse")


@_attrs_define
class ContentGrantResponse:
    """The minted URL, and what it was minted against.

    ``etag`` is the node's, so a client holding a stale item can tell that the
    bytes behind the URL are not the ones it rendered. ``expiresAt`` is the
    deadline the grant itself carries rather than a hint: there is no refresh,
    and a client past it mints again, which re-decides the access.

    ``contentState`` is what the minted bytes are against the disk of the
    machine holding the file's folder: ``on_drive`` (the machine's own copy),
    ``behind`` (the machine has a newer copy the drive could not fetch in time
    — the URL serves the store's older one, as of ``asOf``), or ``none`` for a
    file no machine reports on. A machine that is gone leaves ``on_drive`` and
    ``behind`` as they were: the drive's bytes did not leave with it.

        Attributes:
            url (str):
            expires_at (datetime.datetime):
            kind (ContentGrantResponseKind):
            etag (str):
            content_state (ContentGrantResponseContentstate | Unset):  Default: ContentGrantResponseContentstate.NONE.
            as_of (datetime.datetime | None | Unset):
    """

    url: str
    expires_at: datetime.datetime
    kind: ContentGrantResponseKind
    etag: str
    content_state: ContentGrantResponseContentstate | Unset = ContentGrantResponseContentstate.NONE
    as_of: datetime.datetime | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        url = self.url

        expires_at = self.expires_at.isoformat()

        kind = self.kind.value

        etag = self.etag

        content_state: str | Unset = UNSET
        if not isinstance(self.content_state, Unset):
            content_state = self.content_state.value

        as_of: None | str | Unset
        if isinstance(self.as_of, Unset):
            as_of = UNSET
        elif isinstance(self.as_of, datetime.datetime):
            as_of = self.as_of.isoformat()
        else:
            as_of = self.as_of

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "url": url,
                "expiresAt": expires_at,
                "kind": kind,
                "etag": etag,
            }
        )
        if content_state is not UNSET:
            field_dict["contentState"] = content_state
        if as_of is not UNSET:
            field_dict["asOf"] = as_of

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        url = d.pop("url")

        expires_at = datetime.datetime.fromisoformat(d.pop("expiresAt"))

        kind = ContentGrantResponseKind(d.pop("kind"))

        etag = d.pop("etag")

        _content_state = d.pop("contentState", UNSET)
        content_state: ContentGrantResponseContentstate | Unset
        if isinstance(_content_state, Unset):
            content_state = UNSET
        else:
            content_state = ContentGrantResponseContentstate(_content_state)

        def _parse_as_of(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                as_of_type_0 = datetime.datetime.fromisoformat(data)

                return as_of_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        as_of = _parse_as_of(d.pop("asOf", UNSET))

        content_grant_response = cls(
            url=url,
            expires_at=expires_at,
            kind=kind,
            etag=etag,
            content_state=content_state,
            as_of=as_of,
        )

        content_grant_response.additional_properties = d
        return content_grant_response

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
