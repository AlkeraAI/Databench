"""The per-project plugin on/off toggle (PLUGINS — the Plugins & Connections UI).

Plugins self-activate when their signal is present and are ON by default. This store
records a user's explicit DISABLE so a disabled plugin's tools / lineage / context
drop off the agent's surface (and ``on_disable`` runs). It persists the set of
disabled plugin NAMES at ``.alkera/plugins_enabled.json`` — recording the negative
(disabled) set, so a newly-discovered plugin is enabled by default without a write.
The connection-level allow-list (``connection.remove``) stays the finer lever.
"""

from __future__ import annotations

import json
from pathlib import Path

from alkera_core.atomic_io import write_json_atomic


class PluginEnablementStore:
    """Reads/writes the disabled-plugin set. Toggling is a rare, human-driven op — a
    last-writer-wins atomic file is sufficient; each write is torn-free."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    def disabled(self) -> set[str]:
        try:
            data = json.loads(self._path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return set()
        return {str(n) for n in data.get("disabled", [])}

    def is_enabled(self, name: str) -> bool:
        """A plugin is enabled unless it was explicitly disabled (default-on)."""
        return name not in self.disabled()

    def disable(self, name: str) -> None:
        current = self.disabled()
        if name not in current:
            self._save(current | {name})

    def enable(self, name: str) -> None:
        current = self.disabled()
        if name in current:
            self._save(current - {name})

    def _save(self, disabled: set[str]) -> None:
        write_json_atomic(self._path, {"disabled": sorted(disabled)})


__all__ = ["PluginEnablementStore"]
