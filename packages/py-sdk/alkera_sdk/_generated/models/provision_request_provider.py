from enum import StrEnum


class ProvisionRequestProvider(StrEnum):
    EC2 = "ec2"
    LOCALDEV = "localdev"
    RUNPOD = "runpod"

    def __str__(self) -> str:
        return str(self.value)
