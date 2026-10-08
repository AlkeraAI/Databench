from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.client_error_event_component import ClientErrorEventComponent
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.client_error_event_context_type_0 import ClientErrorEventContextType0


T = TypeVar("T", bound="ClientErrorEvent")


@_attrs_define
class ClientErrorEvent:
    """A passively-captured client error (web / extension), logged not stored.

    Attributes:
        message (str):
        component (ClientErrorEventComponent | Unset):  Default: ClientErrorEventComponent.WEB.
        error_type (None | str | Unset):
        stack (None | str | Unset):
        url (None | str | Unset):
        context (ClientErrorEventContextType0 | None | Unset):
    """

    message: str
    component: ClientErrorEventComponent | Unset = ClientErrorEventComponent.WEB
    error_type: None | str | Unset = UNSET
    stack: None | str | Unset = UNSET
    url: None | str | Unset = UNSET
    context: ClientErrorEventContextType0 | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.client_error_event_context_type_0 import (
            ClientErrorEventContextType0,
        )

        message = self.message

        component: str | Unset = UNSET
        if not isinstance(self.component, Unset):
            component = self.component.value

        error_type: None | str | Unset
        if isinstance(self.error_type, Unset):
            error_type = UNSET
        else:
            error_type = self.error_type

        stack: None | str | Unset
        if isinstance(self.stack, Unset):
            stack = UNSET
        else:
            stack = self.stack

        url: None | str | Unset
        if isinstance(self.url, Unset):
            url = UNSET
        else:
            url = self.url

        context: dict[str, Any] | None | Unset
        if isinstance(self.context, Unset):
            context = UNSET
        elif isinstance(self.context, ClientErrorEventContextType0):
            context = self.context.to_dict()
        else:
            context = self.context

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "message": message,
            }
        )
        if component is not UNSET:
            field_dict["component"] = component
        if error_type is not UNSET:
            field_dict["error_type"] = error_type
        if stack is not UNSET:
            field_dict["stack"] = stack
        if url is not UNSET:
            field_dict["url"] = url
        if context is not UNSET:
            field_dict["context"] = context

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.client_error_event_context_type_0 import (
            ClientErrorEventContextType0,
        )

        d = dict(src_dict)
        message = d.pop("message")

        _component = d.pop("component", UNSET)
        component: ClientErrorEventComponent | Unset
        if isinstance(_component, Unset):
            component = UNSET
        else:
            component = ClientErrorEventComponent(_component)

        def _parse_error_type(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error_type = _parse_error_type(d.pop("error_type", UNSET))

        def _parse_stack(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        stack = _parse_stack(d.pop("stack", UNSET))

        def _parse_url(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        url = _parse_url(d.pop("url", UNSET))

        def _parse_context(data: object) -> ClientErrorEventContextType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                context_type_0 = ClientErrorEventContextType0.from_dict(data)

                return context_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(ClientErrorEventContextType0 | None | Unset, data)

        context = _parse_context(d.pop("context", UNSET))

        client_error_event = cls(
            message=message,
            component=component,
            error_type=error_type,
            stack=stack,
            url=url,
            context=context,
        )

        client_error_event.additional_properties = d
        return client_error_event

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
