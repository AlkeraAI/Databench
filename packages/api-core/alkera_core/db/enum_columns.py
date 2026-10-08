"""Shared column helpers for ORM models."""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import Enum as SAEnum


def enum_type(enum_cls: type[StrEnum], name: str, *, length: int = 32) -> SAEnum:
    """A non-native (VARCHAR + CHECK) enum column storing the StrEnum's
    lowercase values — matches the tenant-model convention in `_enums.py`.
    """
    return SAEnum(
        enum_cls,
        native_enum=False,
        length=length,
        name=name,
        values_callable=lambda e: [m.value for m in e],
    )
