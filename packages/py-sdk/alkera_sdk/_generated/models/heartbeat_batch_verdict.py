from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.heartbeat_batch_verdict_verdict import HeartbeatBatchVerdictVerdict
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.lease_grant import LeaseGrant


T = TypeVar("T", bound="HeartbeatBatchVerdict")


@_attrs_define
class HeartbeatBatchVerdict:
    """
    Attributes:
        node_id (str):
        verdict (HeartbeatBatchVerdictVerdict):
        grant (LeaseGrant | None | Unset):
    """

    node_id: str
    verdict: HeartbeatBatchVerdictVerdict
    grant: LeaseGrant | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.lease_grant import LeaseGrant

        node_id = self.node_id

        verdict = self.verdict.value

        grant: dict[str, Any] | None | Unset
        if isinstance(self.grant, Unset):
            grant = UNSET
        elif isinstance(self.grant, LeaseGrant):
            grant = self.grant.to_dict()
        else:
            grant = self.grant

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "nodeId": node_id,
                "verdict": verdict,
            }
        )
        if grant is not UNSET:
            field_dict["grant"] = grant

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.lease_grant import LeaseGrant

        d = dict(src_dict)
        node_id = d.pop("nodeId")

        verdict = HeartbeatBatchVerdictVerdict(d.pop("verdict"))

        def _parse_grant(data: object) -> LeaseGrant | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                grant_type_0 = LeaseGrant.from_dict(data)

                return grant_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(LeaseGrant | None | Unset, data)

        grant = _parse_grant(d.pop("grant", UNSET))

        heartbeat_batch_verdict = cls(
            node_id=node_id,
            verdict=verdict,
            grant=grant,
        )

        heartbeat_batch_verdict.additional_properties = d
        return heartbeat_batch_verdict

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
