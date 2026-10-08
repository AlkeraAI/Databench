from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.run_accepted_status import RunAcceptedStatus
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.submitted import Submitted


T = TypeVar("T", bound="RunAccepted")


@_attrs_define
class RunAccepted:
    """
    Attributes:
        run_id (str):
        status (RunAcceptedStatus):
        frontier_included (bool):
        submitted (Submitted | Unset):
        repeat (bool | Unset):  Default: False.
    """

    run_id: str
    status: RunAcceptedStatus
    frontier_included: bool
    submitted: Submitted | Unset = UNSET
    repeat: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        run_id = self.run_id

        status = self.status.value

        frontier_included = self.frontier_included

        submitted: dict[str, Any] | Unset = UNSET
        if not isinstance(self.submitted, Unset):
            submitted = self.submitted.to_dict()

        repeat = self.repeat

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "run_id": run_id,
                "status": status,
                "frontier_included": frontier_included,
            }
        )
        if submitted is not UNSET:
            field_dict["submitted"] = submitted
        if repeat is not UNSET:
            field_dict["repeat"] = repeat

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.submitted import Submitted

        d = dict(src_dict)
        run_id = d.pop("run_id")

        status = RunAcceptedStatus(d.pop("status"))

        frontier_included = d.pop("frontier_included")

        _submitted = d.pop("submitted", UNSET)
        submitted: Submitted | Unset
        if isinstance(_submitted, Unset):
            submitted = UNSET
        else:
            submitted = Submitted.from_dict(_submitted)

        repeat = d.pop("repeat", UNSET)

        run_accepted = cls(
            run_id=run_id,
            status=status,
            frontier_included=frontier_included,
            submitted=submitted,
            repeat=repeat,
        )

        run_accepted.additional_properties = d
        return run_accepted

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
