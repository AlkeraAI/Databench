from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.settings_autoreload import SettingsAutoreload
from ..models.settings_dataframe import SettingsDataframe
from ..models.settings_reactivity import SettingsReactivity
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.sources import Sources


T = TypeVar("T", bound="Settings")


@_attrs_define
class Settings:
    """
    Attributes:
        format_ (str | Unset):  Default: '1.0'.
        reactivity (SettingsReactivity | Unset):  Default: SettingsReactivity.AUTORUN.
        dataframe (SettingsDataframe | Unset):  Default: SettingsDataframe.AUTO.
        env (None | str | Unset):
        outputs_in_git (bool | Unset):  Default: False.
        autoreload (SettingsAutoreload | Unset):  Default: SettingsAutoreload.OFF.
        sql_row_limit (int | None | Unset):
        sources (Sources | Unset):
    """

    format_: str | Unset = "1.0"
    reactivity: SettingsReactivity | Unset = SettingsReactivity.AUTORUN
    dataframe: SettingsDataframe | Unset = SettingsDataframe.AUTO
    env: None | str | Unset = UNSET
    outputs_in_git: bool | Unset = False
    autoreload: SettingsAutoreload | Unset = SettingsAutoreload.OFF
    sql_row_limit: int | None | Unset = UNSET
    sources: Sources | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        format_ = self.format_

        reactivity: str | Unset = UNSET
        if not isinstance(self.reactivity, Unset):
            reactivity = self.reactivity.value

        dataframe: str | Unset = UNSET
        if not isinstance(self.dataframe, Unset):
            dataframe = self.dataframe.value

        env: None | str | Unset
        if isinstance(self.env, Unset):
            env = UNSET
        else:
            env = self.env

        outputs_in_git = self.outputs_in_git

        autoreload: str | Unset = UNSET
        if not isinstance(self.autoreload, Unset):
            autoreload = self.autoreload.value

        sql_row_limit: int | None | Unset
        if isinstance(self.sql_row_limit, Unset):
            sql_row_limit = UNSET
        else:
            sql_row_limit = self.sql_row_limit

        sources: dict[str, Any] | Unset = UNSET
        if not isinstance(self.sources, Unset):
            sources = self.sources.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if format_ is not UNSET:
            field_dict["format"] = format_
        if reactivity is not UNSET:
            field_dict["reactivity"] = reactivity
        if dataframe is not UNSET:
            field_dict["dataframe"] = dataframe
        if env is not UNSET:
            field_dict["env"] = env
        if outputs_in_git is not UNSET:
            field_dict["outputs_in_git"] = outputs_in_git
        if autoreload is not UNSET:
            field_dict["autoreload"] = autoreload
        if sql_row_limit is not UNSET:
            field_dict["sql_row_limit"] = sql_row_limit
        if sources is not UNSET:
            field_dict["sources"] = sources

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.sources import Sources

        d = dict(src_dict)
        format_ = d.pop("format", UNSET)

        _reactivity = d.pop("reactivity", UNSET)
        reactivity: SettingsReactivity | Unset
        if isinstance(_reactivity, Unset):
            reactivity = UNSET
        else:
            reactivity = SettingsReactivity(_reactivity)

        _dataframe = d.pop("dataframe", UNSET)
        dataframe: SettingsDataframe | Unset
        if isinstance(_dataframe, Unset):
            dataframe = UNSET
        else:
            dataframe = SettingsDataframe(_dataframe)

        def _parse_env(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        env = _parse_env(d.pop("env", UNSET))

        outputs_in_git = d.pop("outputs_in_git", UNSET)

        _autoreload = d.pop("autoreload", UNSET)
        autoreload: SettingsAutoreload | Unset
        if isinstance(_autoreload, Unset):
            autoreload = UNSET
        else:
            autoreload = SettingsAutoreload(_autoreload)

        def _parse_sql_row_limit(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        sql_row_limit = _parse_sql_row_limit(d.pop("sql_row_limit", UNSET))

        _sources = d.pop("sources", UNSET)
        sources: Sources | Unset
        if isinstance(_sources, Unset):
            sources = UNSET
        else:
            sources = Sources.from_dict(_sources)

        settings = cls(
            format_=format_,
            reactivity=reactivity,
            dataframe=dataframe,
            env=env,
            outputs_in_git=outputs_in_git,
            autoreload=autoreload,
            sql_row_limit=sql_row_limit,
            sources=sources,
        )

        settings.additional_properties = d
        return settings

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
