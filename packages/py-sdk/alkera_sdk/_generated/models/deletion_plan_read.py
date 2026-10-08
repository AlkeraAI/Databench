from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.deletion_plan_read_reauth import DeletionPlanReadReauth

if TYPE_CHECKING:
    from ..models.plan_blocker_read import PlanBlockerRead
    from ..models.planned_org_read import PlannedOrgRead


T = TypeVar("T", bound="DeletionPlanRead")


@_attrs_define
class DeletionPlanRead:
    """
    Attributes:
        orgs (list[PlannedOrgRead]):
        blockers (list[PlanBlockerRead]):
        forfeited_credit_nanos (int):
        can_proceed (bool):
        grace_days (int):
        reauth (DeletionPlanReadReauth):
        mfa_required (bool):
    """

    orgs: list[PlannedOrgRead]
    blockers: list[PlanBlockerRead]
    forfeited_credit_nanos: int
    can_proceed: bool
    grace_days: int
    reauth: DeletionPlanReadReauth
    mfa_required: bool
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        orgs = []
        for orgs_item_data in self.orgs:
            orgs_item = orgs_item_data.to_dict()
            orgs.append(orgs_item)

        blockers = []
        for blockers_item_data in self.blockers:
            blockers_item = blockers_item_data.to_dict()
            blockers.append(blockers_item)

        forfeited_credit_nanos = self.forfeited_credit_nanos

        can_proceed = self.can_proceed

        grace_days = self.grace_days

        reauth = self.reauth.value

        mfa_required = self.mfa_required

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "orgs": orgs,
                "blockers": blockers,
                "forfeited_credit_nanos": forfeited_credit_nanos,
                "can_proceed": can_proceed,
                "grace_days": grace_days,
                "reauth": reauth,
                "mfa_required": mfa_required,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.plan_blocker_read import PlanBlockerRead
        from ..models.planned_org_read import PlannedOrgRead

        d = dict(src_dict)
        orgs = []
        _orgs = d.pop("orgs")
        for orgs_item_data in _orgs:
            orgs_item = PlannedOrgRead.from_dict(orgs_item_data)

            orgs.append(orgs_item)

        blockers = []
        _blockers = d.pop("blockers")
        for blockers_item_data in _blockers:
            blockers_item = PlanBlockerRead.from_dict(blockers_item_data)

            blockers.append(blockers_item)

        forfeited_credit_nanos = d.pop("forfeited_credit_nanos")

        can_proceed = d.pop("can_proceed")

        grace_days = d.pop("grace_days")

        reauth = DeletionPlanReadReauth(d.pop("reauth"))

        mfa_required = d.pop("mfa_required")

        deletion_plan_read = cls(
            orgs=orgs,
            blockers=blockers,
            forfeited_credit_nanos=forfeited_credit_nanos,
            can_proceed=can_proceed,
            grace_days=grace_days,
            reauth=reauth,
            mfa_required=mfa_required,
        )

        deletion_plan_read.additional_properties = d
        return deletion_plan_read

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
