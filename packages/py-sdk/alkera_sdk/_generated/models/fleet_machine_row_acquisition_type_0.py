from enum import StrEnum


class FleetMachineRowAcquisitionType0(StrEnum):
    ADDED = "added"
    GRANTED = "granted"
    PURCHASED = "purchased"

    def __str__(self) -> str:
        return str(self.value)
