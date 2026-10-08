from enum import StrEnum


class MachineCardStepType0(StrEnum):
    BOOTING = "booting"
    CONNECTING = "connecting"
    INSTALLING = "installing"
    RESERVING = "reserving"

    def __str__(self) -> str:
        return str(self.value)
