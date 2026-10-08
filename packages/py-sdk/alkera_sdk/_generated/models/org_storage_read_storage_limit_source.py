from enum import StrEnum


class OrgStorageReadStorageLimitSource(StrEnum):
    DEFAULT = "default"
    OVERRIDE = "override"
    PLAN = "plan"

    def __str__(self) -> str:
        return str(self.value)
