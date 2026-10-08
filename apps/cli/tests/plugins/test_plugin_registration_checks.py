"""A distribution's registration checks see every plugin's declarations as
discovery records them, and a check that refuses keeps the plugin out."""

from __future__ import annotations

from pathlib import Path

import pytest
from alkera_cli.host.paths import project_directory
from alkera_cli.plugins.plugin_base.plugin import CLI_PLUGINS
from alkera_cli.plugins.plugin_base.registration import PLUGIN_REGISTRATION_CHECKS
from alkera_cli.plugins.plugin_base.registry import PluginRegistry


@pytest.mark.asyncio
async def test_every_discovered_plugin_passes_the_registered_checks(tmp_path: Path) -> None:
    """The product registers the knowledge provenance check; every bundled
    plugin that writes knowledge declares its provenance, so discovery admits
    them all."""
    assert PLUGIN_REGISTRATION_CHECKS.items(), "the product registers a check"
    registry = PluginRegistry(project_directory(tmp_path), tmp_path)
    await registry.discover()
    assert len(registry.plugin_snapshot()) == len(CLI_PLUGINS.items())
