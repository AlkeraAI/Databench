# Copyright 2026 Marimo. All rights reserved.
# Modified by Alkera: import paths rewritten; see vendor/marimo/README.alkera.md
from __future__ import annotations

from dataclasses import dataclass

from alkera_notebook._marimo._utils.config.config import ConfigReader


@dataclass
class MarimoCLIState:
    latest_version: str | None = None
    last_checked_at: str | None = None
    accepted_text_to_notebook_terms_at: str | None = None
    notices: list[str] | None = None


FILE_NAME = "state.toml"


def get_cli_state() -> MarimoCLIState:
    config_reader = ConfigReader.for_filename(FILE_NAME)
    # Load the state file or create a default state if it doesn't exist
    state = config_reader.read_toml(MarimoCLIState, fallback=MarimoCLIState())
    return state


def write_cli_state(state: MarimoCLIState) -> None:
    config_reader = ConfigReader.for_filename(FILE_NAME)
    config_reader.write_toml(state)
