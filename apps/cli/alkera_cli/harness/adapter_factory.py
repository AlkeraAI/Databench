"""Builds the harness adapter a chat runs on: pluggable, so tests substitute a fake."""

from __future__ import annotations

from alkera_cli.harness.adapter import HarnessAdapter, HarnessUnavailableError, SessionConfig
from alkera_cli.harness.adapters.claude_agent import ClaudeAgentAdapter
from alkera_cli.harness.adapters.opencode_http import OpencodeHttpAdapter
from alkera_cli.harness.claude_binary import ClaudeBinaryNotFoundError, resolve_claude_binary
from alkera_cli.harness.event_bus import EventBus
from alkera_cli.harness.opencode_binary import (
    OpencodeBinaryNotFoundError,
    ResolvedOpencodeBinary,
    resolve_opencode_binary,
)
from alkera_cli.harness.sandbox_processes import SandboxProbes


def unsupported_harness_message(harness_type: str) -> str:
    return (
        f"No harness adapter registered for type={harness_type!r}. "
        "This chat was created with a harness type this build doesn't "
        "support — upgrade alkera-cli."
    )


class AdapterFactory:
    """Builds `HarnessAdapter` instances on demand.

    The factory dispatches on the chat manifest's ``harness_type``
    field — which is set at chat creation time and IMMUTABLE for the
    lifetime of that chat. Two adapters implement the contract: a chat
    created with ``harness_type="agent"`` always resumes on the opencode
    adapter, and one created with ``"claude-agent"`` on the Claude Code
    adapter. The slugs are stored in every chat manifest, so they never
    change (``harness.registry`` maps friendly names onto them).

    Adding a new
    harness means registering a builder here AND extending the typed
    values in `ChatManifest.harness_type`'s docstring + tests. The
    chat schema itself uses `str` (not `Literal`) so unknown harness
    types from a future writer don't break old readers — but old
    readers won't know how to spawn the new harness, so they'll
    raise a clear error at open time.

    ``probes`` is where each agent server it builds records how to read its
    sandbox's processes, by session id; the composition that builds a box
    hands the same registry to the service that decides sleeps.

    Tests override with `_FakeAdapterFactory`.
    """

    def __init__(
        self,
        *,
        binary: ResolvedOpencodeBinary | None = None,
        probes: SandboxProbes | None = None,
    ) -> None:
        self._binary = binary
        self._probes = probes

    @property
    def probes(self) -> SandboxProbes | None:
        """The registry the agent servers this factory builds record into."""
        return self._probes

    def __call__(
        self,
        config: SessionConfig,
        *,
        bus: EventBus,
        harness_type: str = "agent",
    ) -> HarnessAdapter:
        if harness_type == "agent":
            try:
                binary = self._binary or resolve_opencode_binary()
            except OpencodeBinaryNotFoundError as exc:
                # Translate the concrete-adapter error into the generic
                # IR-level one so callers (CLI / daemon) stay
                # harness-agnostic.
                raise HarnessUnavailableError(str(exc)) from exc
            return OpencodeHttpAdapter(config, binary=binary, event_bus=bus, probes=self._probes)
        if harness_type == "claude-agent":
            # The Claude binary must be installed LOCALLY (we never ship it).
            # Translate "not installed" into the generic IR-level error so callers
            # stay harness-agnostic. (UIs should gate on `is_available` first.)
            try:
                claude_bin = resolve_claude_binary()
            except ClaudeBinaryNotFoundError as exc:
                raise HarnessUnavailableError(str(exc)) from exc
            return ClaudeAgentAdapter(config, binary=claude_bin, event_bus=bus)
        raise HarnessUnavailableError(unsupported_harness_message(harness_type))

    def is_available(self, harness_type: str = "agent") -> bool:
        """Whether the harness for ``harness_type`` can run on this machine (its
        binary is installed/discoverable). Lets the CLI/daemon/UI gate harness
        selection before creating a chat. Unknown types → False."""
        if harness_type == "agent":
            return OpencodeHttpAdapter.is_available()
        if harness_type == "claude-agent":
            return ClaudeAgentAdapter.is_available()
        return False


__all__ = ["AdapterFactory", "unsupported_harness_message"]
