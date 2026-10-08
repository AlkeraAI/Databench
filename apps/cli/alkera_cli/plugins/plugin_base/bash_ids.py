"""Constants for the parent-hosted ``bash`` tool — the truncation limits and the
kill grace are OpenCode's, so a shortened result reads the same either way; the
default command budget is Alkera's own and is carried to OpenCode's native shell
by the adapter, so the two never disagree about how long a command may run.

Kept in their own module so the executor, the tool, and the prompt all share one
source of truth (and a test can pin them).
"""

from __future__ import annotations

import os

from alkera_cli.host.limits import env_count, env_seconds

#: The model-facing tool name. Matches OpenCode's ``ShellID.ToolID`` so a model
#: trained on either calls the same tool.
BASH_TOOL_NAME = "bash"

#: Output truncation thresholds — ported from OpenCode's ``tool/truncate.ts``.
#: Output beyond EITHER is tailed for the model and the full text spilled to a file.
#: Nothing is lost when they bite (the spill file holds the whole output), so what
#: they really choose is how much of a long build log the model reads without a
#: second call. 0 on either removes that half of the threshold; the other still
#: applies, and a command that prints without end is still bounded by the rolling
#: window below.
ENV_BASH_MAX_LINES = "ALKERA_BASH_MAX_LINES"
ENV_BASH_MAX_BYTES = "ALKERA_BASH_MAX_BYTES"
MAX_LINES = env_count(os.environ.get(ENV_BASH_MAX_LINES), default=2000)
_MAX_BYTES_DEFAULT = 50 * 1024  # 51200
MAX_BYTES = env_count(os.environ.get(ENV_BASH_MAX_BYTES), default=_MAX_BYTES_DEFAULT)

#: The rolling in-memory window keeps ~2x maxBytes so the final tail always has
#: enough material (OpenCode's ``keep = maxBytes * 2``). Derived, never set: a
#: window smaller than the tail it feeds would truncate the tail itself. With the
#: byte threshold removed it falls back to the shipped default, because the
#: window is what keeps an endless writer out of memory.
KEEP_BYTES = (MAX_BYTES or _MAX_BYTES_DEFAULT) * 2

#: A timed-out / aborted command is sent SIGTERM then SIGKILL after this grace —
#: OpenCode's ``forceKillAfter: "3 seconds"``. A command that traps SIGTERM to
#: flush state (a container build, a database client) may want longer; 0 waits for
#: it to exit on its own and never escalates.
ENV_BASH_FORCE_KILL = "ALKERA_BASH_FORCE_KILL_SECONDS"
FORCE_KILL_SECONDS = env_seconds(os.environ.get(ENV_BASH_FORCE_KILL), default=3.0)

#: The wall clock a command runs under when the model names no timeout: none.
#: A command is never killed for taking long. A build, a test suite, a training
#: run or a migration may run for hours or days, and a command killed part-way
#: costs the turn everything it had done. What ends a command nobody bounded is
#: evidence that it is stuck (see ``STUCK_SECONDS``). An operator who wants a
#: wall clock anyway sets a positive value here; a model that wants one for a
#: command passes its own ``timeout``.
ENV_BASH_DEFAULT_TIMEOUT_MS = "ALKERA_BASH_DEFAULT_TIMEOUT_MS"
DEFAULT_TIMEOUT_MS = env_count(os.environ.get(ENV_BASH_DEFAULT_TIMEOUT_MS), default=None)

#: How long a command with no wall clock may go without making progress before
#: it is ended as stuck. Progress is new output, or CPU time used by the
#: command's processes. A quiet compile or a silent training loop uses CPU and
#: runs on; a deadlock, a hung network read or a prompt nobody will answer does
#: neither. 0 never ends a command for being stuck.
ENV_BASH_STUCK_SECONDS = "ALKERA_BASH_STUCK_SECONDS"
STUCK_SECONDS = env_seconds(os.environ.get(ENV_BASH_STUCK_SECONDS), default=30 * 60.0)

#: How often a command with no wall clock is checked for progress. The cadence
#: of the check, not a bound.
STUCK_CHECK_SECONDS = 15.0

#: The largest value opencode's native shell accepts for its default timeout
#: (a 32-bit millisecond count, about 24.8 days). It stands for "no wall clock"
#: on the platform whose shell is opencode's own, which has no unbounded value.
NATIVE_SHELL_NEVER_MS = 2_147_483_647


__all__ = [
    "BASH_TOOL_NAME",
    "DEFAULT_TIMEOUT_MS",
    "ENV_BASH_DEFAULT_TIMEOUT_MS",
    "ENV_BASH_FORCE_KILL",
    "ENV_BASH_MAX_BYTES",
    "ENV_BASH_MAX_LINES",
    "FORCE_KILL_SECONDS",
    "KEEP_BYTES",
    "MAX_BYTES",
    "MAX_LINES",
]
