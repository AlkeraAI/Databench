from enum import StrEnum


class RealtimeProtocolDescriptorDocTypesItem(StrEnum):
    ARTIFACT = "artifact"
    CHAT = "chat"
    CHAT_DRAFT = "chat_draft"
    CHAT_WORKSPACE = "chat_workspace"
    FILE = "file"
    NOTEBOOK = "notebook"

    def __str__(self) -> str:
        return str(self.value)
