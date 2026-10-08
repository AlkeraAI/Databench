from enum import StrEnum


class AdminCreatedUserReadOrgRole(StrEnum):
    ADMIN = "admin"
    MEMBER = "member"

    def __str__(self) -> str:
        return str(self.value)
