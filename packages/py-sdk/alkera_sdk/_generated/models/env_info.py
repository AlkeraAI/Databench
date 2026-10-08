from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.env_info_allowed_actions_type_0_item import EnvInfoAllowedActionsType0Item
from ..types import UNSET, Unset

T = TypeVar("T", bound="EnvInfo")


@_attrs_define
class EnvInfo:
    """
    Attributes:
        env_id (str):
        kind (str):
        spec_root (str):
        python (str):
        state (str):
        recorded_in_file (bool):
        recorded (str | Unset):  Default: ''.
        last_failure (str | Unset):  Default: ''.
        allowed_actions (list[EnvInfoAllowedActionsType0Item] | None | Unset):
    """

    env_id: str
    kind: str
    spec_root: str
    python: str
    state: str
    recorded_in_file: bool
    recorded: str | Unset = ""
    last_failure: str | Unset = ""
    allowed_actions: list[EnvInfoAllowedActionsType0Item] | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        env_id = self.env_id

        kind = self.kind

        spec_root = self.spec_root

        python = self.python

        state = self.state

        recorded_in_file = self.recorded_in_file

        recorded = self.recorded

        last_failure = self.last_failure

        allowed_actions: list[str] | None | Unset
        if isinstance(self.allowed_actions, Unset):
            allowed_actions = UNSET
        elif isinstance(self.allowed_actions, list):
            allowed_actions = []
            for allowed_actions_type_0_item_data in self.allowed_actions:
                allowed_actions_type_0_item = allowed_actions_type_0_item_data.value
                allowed_actions.append(allowed_actions_type_0_item)

        else:
            allowed_actions = self.allowed_actions

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "env_id": env_id,
                "kind": kind,
                "spec_root": spec_root,
                "python": python,
                "state": state,
                "recorded_in_file": recorded_in_file,
            }
        )
        if recorded is not UNSET:
            field_dict["recorded"] = recorded
        if last_failure is not UNSET:
            field_dict["last_failure"] = last_failure
        if allowed_actions is not UNSET:
            field_dict["allowed_actions"] = allowed_actions

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        env_id = d.pop("env_id")

        kind = d.pop("kind")

        spec_root = d.pop("spec_root")

        python = d.pop("python")

        state = d.pop("state")

        recorded_in_file = d.pop("recorded_in_file")

        recorded = d.pop("recorded", UNSET)

        last_failure = d.pop("last_failure", UNSET)

        def _parse_allowed_actions(
            data: object,
        ) -> list[EnvInfoAllowedActionsType0Item] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                allowed_actions_type_0 = []
                _allowed_actions_type_0 = data
                for allowed_actions_type_0_item_data in _allowed_actions_type_0:
                    allowed_actions_type_0_item = EnvInfoAllowedActionsType0Item(
                        allowed_actions_type_0_item_data
                    )

                    allowed_actions_type_0.append(allowed_actions_type_0_item)

                return allowed_actions_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[EnvInfoAllowedActionsType0Item] | None | Unset, data)

        allowed_actions = _parse_allowed_actions(d.pop("allowed_actions", UNSET))

        env_info = cls(
            env_id=env_id,
            kind=kind,
            spec_root=spec_root,
            python=python,
            state=state,
            recorded_in_file=recorded_in_file,
            recorded=recorded,
            last_failure=last_failure,
            allowed_actions=allowed_actions,
        )

        env_info.additional_properties = d
        return env_info

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
