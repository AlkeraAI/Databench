from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.cell_state_output_origin_type_0 import CellStateOutputOriginType0
from ..models.cell_state_status import CellStateStatus
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.cell_config import CellConfig
    from ..models.cell_extra import CellExtra
    from ..models.cell_meta import CellMeta
    from ..models.display_output import DisplayOutput
    from ..models.error_output import ErrorOutput
    from ..models.output_summary import OutputSummary
    from ..models.run_attribution import RunAttribution
    from ..models.stream_output import StreamOutput


T = TypeVar("T", bound="CellState")


@_attrs_define
class CellState:
    """
    Attributes:
        id (str):
        name (str):
        kind (str):
        index (int):
        status (CellStateStatus):
        rerun_waits_for (None | str | Unset):
        defs (list[str] | Unset):
        refs (list[str] | Unset):
        graph_errors (list[str] | Unset):
        source (None | str | Unset):
        output (None | OutputSummary | Unset):
        output_outdated (bool | Unset):  Default: False.
        output_origin (CellStateOutputOriginType0 | None | Unset):
        outputs (list[DisplayOutput | ErrorOutput | StreamOutput] | Unset):
        last_run (None | RunAttribution | Unset):
        config (CellConfig | Unset):
        meta (CellMeta | Unset):
        extra (CellExtra | Unset):
    """

    id: str
    name: str
    kind: str
    index: int
    status: CellStateStatus
    rerun_waits_for: None | str | Unset = UNSET
    defs: list[str] | Unset = UNSET
    refs: list[str] | Unset = UNSET
    graph_errors: list[str] | Unset = UNSET
    source: None | str | Unset = UNSET
    output: None | OutputSummary | Unset = UNSET
    output_outdated: bool | Unset = False
    output_origin: CellStateOutputOriginType0 | None | Unset = UNSET
    outputs: list[DisplayOutput | ErrorOutput | StreamOutput] | Unset = UNSET
    last_run: None | RunAttribution | Unset = UNSET
    config: CellConfig | Unset = UNSET
    meta: CellMeta | Unset = UNSET
    extra: CellExtra | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.display_output import DisplayOutput
        from ..models.output_summary import OutputSummary
        from ..models.run_attribution import RunAttribution
        from ..models.stream_output import StreamOutput

        id = self.id

        name = self.name

        kind = self.kind

        index = self.index

        status = self.status.value

        rerun_waits_for: None | str | Unset
        if isinstance(self.rerun_waits_for, Unset):
            rerun_waits_for = UNSET
        else:
            rerun_waits_for = self.rerun_waits_for

        defs: list[str] | Unset = UNSET
        if not isinstance(self.defs, Unset):
            defs = self.defs

        refs: list[str] | Unset = UNSET
        if not isinstance(self.refs, Unset):
            refs = self.refs

        graph_errors: list[str] | Unset = UNSET
        if not isinstance(self.graph_errors, Unset):
            graph_errors = self.graph_errors

        source: None | str | Unset
        if isinstance(self.source, Unset):
            source = UNSET
        else:
            source = self.source

        output: dict[str, Any] | None | Unset
        if isinstance(self.output, Unset):
            output = UNSET
        elif isinstance(self.output, OutputSummary):
            output = self.output.to_dict()
        else:
            output = self.output

        output_outdated = self.output_outdated

        output_origin: None | str | Unset
        if isinstance(self.output_origin, Unset):
            output_origin = UNSET
        elif isinstance(self.output_origin, CellStateOutputOriginType0):
            output_origin = self.output_origin.value
        else:
            output_origin = self.output_origin

        outputs: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.outputs, Unset):
            outputs = []
            for outputs_item_data in self.outputs:
                outputs_item: dict[str, Any]
                if isinstance(outputs_item_data, DisplayOutput):
                    outputs_item = outputs_item_data.to_dict()
                elif isinstance(outputs_item_data, StreamOutput):
                    outputs_item = outputs_item_data.to_dict()
                else:
                    outputs_item = outputs_item_data.to_dict()

                outputs.append(outputs_item)

        last_run: dict[str, Any] | None | Unset
        if isinstance(self.last_run, Unset):
            last_run = UNSET
        elif isinstance(self.last_run, RunAttribution):
            last_run = self.last_run.to_dict()
        else:
            last_run = self.last_run

        config: dict[str, Any] | Unset = UNSET
        if not isinstance(self.config, Unset):
            config = self.config.to_dict()

        meta: dict[str, Any] | Unset = UNSET
        if not isinstance(self.meta, Unset):
            meta = self.meta.to_dict()

        extra: dict[str, Any] | Unset = UNSET
        if not isinstance(self.extra, Unset):
            extra = self.extra.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "name": name,
                "kind": kind,
                "index": index,
                "status": status,
            }
        )
        if rerun_waits_for is not UNSET:
            field_dict["rerun_waits_for"] = rerun_waits_for
        if defs is not UNSET:
            field_dict["defs"] = defs
        if refs is not UNSET:
            field_dict["refs"] = refs
        if graph_errors is not UNSET:
            field_dict["graph_errors"] = graph_errors
        if source is not UNSET:
            field_dict["source"] = source
        if output is not UNSET:
            field_dict["output"] = output
        if output_outdated is not UNSET:
            field_dict["output_outdated"] = output_outdated
        if output_origin is not UNSET:
            field_dict["output_origin"] = output_origin
        if outputs is not UNSET:
            field_dict["outputs"] = outputs
        if last_run is not UNSET:
            field_dict["last_run"] = last_run
        if config is not UNSET:
            field_dict["config"] = config
        if meta is not UNSET:
            field_dict["meta"] = meta
        if extra is not UNSET:
            field_dict["extra"] = extra

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.cell_config import CellConfig
        from ..models.cell_extra import CellExtra
        from ..models.cell_meta import CellMeta
        from ..models.display_output import DisplayOutput
        from ..models.error_output import ErrorOutput
        from ..models.output_summary import OutputSummary
        from ..models.run_attribution import RunAttribution
        from ..models.stream_output import StreamOutput

        d = dict(src_dict)
        id = d.pop("id")

        name = d.pop("name")

        kind = d.pop("kind")

        index = d.pop("index")

        status = CellStateStatus(d.pop("status"))

        def _parse_rerun_waits_for(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        rerun_waits_for = _parse_rerun_waits_for(d.pop("rerun_waits_for", UNSET))

        defs = cast(list[str], d.pop("defs", UNSET))

        refs = cast(list[str], d.pop("refs", UNSET))

        graph_errors = cast(list[str], d.pop("graph_errors", UNSET))

        def _parse_source(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        source = _parse_source(d.pop("source", UNSET))

        def _parse_output(data: object) -> None | OutputSummary | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                output_type_0 = OutputSummary.from_dict(data)

                return output_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | OutputSummary | Unset, data)

        output = _parse_output(d.pop("output", UNSET))

        output_outdated = d.pop("output_outdated", UNSET)

        def _parse_output_origin(data: object) -> CellStateOutputOriginType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                output_origin_type_0 = CellStateOutputOriginType0(data)

                return output_origin_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(CellStateOutputOriginType0 | None | Unset, data)

        output_origin = _parse_output_origin(d.pop("output_origin", UNSET))

        _outputs = d.pop("outputs", UNSET)
        outputs: list[DisplayOutput | ErrorOutput | StreamOutput] | Unset = UNSET
        if _outputs is not UNSET:
            outputs = []
            for outputs_item_data in _outputs:

                def _parse_outputs_item(data: object) -> DisplayOutput | ErrorOutput | StreamOutput:
                    try:
                        if not isinstance(data, dict):
                            raise TypeError()
                        outputs_item_type_0 = DisplayOutput.from_dict(data)

                        return outputs_item_type_0
                    except (TypeError, ValueError, AttributeError, KeyError):
                        pass
                    try:
                        if not isinstance(data, dict):
                            raise TypeError()
                        outputs_item_type_1 = StreamOutput.from_dict(data)

                        return outputs_item_type_1
                    except (TypeError, ValueError, AttributeError, KeyError):
                        pass
                    if not isinstance(data, dict):
                        raise TypeError()
                    outputs_item_type_2 = ErrorOutput.from_dict(data)

                    return outputs_item_type_2

                outputs_item = _parse_outputs_item(outputs_item_data)

                outputs.append(outputs_item)

        def _parse_last_run(data: object) -> None | RunAttribution | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                last_run_type_0 = RunAttribution.from_dict(data)

                return last_run_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RunAttribution | Unset, data)

        last_run = _parse_last_run(d.pop("last_run", UNSET))

        _config = d.pop("config", UNSET)
        config: CellConfig | Unset
        if isinstance(_config, Unset):
            config = UNSET
        else:
            config = CellConfig.from_dict(_config)

        _meta = d.pop("meta", UNSET)
        meta: CellMeta | Unset
        if isinstance(_meta, Unset):
            meta = UNSET
        else:
            meta = CellMeta.from_dict(_meta)

        _extra = d.pop("extra", UNSET)
        extra: CellExtra | Unset
        if isinstance(_extra, Unset):
            extra = UNSET
        else:
            extra = CellExtra.from_dict(_extra)

        cell_state = cls(
            id=id,
            name=name,
            kind=kind,
            index=index,
            status=status,
            rerun_waits_for=rerun_waits_for,
            defs=defs,
            refs=refs,
            graph_errors=graph_errors,
            source=source,
            output=output,
            output_outdated=output_outdated,
            output_origin=output_origin,
            outputs=outputs,
            last_run=last_run,
            config=config,
            meta=meta,
            extra=extra,
        )

        cell_state.additional_properties = d
        return cell_state

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
