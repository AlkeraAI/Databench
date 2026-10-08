from enum import StrEnum


class ChangeEnvironmentApiV1NotebooksDriveIdItemIdEnvActionPostAction(StrEnum):
    BUILD = "build"
    CANCEL = "cancel"
    REMOVE = "remove"

    def __str__(self) -> str:
        return str(self.value)
