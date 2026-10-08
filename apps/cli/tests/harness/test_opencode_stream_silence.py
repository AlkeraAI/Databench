"""A model step on a dead gateway connection ends; a long quiet one does not.

opencode waits on a streamed step with no bound of its own. A box-to-gateway
connection that went half-open (a NAT or load balancer dropped it without a
reset) then hangs the turn forever: the agent is alive, the box stamps the turn
as working, and nothing ever ends it. The gateway writes a keepalive comment
every 15 seconds while the provider is silent, so a step that hears no bytes at
all for five minutes is on a dead connection. Every gateway provider opencode
is pointed at carries that idle bound (``chunkTimeout``), and the settings
validator keeps the keepalive well inside it so a thinking step is never cut.
"""

from __future__ import annotations

from alkera_cli.contracts.gateway_model import GatewayModel
from alkera_cli.harness.adapters.opencode_alkera import build_alkera_opencode_config

CLAUDE = GatewayModel(id="claude-opus-5", display_name="Opus 5", wire="anthropic")
GPT = GatewayModel(id="gpt-5.5", display_name="GPT 5.5", wire="openai")


def test_every_gateway_provider_ends_a_step_that_hears_nothing_for_five_minutes() -> None:
    config = build_alkera_opencode_config(gateway_url="http://gw", token="t", models=[CLAUDE, GPT])

    providers = config["provider"]
    assert set(providers) == {"alkera-anthropic", "alkera-openai"}
    for provider in providers.values():
        assert provider["options"]["chunkTimeout"] == 300_000
        # An idle bound only: no wall clock on a step, which may think for hours.
        assert "timeout" not in provider["options"]
