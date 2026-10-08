from enum import StrEnum


class SettingsDataframe(StrEnum):
    AUTO = "auto"
    PANDAS = "pandas"
    POLARS = "polars"

    def __str__(self) -> str:
        return str(self.value)
