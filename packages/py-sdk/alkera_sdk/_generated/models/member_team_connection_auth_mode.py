from enum import StrEnum


class MemberTeamConnectionAuthMode(StrEnum):
    PER_USER = "per_user"
    SHARED = "shared"

    def __str__(self) -> str:
        return str(self.value)
