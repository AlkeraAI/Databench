from enum import StrEnum


class MachineCardKind(StrEnum):
    ORG_BOX = "org_box"
    ORG_MACHINE = "org_machine"
    PERSONAL = "personal"
    SHARED = "shared"

    def __str__(self) -> str:
        return str(self.value)
