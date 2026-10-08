from enum import StrEnum


class MachineBuyingReadReasonType0(StrEnum):
    PLAN = "plan"
    QUOTA = "quota"

    def __str__(self) -> str:
        return str(self.value)
