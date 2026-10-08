from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.operation_conflict_wire import OperationConflictWire
    from ..models.operation_error_wire import OperationErrorWire


T = TypeVar("T", bound="OperationWire")


@_attrs_define
class OperationWire:
    """The ``Operation`` resource every client polls.

    Attributes:
        id (str):
        drive_id (str):
        kind (str):
        state (str):
        done (int | Unset):  Default: 0.
        total (int | None | Unset):
        bytes_ (int | Unset):  Default: 0.
        skipped (int | Unset):  Default: 0.
        conflicts (list[OperationConflictWire] | Unset):
        errors (list[OperationErrorWire] | Unset):
        result_node_id (None | str | Unset):
        result_version_id (None | str | Unset):
        result_unchanged (bool | Unset):  Default: False.
        result_url (None | str | Unset):
        result_url_expires_at (datetime.datetime | None | Unset):
        cancel_requested (bool | Unset):  Default: False.
        undoable_until (datetime.datetime | None | Unset):
        undoable (bool | Unset):  Default: False.
    """

    id: str
    drive_id: str
    kind: str
    state: str
    done: int | Unset = 0
    total: int | None | Unset = UNSET
    bytes_: int | Unset = 0
    skipped: int | Unset = 0
    conflicts: list[OperationConflictWire] | Unset = UNSET
    errors: list[OperationErrorWire] | Unset = UNSET
    result_node_id: None | str | Unset = UNSET
    result_version_id: None | str | Unset = UNSET
    result_unchanged: bool | Unset = False
    result_url: None | str | Unset = UNSET
    result_url_expires_at: datetime.datetime | None | Unset = UNSET
    cancel_requested: bool | Unset = False
    undoable_until: datetime.datetime | None | Unset = UNSET
    undoable: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        drive_id = self.drive_id

        kind = self.kind

        state = self.state

        done = self.done

        total: int | None | Unset
        if isinstance(self.total, Unset):
            total = UNSET
        else:
            total = self.total

        bytes_ = self.bytes_

        skipped = self.skipped

        conflicts: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.conflicts, Unset):
            conflicts = []
            for conflicts_item_data in self.conflicts:
                conflicts_item = conflicts_item_data.to_dict()
                conflicts.append(conflicts_item)

        errors: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.errors, Unset):
            errors = []
            for errors_item_data in self.errors:
                errors_item = errors_item_data.to_dict()
                errors.append(errors_item)

        result_node_id: None | str | Unset
        if isinstance(self.result_node_id, Unset):
            result_node_id = UNSET
        else:
            result_node_id = self.result_node_id

        result_version_id: None | str | Unset
        if isinstance(self.result_version_id, Unset):
            result_version_id = UNSET
        else:
            result_version_id = self.result_version_id

        result_unchanged = self.result_unchanged

        result_url: None | str | Unset
        if isinstance(self.result_url, Unset):
            result_url = UNSET
        else:
            result_url = self.result_url

        result_url_expires_at: None | str | Unset
        if isinstance(self.result_url_expires_at, Unset):
            result_url_expires_at = UNSET
        elif isinstance(self.result_url_expires_at, datetime.datetime):
            result_url_expires_at = self.result_url_expires_at.isoformat()
        else:
            result_url_expires_at = self.result_url_expires_at

        cancel_requested = self.cancel_requested

        undoable_until: None | str | Unset
        if isinstance(self.undoable_until, Unset):
            undoable_until = UNSET
        elif isinstance(self.undoable_until, datetime.datetime):
            undoable_until = self.undoable_until.isoformat()
        else:
            undoable_until = self.undoable_until

        undoable = self.undoable

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "driveId": drive_id,
                "kind": kind,
                "state": state,
            }
        )
        if done is not UNSET:
            field_dict["done"] = done
        if total is not UNSET:
            field_dict["total"] = total
        if bytes_ is not UNSET:
            field_dict["bytes"] = bytes_
        if skipped is not UNSET:
            field_dict["skipped"] = skipped
        if conflicts is not UNSET:
            field_dict["conflicts"] = conflicts
        if errors is not UNSET:
            field_dict["errors"] = errors
        if result_node_id is not UNSET:
            field_dict["resultNodeId"] = result_node_id
        if result_version_id is not UNSET:
            field_dict["resultVersionId"] = result_version_id
        if result_unchanged is not UNSET:
            field_dict["resultUnchanged"] = result_unchanged
        if result_url is not UNSET:
            field_dict["resultUrl"] = result_url
        if result_url_expires_at is not UNSET:
            field_dict["resultUrlExpiresAt"] = result_url_expires_at
        if cancel_requested is not UNSET:
            field_dict["cancelRequested"] = cancel_requested
        if undoable_until is not UNSET:
            field_dict["undoableUntil"] = undoable_until
        if undoable is not UNSET:
            field_dict["undoable"] = undoable

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.operation_conflict_wire import OperationConflictWire
        from ..models.operation_error_wire import OperationErrorWire

        d = dict(src_dict)
        id = d.pop("id")

        drive_id = d.pop("driveId")

        kind = d.pop("kind")

        state = d.pop("state")

        done = d.pop("done", UNSET)

        def _parse_total(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        total = _parse_total(d.pop("total", UNSET))

        bytes_ = d.pop("bytes", UNSET)

        skipped = d.pop("skipped", UNSET)

        _conflicts = d.pop("conflicts", UNSET)
        conflicts: list[OperationConflictWire] | Unset = UNSET
        if _conflicts is not UNSET:
            conflicts = []
            for conflicts_item_data in _conflicts:
                conflicts_item = OperationConflictWire.from_dict(conflicts_item_data)

                conflicts.append(conflicts_item)

        _errors = d.pop("errors", UNSET)
        errors: list[OperationErrorWire] | Unset = UNSET
        if _errors is not UNSET:
            errors = []
            for errors_item_data in _errors:
                errors_item = OperationErrorWire.from_dict(errors_item_data)

                errors.append(errors_item)

        def _parse_result_node_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        result_node_id = _parse_result_node_id(d.pop("resultNodeId", UNSET))

        def _parse_result_version_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        result_version_id = _parse_result_version_id(d.pop("resultVersionId", UNSET))

        result_unchanged = d.pop("resultUnchanged", UNSET)

        def _parse_result_url(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        result_url = _parse_result_url(d.pop("resultUrl", UNSET))

        def _parse_result_url_expires_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                result_url_expires_at_type_0 = datetime.datetime.fromisoformat(data)

                return result_url_expires_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        result_url_expires_at = _parse_result_url_expires_at(d.pop("resultUrlExpiresAt", UNSET))

        cancel_requested = d.pop("cancelRequested", UNSET)

        def _parse_undoable_until(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                undoable_until_type_0 = datetime.datetime.fromisoformat(data)

                return undoable_until_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        undoable_until = _parse_undoable_until(d.pop("undoableUntil", UNSET))

        undoable = d.pop("undoable", UNSET)

        operation_wire = cls(
            id=id,
            drive_id=drive_id,
            kind=kind,
            state=state,
            done=done,
            total=total,
            bytes_=bytes_,
            skipped=skipped,
            conflicts=conflicts,
            errors=errors,
            result_node_id=result_node_id,
            result_version_id=result_version_id,
            result_unchanged=result_unchanged,
            result_url=result_url,
            result_url_expires_at=result_url_expires_at,
            cancel_requested=cancel_requested,
            undoable_until=undoable_until,
            undoable=undoable,
        )

        operation_wire.additional_properties = d
        return operation_wire

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
