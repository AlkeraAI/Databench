from enum import StrEnum


class OrgMachineDetailAcquisition(StrEnum):
    ADDED = "added"
    GRANTED = "granted"
    PURCHASED = "purchased"

    def __str__(self) -> str:
        return str(self.value)
