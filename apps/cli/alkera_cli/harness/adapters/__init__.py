"""Concrete `HarnessAdapter` implementations.

One module per harness. Each adapter's job is to translate its native
protocol into the IR (`alkera_core.schemas.chat.Event`).

opencode (https://github.com/sst/opencode, MIT, vendored under
`vendor/opencode/`) is one implementation of the `HarnessAdapter` contract
(`opencode_http.py`, the default, `harness_type="agent"`); Claude Code
(`claude_agent.py`) is another.
"""

from __future__ import annotations
