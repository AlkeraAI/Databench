from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="AdminOrgSettingsUpdate")


@_attrs_define
class AdminOrgSettingsUpdate:
    """The platform-staff update: everything an org admin may change, plus the
    chat sandbox limits, which only Alkera staff set (they size the machine the
    org is billed for), and the project-workspace cap. An explicit null resets
    a limit to its default; an absent field leaves it standing.

        Attributes:
            allow_login_google (bool | None | Unset):
            allow_login_github (bool | None | Unset):
            web_search_enabled (bool | None | Unset):
            ownership_escalation_enabled (bool | None | Unset):
            gate_force_rules (list[str] | None | Unset):
            sandbox_vcpu (int | None | Unset):
            sandbox_memory_mb (int | None | Unset):
            workspace_project_cap (int | None | Unset):
    """

    allow_login_google: bool | None | Unset = UNSET
    allow_login_github: bool | None | Unset = UNSET
    web_search_enabled: bool | None | Unset = UNSET
    ownership_escalation_enabled: bool | None | Unset = UNSET
    gate_force_rules: list[str] | None | Unset = UNSET
    sandbox_vcpu: int | None | Unset = UNSET
    sandbox_memory_mb: int | None | Unset = UNSET
    workspace_project_cap: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        allow_login_google: bool | None | Unset
        if isinstance(self.allow_login_google, Unset):
            allow_login_google = UNSET
        else:
            allow_login_google = self.allow_login_google

        allow_login_github: bool | None | Unset
        if isinstance(self.allow_login_github, Unset):
            allow_login_github = UNSET
        else:
            allow_login_github = self.allow_login_github

        web_search_enabled: bool | None | Unset
        if isinstance(self.web_search_enabled, Unset):
            web_search_enabled = UNSET
        else:
            web_search_enabled = self.web_search_enabled

        ownership_escalation_enabled: bool | None | Unset
        if isinstance(self.ownership_escalation_enabled, Unset):
            ownership_escalation_enabled = UNSET
        else:
            ownership_escalation_enabled = self.ownership_escalation_enabled

        gate_force_rules: list[str] | None | Unset
        if isinstance(self.gate_force_rules, Unset):
            gate_force_rules = UNSET
        elif isinstance(self.gate_force_rules, list):
            gate_force_rules = self.gate_force_rules

        else:
            gate_force_rules = self.gate_force_rules

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

        def _parse_allow_login_google(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        allow_login_google = _parse_allow_login_google(d.pop("allow_login_google", UNSET))

        def _parse_allow_login_github(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        allow_login_github = _parse_allow_login_github(d.pop("allow_login_github", UNSET))

        def _parse_web_search_enabled(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        web_search_enabled = _parse_web_search_enabled(d.pop("web_search_enabled", UNSET))

        def _parse_ownership_escalation_enabled(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        ownership_escalation_enabled = _parse_ownership_escalation_enabled(
            d.pop("ownership_escalation_enabled", UNSET)
        )

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

        admin_org_settings_update = cls(
            allow_login_google=allow_login_google,
            allow_login_github=allow_login_github,
            web_search_enabled=web_search_enabled,
            ownership_escalation_enabled=ownership_escalation_enabled,
            gate_force_rules=gate_force_rules,
            sandbox_vcpu=sandbox_vcpu,
            sandbox_memory_mb=sandbox_memory_mb,
            workspace_project_cap=workspace_project_cap,
        )

        admin_org_settings_update.additional_properties = d
        return admin_org_settings_update

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
