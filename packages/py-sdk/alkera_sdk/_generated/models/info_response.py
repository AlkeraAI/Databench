from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="InfoResponse")


@_attrs_define
class InfoResponse:
    """Build / deployment introspection — what's actually running.

    Used by ops to verify which image is live in a customer's VPC without
    needing credentials. Mirrors what tools like Sentry or GitHub Releases
    expect to see in a `/health` or `/version` endpoint.

        Attributes:
            app (str):
            version (str):
            build_id (None | str):
            env (str):
    """

    app: str
    version: str
    build_id: None | str
    env: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        app = self.app

        version = self.version

        build_id: None | str
        build_id = self.build_id

        env = self.env

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "app": app,
                "version": version,
                "build_id": build_id,
                "env": env,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        app = d.pop("app")

        version = d.pop("version")

        def _parse_build_id(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        build_id = _parse_build_id(d.pop("build_id"))

        env = d.pop("env")

        info_response = cls(
            app=app,
            version=version,
            build_id=build_id,
            env=env,
        )

        info_response.additional_properties = d
        return info_response

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
