from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.kernel_info_reactivity import KernelInfoReactivity
from ..models.kernel_info_state import KernelInfoState
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.env_info import EnvInfo
    from ..models.queued_run import QueuedRun


T = TypeVar("T", bound="KernelInfo")


@_attrs_define
class KernelInfo:
    """
    Attributes:
        state (KernelInfoState):
        env (EnvInfo | None):
        reactivity (KernelInfoReactivity):
        memory_bytes (int | None | Unset):
        started_at (datetime.datetime | None | Unset):
        queue (list[QueuedRun] | Unset):
        kernel_id (None | str | Unset):
        seq (int | None | Unset):
        env_outdated (bool | Unset):  Default: False.
    """

    state: KernelInfoState
    env: EnvInfo | None
    reactivity: KernelInfoReactivity
    memory_bytes: int | None | Unset = UNSET
    started_at: datetime.datetime | None | Unset = UNSET
    queue: list[QueuedRun] | Unset = UNSET
    kernel_id: None | str | Unset = UNSET
    seq: int | None | Unset = UNSET
    env_outdated: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.env_info import EnvInfo

        state = self.state.value

        env: dict[str, Any] | None
        if isinstance(self.env, EnvInfo):
            env = self.env.to_dict()
        else:
            env = self.env

        reactivity = self.reactivity.value

        memory_bytes: int | None | Unset
        if isinstance(self.memory_bytes, Unset):
            memory_bytes = UNSET
        else:
            memory_bytes = self.memory_bytes

        started_at: None | str | Unset
        if isinstance(self.started_at, Unset):
            started_at = UNSET
        elif isinstance(self.started_at, datetime.datetime):
            started_at = self.started_at.isoformat()
        else:
            started_at = self.started_at

        queue: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.queue, Unset):
            queue = []
            for queue_item_data in self.queue:
                queue_item = queue_item_data.to_dict()
                queue.append(queue_item)

        kernel_id: None | str | Unset
        if isinstance(self.kernel_id, Unset):
            kernel_id = UNSET
        else:
            kernel_id = self.kernel_id

        seq: int | None | Unset
        if isinstance(self.seq, Unset):
            seq = UNSET
        else:
            seq = self.seq

        env_outdated = self.env_outdated

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "state": state,
                "env": env,
                "reactivity": reactivity,
            }
        )
        if memory_bytes is not UNSET:
            field_dict["memory_bytes"] = memory_bytes
        if started_at is not UNSET:
            field_dict["started_at"] = started_at
        if queue is not UNSET:
            field_dict["queue"] = queue
        if kernel_id is not UNSET:
            field_dict["kernel_id"] = kernel_id
        if seq is not UNSET:
            field_dict["seq"] = seq
        if env_outdated is not UNSET:
            field_dict["env_outdated"] = env_outdated

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.env_info import EnvInfo
        from ..models.queued_run import QueuedRun

        d = dict(src_dict)
        state = KernelInfoState(d.pop("state"))

        def _parse_env(data: object) -> EnvInfo | None:
            if data is None:
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                env_type_0 = EnvInfo.from_dict(data)

                return env_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(EnvInfo | None, data)

        env = _parse_env(d.pop("env"))

        reactivity = KernelInfoReactivity(d.pop("reactivity"))

        def _parse_memory_bytes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        memory_bytes = _parse_memory_bytes(d.pop("memory_bytes", UNSET))

        def _parse_started_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                started_at_type_0 = datetime.datetime.fromisoformat(data)

                return started_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        started_at = _parse_started_at(d.pop("started_at", UNSET))

        _queue = d.pop("queue", UNSET)
        queue: list[QueuedRun] | Unset = UNSET
        if _queue is not UNSET:
            queue = []
            for queue_item_data in _queue:
                queue_item = QueuedRun.from_dict(queue_item_data)

                queue.append(queue_item)

        def _parse_kernel_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        kernel_id = _parse_kernel_id(d.pop("kernel_id", UNSET))

        def _parse_seq(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        seq = _parse_seq(d.pop("seq", UNSET))

        env_outdated = d.pop("env_outdated", UNSET)

        kernel_info = cls(
            state=state,
            env=env,
            reactivity=reactivity,
            memory_bytes=memory_bytes,
            started_at=started_at,
            queue=queue,
            kernel_id=kernel_id,
            seq=seq,
            env_outdated=env_outdated,
        )

        kernel_info.additional_properties = d
        return kernel_info

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
