from enum import StrEnum


class SettingsAutoreload(StrEnum):
    OFF = "off"
    ON = "on"

    def __str__(self) -> str:
        return str(self.value)
