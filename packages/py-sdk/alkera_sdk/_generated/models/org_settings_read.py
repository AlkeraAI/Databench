from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="OrgSettingsRead")


@_attrs_define
class OrgSettingsRead:
    """Org-wide settings. A missing row reads as all-defaults (see service).

    Attributes:
        allow_login_google (bool | Unset):  Default: True.
        allow_login_github (bool | Unset):  Default: True.
        web_search_enabled (bool | Unset):  Default: True.
        ownership_escalation_enabled (bool | Unset):  Default: False.
        gate_force_rules (list[str] | None | Unset):
        gate_builtin_force_rules (list[str] | Unset):
        gate_known_rules (list[str] | Unset):
        sandbox_vcpu (int | None | Unset):
        sandbox_memory_mb (int | None | Unset):
        workspace_project_cap (int | None | Unset):
    """

    allow_login_google: bool | Unset = True
    allow_login_github: bool | Unset = True
    web_search_enabled: bool | Unset = True
    ownership_escalation_enabled: bool | Unset = False
    gate_force_rules: list[str] | None | Unset = UNSET
    gate_builtin_force_rules: list[str] | Unset = UNSET
    gate_known_rules: list[str] | Unset = UNSET
    sandbox_vcpu: int | None | Unset = UNSET
    sandbox_memory_mb: int | None | Unset = UNSET
    workspace_project_cap: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        allow_login_google = self.allow_login_google

        allow_login_github = self.allow_login_github

        web_search_enabled = self.web_search_enabled

        ownership_escalation_enabled = self.ownership_escalation_enabled

        gate_force_rules: list[str] | None | Unset
        if isinstance(self.gate_force_rules, Unset):
            gate_force_rules = UNSET
        elif isinstance(self.gate_force_rules, list):
            gate_force_rules = self.gate_force_rules

        else:
            gate_force_rules = self.gate_force_rules

        gate_builtin_force_rules: list[str] | Unset = UNSET
        if not isinstance(self.gate_builtin_force_rules, Unset):
            gate_builtin_force_rules = self.gate_builtin_force_rules

        gate_known_rules: list[str] | Unset = UNSET
        if not isinstance(self.gate_known_rules, Unset):
            gate_known_rules = self.gate_known_rules

        sandbox_vcpu: int | None | Unset
        if isinstance(self.sandbox_vcpu, Unset):
            sandbox_vcpu = UNSET
        else:
            sandbox_vcpu = self.sandbox_vcpu

        sandbox_memory_mb: int | None | Unset
        if isinstance(self.sandbox_memory_mb, Unset):
            sandbox_memory_mb = UNSET
        else:
            sandbox_memory_mb = self.sandbox_memory_mb

        workspace_project_cap: int | None | Unset
        if isinstance(self.workspace_project_cap, Unset):
            workspace_project_cap = UNSET
        else:
            workspace_project_cap = self.workspace_project_cap

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if allow_login_google is not UNSET:
            field_dict["allow_login_google"] = allow_login_google
        if allow_login_github is not UNSET:
            field_dict["allow_login_github"] = allow_login_github
        if web_search_enabled is not UNSET:
            field_dict["web_search_enabled"] = web_search_enabled
        if ownership_escalation_enabled is not UNSET:
            field_dict["ownership_escalation_enabled"] = ownership_escalation_enabled
        if gate_force_rules is not UNSET:
            field_dict["gate_force_rules"] = gate_force_rules
        if gate_builtin_force_rules is not UNSET:
            field_dict["gate_builtin_force_rules"] = gate_builtin_force_rules
        if gate_known_rules is not UNSET:
            field_dict["gate_known_rules"] = gate_known_rules
        if sandbox_vcpu is not UNSET:
            field_dict["sandbox_vcpu"] = sandbox_vcpu
        if sandbox_memory_mb is not UNSET:
            field_dict["sandbox_memory_mb"] = sandbox_memory_mb
        if workspace_project_cap is not UNSET:
            field_dict["workspace_project_cap"] = workspace_project_cap

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        allow_login_google = d.pop("allow_login_google", UNSET)

        allow_login_github = d.pop("allow_login_github", UNSET)

        web_search_enabled = d.pop("web_search_enabled", UNSET)

        ownership_escalation_enabled = d.pop("ownership_escalation_enabled", UNSET)

        def _parse_gate_force_rules(data: object) -> list[str] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                gate_force_rules_type_0 = cast(list[str], data)

                return gate_force_rules_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[str] | None | Unset, data)

        gate_force_rules = _parse_gate_force_rules(d.pop("gate_force_rules", UNSET))

        gate_builtin_force_rules = cast(list[str], d.pop("gate_builtin_force_rules", UNSET))

        gate_known_rules = cast(list[str], d.pop("gate_known_rules", UNSET))

        def _parse_sandbox_vcpu(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        sandbox_vcpu = _parse_sandbox_vcpu(d.pop("sandbox_vcpu", UNSET))

        def _parse_sandbox_memory_mb(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        sandbox_memory_mb = _parse_sandbox_memory_mb(d.pop("sandbox_memory_mb", UNSET))

        def _parse_workspace_project_cap(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        workspace_project_cap = _parse_workspace_project_cap(d.pop("workspace_project_cap", UNSET))

        org_settings_read = cls(
            allow_login_google=allow_login_google,
            allow_login_github=allow_login_github,
            web_search_enabled=web_search_enabled,
            ownership_escalation_enabled=ownership_escalation_enabled,
            gate_force_rules=gate_force_rules,
            gate_builtin_force_rules=gate_builtin_force_rules,
            gate_known_rules=gate_known_rules,
            sandbox_vcpu=sandbox_vcpu,
            sandbox_memory_mb=sandbox_memory_mb,
            workspace_project_cap=workspace_project_cap,
        )

        org_settings_read.additional_properties = d
        return org_settings_read

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
