from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.chart_export_request_format import ChartExportRequestFormat
from ..models.chart_export_request_scheme import ChartExportRequestScheme
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.chart_export_spec import ChartExportSpec


T = TypeVar("T", bound="ChartExportRequest")


@_attrs_define
class ChartExportRequest:
    """A chart to render. ``spec`` is an Alkera chart profile spec with its
    rows inline; ``scale`` multiplies a PNG's pixel size.

        Attributes:
            spec (ChartExportSpec):
            format_ (ChartExportRequestFormat | Unset):  Default: ChartExportRequestFormat.SVG.
            scheme (ChartExportRequestScheme | Unset):  Default: ChartExportRequestScheme.LIGHT.
            scale (float | Unset):  Default: 2.0.
    """

    spec: ChartExportSpec
    format_: ChartExportRequestFormat | Unset = ChartExportRequestFormat.SVG
    scheme: ChartExportRequestScheme | Unset = ChartExportRequestScheme.LIGHT
    scale: float | Unset = 2.0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        spec = self.spec.to_dict()

        format_: str | Unset = UNSET
        if not isinstance(self.format_, Unset):
            format_ = self.format_.value

        scheme: str | Unset = UNSET
        if not isinstance(self.scheme, Unset):
            scheme = self.scheme.value

        scale = self.scale

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "spec": spec,
            }
        )
        if format_ is not UNSET:
            field_dict["format"] = format_
        if scheme is not UNSET:
            field_dict["scheme"] = scheme
        if scale is not UNSET:
            field_dict["scale"] = scale

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.chart_export_spec import ChartExportSpec

        d = dict(src_dict)
        spec = ChartExportSpec.from_dict(d.pop("spec"))

        _format_ = d.pop("format", UNSET)
        format_: ChartExportRequestFormat | Unset
        if isinstance(_format_, Unset):
            format_ = UNSET
        else:
            format_ = ChartExportRequestFormat(_format_)

        _scheme = d.pop("scheme", UNSET)
        scheme: ChartExportRequestScheme | Unset
        if isinstance(_scheme, Unset):
            scheme = UNSET
        else:
            scheme = ChartExportRequestScheme(_scheme)

        scale = d.pop("scale", UNSET)

        chart_export_request = cls(
            spec=spec,
            format_=format_,
            scheme=scheme,
            scale=scale,
        )

        chart_export_request.additional_properties = d
        return chart_export_request

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
