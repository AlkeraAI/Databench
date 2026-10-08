"""The server and the web build agree on the realtime client generation.

The server names the oldest client generation it serves in ``welcome``; a web
tab compiled with a lower one reloads into the current build. If the two
constants drifted apart, either every tab would reload on every connect (web
behind the server) or tabs built before a protocol change would keep speaking
it (server bumped, web not). They move together, and this pins it.
"""

from __future__ import annotations

import re
from pathlib import Path

from alkera_core.schemas.realtime import REALTIME_CLIENT_GENERATION, WelcomeFrame

WEB_CONSTANT = (
    Path(__file__).resolve().parents[5]
    / "apps"
    / "web"
    / "src"
    / "api"
    / "realtime"
    / "clientGeneration.ts"
)


def test_the_web_build_compiles_in_the_servers_generation() -> None:
    source = WEB_CONSTANT.read_text(encoding="utf-8")
    found = re.findall(r"export const REALTIME_CLIENT_GENERATION = (\d+);", source)
    assert found == [str(REALTIME_CLIENT_GENERATION)], (found, REALTIME_CLIENT_GENERATION)


def test_a_welcome_from_a_server_that_predates_the_field_reads_as_generation_zero() -> None:
    welcome = WelcomeFrame.model_validate(
        {"peer_id": "p:1", "server_time": "2026-09-05T12:00:00Z", "instance": "i1"}
    )
    assert welcome.min_client_generation == 0
