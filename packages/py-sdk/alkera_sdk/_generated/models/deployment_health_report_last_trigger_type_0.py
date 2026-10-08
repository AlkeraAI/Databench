from enum import StrEnum


class DeploymentHealthReportLastTriggerType0(StrEnum):
    MANUAL = "manual"
    SCHEDULED = "scheduled"

    def __str__(self) -> str:
        return str(self.value)
