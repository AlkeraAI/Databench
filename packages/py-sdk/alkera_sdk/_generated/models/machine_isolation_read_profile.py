from enum import StrEnum


class MachineIsolationReadProfile(StrEnum):
    ORG_NAMESPACES = "org_namespaces"
    SINGLE_ORG = "single_org"

    def __str__(self) -> str:
        return str(self.value)
