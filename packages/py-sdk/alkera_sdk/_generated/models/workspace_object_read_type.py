from enum import StrEnum


class WorkspaceObjectReadType(StrEnum):
    APP = "app"
    BOARD = "board"
    CHAT = "chat"
    CHAT_TEMPLATE = "chat_template"
    QUERY = "query"
    REPORT = "report"
    RESULT = "result"
    WORKSPACE = "workspace"

    def __str__(self) -> str:
        return str(self.value)
