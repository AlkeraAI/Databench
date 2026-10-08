from enum import StrEnum


class SshMachineAddUseMode(StrEnum):
    ASSIGNED = "assigned"
    POOL = "pool"

    def __str__(self) -> str:
        return str(self.value)
