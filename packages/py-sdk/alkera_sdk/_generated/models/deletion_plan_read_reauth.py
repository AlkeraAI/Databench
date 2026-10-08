from enum import StrEnum


class DeletionPlanReadReauth(StrEnum):
    PASSWORD = "password"
    RECENT_SIGN_IN = "recent_sign_in"

    def __str__(self) -> str:
        return str(self.value)
