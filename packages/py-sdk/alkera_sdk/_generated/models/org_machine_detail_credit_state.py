from enum import StrEnum


class OrgMachineDetailCreditState(StrEnum):
    DRAINING = "draining"
    LOW = "low"
    OK = "ok"
    STOPPED = "stopped"
    URGENT = "urgent"

    def __str__(self) -> str:
        return str(self.value)
