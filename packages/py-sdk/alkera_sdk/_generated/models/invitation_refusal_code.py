from enum import StrEnum


class InvitationRefusalCode(StrEnum):
    EMAIL_VERIFICATION_REQUIRED = "email_verification_required"
    MEMBERSHIP_DEACTIVATED = "membership_deactivated"
    OTHER_ORG = "other_org"

    def __str__(self) -> str:
        return str(self.value)
