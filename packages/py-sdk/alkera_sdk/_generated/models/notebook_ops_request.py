from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.delete_cell import DeleteCell
    from ..models.edit_cell import EditCell
    from ..models.insert_cell import InsertCell
    from ..models.move_cell import MoveCell
    from ..models.replace_cell import ReplaceCell
    from ..models.restore_cell import RestoreCell
    from ..models.set_cell_config import SetCellConfig
    from ..models.set_cell_kind import SetCellKind
    from ..models.set_cell_meta import SetCellMeta
    from ..models.set_cell_name import SetCellName
    from ..models.set_setting import SetSetting


T = TypeVar("T", bound="NotebookOpsRequest")


@_attrs_define
class NotebookOpsRequest:
    """``POST /api/v1/notebooks/{drive_id}/{item_id}/ops``: a batch applied
    atomically on a Loro peer at ``base_token`` (the head when absent).

        Attributes:
            ops (list[DeleteCell | EditCell | InsertCell | MoveCell | ReplaceCell | RestoreCell | SetCellConfig |
                SetCellKind | SetCellMeta | SetCellName | SetSetting]):
            base_token (None | str | Unset):
            submit_id (None | str | Unset):
            agent_chat_id (None | str | Unset):
    """

    ops: list[
        DeleteCell
        | EditCell
        | InsertCell
        | MoveCell
        | ReplaceCell
        | RestoreCell
        | SetCellConfig
        | SetCellKind
        | SetCellMeta
        | SetCellName
        | SetSetting
    ]
    base_token: None | str | Unset = UNSET
    submit_id: None | str | Unset = UNSET
    agent_chat_id: None | str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        from ..models.delete_cell import DeleteCell
        from ..models.edit_cell import EditCell
        from ..models.insert_cell import InsertCell
        from ..models.move_cell import MoveCell
        from ..models.replace_cell import ReplaceCell
        from ..models.restore_cell import RestoreCell
        from ..models.set_cell_config import SetCellConfig
        from ..models.set_cell_kind import SetCellKind
        from ..models.set_cell_meta import SetCellMeta
        from ..models.set_cell_name import SetCellName

        ops = []
        for ops_item_data in self.ops:
            ops_item: dict[str, Any]
            if isinstance(ops_item_data, InsertCell):
                ops_item = ops_item_data.to_dict()
            elif isinstance(ops_item_data, EditCell):
                ops_item = ops_item_data.to_dict()
            elif isinstance(ops_item_data, ReplaceCell):
                ops_item = ops_item_data.to_dict()
            elif isinstance(ops_item_data, DeleteCell):
                ops_item = ops_item_data.to_dict()
            elif isinstance(ops_item_data, RestoreCell):
                ops_item = ops_item_data.to_dict()
            elif isinstance(ops_item_data, MoveCell):
                ops_item = ops_item_data.to_dict()
            elif isinstance(ops_item_data, SetCellName):
                ops_item = ops_item_data.to_dict()
            elif isinstance(ops_item_data, SetCellKind):
                ops_item = ops_item_data.to_dict()
            elif isinstance(ops_item_data, SetCellConfig):
                ops_item = ops_item_data.to_dict()
            elif isinstance(ops_item_data, SetCellMeta):
                ops_item = ops_item_data.to_dict()
            else:
                ops_item = ops_item_data.to_dict()

            ops.append(ops_item)

        base_token: None | str | Unset
        if isinstance(self.base_token, Unset):
            base_token = UNSET
        else:
            base_token = self.base_token

        submit_id: None | str | Unset
        if isinstance(self.submit_id, Unset):
            submit_id = UNSET
        else:
            submit_id = self.submit_id

        agent_chat_id: None | str | Unset
        if isinstance(self.agent_chat_id, Unset):
            agent_chat_id = UNSET
        else:
            agent_chat_id = self.agent_chat_id

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "ops": ops,
            }
        )
        if base_token is not UNSET:
            field_dict["base_token"] = base_token
        if submit_id is not UNSET:
            field_dict["submit_id"] = submit_id
        if agent_chat_id is not UNSET:
            field_dict["agent_chat_id"] = agent_chat_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.delete_cell import DeleteCell
        from ..models.edit_cell import EditCell
        from ..models.insert_cell import InsertCell
        from ..models.move_cell import MoveCell
        from ..models.replace_cell import ReplaceCell
        from ..models.restore_cell import RestoreCell
        from ..models.set_cell_config import SetCellConfig
        from ..models.set_cell_kind import SetCellKind
        from ..models.set_cell_meta import SetCellMeta
        from ..models.set_cell_name import SetCellName
        from ..models.set_setting import SetSetting

        d = dict(src_dict)
        ops = []
        _ops = d.pop("ops")
        for ops_item_data in _ops:

            def _parse_ops_item(
                data: object,
            ) -> (
                DeleteCell
                | EditCell
                | InsertCell
                | MoveCell
                | ReplaceCell
                | RestoreCell
                | SetCellConfig
                | SetCellKind
                | SetCellMeta
                | SetCellName
                | SetSetting
            ):
                try:
                    if not isinstance(data, dict):
                        raise TypeError()
                    ops_item_type_0 = InsertCell.from_dict(data)

                    return ops_item_type_0
                except (TypeError, ValueError, AttributeError, KeyError):
                    pass
                try:
                    if not isinstance(data, dict):
                        raise TypeError()
                    ops_item_type_1 = EditCell.from_dict(data)

                    return ops_item_type_1
                except (TypeError, ValueError, AttributeError, KeyError):
                    pass
                try:
                    if not isinstance(data, dict):
                        raise TypeError()
                    ops_item_type_2 = ReplaceCell.from_dict(data)

                    return ops_item_type_2
                except (TypeError, ValueError, AttributeError, KeyError):
                    pass
                try:
                    if not isinstance(data, dict):
                        raise TypeError()
                    ops_item_type_3 = DeleteCell.from_dict(data)

                    return ops_item_type_3
                except (TypeError, ValueError, AttributeError, KeyError):
                    pass
                try:
                    if not isinstance(data, dict):
                        raise TypeError()
                    ops_item_type_4 = RestoreCell.from_dict(data)

                    return ops_item_type_4
                except (TypeError, ValueError, AttributeError, KeyError):
                    pass
                try:
                    if not isinstance(data, dict):
                        raise TypeError()
                    ops_item_type_5 = MoveCell.from_dict(data)

                    return ops_item_type_5
                except (TypeError, ValueError, AttributeError, KeyError):
                    pass
                try:
                    if not isinstance(data, dict):
                        raise TypeError()
                    ops_item_type_6 = SetCellName.from_dict(data)

                    return ops_item_type_6
                except (TypeError, ValueError, AttributeError, KeyError):
                    pass
                try:
                    if not isinstance(data, dict):
                        raise TypeError()
                    ops_item_type_7 = SetCellKind.from_dict(data)

                    return ops_item_type_7
                except (TypeError, ValueError, AttributeError, KeyError):
                    pass
                try:
                    if not isinstance(data, dict):
                        raise TypeError()
                    ops_item_type_8 = SetCellConfig.from_dict(data)

                    return ops_item_type_8
                except (TypeError, ValueError, AttributeError, KeyError):
                    pass
                try:
                    if not isinstance(data, dict):
                        raise TypeError()
                    ops_item_type_9 = SetCellMeta.from_dict(data)

                    return ops_item_type_9
                except (TypeError, ValueError, AttributeError, KeyError):
                    pass
                if not isinstance(data, dict):
                    raise TypeError()
                ops_item_type_10 = SetSetting.from_dict(data)

                return ops_item_type_10

            ops_item = _parse_ops_item(ops_item_data)

            ops.append(ops_item)

        def _parse_base_token(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        base_token = _parse_base_token(d.pop("base_token", UNSET))

        def _parse_submit_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        submit_id = _parse_submit_id(d.pop("submit_id", UNSET))

        def _parse_agent_chat_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        agent_chat_id = _parse_agent_chat_id(d.pop("agent_chat_id", UNSET))

        notebook_ops_request = cls(
            ops=ops,
            base_token=base_token,
            submit_id=submit_id,
            agent_chat_id=agent_chat_id,
        )

        return notebook_ops_request
