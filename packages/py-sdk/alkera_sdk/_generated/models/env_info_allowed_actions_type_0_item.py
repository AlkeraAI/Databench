from enum import StrEnum


class EnvInfoAllowedActionsType0Item(StrEnum):
    BUILD = "build"
    CANCEL = "cancel"
    INSTALL = "install"
    REMOVE = "remove"

    def __str__(self) -> str:
        return str(self.value)
