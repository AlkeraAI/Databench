from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.above_target import AboveTarget
    from ..models.all_target import AllTarget
    from ..models.below_target import BelowTarget
    from ..models.cells_target import CellsTarget
    from ..models.stale_target import StaleTarget


T = TypeVar("T", bound="RunRequest")


@_attrs_define
class RunRequest:
    """``POST .../runs``. ``frontier`` is the document token the requester
    holds: the run waits (up to 2 s) for the document to include it, then
    takes its targets' text from there.

        Attributes:
            target (AboveTarget | AllTarget | BelowTarget | CellsTarget | StaleTarget):
            frontier (None | str | Unset):
            confirm_expensive (bool | Unset):  Default: False.
            client_run_id (None | str | Unset):
    """

    target: AboveTarget | AllTarget | BelowTarget | CellsTarget | StaleTarget
    frontier: None | str | Unset = UNSET
    confirm_expensive: bool | Unset = False
    client_run_id: None | str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        from ..models.above_target import AboveTarget
        from ..models.all_target import AllTarget
        from ..models.cells_target import CellsTarget
        from ..models.stale_target import StaleTarget

        target: dict[str, Any]
        if isinstance(self.target, CellsTarget):
            target = self.target.to_dict()
        elif isinstance(self.target, AllTarget):
            target = self.target.to_dict()
        elif isinstance(self.target, StaleTarget):
            target = self.target.to_dict()
        elif isinstance(self.target, AboveTarget):
            target = self.target.to_dict()
        else:
            target = self.target.to_dict()

        frontier: None | str | Unset
        if isinstance(self.frontier, Unset):
            frontier = UNSET
        else:
            frontier = self.frontier

        confirm_expensive = self.confirm_expensive

        client_run_id: None | str | Unset
        if isinstance(self.client_run_id, Unset):
            client_run_id = UNSET
        else:
            client_run_id = self.client_run_id

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "target": target,
            }
        )
        if frontier is not UNSET:
            field_dict["frontier"] = frontier
        if confirm_expensive is not UNSET:
            field_dict["confirm_expensive"] = confirm_expensive
        if client_run_id is not UNSET:
            field_dict["client_run_id"] = client_run_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.above_target import AboveTarget
        from ..models.all_target import AllTarget
        from ..models.below_target import BelowTarget
        from ..models.cells_target import CellsTarget
        from ..models.stale_target import StaleTarget

        d = dict(src_dict)

        def _parse_target(
            data: object,
        ) -> AboveTarget | AllTarget | BelowTarget | CellsTarget | StaleTarget:
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                target_type_0 = CellsTarget.from_dict(data)

                return target_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                target_type_1 = AllTarget.from_dict(data)

                return target_type_1
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                target_type_2 = StaleTarget.from_dict(data)

                return target_type_2
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                target_type_3 = AboveTarget.from_dict(data)

                return target_type_3
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            if not isinstance(data, dict):
                raise TypeError()
            target_type_4 = BelowTarget.from_dict(data)

            return target_type_4

        target = _parse_target(d.pop("target"))

        def _parse_frontier(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        frontier = _parse_frontier(d.pop("frontier", UNSET))

        confirm_expensive = d.pop("confirm_expensive", UNSET)

        def _parse_client_run_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        client_run_id = _parse_client_run_id(d.pop("client_run_id", UNSET))

        run_request = cls(
            target=target,
            frontier=frontier,
            confirm_expensive=confirm_expensive,
            client_run_id=client_run_id,
        )

        return run_request
