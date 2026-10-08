from enum import StrEnum


class CredentialState(StrEnum):
    ABSENT = "absent"
    EXPIRED = "expired"
    EXPIRING = "expiring"
    NEEDS_REAUTH = "needs_reauth"
    PRESENT = "present"
    REFRESHING = "refreshing"
    REVOKED = "revoked"
    UNREADABLE = "unreadable"

    def __str__(self) -> str:
        return str(self.value)
