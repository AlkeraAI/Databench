from enum import StrEnum


class MemberTeamConnectionSharedCustody(StrEnum):
    LEASE = "lease"
    VALUE_0 = ""

    def __str__(self) -> str:
        return str(self.value)
