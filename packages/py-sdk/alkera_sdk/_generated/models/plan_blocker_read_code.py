from enum import StrEnum


class PlanBlockerReadCode(StrEnum):
    LAST_ADMIN = "last_admin"
    LEGAL_HOLD = "legal_hold"
    LIVE_COMPUTE = "live_compute"
    ORG_MACHINES = "org_machines"
    PAID_PLAN = "paid_plan"
    PLATFORM_STAFF = "platform_staff"
    UNPAID_BALANCE = "unpaid_balance"

    def __str__(self) -> str:
        return str(self.value)
