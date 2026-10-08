from enum import StrEnum


class SourcesAdditionalProperty(StrEnum):
    DEFAULT = "default"
    DETECTED = "detected"
    NOTEBOOK = "notebook"
    WORKSPACE = "workspace"

    def __str__(self) -> str:
        return str(self.value)
