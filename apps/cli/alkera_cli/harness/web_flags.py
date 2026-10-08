"""Which local web tools a runtime may register.

Its own module, with no imports: the provider that FILLS it
(:mod:`alkera_cli.harness.org_flags`) reads the gateway catalog, and the
gateway client imports the harness package — so a runtime that imported the
shape from the provider would close an import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WebToolFlags:
    """Which local web tools may be registered for this org, this deployment.

    Two flags rather than one because the deployment's ``web.fetch`` kill switch
    subtracts from the org toggle independently: an install may keep the key-less
    metasearch and refuse the tool that pulls an arbitrary URL. Both default
    off — a runtime with no gateway (tests, offline commands, a signed-out
    editor) registers neither."""

    search: bool = False
    fetch: bool = False


__all__ = ["WebToolFlags"]
