from enum import StrEnum


class MachineHeartbeatRequestSandboxType0(StrEnum):
    GVISOR = "gvisor"
    NONE = "none"

    def __str__(self) -> str:
        return str(self.value)
