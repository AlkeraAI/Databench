"""Concrete `HarnessAdapter` driving opencode via HTTP + SSE (the default harness).

One opencode subprocess per active chat, pointed at `<chat>/.runtime/` via `XDG_DATA_HOME`.

Lifecycle::

    adapter = OpencodeHttpAdapter(config, binary)
    await adapter.start()           # spawn + connect + SSE subscribe
    async for ev in adapter.subscribe(): ...
    await adapter.send_prompt(PromptInput(text="..."))
    # turn unfolds via events
    await adapter.cancel()           # POST /session/{sid}/abort + close
    await adapter.stop()             # SIGTERM + cleanup

Critical invariants enforced here:

1. **Streaming closure** — every text/reasoning part that opens with
   `PartStarted` MUST close with a `PartCreated` (full final state).
   On cancel or crash, the adapter SYNTHESIZES `PartCreated` for any
   still-open parts with whatever partial text has been buffered.
2. **Attributed terminals**: every `SessionStatusChanged` carries the
   transport attempt that produced it (`_TranslatorContext` attempt rules);
   a cancel or crash synthesizes one terminal for the attempt in flight.
3. **No SSE drops past reconnect** — on a transport blip the adapter
   reconnects with exponential backoff; if the harness has died,
   transitions to `error` state cleanly without leaving dangling
   open parts.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import secrets
import shutil
import socket
from collections import deque
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from alkera_core.gateway import (
    join_model_effort,
    split_model_effort,
    thinking_display_for_effort,
)
from alkera_core.process import (
    kill_process,
    process_alive,
    release,
    spawn_async,
    terminate_process,
)
from alkera_core.project import write_json_atomic, write_text_atomic
from alkera_core.project.directory import SANDBOX_SUBDIR
from alkera_core.schemas.chat import (
    ConversationCleared,
    Event,
    PartCreated,
    ReasoningPart,
    SessionStatusChanged,
    TextPart,
    ToolCallUpdate,
)
from httpx_sse import EventSource, aconnect_sse

from alkera_cli.files.chat_fs import ChatTree
from alkera_cli.harness.adapter import (
    HarnessAdapter,
    HarnessCrashError,
    HarnessGoneError,
    HarnessInfo,
    HarnessModelError,
    HarnessNotReadyError,
    HarnessSandboxRefusedError,
    HarnessStartError,
    HarnessStartRefusedError,
    HarnessStoreUnreadableError,
    PromptInput,
    SessionConfig,
)
from alkera_cli.harness.adapters import opencode_secrets
from alkera_cli.harness.adapters.opencode_sandbox import (
    SandboxPlan,
    agent_directory,
    agent_spelling,
    at_host,
    plan_sandbox,
)
from alkera_cli.harness.adapters.opencode_sessions import choose_session
from alkera_cli.harness.adapters.opencode_translate import (
    OpencodeEventTranslator,
    _OpenPart,
    _TranslatorContext,
)
from alkera_cli.harness.agent_root import agent_config_root, agent_config_roots_dir
from alkera_cli.harness.event_bus import EventBus
from alkera_cli.harness.mcp_server import WEB_MCP_MOUNT
from alkera_cli.harness.opencode_binary import ResolvedOpencodeBinary
from alkera_cli.harness.opencode_db import set_aside_agent_store
from alkera_cli.harness.orphan_sweep import (
    process_create_time,
    read_pid_breadcrumb,
    register_agent,
    same_process,
    unregister_agent,
    write_pid_breadcrumb,
)
from alkera_cli.harness.sandbox import (
    NO_SANDBOX,
    RUNTIME_STATE_SUBDIR,
    SIGKILL_EXIT_STATUSES,
    SandboxLaunch,
    SandboxRefusedError,
    SandboxSettings,
    explain_sandbox,
    memory_limit_detail,
    read_oom_kills,
    read_oom_kills_under,
    run_steps,
    wrapper_argv,
)
from alkera_cli.harness.sandbox_probe import current_capability
from alkera_cli.harness.sandbox_processes import SandboxProbes
from alkera_cli.harness.spawn import sandbox_command, sandbox_spec
from alkera_cli.harness.turn_model import config_declares, opencode_model_ref
from alkera_cli.host.backoff import exponential_delay
from alkera_cli.host.limits import env_count, env_seconds
from alkera_cli.plugins.plugin_base.agent_env import bounded_agent_env
from alkera_cli.plugins.plugin_base.bash_ids import DEFAULT_TIMEOUT_MS, NATIVE_SHELL_NEVER_MS

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Agent-process bounds. Every number below sits between this adapter and a
# subprocess on a machine nobody who chose the number has seen, so each one is
# an operator knob: the constant names the shipped default, the ``ENV_`` name
# beside it names the variable that moves it, and — where the mechanism has
# another way to end — a non-positive value removes the bound entirely.
# ---------------------------------------------------------------------------

#: Wait this long for opencode to report its listen URL before declaring failure.
#: Generous on purpose: opencode's FIRST run performs a one-time SQLite migration
#: ("Performing one time database migration, may take a few minutes..." on stderr)
#: before it binds, and 15s killed exactly that on a fresh Windows host — the
#: harness was alive and working, not stuck. A dead child never waits this long:
#: the poll loop checks `proc.returncode` every 50ms and fails fast, so this
#: deadline only bounds a live-but-unbound harness. 0 waits for the bind as long
#: as the process lives.
ENV_LISTEN_TIMEOUT = "ALKERA_OPENCODE_LISTEN_TIMEOUT_SECONDS"
_LISTEN_TIMEOUT_DEFAULT = 300.0
#: ``inf`` rather than ``None`` so the two deadline sites stay plain arithmetic.
LISTEN_TIMEOUT_S: float = (
    env_seconds(os.environ.get(ENV_LISTEN_TIMEOUT), default=_LISTEN_TIMEOUT_DEFAULT) or math.inf
)

#: The first wait between whole-start retry attempts (spawn → bind → ready).
_START_RETRY_FIRST_DELAY = 1.0
#: Bound on the generated backoff table: an unbounded retry is still a loop, and
#: a wait past the cap is indistinguishable from a hung chat.
_MAX_START_RETRY_DELAYS = 64
_START_RETRY_DELAY_CAP = 60.0


def start_retry_delays(attempts: int | None) -> tuple[float, ...]:
    """The wait before each retry, for ``attempts`` whole start attempts.

    ``None`` (keep retrying) and any count past the two shipped waits keep
    doubling the last one, so a longer leash backs off rather than hammering a
    host that is already struggling to spawn.
    """
    wanted = _MAX_START_RETRY_DELAYS if attempts is None else max(attempts - 1, 0)
    return tuple(
        exponential_delay(retry, first=_START_RETRY_FIRST_DELAY, cap=_START_RETRY_DELAY_CAP)
        for retry in range(wanted)
    )


#: Whole-start attempts before the chat is declared unopenable. Three by default;
#: a transient failure (a crash on a cold spawn, AV holding the fresh binary, a
#: port hiccup) shouldn't kill the chat, and each attempt may itself run to the
#: listen timeout, so this is a count of tries and not a time budget. 0 keeps
#: retrying until the caller gives up.
ENV_START_ATTEMPTS = "ALKERA_OPENCODE_START_ATTEMPTS"
START_ATTEMPTS = env_count(os.environ.get(ENV_START_ATTEMPTS), default=3)
_START_RETRY_DELAYS = start_retry_delays(START_ATTEMPTS)

# opencode's PRIMARY readiness signal: it writes its listen URL to this file (in
# the per-chat sandbox, path passed via ALKERA_LISTEN_FILE — see serve.ts). A
# synchronous file write is immune to stdout block-buffering, which is how a
# `bun build --compile` standalone behaves on a Windows pipe (the stdout banner
# can sit unflushed forever). The banner is kept as a fallback / log line.
_LISTEN_URL_FILE = "listen-url"
_LISTEN_MARKER = "server listening on "

# SSE reconnect backoff (multiplicative, capped at the last entry). A turn may
# run for days, so the count of reconnects over a session's lifetime says nothing
# about whether opencode is alive: a reconnect that carries frames is a healthy
# stream, however many preceded it. The backoff index is therefore reset by a
# connection that delivered anything, and the give-up below is decided by the
# agent process, never by an attempt count.
SSE_RETRY_DELAYS_MS = (250, 500, 1000, 2000, 4000)

#: Environment override for how long the `/event` stream may keep failing while
#: opencode ALSO refuses its health endpoint before the adapter calls the harness
#: crashed. Set it to 0 to never give up on a process that is still running.
ENV_SSE_UNHEALTHY_GIVE_UP = "ALKERA_OPENCODE_SSE_UNHEALTHY_SECONDS"
#: 15 minutes of an agent that is running but answers nothing — not a blip, and
#: not something a longer wait recovers from. A dead child is caught long before
#: this by the process watcher, which is the normal way a crash is declared.
_SSE_UNHEALTHY_GIVE_UP_DEFAULT = 900.0
SSE_UNHEALTHY_GIVE_UP_SECONDS = env_seconds(
    os.environ.get(ENV_SSE_UNHEALTHY_GIVE_UP), default=_SSE_UNHEALTHY_GIVE_UP_DEFAULT
)


async def _sse_backoff(delay_ms: int) -> None:
    """Wait between two `/event` connection attempts. A seam: a suite zeroes it."""
    await asyncio.sleep(delay_ms / 1000.0)


#: Budget for one health probe between two failed `/event` connections. Short on
#: purpose: it answers "is opencode serving right now", and a slow answer is one
#: more failed probe, never a verdict. 0 lets a probe run as long as the give-up
#: window it feeds.
ENV_HEALTH_PROBE_TIMEOUT = "ALKERA_OPENCODE_HEALTH_PROBE_TIMEOUT_SECONDS"
_HEALTH_PROBE_TIMEOUT_SECONDS = env_seconds(os.environ.get(ENV_HEALTH_PROBE_TIMEOUT), default=5.0)

#: Read budget for the CONTROL calls — prompt submission, abort, permission and
#: question replies, session listing. Each returns as soon as opencode has taken
#: the request, so a stall here really is a stalled server. 0 removes the read
#: budget, leaving the caller's own cancel as the bound.
ENV_CONTROL_READ_TIMEOUT = "ALKERA_OPENCODE_CONTROL_READ_TIMEOUT_SECONDS"
_CONTROL_READ_TIMEOUT_SECONDS = env_seconds(os.environ.get(ENV_CONTROL_READ_TIMEOUT), default=120.0)

#: Environment override for how long `/event` may be silent before the adapter
#: reconnects. opencode heartbeats the stream every 10 seconds, so three missed
#: beats mean the connection is dead even though the agent may be fine —
#: reconnecting is free, and no turn ends over it. 0 waits forever.
ENV_SSE_READ_TIMEOUT = "ALKERA_OPENCODE_SSE_READ_TIMEOUT_SECONDS"
SSE_READ_TIMEOUT_SECONDS = env_seconds(os.environ.get(ENV_SSE_READ_TIMEOUT), default=30.0)

#: The loopback halves of every call to the agent. opencode listens on this
#: machine, so a connect or a pool wait that takes seconds is a wedged server and
#: not a slow network — but "this machine" can be a cold container under a
#: thundering start, so both stay movable. 0 removes either bound.
ENV_CONNECT_TIMEOUT = "ALKERA_OPENCODE_CONNECT_TIMEOUT_SECONDS"
_CONNECT_TIMEOUT_SECONDS = env_seconds(os.environ.get(ENV_CONNECT_TIMEOUT), default=5.0)
#: Write budget for one request body. A prompt carrying pasted files is the
#: largest thing that travels it, so it is generous where connect is not.
ENV_WRITE_TIMEOUT = "ALKERA_OPENCODE_WRITE_TIMEOUT_SECONDS"
_WRITE_TIMEOUT_SECONDS = env_seconds(os.environ.get(ENV_WRITE_TIMEOUT), default=30.0)


def _agent_timeout(read: float | None) -> httpx.Timeout:
    """The per-call budget: the shared loopback halves plus this call's read."""
    return httpx.Timeout(
        connect=_CONNECT_TIMEOUT_SECONDS,
        read=read,
        write=_WRITE_TIMEOUT_SECONDS,
        pool=_CONNECT_TIMEOUT_SECONDS,
    )


#: Calls that run a model turn inside the request — compaction summarizes a
#: near-full context with the chat's own model — get no read budget at all. The
#: work is bounded by the model and by the caller's own cancel; a client-side
#: read timeout on one of these reads a working agent as a crashed one.
_BLOCKING_TIMEOUT = _agent_timeout(None)

#: How long a raced `idle` waits for an outstanding tool part's closing frame
#: before the adapter synthesizes the closure itself. opencode's session-status
#: publisher and its part store race on its event bus, so `session.status(idle)`
#: can hit the wire a beat before the tool's final `message.part.updated` — the
#: adapter contract (README: "Event ordering guarantees") promises tool events
#: precede idle, so the idle is held until the real closure lands. The grace
#: exists for the frame-LOST case only (an SSE reconnect has no replay): normal
#: delivery releases the hold in milliseconds. 0 waits for the real closure
#: however long it takes, which is the right reading for a deployment that would
#: rather stall a turn's end than report a tool result the agent never closed.
ENV_IDLE_TOOL_CLOSE_GRACE = "ALKERA_OPENCODE_IDLE_TOOL_CLOSE_GRACE_SECONDS"
IDLE_TOOL_CLOSE_GRACE_SECONDS = env_seconds(os.environ.get(ENV_IDLE_TOOL_CLOSE_GRACE), default=5.0)

#: How much of the agent's error body rides in the message a failed send or
#: compaction raises. A traceback's cause sits at its end, so a snippet that is
#: too tight hides the only line worth reading; the whole body would put an
#: agent-controlled string of any length into our error. 0 keeps it whole.
ENV_ERROR_BODY_CHARS = "ALKERA_OPENCODE_ERROR_BODY_CHARS"
ERROR_BODY_CHARS = env_count(os.environ.get(ENV_ERROR_BODY_CHARS), default=500)

#: Lines of the agent's startup stdout/stderr kept to explain a chat that never
#: opened. The tails are the ONLY account of a bun startup error or a missing
#: DLL, and they are bounded because a wedged pipe would otherwise grow without
#: end — so this one has no "unbounded" reading and a non-positive value keeps
#: the default.
ENV_STARTUP_TAIL_LINES = "ALKERA_OPENCODE_STARTUP_TAIL_LINES"
STARTUP_TAIL_LINES = env_count(os.environ.get(ENV_STARTUP_TAIL_LINES), default=50) or 50

#: How long a closing chat waits for the agent to exit after SIGTERM, and then
#: after SIGKILL. Both bound a shutdown nobody is watching, so both stay short;
#: an operator whose agent flushes a large state on exit raises the first. 0
#: waits for the process to actually go.
ENV_TERMINATE_GRACE = "ALKERA_OPENCODE_TERMINATE_GRACE_SECONDS"
TERMINATE_GRACE_SECONDS = env_seconds(os.environ.get(ENV_TERMINATE_GRACE), default=3.0)
ENV_KILL_GRACE = "ALKERA_OPENCODE_KILL_GRACE_SECONDS"
KILL_GRACE_SECONDS = env_seconds(os.environ.get(ENV_KILL_GRACE), default=2.0)

#: How long an agent that has already BOUND has to answer its first request. The
#: process is known to exist, so this only covers a machine that is busy right
#: after the spawn; 0 waits until it answers or the caller cancels.
ENV_READY_TIMEOUT = "ALKERA_OPENCODE_READY_TIMEOUT_SECONDS"
READY_TIMEOUT_SECONDS = env_seconds(os.environ.get(ENV_READY_TIMEOUT), default=5.0)
#: Gap between two readiness pings. Not a bound — the cadence of the poll.
READY_POLL_SECONDS = 0.2
#: Gap between two liveness checks while waiting for a signalled process to go.
_EXIT_POLL_SECONDS = 0.1


async def _await_exit(pid: int, grace: float | None) -> bool:
    """Whether ``pid`` went away within ``grace`` seconds (``None`` = wait)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + (grace or math.inf)
    while True:
        if not process_alive(pid):
            return True
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(_EXIT_POLL_SECONDS)


# opencode permission config (passed via ALKERA_PERMISSION). `"*": "ask"` makes
# EVERY permission-relevant tool require approval — including the read-only tools
# (full passthrough: no vendor heuristic decides anything; OUR classifier
# + policy do). A plain "deny" value becomes a `pattern:"*"` deny rule, which
# opencode treats as the tool being DISABLED — physically removed from the toolset
# sent to the model (`Permission.disabled` → `resolveTools`), not merely rejected
# on call. (Same mechanism opencode uses to drop todowrite/task for subagents.)
# The result: opencode emits `permission.asked` for read/glob/grep/list/lsp +
# edit / bash / webfetch / websearch / external_directory / repo_*, and our
# runtime policy decides — auto-allowing reads (read-effect) without a human
# prompt; question / plan_present run freely (own UX); todowrite / skill are not
# exposed to the model at all, and neither is `task` — whose primary gate is the
# registry flag below, not this deny.
_OPENCODE_PERMISSION_ASK: dict[str, str] = {
    "*": "ask",
    # Read-only tools also flow to OUR broker (full passthrough), so no
    # vendor heuristic auto-allows them. Our classifier marks them read-effect so
    # the runtime policy auto-allows without a human prompt; the difference is
    # WHO decides (us, not opencode's config).
    "question": "allow",
    # The plan-approval tool is its own UX (it prompts via the question
    # surface); don't gate the tool call itself behind a permission ask.
    "plan_present": "allow",
    # Alkera plugin tools (the local-MCP "alkera" server) auto-allow — they
    # gate write effects INSIDE the tool body via the broker,
    # exactly like the Claude SDK-MCP path, so the "*": "ask" default must not
    # double-prompt them. opencode names local-MCP tools "<server>_<tool>".
    "alkera_*": "allow",
    # The dedicated `web` loopback mount (web_search / web_fetch) — same
    # parent-hosted tools, served un-prefixed so the model sees clean names.
    # Read-effect + in-parent gating, so auto-allow exactly like `alkera_*`.
    # (The `_` is literal in opencode's matcher, so the vendor `webfetch` /
    # `websearch` tool ids can never match this rule.)
    "web_*": "allow",
    # `external_directory` is opencode's SEPARATE gate fired when a tool touches a
    # path outside the project cwd — it arrives ON TOP OF the tool's own ask, so a
    # single `rm /tmp/x` would prompt twice (a vague "outside your project" ask AND
    # the real "Delete these files?"). Auto-allow it so only the tool's own action
    # gate prompts — that ask names what's actually happening, with the real path.
    # The boundary isn't lost: the action itself (edit/bash) still gates; a bare
    # read outside the cwd is read-effect, which our policy auto-allows anyway —
    # EXCEPT when the path it names trips the sensitive-path floor the
    # translator applies (`harness/sensitive_paths.py`), which raises it to
    # EGRESS on the tool's own ask.
    "external_directory": "allow",
    # Alkera owns these. A bare "deny" (pattern `*`) DOES keep the tool out of the
    # advertised set: `Permission.disabled()` collects every `pattern:"*" deny` and
    # request.ts `resolveTools` subtracts them before the request is built. What a
    # deny cannot do is survive a ruleset that outranks it — a per-agent or
    # per-config rule merged after ours flips the tool back on, advertised. So the
    # tools we must never serve are ALSO removed at the registry, where nothing
    # downstream can advertise them back: the shell via ALKERA_PARENT_SHELL below,
    # the task tool via ALKERA_SUBAGENTS_ENABLED. `task` therefore keeps this deny
    # only as the call-time floor under that removal; for todowrite / skill — which
    # are ours to own but not dangerous to be offered — the deny IS the removal.
    "task": "deny",
    "todowrite": "deny",
    "skill": "deny",
}
# The shell is parent-hosted (plugin_base/bash_tool.py, served over the loopback MCP)
# — but ONLY on POSIX, where that replacement is registered (it relies on process
# groups / signals). On Windows the (Windows-capable, pwsh/cmd) native bash stays
# until the parent-hosted port is cross-platform, so a Windows agent always has a shell.
#
# On POSIX the vendor patch behind ALKERA_PARENT_SHELL (see `_build_env`) REMOVES the
# native ShellTool from the advertised set and re-advertises our MCP tool under the
# bare name `bash` — the name models are pretrained on. So the `bash` permission key
# now belongs to OUR tool, and auto-allows exactly like the `alkera_*` / `web_*` globs
# above: it gates its own write effects in-parent via the broker.
#
# This MUST stay POSIX-conditional. Wherever the flag is off the native ShellTool is
# still present, and an unconditional "allow" would hand it a free pass.
if os.name == "posix":
    _OPENCODE_PERMISSION_ASK["bash"] = "allow"

# The injected opencode config (ALKERA_CONFIG_CONTENT, see `_build_env`)
# deliberately names NO model and NO provider of its own. Every model comes
# from the gateway config the runtime merges in (`harness_native["agent_config"]`,
# built from the chat's pinned model + the user's token); a session that
# reaches the adapter without one has nothing to fall back on and its turn is
# refused (`_admit_turn_model`). A default here would let a chat nobody pinned
# run on a model nobody metered, billed or audited — opencode's own hosted
# provider connects with no credential at all, which is exactly what happened
# before this was removed.
#
# `snapshot: false` disables opencode's filesystem snapshot tracking
# (snapshot/index.ts). With it on (the opencode default), the first turn of
# every chat inits a git repo whose work-tree is the user's project root and
# `git add --all`s every tracked + untracked-non-ignored file into its object
# store — thousands of loose git objects for a monorepo, recreated per chat
# because each chat gets its own isolated XDG_DATA_HOME (`.runtime/agent/`).
# Alkera never drives opencode's snapshot-backed revert, so we pay that cost
# for nothing; off, no `.runtime/agent/snapshot/` tree is ever written.
_OPENCODE_DEFAULT_CONFIG: dict[str, Any] = {
    "snapshot": False,
}

#: What the reader sees when a turn is refused for want of a model. Published
#: as a `status="error"` detail AND carried by the raised `HarnessModelError`.
NO_MODEL_MESSAGE = (
    "This chat has no model configured, so nothing was run. "
    "Choose a model from the model picker and send again."
)
UNROUTED_MODEL_MESSAGE = (
    "This chat's model {model} is not served through the model gateway, so nothing was run. "
    "Choose a model from the model picker and send again."
)


def _split_model_ref(raw: Any) -> dict[str, str] | None:
    """`"provider/model"` → `{provider_id, model_id}`, or None when either half
    is missing — a bare id is not a route, and an empty string is not a model."""
    if not isinstance(raw, str) or "/" not in raw:
        return None
    provider_id, _, model_id = raw.partition("/")
    if not provider_id or not model_id:
        return None
    return {"provider_id": provider_id, "model_id": model_id}


# Inherited env vars that could inject the user's own config, auth, or db path
# into our otherwise-isolated harness. Scrubbed in `_build_env` so the spawned
# process reads ONLY our per-chat sandbox. We keep the opencode-native names —
# a machine that also runs upstream opencode may export them, and the
# config/auth/db readers we did NOT rename still honour them — AND add our
# renamed ALKERA_CONFIG_CONTENT and the password, which the harness hands over by
# file (see `write_agent_secrets`) and never in the environment.
#: Env var carrying the EXACT diff `_build_env` applied to the original
#: process environment, as JSON `{name: original-value | null}` (null = the
#: var did not exist). The vendored shell tool (tool/shell.ts, ALKERA EDIT)
#: applies it before spawning agent commands, so the sandbox below isolates
#: opencode ITSELF while the user's `bash` commands run with their TRUE
#: original environment — without this, uv cached wheels + interpreters
#: inside the chat's .runtime, git lost ~/.config/git, etc.
SHELL_ENV_RESTORE_VAR = "ALKERA_SHELL_ENV_RESTORE"


def shell_env_restore(original: dict[str, str], spawned: dict[str, str]) -> dict[str, str | None]:
    """The reverse diff that turns ``spawned`` back into ``original`` exactly:
    changed/removed vars map to their original value, added vars map to None
    (delete). Pure, so the round-trip is unit-testable byte-for-byte."""
    restore: dict[str, str | None] = {}
    for key in original.keys() | spawned.keys():
        if original.get(key) != spawned.get(key):
            restore[key] = original.get(key)
    return restore


_OPENCODE_ENV_LEAKS: tuple[str, ...] = (
    "OPENCODE_CONFIG",
    "OPENCODE_CONFIG_DIR",
    "OPENCODE_CONFIG_CONTENT",
    "OPENCODE_AUTH_CONTENT",
    "OPENCODE_DB",
    "ALKERA_CONFIG_CONTENT",
    "ALKERA_SERVER_PASSWORD",
)

# Env flags that lock the spawned harness down to "talk only to our gateway"
# and pin deterministic on-disk names. Each is a documented opencode
# flag (see vendor/opencode/README.alkera.md):
#   DISABLE_MODELS_FETCH     — no models.dev fetch/refresh; the catalog comes
#                              from the snapshot compiled into the binary.
#   DISABLE_SHARE            — no session sharing to opncd.ai.
#   DISABLE_AUTOUPDATE       — no version-check pings.
#   DISABLE_LSP_DOWNLOAD     — no LSP-server downloads (npm + raw `npm install`).
#   DISABLE_CHANNEL_DB       — deterministic DB filename (`agent.db`) across all
#                              build channels (also stops the per-boot migration
#                              banner and keeps the store's path predictable).
#   DISABLE_NPM_INSTALL      — master kill-switch for opencode's runtime npm
#                              installer (@npmcli/arborist → registry.npmjs.org).
#                              Closes EVERY remaining per-sandbox phone-home we
#                              don't use: the `@opencode-ai/plugin` config-dep
#                              install, external plugin packages, non-bundled
#                              provider SDKs, and the edit/write-tool formatters
#                              (prettier/oxfmt/biome). Gated at the single
#                              `Npm.reify` chokepoint in core/src/npm.ts, so the
#                              guarantee is complete (an airgap e2e proves zero
#                              egress). We use none of those features.
#   PURE                     — skip external (config-declared) plugins entirely,
#                              so opencode never even tries to resolve/install
#                              them. Belt-and-suspenders with DISABLE_NPM_INSTALL.
#
# NOTE: ripgrep is handled separately, NOT in this unconditional list — the
# ALKERA_DISABLE_RIPGREP_DOWNLOAD gate is only safe to set when we ship a
# bundled `rg` for opencode to find on PATH (see `_apply_ripgrep_env`). opencode's
# glob/grep depend on `rg` and hard-die (Effect.orDie) without it, so disabling
# the download with no bundled rg present would break them — exactly the
# regression that forced the first gate attempt to be reverted. With a bundled
# rg it's safe. See vendor/opencode/README.alkera.md.
_OPENCODE_LOCKDOWN_FLAGS: tuple[str, ...] = (
    "ALKERA_DISABLE_MODELS_FETCH",
    "ALKERA_DISABLE_SHARE",
    "ALKERA_DISABLE_AUTOUPDATE",
    "ALKERA_DISABLE_LSP_DOWNLOAD",
    "ALKERA_DISABLE_CHANNEL_DB",
    "ALKERA_DISABLE_NPM_INSTALL",
    "ALKERA_PURE",
)

# Set when a bundled rg is available; not in _OPENCODE_LOCKDOWN_FLAGS because
# it's conditional on shipping that rg.
_OPENCODE_RIPGREP_DISABLE_FLAG = "ALKERA_DISABLE_RIPGREP_DOWNLOAD"

# Set only on POSIX, where the parent-hosted shell is actually registered — not in
# _OPENCODE_LOCKDOWN_FLAGS for the same reason as the rg flag above. It drops the
# native ShellTool from the model's tool set AND advertises our loopback-MCP tool as
# bare `bash`; both halves live behind this one flag so they can't be set half-on
# (setting it without our replacement registered would leave the agent with NO shell).
_OPENCODE_PARENT_SHELL_FLAG = "ALKERA_PARENT_SHELL"

# Set only when the parent mounts its own web tools (the `web` loopback mount, whose
# tools compose as `web_search` / `web_fetch`). It drops the vendor's native
# WebFetchTool from the model's tool set, so the model is offered ONE fetch tool
# instead of two — and never the vendor one, which our classifier marks EGRESS
# (opencode_translate `webfetch`) and the policy therefore refuses outright in
# read_only/plan. Deliberately a SECOND flag, not a reuse of the shell flag: the
# mount and the parent-hosted shell are independent, and a box with no alkera web
# tools must keep the vendor fetch or it would have no fetch at all.
_OPENCODE_PARENT_WEB_FETCH_FLAG = "ALKERA_PARENT_WEB_FETCH"

# Whether this deployment serves delegation. Carries `SessionConfig.subagents_enabled`
# — itself the `ALKERA_SUBAGENTS_ENABLED` setting, resolved in the runtime — into the
# child, where the vendored `tool/registry.ts` drops the `task` tool from the
# ADVERTISED set. The parent's own `spawn_agent`/`list_agent_types` are withheld by
# the same setting, so without this the model would still be offered opencode's
# private spawner and delegate into a child session the product has no surface for.
# Always set, never inherited: a value in the user's shell must not decide it.
_OPENCODE_SUBAGENTS_FLAG = "ALKERA_SUBAGENTS_ENABLED"

# opencode caps every step's output at 32k tokens (`OUTPUT_TOKEN_MAX` in the vendor
# `provider/transform.ts`) unless this env raises the ceiling; a model's own
# `limit.output` still clamps below it. Set from the injected config so a long
# write does not end the turn at `finish: length` while the model had budget left.
_OPENCODE_OUTPUT_CEILING_FLAG = "OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX"

# opencode's native shell defaults a command with no timeout to two minutes
# (`bashDefaultTimeoutMs` in the vendor `tool/shell.ts`). POSIX never reaches it —
# the parent-hosted shell replaces it — but Windows has no parent shell to swap
# in, and a default that differs by platform kills the same command on one and
# not the other. Carry ours across ("no wall clock" is the largest count the flag
# takes). The variable is renamed in the vendored source like every other flag we
# set, so the spawn env stays free of OPENCODE-prefixed names.
_OPENCODE_BASH_TIMEOUT_FLAG = "ALKERA_BASH_DEFAULT_TIMEOUT_MS"


def _largest_output_limit(oc_config: Mapping[str, Any]) -> int:
    """The largest per-model ``limit.output`` in an opencode config, 0 when no
    model declares one. One process serves every configured model, and the env
    ceiling is process-wide, so the largest is the only value that under-clamps
    none of them."""
    largest = 0
    providers = oc_config.get("provider")
    if not isinstance(providers, Mapping):
        return 0
    for provider in providers.values():
        models = provider.get("models") if isinstance(provider, Mapping) else None
        if not isinstance(models, Mapping):
            continue
        for model in models.values():
            limit = model.get("limit") if isinstance(model, Mapping) else None
            output = limit.get("output") if isinstance(limit, Mapping) else None
            if isinstance(output, int) and not isinstance(output, bool):
                largest = max(largest, output)
    return largest


# A session id becomes a path component under ALKERA_HOME; keep it a safe slug
# (the store already uses it as a directory name, but this root is one level
# further from the project store, so re-check rather than assume).


#: Marks a config root as owned by a live ``alkera``: pid + process creation time +
#: the host that recorded them. Superset of the per-chat agent breadcrumb, which is
#: written next to a chat's own state and so is never read from another machine.
#: Dotfile so it can never be picked up by opencode's config-dir discovery, which
#: globs `{agent,agents}/**/*.md`.
_AGENT_ROOT_OWNER_FILE = ".owner"


def _write_owner_breadcrumb(path: Path) -> None:
    """Stamp a config root as owned by THIS process on THIS host.

    The host matters because ``ALKERA_HOME`` lives under the user's home directory,
    which in an enterprise deployment is routinely shared (NFS/AFS) between machines:
    a pid recorded over there says nothing about what is running here. Atomic, so a
    SIGKILL mid-write can never leave a torn breadcrumb another ``alkera`` would have
    to guess about."""
    write_json_atomic(
        path,
        {
            "pid": os.getpid(),
            "create_time": process_create_time(os.getpid()),
            "host": socket.gethostname(),
        },
    )


def _read_owner_breadcrumb(path: Path) -> tuple[int, float | None] | None:
    """``(pid, create_time)`` from a config root's ``.owner`` — but ONLY when THIS
    host wrote it.

    A breadcrumb naming another machine (or none at all) reads as ``None``: its pid
    describes a process we cannot probe, so nothing here can prove the root is
    abandoned, and the root belongs to a chat that may well be live. Mirrors
    :func:`~alkera_cli.harness.orphan_sweep.sweep_orphaned_agents`, which skips
    cross-host registry entries for the same reason."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("host") != socket.gethostname():
        return None
    pid = data.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool):
        return None
    create_time = data.get("create_time")
    return pid, (create_time if isinstance(create_time, (int, float)) else None)


def sweep_stale_agent_config_roots(*, keep: Path | None = None) -> int:
    """Delete agent config roots whose owning ``alkera`` process is provably gone;
    return how many were removed.

    :meth:`OpencodeHttpAdapter.stop` removes a chat's root on every normal path, but
    a SIGKILLed ``alkera`` never reaches it — and these roots hold opencode's whole
    cache/state tree under the user's HOME, so without a sweep one accumulates per
    chat ever opened. Runs at the start of every spawn (cheap: one ``iterdir``).

    Only a POSITIVELY dead owner is reaped, mirroring
    :mod:`alkera_cli.harness.orphan_sweep`: a root is removed when its ``.owner``
    breadcrumb was written by THIS host and names a pid whose recorded creation time
    no longer matches a live process. A live owner (including THIS process's other
    open chats), an owner on another machine (a shared/NFS home puts its roots right
    here, and its pids mean nothing locally), an unreadable breadcrumb, and a
    breadcrumb with no recorded creation time are all left alone — we never delete a
    root we cannot prove is abandoned. Best-effort; never raises."""
    try:
        entries = [p for p in agent_config_roots_dir().iterdir() if p.is_dir()]
    except OSError:
        return 0
    removed = 0
    for entry in entries:
        if keep is not None and entry == keep:
            continue
        breadcrumb = _read_owner_breadcrumb(entry / _AGENT_ROOT_OWNER_FILE)
        if breadcrumb is None:
            continue
        pid, create_time = breadcrumb
        if create_time is None or same_process(pid, create_time):
            continue
        shutil.rmtree(entry, ignore_errors=True)
        removed += 1
    return removed


def build_agent_env(
    *,
    data_home: str,
    config_home: str,
    listen_file: str,
    secrets_file: str,
    permission: dict[str, str] | None = None,
    base_env: Mapping[str, str] | None = None,
    fenced: bool = False,
    state_home: str | None = None,
) -> dict[str, str]:
    """The isolation + lockdown floor EVERY spawned agent process gets.

    ``fenced`` is a bounded (cloud) session: the process starts from the few
    names of the box's environment it needs (``plugin_base.agent_env``), never
    the rest (a bearer, a leased database, a key under a name nobody listed),
    and the harness's own names are set over them. Its password and injected
    config are never in it: they are in ``secrets_file``, spelled as the agent
    sees it (see ``opencode_secrets``).

    Both the per-chat adapter spawn (:meth:`OpencodeHttpAdapter._build_env`) and
    the daemon's startup pre-warm probe (``harness.prewarm``) build their env
    here so the two can never drift. The load-bearing secret is the password: the
    agent's loopback API can spawn a PTY and run shell commands, and its
    authorization is a no-op without one (``ServerAuth.required``) — so a spawn
    without it is an unauthenticated local RCE surface for its lifetime.

    ``config_home`` drives config and home, ``state_home`` (default: the same
    root) state and cache (see :func:`agent_config_root`);
    ``data_home`` stays per-chat so the agent's store (``agent/agent.db``) stays
    under the chat's harness directory."""
    env = dict(base_env if base_env is not None else os.environ)
    if fenced:
        env = bounded_agent_env(env)
    # Total config isolation: the spawned opencode reads ONLY these roots —
    # never the user's ~/.config/opencode, project opencode.json, ~/.opencode,
    # recent-model state, or auth. opencode derives every root from these XDG
    # vars at startup (global.ts), so setting them here — the single spawn-env
    # choke point — is uniform across the bun-dev and bundled-binary paths.
    env["XDG_DATA_HOME"] = data_home
    env["XDG_CONFIG_HOME"] = config_home
    env["XDG_STATE_HOME"] = state_home or config_home
    env["XDG_CACHE_HOME"] = state_home or config_home
    # opencode resolves "home" from ALKERA_TEST_HOME first (global.ts, renamed)
    # — that var ONLY affects Path.home, so pointing it at the config root makes
    # the ungated `~/.opencode` config walk find nothing.
    env["ALKERA_TEST_HOME"] = config_home
    # Don't walk UP from the user's workspace for opencode.json/.opencode.
    # NB: this gates *config* only. Repo instruction-file discovery
    # (AGENTS.md/CLAUDE.md/CONTEXT.md/ALKERA.md) is gated separately by
    # ALKERA_DISABLE_PROJECT_INSTRUCTIONS, which we deliberately leave UNSET so
    # those files ARE read from the user's project (see the instruction-file row
    # in vendor/opencode/README.alkera.md). Locking config without locking
    # instructions is the whole point of the split.
    env["ALKERA_DISABLE_PROJECT_CONFIG"] = "true"
    # Strip user-shell vars that would inject external config/auth/db.
    for leak in _OPENCODE_ENV_LEAKS:
        env.pop(leak, None)
    env[opencode_secrets.AGENT_SECRETS_FILE_VAR] = secrets_file
    # Buffering-immune readiness: opencode writes its listen URL here once
    # bound (see _await_listen_url + serve.ts).
    env["ALKERA_LISTEN_FILE"] = listen_file
    # Force opencode to ASK before EVERY permission-relevant tool runs, so our
    # broker + runtime policy decide (full passthrough). Without
    # this, opencode's default `build` agent is `"*": "allow"` and never emits
    # permission.asked.
    env["ALKERA_PERMISSION"] = json.dumps(
        _OPENCODE_PERMISSION_ASK if permission is None else permission
    )
    # Lock the harness down: no outbound calls except our model gateway, and
    # deterministic on-disk names. See _OPENCODE_LOCKDOWN_FLAGS.
    for flag in _OPENCODE_LOCKDOWN_FLAGS:
        env[flag] = "true"
    return env


# ---------------------------------------------------------------------------
# Adapter implementation
# ---------------------------------------------------------------------------


def _unhealthy_too_long(elapsed: float) -> bool:
    """Whether an agent that answers nothing has been that way past the window."""
    window = SSE_UNHEALTHY_GIVE_UP_SECONDS
    return window is not None and elapsed >= window


@dataclass(slots=True)
class _AdapterState:
    """Mutable runtime state. Kept in a dataclass so it's easy to inspect."""

    probes: SandboxProbes = field(default_factory=SandboxProbes)
    started: bool = False
    stopping: bool = False
    crashed: bool = False
    opencode_session_id: str | None = None
    base_url: str = ""
    password: str = ""
    proc: asyncio.subprocess.Process | None = None
    sandbox: SandboxLaunch | None = None
    """The sandbox the agent server was launched in; its after-exit steps run
    once the process is gone, whichever way it went."""
    oom_kills_at_spawn: int = 0
    """The chat cgroup's ``oom_kill`` count when this agent was spawned; a
    count above it at exit means the kernel killed this agent for memory."""
    oom_kills_seen: int = 0
    """The highest ``oom_kill`` count read during this agent's life. The
    kernel counts the kill before the container runtime and systemd take the
    cgroup down, so it is read at the first sign the agent is gone (the event
    stream breaking, the process exiting) and kept — the file may be gone by
    the time the exit is accounted for."""
    sandbox_plan: SandboxPlan | None = None
    """The sandbox this start decided on before the environment was built:
    what the config and the listen address are composed for. ``None`` with
    ``sandbox_planned`` set is a session that runs unsandboxed."""
    sandbox_planned: bool = False
    sandbox_host: str = ""
    """The probe's one line about this host, kept even when the session runs
    unsandboxed, so a failed start can still say what the box found."""
    sandbox_wrapper: tuple[str, ...] = ()
    """The argv in front of the agent binary at the last spawn — the
    death-signal launcher, the cgroup placement, the alias, the uid drop — as
    a failed start reports it. Never the environment."""
    http_client: httpx.AsyncClient | None = None
    sse_frames_this_connect: int = 0
    """Frames the CURRENT `/event` connection has carried. A reconnect that
    delivers anything proves the agent is serving, which is what resets the
    reconnect backoff — a count of past failures never does."""
    sse_task: asyncio.Task[None] | None = None
    process_watch_task: asyncio.Task[None] | None = None
    last_model: dict[str, str] | None = None
    """Most recent `{provider_id, model_id}` sent on a prompt. Used to
    pick the model for a manual compaction (opencode's /summarize needs
    a provider+model); falls back to the configured default."""
    held_idle: list[SessionStatusChanged] | None = None
    """`idle` statuses that arrived while a tool part was still non-final —
    held back so the tool's closing update publishes first (the ordering the
    adapter contract guarantees). Both idle flavors of a turn end ride the
    hold, so each releases in stream order. Released by the closing frame,
    dropped when a newer status supersedes them, or force-released by the
    grace watchdog. None while nothing is held."""
    idle_watchdog_task: asyncio.Task[None] | None = None
    """Armed whenever `held_idle` is set; synthesizes the tool closure and
    releases the idle if the real frame never arrives (lost to an SSE
    reconnect, which has no replay)."""


class OpencodeHttpAdapter(HarnessAdapter):
    """Drives one opencode subprocess for one chat. See module docstring."""

    # Public name surfaced in logs, events' `harness` field, and the
    # `manifest.harness_type` registry. Every chat manifest stores it, so it
    # never changes (it was "oc" before chats were persisted).
    name = "agent"
    # NOTE: "share" is intentionally omitted — sharing would POST a session to
    # opncd.ai. We also set ALKERA_DISABLE_SHARE, so the UI never offers it.
    # NOTE: "revert" is intentionally omitted — it would mean opencode's
    # snapshot-backed undo, but we disable snapshots (_OPENCODE_DEFAULT_CONFIG)
    # because they snapshot the whole workspace per chat, and nothing in Alkera
    # ever wired the revert path.
    capabilities = frozenset(
        {
            "fork",
            "resume",
            "summarize",
            "clear",
            "mcp_dynamic",
            "subagents",
            "permission_runtime",
        }
    )

    @classmethod
    def is_available(cls) -> bool:
        # The opencode binary ships with us (bundled, staged or bun-dev tiers).
        return True

    def __init__(
        self,
        config: SessionConfig,
        *,
        binary: ResolvedOpencodeBinary,
        event_bus: EventBus | None = None,
        probes: SandboxProbes | None = None,
    ) -> None:
        self._config = config
        self._binary = binary
        self._bus = event_bus or EventBus()
        self._state = _AdapterState(probes=probes or SandboxProbes())
        # Translator owns the in-flight-parts table; the adapter's
        # synthesize-close walks the same dict (shared by reference).
        self._translator_ctx = _TranslatorContext(
            session_id=config.session_id,
            # Lets the translator's sensitive-path floor resolve the worktree-
            # relative paths opencode reports, and exempt this chat's own scratch.
            workspace_root=config.project_dir,
            sandbox_dir=config.chat_dir / SANDBOX_SUBDIR,
            # In lockstep with `_OPENCODE_PARENT_SHELL_FLAG` in `_build_env`:
            # where that patch runs, the tool the model calls `bash` is ours and
            # gates its own command, so the vendor's ask for it is answered here
            # rather than raised a second time at the reader.
            parent_hosted_shell=os.name == "posix",
        )
        self._translator = OpencodeEventTranslator(self._translator_ctx)
        # Per-instance override for the idle-hold liveness bound; None means
        # IDLE_TOOL_CLOSE_GRACE_SECONDS, read at fire time so tests can patch
        # either the module constant or this attribute.
        self._idle_hold_grace: float | None = None
        # Where the harness's data lives. Lock-isolated per chat, under
        # `.runtime`; the inner dir is `agent/` (vendor `app` constant).
        # Existing chats keep their session database there.
        self._harness_dir = config.chat_dir / RUNTIME_STATE_SUBDIR
        # opencode's config/agent/tool DISCOVERY roots, deliberately outside the
        # project tree the agent can write to. See `agent_config_root`.
        self._agent_config_root = agent_config_root(config.session_id)
        # The root splits in two. ``config/`` is what the agent reads and must
        # never change (its agent definitions, whose ``permission:`` merges
        # after ours, and its instructions): a sandboxed chat sees it read-only.
        # ``state/`` is what the agent writes (its state and cache — opencode
        # names both ``<root>/agent`` otherwise, the same directory as its
        # config): a sandboxed chat owns it.
        self._agent_config_dir = self._agent_config_root / "config"
        self._agent_state_dir = self._agent_config_root / "state"
        # Where the agent server announces the port it bound: in its own state
        # (process-scoped, never part of the chat's folder), which a sandbox
        # binds at a fixed internal path.
        self._listen_file = self._agent_state_dir / _LISTEN_URL_FILE
        self._secrets_file = self._agent_state_dir / opencode_secrets.SECRETS_FILE

    # ------------------------------------------------------------------
    # Public introspection
    # ------------------------------------------------------------------

    def info(self) -> HarnessInfo:
        sha = self._binary.sha
        return HarnessInfo(
            name=self.name,
            source=self._binary.source,
            version=sha[:8] if sha else None,
        )

    def native_state(self) -> dict[str, Any]:
        """Return the opencode session id we attached to so the runtime
        can persist it into the manifest. Next ``open_chat`` reads it
        back via ``SessionConfig.harness_native`` and pins to the same
        id instead of picking-latest."""
        if self._state.opencode_session_id is None:
            return {}
        # The key lands in the chat manifest and is shared with the Claude Code
        # adapter, so it is harness-agnostic: `agent_session_id`.
        return {"agent_session_id": self._state.opencode_session_id}

    @property
    def opencode_session_id(self) -> str | None:
        return self._state.opencode_session_id

    @property
    def event_bus(self) -> EventBus:
        return self._bus

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._state.started:
            raise RuntimeError("OpencodeHttpAdapter.start() called twice")
        # A startup pre-warm may be mid-flight (staging the binary / running
        # opencode's one-time database migration): wait for it instead of
        # racing a SECOND migrator against the same database. A no-op when no
        # pre-warm ran or it already settled; bounded and best-effort. Lazy
        # import — prewarm imports our LISTEN_TIMEOUT_S at module level.
        from alkera_cli.harness.prewarm import wait_for_prewarm

        await wait_for_prewarm()

        # The spawn → bind → ready-ping sequence rides real machine weather (a
        # cold first launch, AV scanning a fresh binary, a port hiccup), so a
        # transient failure gets a bounded retry instead of killing the chat.
        # Every failed attempt tears its half-started child down first
        # (_cleanup_failed_start), so retries can never stack agent processes.
        # A failure that describes state a relaunch can't heal is NOT retried —
        # a missing pinned session (Unavailable), or an agent that bound, answered,
        # and then refused the session call (Refused) — but still gets the cleanup
        # so the dead attempt's child never outlives the error.
        last: HarnessStartError | None = None
        store_set_aside = False
        for attempt in range(1, len(_START_RETRY_DELAYS) + 2):
            try:
                await self._start_once()
                return
            except HarnessStoreUnreadableError as exc:
                await self._cleanup_failed_start()
                if store_set_aside:
                    # A fresh store refused too: this is not the travelling
                    # database's doing, and a third spawn would meet it again.
                    self._remove_agent_config_root()
                    raise
                aside = self._set_aside_agent_store()
                if aside is None:
                    self._remove_agent_config_root()
                    raise
                store_set_aside = True
                logger.warning(
                    "the agent's session store for %s could not be read (%s); it was set "
                    "aside as %s and the chat continues in a fresh session from the transcript",
                    self._config.session_id,
                    exc,
                    aside.name,
                )
                continue
            except HarnessStartRefusedError:
                # The agent is up and refusing, so every respawn meets the same
                # state: two more of them would only bury this error under the
                # third attempt's copy of it, and on a busy machine a spawn is
                # the expensive part. Surface it now, with the root gone as on
                # any other spent start.
                await self._cleanup_failed_start()
                self._remove_agent_config_root()
                raise
            except HarnessStartError as exc:
                await self._cleanup_failed_start()
                last = exc
                if attempt <= len(_START_RETRY_DELAYS):
                    logger.warning(
                        "harness start attempt %d/%d failed: %s — retrying",
                        attempt,
                        len(_START_RETRY_DELAYS) + 1,
                        exc,
                    )
                    await asyncio.sleep(_START_RETRY_DELAYS[attempt - 1])
            except BaseException:
                await self._cleanup_failed_start()
                self._remove_agent_config_root()
                raise
        assert last is not None
        # Every attempt is spent: no agent will ever run from this root, so drop it
        # rather than strand an opencode cache tree under the user's home.
        self._remove_agent_config_root()
        raise last

    def _set_aside_agent_store(self) -> Path | None:
        """Move the agent's unreadable database out of the way and drop the
        session pin that named a row inside it, so the next attempt opens a
        fresh store and creates a fresh session (``native_state()`` pins the
        new id after the start). The path is where the agent keeps its store
        under the chat's harness directory."""
        db_path = self._harness_dir / "agent" / "agent.db"
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        aside = set_aside_agent_store(db_path, stamp=stamp)
        if aside is not None:
            self._config.harness_native.pop("agent_session_id", None)
        return aside

    async def _cleanup_failed_start(self) -> None:
        """Tear down whatever a failed :meth:`_start_once` left behind so the
        next attempt (or the caller's error path) starts clean — never two
        children, never a leaked HTTP client or Job Object handle."""
        client = self._state.http_client
        self._state.http_client = None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.aclose()
        proc = self._state.proc
        self._state.proc = None
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=TERMINATE_GRACE_SECONDS)
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
        if proc is not None:
            # The Windows Job Object binding it to us (a no-op on POSIX).
            release(proc.pid)
        await self._sandbox_after_exit()
        self._state.base_url = ""

    async def _start_once(self) -> None:
        """One spawn → bind → ready attempt; :meth:`start` drives the retries."""
        self._harness_dir.mkdir(parents=True, exist_ok=True)
        # Owner-only: this root holds the agent's config + the user's global
        # instructions, and lives under ALKERA_HOME alongside the auth file.
        self._ensure_agent_config_root()
        # Sweep config roots abandoned by an alkera that was killed before it could
        # run stop() — they're pure scratch, and unswept they pile up one per chat.
        sweep_stale_agent_config_roots(keep=self._agent_config_root)
        # Reap any orphaned opencode subprocess left by a prior crashed
        # alkera process for THIS chat. We're confident it's ours
        # because the pid file lives inside our isolated .runtime/.
        await self._reap_orphan_opencode()

        password = secrets.token_urlsafe(32)
        self._state.password = password

        # Clear a stale listen-url file from a prior run; opencode rewrites it on bind.
        with contextlib.suppress(OSError):
            ChatTree(self._agent_state_dir).unlink(_LISTEN_URL_FILE)

        # The sandbox is decided before the environment: under gVisor the
        # agent's config must name the tool server at the address the container
        # can reach, and the server must bind where the daemon can reach it.
        self._state.sandbox_planned = False
        self._state.sandbox_plan = self._sandbox_plan()
        self._state.sandbox_planned = True
        launch_env = self._build_env(password)
        opencode_secrets.write_agent_secrets(self._agent_state_dir, launch_env.secrets)
        try:
            proc = await self._spawn_opencode(launch_env.env)
        except HarnessStartRefusedError:
            # The box offers less sandbox than the chat requires: a respawn
            # meets the same box.
            raise
        except Exception as exc:
            raise HarnessStartError(f"harness subprocess failed to start: {exc}") from exc
        self._state.proc = proc

        # Crash-recovery breadcrumb (pid + creation time, so the reaper can
        # confirm identity rather than blindly trusting a possibly-recycled
        # PID). Also register in the central agent registry so the startup
        # orphan sweep can reap it if our parent alkera dies ungracefully
        # (the macOS no-orphan net).
        write_pid_breadcrumb(self._harness_dir / "pid", proc.pid)
        register_agent(proc.pid, pid_file=self._harness_dir / "pid")

        try:
            base_url = await self._await_listen_url(proc)
        except Exception as exc:
            # Kill the half-started subprocess so we don't leak it.
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            raise HarnessStartError(str(exc)) from exc
        base_url = self._agent_address(base_url)
        self._state.base_url = base_url

        # HTTP client with persistent connection pool + auth.
        self._state.http_client = httpx.AsyncClient(
            base_url=base_url,
            auth=("opencode", password),
            headers={
                "x-opencode-directory": agent_directory(self._state.sandbox_plan, self._config)
            },
            timeout=_agent_timeout(_CONTROL_READ_TIMEOUT_SECONDS),
        )

        # Wait for opencode to be answering pings before we go further.
        await self._wait_for_ready()
        await opencode_secrets.require_agent_auth(self._state.http_client)

        # Create or attach to the opencode session.
        self._state.opencode_session_id = await self._ensure_opencode_session()

        # Kick off background tasks.
        self._state.sse_task = asyncio.create_task(self._consume_sse(), name="opencode-sse")
        self._state.process_watch_task = asyncio.create_task(
            self._watch_process(), name="opencode-process-watch"
        )
        self._state.started = True

    async def stop(self) -> None:
        if self._state.stopping:
            return
        self._state.stopping = True

        # Cancel background tasks first.
        for task_name in ("sse_task", "process_watch_task", "idle_watchdog_task"):
            task: asyncio.Task[None] | None = getattr(self._state, task_name)
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

        # Close HTTP client.
        if self._state.http_client is not None:
            with contextlib.suppress(Exception):
                await self._state.http_client.aclose()
            self._state.http_client = None

        # Terminate subprocess.
        proc = self._state.proc
        agent_pid = proc.pid if proc is not None else None
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=TERMINATE_GRACE_SECONDS)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(proc.wait(), timeout=KILL_GRACE_SECONDS)
        self._state.proc = None

        # Release the Windows Job Object handle (no-op on POSIX). The child is
        # already terminated above; closing our handle also reaps any straggler.
        if agent_pid is not None:
            release(agent_pid)
        await self._sandbox_after_exit()

        # Drop the pid + registry breadcrumbs.
        with contextlib.suppress(FileNotFoundError):
            (self._harness_dir / "pid").unlink()
        if agent_pid is not None:
            unregister_agent(agent_pid)

        # The agent's config/cache/state root is scratch for THIS chat's harness
        # process: opencode recreates all of it at startup, and the one file we
        # author there (global-instructions.md) is re-materialized on every start.
        # It lives under the user's HOME, where nothing else ever deletes it, so
        # tear it down with the process that used it. The chat's own `.runtime`
        # data dir is untouched: it holds the agent DB a later resume attaches to.
        self._remove_agent_config_root()

        # A held idle must not fire after the bus closes.
        self._drop_held_idle()

        # Close the bus last so any final synthesized events flow out.
        await self._bus.close()

    # ------------------------------------------------------------------
    # Operations
    # ------------------------------------------------------------------

    async def refresh_tools(self) -> None:
        """Have opencode connect each Alkera MCP mount again. It lists a
        remote server's tools once, when the client is created, and answers
        every later ``tools()`` read from that cached list; the loopback
        server is stateless and so can never push ``tools/list_changed`` at
        it. ``POST /mcp/{name}/connect`` re-creates the client, which lists
        again and replaces the cached set, so the next prompt is composed
        over the tools the session has now. A mount that cannot be reconnected
        keeps its list, logged: the turn runs either way."""
        client = self._state.http_client
        alkera_mcp = self._config.harness_native.get("alkera_mcp")
        if client is None or not isinstance(alkera_mcp, Mapping):
            return
        for name in alkera_mcp:
            try:
                resp = await client.post(f"/mcp/{name}/connect")
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                logger.warning(
                    "chat %s: the agent could not re-list the %s tools (%s); it keeps the set "
                    "it listed at connect",
                    self._config.session_id,
                    name,
                    exc,
                )

    async def send_prompt(self, prompt: PromptInput) -> None:
        self._require_ready()
        assert self._state.http_client is not None
        sid = self._state.opencode_session_id
        if sid is None:
            raise HarnessNotReadyError("no agent session yet")
        # CRITICAL: post to the ASYNC prompt endpoint. The sync
        # `/session/{sid}/message` endpoint BLOCKS until the entire turn
        # completes and returns the assistant message — so any long turn
        # (subagents, big explorations) overruns our HTTP read timeout
        # with a ReadTimeout, and retrying would re-run the turn.
        # `prompt_async` returns 204 immediately; the turn unfolds over
        # the `/event` SSE stream we already consume.
        # opencode expects the prompt + parts + provider/model fields.
        # We pass minimum viable shape; tool selection happens server-side.
        parts: list[dict[str, Any]] = []
        if prompt.system:
            # Inject the per-turn directive (plan-mode rules) as a
            # SYNTHETIC text part that leads the user message. This is
            # the highest-reliability steering channel — opencode itself
            # steers the model with in-conversation `<system-reminder>`
            # blocks (prompt.ts:1421), and a leading synthetic part sits
            # right where the model reads, unlike the `system` field
            # which is appended last in the system block and gets
            # under-weighted. `synthetic: true` keeps it out of our UI
            # (the renderer skips synthetic parts) and opencode's own
            # reminder loop skips it too (prompt.ts:1419).
            parts.append(
                {
                    "type": "text",
                    "text": f"<system-reminder>\n{prompt.system}\n</system-reminder>",
                    "synthetic": True,
                }
            )
        parts.append({"type": "text", "text": prompt.text})
        parts.extend(prompt.parts)
        body: dict[str, Any] = {"parts": parts}
        effective_model = await self._admit_turn_model(prompt)
        if effective_model:
            # Nested (`PromptInput`); top-level ids are dropped without an error.
            body["model"] = opencode_model_ref(effective_model)
            # Remember the active model so a manual /compact can reuse it.
            self._state.last_model = dict(effective_model)
        if prompt.agent and prompt.agent != "plan":
            # Routes the turn through a specific opencode agent (a subagent).
            # (PromptInput schema: prompt.ts:1685.)
            body["agent"] = prompt.agent
        # Plan mode is deliberately NOT routed to opencode's built-in "plan" agent.
        # That agent injects a system reminder (plan.txt) ordering the model that ANY
        # file edit is "STRICTLY FORBIDDEN ... ZERO exceptions ... overrides ALL other
        # instructions" — which shouts down OUR file-based plan steering ("write your
        # plan to <sandbox>/plan.md, then present_plan(path)"), so the model refuses to
        # write the plan file at all. Instead we drive plan mode exactly like read_only:
        # the default agent + OUR per-turn steering (delivered above as a
        # <system-reminder>) + the broker policy, which refuses project writes but
        # auto-allows writes confined to the chat sandbox (ChatSession._is_sandbox_write).
        # present_plan stays available regardless of agent (it's gated on the question
        # tool, not the plan agent — registry.ts).
        if display := self._thinking_display(prompt, effective_model):
            body["thinkingDisplay"] = display
        # NOTE: we deliberately do NOT also set `body["system"]`. The
        # synthetic <system-reminder> text part above is the channel
        # that actually steers the model (opencode itself uses the same
        # mechanism — prompt.ts:1421). The `system` field is appended
        # LAST in opencode's system block (request.ts:60) where the
        # model under-weights it, and duplicating the directive there
        # just bloats prompt cost.
        # The attempt is registered BEFORE the POST (its frames may land before
        # this coroutine resumes) and abandoned if the POST does not complete.
        # `previous` is set only for a transfer: a pending prompt that a `busy`
        # promoted during the failing POST keeps its id (the run is real).
        ctx = self._translator_ctx
        previous = ctx.attempt_id if ctx.attempt_live else None
        ctx.open_attempt(prompt.turn_id)
        try:
            resp = await self._state.http_client.post(f"/session/{sid}/prompt_async", json=body)
            resp.raise_for_status()
        except BaseException as exc:
            ctx.abandon_attempt(prompt.turn_id, previous)
            if isinstance(exc, httpx.HTTPStatusError):
                # Include the status + a snippet of the response body — these
                # carry the real reason (e.g. 400 validation, 500 stack).
                body_text = exc.response.text.strip()
                snippet = f": {body_text[:ERROR_BODY_CHARS]}" if body_text else ""
                raise HarnessCrashError(
                    f"send_prompt POST failed: HTTP {exc.response.status_code}{snippet}"
                ) from exc
            if isinstance(exc, httpx.HTTPError):
                # Transport-level errors (connect/read/protocol) often have an
                # empty str(), so surface the exception TYPE explicitly.
                detail = str(exc).strip() or "(no detail)"
                raise HarnessCrashError(
                    f"send_prompt POST failed: {type(exc).__name__}: {detail}"
                ) from exc
            raise

    async def cancel(self) -> None:
        if not self._state.started or self._state.crashed:
            return
        assert self._state.http_client is not None
        sid = self._state.opencode_session_id
        if sid is None:
            return
        # The attempt closes BEFORE the abort POST: opencode publishes the whole
        # abort tail before answering, and the SSE task may translate it while
        # this coroutine awaits. An idle HELD for an open tool part is a real
        # terminal subscribers never saw, so the cancel that discards it speaks
        # for its attempt (the runtime already settled; the UI still needs the
        # "Aborted" resolution).
        held = self._state.held_idle
        stamp = self._translator_ctx.end_attempt()
        if stamp is None and held:
            stamp = held[-1].turn_id
        with contextlib.suppress(httpx.HTTPError):
            await self._state.http_client.post(f"/session/{sid}/abort")
        await self._synthesize_close_events(stamp, stop_reason="cancelled")

    async def compact(self) -> None:
        """Force a context compaction of the session.

        POSTs opencode's `/session/{sid}/summarize` (auto=false). opencode
        then runs its compaction agent: the summary streams as a
        `summary:true` assistant message and `session.compacted` fires —
        both flow back through the SSE consumer + translator, surfacing as
        a single `CompactionApplied` event (the summary message itself is
        suppressed). Uses the last-prompted model, falling back to the
        configured default. opencode applies the compaction to its own
        session store, so subsequent turns (and resumes) use only the
        summarized context."""
        self._require_ready()
        assert self._state.http_client is not None
        sid = self._state.opencode_session_id
        if sid is None:
            raise HarnessNotReadyError("no agent session yet")
        model = self._state.last_model or self._default_model()
        body = {
            "providerID": model.get("provider_id"),
            "modelID": model.get("model_id"),
            "auto": False,
        }
        try:
            resp = await self._state.http_client.post(
                f"/session/{sid}/summarize", json=body, timeout=_BLOCKING_TIMEOUT
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            body_text = exc.response.text.strip()
            snippet = f": {body_text[:ERROR_BODY_CHARS]}" if body_text else ""
            raise HarnessCrashError(
                f"compact POST failed: HTTP {exc.response.status_code}{snippet}"
            ) from exc
        except httpx.HTTPError as exc:
            detail = str(exc).strip() or "(no detail)"
            raise HarnessCrashError(f"compact POST failed: {type(exc).__name__}: {detail}") from exc

    async def clear(self) -> None:
        """Reset the conversation context to empty. opencode has no clear-in-place
        route, so a brand-new session is minted via `POST /session` and re-pinned.
        The new session is created FIRST, so a creation failure leaves the adapter
        untouched. `ConversationCleared` is synthesized here (opencode has no
        native event for a client-orchestrated clear); the swapped id reaches
        resume via `native_state()`. Workspace files are never touched."""
        self._require_ready()
        assert self._state.http_client is not None

        # Create the fresh session first — if this fails, nothing else has
        # changed and the existing session stays usable.
        new_sid = await self._create_opencode_session()

        # Stop any in-flight turn on the OLD session so it can't emit events
        # after the boundary, and finalize whatever was still streaming.
        ctx = self._translator_ctx
        in_flight = ctx.attempt_live or ctx.pending_attempt is not None or ctx.open_parts
        stamp = ctx.end_attempt()
        old_sid = self._state.opencode_session_id
        if old_sid is not None:
            with contextlib.suppress(httpx.HTTPError):
                await self._state.http_client.post(f"/session/{old_sid}/abort")
        if in_flight:
            await self._synthesize_close_events(stamp, stop_reason="cancelled")

        # Capture the elided message ids for the audit event before the
        # translator forgets them.
        cleared = list(ctx.message_order)

        self._state.opencode_session_id = new_sid
        # Forget all cross-event state so stale ids from the old session
        # can't leak into post-clear events.
        self._translator.reset()

        await self._bus.publish(
            ConversationCleared(
                event_id=self._new_event_id(),
                time=self._now(),
                session_id=self._our_session_id(),
                cleared_message_ids=cleared,
            )
        )
        logger.info(
            "cleared chat %s: agent session %s → %s",
            self._our_session_id(),
            old_sid,
            new_sid,
        )

    def serves_model(self, model: Mapping[str, str]) -> bool:  # config is read once, at spawn
        return config_declares(self._opencode_config(), model)

    def _default_model(self) -> dict[str, str]:
        """The model the injected config names, as the `{provider_id, model_id}`
        shape prompts + /summarize use. Raises `HarnessModelError` when the
        config names none: there is no model to default to."""
        model = _split_model_ref(self._opencode_config().get("model"))
        if model is None:
            raise HarnessModelError(NO_MODEL_MESSAGE)
        return model

    async def _admit_turn_model(self, prompt: PromptInput) -> dict[str, str] | None:
        """The `{provider_id, model_id}` this turn sends (None = opencode runs the
        config's own `model`), once it is known to be one the injected config
        routes through the gateway. Otherwise the turn is refused: the refusal is
        published as an error status for the reader — stamped with the attempt so
        the surface settles this turn — and raised as `HarnessModelError`.

        The provider must be one the config's `provider` map declares. The
        gateway builder declares only `alkera-*` providers pointed at the gateway
        (and the e2e runner its mock), so a built-in provider — opencode's own
        hosted one, `anthropic`, `openai`, … — can never be reached by naming it,
        whatever keys or auth files the machine holds."""
        try:
            effective = self._effective_prompt_model(prompt)
            config = self._opencode_config()
            model = effective if effective is not None else _split_model_ref(config.get("model"))
            if model is None or not model.get("provider_id") or not model.get("model_id"):
                raise HarnessModelError(NO_MODEL_MESSAGE)
            declared = config.get("provider")
            if not isinstance(declared, dict) or model["provider_id"] not in declared:
                named = f"{model['provider_id']}/{model['model_id']}"
                raise HarnessModelError(UNROUTED_MODEL_MESSAGE.format(model=named))
        except HarnessModelError as exc:
            await self._bus.publish(
                SessionStatusChanged(
                    event_id=self._new_event_id(),
                    time=self._now(),
                    session_id=self._our_session_id(),
                    status="error",
                    phase="error",
                    detail=str(exc),
                    turn_id=prompt.turn_id,
                )
            )
            raise
        return effective

    def _effective_prompt_model(self, prompt: PromptInput) -> dict[str, str] | None:
        """The `{provider_id, model_id}` to send: the prompt's own `model` (None
        = the model the session last ran); a variant is composed as `<base>::<variant>`
        onto the turn's own model (prompt, last sent, spawn, config default)."""
        if prompt.variant is None:
            return dict(prompt.model) if prompt.model else None
        base = (
            (prompt.model and dict(prompt.model))
            or (self._state.last_model and dict(self._state.last_model))
            or (self._config.model and dict(self._config.model))
            or self._default_model()
        )
        provider_id = str(base.get("provider_id", ""))
        base_id, _ = split_model_effort(str(base.get("model_id", "")))
        return {"provider_id": provider_id, "model_id": join_model_effort(base_id, prompt.variant)}

    def _thinking_display(
        self, prompt: PromptInput, effective_model: dict[str, str] | None
    ) -> str | None:
        """Derive display from the prompt's effective reasoning effort."""
        model = (
            effective_model
            or (self._config.model and dict(self._config.model))
            or (self._state.last_model and dict(self._state.last_model))
            or self._default_model()
        )
        effort = prompt.variant
        if effort is None:
            _, effort = split_model_effort(str(model.get("model_id", "")))
        return thinking_display_for_effort(effort)

    async def resolve_permission(
        self, request_id: str, option_id: str, *, reason: str | None = None
    ) -> None:
        self._require_ready()
        assert self._state.http_client is not None
        # Translate our IR option to opencode's three-valued reply.
        reply_map = {
            "allow_once": "once",
            "allow_always": "always",
            "reject_once": "reject",
            "reject_always": "reject",
            "cancelled": "reject",
        }
        reply = reply_map.get(option_id, "reject")
        if reply == "always" and request_id not in self._translator_ctx.scoped_always_requests:
            # opencode records THIS ask's own globs when it is told "always" and
            # then stops raising the ask for anything they match, so a standing
            # grant travels only for an ask we PROVED carries a real scope. Every
            # MCP-tool ask ships the match-everything glob, and an ask whose
            # record is gone — a stream reset, a restart, a client answering one
            # this adapter never saw — proves nothing. Answered once instead; the
            # decision engine has already written Alkera's own precise rule where
            # one applies, so an approval a person meant to stand still does.
            reply = "once"
        self._translator_ctx.scoped_always_requests.discard(request_id)
        body: dict[str, str] = {"reply": reply}
        # A reject's reason rides opencode's optional `message` field, and the
        # vendored opencode shows it verbatim as the call's error — the runtime
        # composed it (`refusal_feedback`), so a policy denial reads as the
        # policy's reason and never as the user's decision.
        if reason and reply == "reject":
            body["message"] = reason
        try:
            resp = await self._state.http_client.post(f"/permission/{request_id}/reply", json=body)
        except httpx.TransportError as exc:
            logger.warning("permission reply failed: %s", exc)
            return
        # opencode answers an unknown or already-settled ask with a 4xx; httpx
        # does not raise on it, so without this the refusal vanished with no
        # exception and no log line, and the reply was taken as delivered.
        resp.raise_for_status()

    async def answer_question(self, request_id: str, answers: list[list[str]]) -> None:
        self._require_ready()
        assert self._state.http_client is not None
        # opencode's reply payload: `{answers: Answer[]}` where each
        # Answer is `string[]` (per question/index.ts:71-76 +
        # routes/.../groups/question.ts:13). Coerce defensively.
        body = {"answers": [list(a) for a in answers]}
        try:
            await self._state.http_client.post(f"/question/{request_id}/reply", json=body)
        except httpx.HTTPError as exc:
            logger.warning("question reply failed: %s", exc)

    async def reject_question(self, request_id: str, reason: str | None = None) -> None:
        self._require_ready()
        assert self._state.http_client is not None
        # opencode's reject endpoint takes no payload. We log the
        # reason locally for the chat.jsonl record only.
        del reason  # surfaced via the IR's QuestionRejected event
        try:
            await self._state.http_client.post(f"/question/{request_id}/reject")
        except httpx.HTTPError as exc:
            logger.warning("question reject failed: %s", exc)

    # ------------------------------------------------------------------
    # Subscription
    # ------------------------------------------------------------------

    def subscribe(self) -> AsyncIterator[Event]:
        return self._bus.subscribe()

    # ------------------------------------------------------------------
    # Internal: subprocess spawn + readiness
    # ------------------------------------------------------------------

    async def _reap_orphan_opencode(self) -> None:
        """If a prior alkera process for this chat died ungracefully, an
        opencode subprocess is left running (the OS doesn't always cascade
        the kill to children) — it's still listening on its own port and
        still holding its DB locks, which would conflict with our new
        spawn. The pid file in our isolated ``.runtime/`` is the
        breadcrumb the prior run wrote on start().

        Reap policy — reaps ONLY a confirmed true orphan:
        - Read the breadcrumb (pid + creation time). Absent/garbage → no-op.
        - Confirm identity via `same_process` (alive AND creation time matches
          what we recorded). This rejects a recycled PID now owned by an
          unrelated process — we never `os.kill(pid, 0)` (on Windows that
          routes to TerminateProcess and would KILL the target). If it's not
          provably our prior agent, just drop the stale breadcrumb.
        - If confirmed, ask it to terminate, wait up to 3s, then force-kill.
        - Unlink the pid + registry breadcrumbs when done.

        Extra confidence that the PID is OUR opencode comes from the breadcrumb
        LIVING IN OUR CHAT DIR — opencode would never write another chat's pid
        here. Cross-platform via `alkera_core.process` + `orphan_sweep`."""
        pid_path = self._harness_dir / "pid"
        breadcrumb = read_pid_breadcrumb(pid_path)
        if breadcrumb is None:
            # Missing → nothing to do; garbage → clear it best-effort.
            with contextlib.suppress(OSError):
                pid_path.unlink(missing_ok=True)
            return
        pid, create_time = breadcrumb

        if not same_process(pid, create_time):
            # Dead, recycled to an unrelated process, or identity unconfirmable
            # — NOT a true orphan. Drop the stale breadcrumbs, kill nothing.
            with contextlib.suppress(OSError):
                pid_path.unlink()
            unregister_agent(pid)
            return

        logger.info("reaping orphaned agent subprocess pid=%d from prior run", pid)
        terminate_process(pid)

        # Poll for the terminate to take effect, on the same grace a live chat's
        # own close gives its agent.
        if not await _await_exit(pid, TERMINATE_GRACE_SECONDS):
            logger.warning("agent pid=%d ignored terminate; force-killing", pid)
            kill_process(pid)

        with contextlib.suppress(OSError):
            pid_path.unlink()
        unregister_agent(pid)

    def _ensure_agent_config_root(self) -> None:
        """Create the agent's config root owner-only. Goes through
        ``paths.ensure_home()`` first so a fresh install doesn't get an
        ``~/.alkera`` created at the default (group/world-readable) mode by our
        ``parents=True`` — the auth file lives there.

        Also stamps the ``.owner`` breadcrumb naming this process AND this host,
        which is what keeps :func:`sweep_stale_agent_config_roots` (running in any
        other ``alkera``, possibly on another machine sharing this home) from
        deleting a root that is still in use."""
        from alkera_cli.host import paths

        paths.ensure_home()
        self._agent_config_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        _write_owner_breadcrumb(self._agent_config_root / _AGENT_ROOT_OWNER_FILE)
        # The agent creates its config directory at startup and cannot under a
        # read-only root, so it is made here, beside the state it can write.
        (self._agent_config_dir / "agent").mkdir(parents=True, exist_ok=True, mode=0o700)
        self._agent_state_dir.mkdir(exist_ok=True, mode=0o700)

    def _remove_agent_config_root(self) -> None:
        """Delete this chat's agent config root. Best-effort — a stray handle a
        just-exited child still holds (Windows) must never fail a teardown."""
        shutil.rmtree(self._agent_config_root, ignore_errors=True)

    def _build_env(self, password: str) -> opencode_secrets.AgentLaunchEnv:
        # Only our own UX surfaces (question/plan_present/alkera_*) stay allowed;
        # read tools ask too (our classifier marks them read so the policy
        # auto-allows). `ALKERA_PERMISSION` merges into opencode's config
        # (config.ts) → agent ruleset. Each Alkera subagent is a SEPARATE opencode
        # process that also runs `_build_env`, so it inherits the same `"*": "ask"`
        # → its asks flow to the parent-shared broker (opencode's own task-tool
        # subagents are disabled via `"task": "deny"`, so the vendor
        # `deriveSubagentSessionPermission` "*": "allow" path is never reached).
        permission = dict(_OPENCODE_PERMISSION_ASK)
        # A fenced session keeps the `bash` auto-allow too. Where the parent-hosted
        # shell is registered, `bash` IS that tool, and the vendor's ask for an MCP
        # tool carries only the rule glob, so a vendor-side ask would be a second
        # prompt naming no command. The fence and the read-only refusal live in
        # `gate_shell_action`, over the real command, on every session.
        if self._config.parent_session_id is not None:
            # Subagents run headless — no human watches them — so the interactive
            # tools (plan approval + ask-user) would block forever. `deny` here is
            # not a runtime rejection: opencode's `Permission.disabled()` strips any
            # `pattern:"*" deny` tool from the toolset BEFORE it reaches the model
            # (request.ts `resolveTools`), so these are never even surfaced to a
            # subagent — exactly like `task`/`todowrite`/`skill`.
            permission["plan_present"] = "deny"
            permission["plan_exit"] = "deny"
            permission["question"] = "deny"
        # XDG_DATA_HOME stays the per-chat harness dir so the agent's store
        # (`<harness>/agent/agent.db`) stays where it is set aside from; config/state/cache/home go
        # to a root the agent can't quietly write into (see agent_config_root).
        # Each is spelled as the agent sees it: a sandbox binds these trees at
        # fixed internal paths, never where the host keeps them.
        plan = self._state.sandbox_plan
        env = build_agent_env(
            data_home=plan.data_home(self._harness_dir) if plan else str(self._harness_dir),
            config_home=agent_spelling(self._state.sandbox_plan, self._agent_config_dir),
            state_home=agent_spelling(self._state.sandbox_plan, self._agent_state_dir),
            listen_file=agent_spelling(self._state.sandbox_plan, self._listen_file),
            secrets_file=agent_spelling(self._state.sandbox_plan, self._secrets_file),
            permission=permission,
            fenced=self._config.fenced,
        )
        # Our opencode config, handed over in the secrets file. The single seam for
        # opencode config: the default names no model, and the gateway builder (or
        # the e2e harness) supplies it via `SessionConfig.harness_native["agent_config"]`.
        oc_config = self._opencode_config()
        self._add_global_instructions(oc_config)
        output_cap = _largest_output_limit(oc_config)
        if output_cap > 0:
            env[_OPENCODE_OUTPUT_CEILING_FLAG] = str(output_cap)
        env[_OPENCODE_BASH_TIMEOUT_FLAG] = str(DEFAULT_TIMEOUT_MS or NATIVE_SHELL_NEVER_MS)
        # Point opencode at our bundled ripgrep (when we ship one) instead of
        # letting it download rg from github.com on first glob/grep.
        self._apply_ripgrep_env(env)
        # POSIX only: swap the native shell for our parent-hosted one, advertised
        # under the bare name the model was pretrained on. Kept in lockstep with the
        # `bash` permission key above — both are guarded on os.name == "posix".
        if os.name == "posix":
            env[_OPENCODE_PARENT_SHELL_FLAG] = "true"
        # Drop the vendor's fetch tool whenever OUR web tools are mounted, so the
        # model never sees two fetch tools (nor the one it can't use). Set either way
        # — an inherited value from the user's shell must not decide this, and with
        # the mount off the vendor tool is the only fetch there is.
        if self._web_tools_mounted():
            env[_OPENCODE_PARENT_WEB_FETCH_FLAG] = "true"
        else:
            env.pop(_OPENCODE_PARENT_WEB_FETCH_FLAG, None)
        # Delegation, from the session's config rather than this process's env, so
        # an embedding host or a test decides it without mutating global state.
        env[_OPENCODE_SUBAGENTS_FLAG] = "true" if self._config.subagents_enabled else "false"
        # LAST: the reverse diff of everything above, so a command gets its original
        # environment back (a fenced agent's original is only what it was handed).
        original = bounded_agent_env(os.environ) if self._config.fenced else dict(os.environ)
        env[SHELL_ENV_RESTORE_VAR] = json.dumps(shell_env_restore(original, env))
        return opencode_secrets.launch_env(env, password, oc_config)

    def _web_tools_mounted(self) -> bool:
        """Whether this session serves the parent-hosted web tools.

        The runtime adds the dedicated `web` MCP mount to `alkera_mcp` only when the
        org's toggle registered `web.search`/`web.fetch` (harness `runtime`), so the
        presence of that mount IS the question "does the model have `web_fetch`" —
        asked of the same map the subprocess is configured from, not of a separate
        flag that could drift out of step with it.
        """
        alkera_mcp = self._config.harness_native.get("alkera_mcp")
        return isinstance(alkera_mcp, Mapping) and WEB_MCP_MOUNT in alkera_mcp

    def _apply_ripgrep_env(self, env: dict[str, str]) -> None:
        """Make opencode resolve OUR bundled `rg` and never download one.

        When the resolved binary carries a `ripgrep_path`, prepend its
        directory to PATH — opencode's `which("rg")` (see vendor `which.ts`)
        searches PATH first, so our rg wins over any system rg — and set the
        `ALKERA_DISABLE_RIPGREP_DOWNLOAD` gate so that, even in the
        impossible case PATH resolution misses, opencode fails loudly instead
        of fetching from github.com (zero outbound calls except our gateway).

        When there's no bundled rg (`ripgrep_path is None` — e.g. a bare
        bun-dev checkout), do nothing: opencode keeps its system-rg/download
        fallback, so glob/grep still work. Mutates ``env`` in place."""
        rg = self._binary.ripgrep_path
        if rg is None:
            return
        existing = env.get("PATH", "")
        env["PATH"] = f"{rg.parent}{os.pathsep}{existing}" if existing else str(rg.parent)
        env[_OPENCODE_RIPGREP_DISABLE_FLAG] = "true"

    def _opencode_config(self) -> dict[str, Any]:
        """The inline opencode config (→ ALKERA_CONFIG_CONTENT).

        Starts from the default (no model, snapshots off) and shallow-merges
        any caller override from `SessionConfig.harness_native["agent_config"]`
        — the gateway builder sets provider + model, the e2e harness points
        opencode at the mock provider + tunes compaction. Override keys win
        (e.g. `model`, `provider`, `compaction`).
        """
        merged: dict[str, Any] = {**_OPENCODE_DEFAULT_CONFIG}
        override = self._config.harness_native.get("agent_config")
        if isinstance(override, dict):
            merged.update(override)
        # The Alkera tool surface: emit the local-MCP block the
        # subprocess connects back through. The runtime injects it as
        # `harness_native["alkera_mcp"]`; merge into any existing `mcp` map so a
        # caller-provided server (e.g. an e2e override) isn't clobbered.
        alkera_mcp = self._config.harness_native.get("alkera_mcp")
        if isinstance(alkera_mcp, dict):
            merged["mcp"] = {**merged.get("mcp", {}), **alkera_mcp}
        # Under gVisor the agent has a network of its own: the daemon's
        # loopback is not its loopback, so the tool server is named at the host
        # end of the container's veth pair, where the box firewall admits
        # exactly this chat to exactly these ports.
        plan = self._state.sandbox_plan
        network = plan.network if plan is not None else None
        if network is not None and isinstance(alkera_mcp, dict):
            mcp = dict(merged.get("mcp", {}))
            for name, entry in alkera_mcp.items():
                if isinstance(entry, dict) and isinstance(entry.get("url"), str):
                    mcp[name] = {**entry, "url": at_host(entry["url"], network.host_ip)}
            merged["mcp"] = mcp
        return merged

    def _add_global_instructions(self, config: dict[str, Any]) -> None:
        """Materialize the user's GLOBAL instructions into the agent config root and
        append the ABSOLUTE path to opencode's `config.instructions[]`.

        The runtime puts the (already size-capped) content in
        `harness_native["global_instructions"]`. We write it to an absolute path
        and reference THAT — never a `~/`-relative path: `build_agent_env` points
        opencode's home/XDG away from the user's real home (`ALKERA_TEST_HOME`), so
        `~/` would expand into the config root and the file would not be found.
        opencode then renders the block as `Instructions from: <path>` and
        re-applies its own per-file/total size caps (the vendored instruction
        patch). Appended last so a repo `ALKERA.md`/`AGENTS.md` still leads. No
        content → no-op.

        It lives OUTSIDE the project (with the rest of the agent's config) because
        the agent's own `write` tool must not be able to rewrite the instructions
        it will be handed on the next turn.
        """
        content = self._config.harness_native.get("global_instructions")
        if not isinstance(content, str) or not content.strip():
            return
        self._ensure_agent_config_root()
        gi_path = self._agent_config_dir / "global-instructions.md"
        write_text_atomic(gi_path, content, mode=0o600)
        existing = config.get("instructions")
        instructions = list(existing) if isinstance(existing, list) else []
        instructions.append(agent_spelling(self._state.sandbox_plan, gi_path))
        config["instructions"] = instructions

    async def _spawn_opencode(self, env: dict[str, str]) -> asyncio.subprocess.Process:
        # A container with a network of its own binds every address it has —
        # its loopback and its end of the veth pair, which is the only way in
        # and is reachable from the host alone; the daemon's own loopback stays
        # the address everywhere else.
        if not self._state.sandbox_planned:
            self._state.sandbox_plan = self._sandbox_plan()
            self._state.sandbox_planned = True
        plan = self._state.sandbox_plan
        hostname = "0.0.0.0" if plan is not None and plan.network is not None else "127.0.0.1"  # noqa: S104 -- the container's own addresses, not the box's
        args = [
            *self._binary.prefix_args,
            "serve",
            "--hostname",
            hostname,
            "--port",
            "0",
        ]
        logger.info(
            "spawning agent: %s %s (src=%s)",
            self._binary.path,
            " ".join(args),
            self._binary.source,
        )
        # The spawn seam detaches the child from the terminal's Ctrl-C so a
        # console SIGINT doesn't kill the harness outright (our event-loop
        # SIGINT handler cancels just the current turn via the HTTP abort API),
        # binds it to our lifetime so it can't be orphaned, and gives it no
        # stdin: the agent server reads nothing from us there.
        # A cloud chat's agent server runs in its sandbox: under gVisor (runsc)
        # on a pool box, or with the per-chat uid + cgroup and no boundary on a
        # single-tenant box. Either way the launch's before-steps own the chat's
        # trees and make its cgroup (and, for gVisor, write the OCI config) first,
        # HOME is the sandbox home, and the command is the sandbox's own. A local
        # session, and any host with nothing to apply, is ``none`` with no
        # controls and spawns exactly as before.
        # Under a sandbox the box is Linux and the command is spelled for it,
        # whatever platform composes the launch, at the path the container
        # sees the binary at; with no sandbox the binary is this host's own
        # and keeps its native spelling.
        binary = plan.agent_path(self._binary.path) if plan is not None else str(self._binary.path)
        agent_argv = [binary, *args]
        launch = self._sandbox_launch(agent_argv, env)
        self._state.sandbox = launch
        # The command is composed before the steps run so a step that fails
        # is reported with the launch it was preparing.
        command = sandbox_command(agent_argv, launch)
        self._state.sandbox_wrapper = wrapper_argv(command, agent_argv)
        logger.info("agent launch for %s: %s", self._config.session_id, self._sandbox_explanation())
        if launch.before:
            try:
                await asyncio.to_thread(run_steps, launch.before)
            except SandboxRefusedError as exc:
                # A step the launch depends on failed on this box: a respawn
                # would run the same steps on the same box.
                raise self._refuse(exc) from exc
        env = launch.apply_env(env)
        proc = await spawn_async(
            sandbox_spec(agent_argv, sandbox=launch, env=env, stdout="pipe", stderr="pipe")
        )
        self._state.probes.register(self._config.session_id, SandboxPlan.probe_of(plan, proc.pid))
        after_spawn = launch.after_spawn(proc.pid)
        if after_spawn:
            await asyncio.to_thread(run_steps, after_spawn)
        await self._note_oom_baseline()
        return proc

    def _sandbox_plan(self) -> SandboxPlan | None:
        """The sandbox this session's agent server will run in, or ``None``
        for a session that runs unsandboxed.

        Only a fenced session (a cloud chat) is sandboxed; a local CLI or
        editor session runs in the person's own project as the person,
        unsandboxed. A fenced session's root is its working directory, and a
        fenced session that names none is refused rather than run unsandboxed:
        the fence is the binding, and a session bound by one never runs as the
        daemon in the daemon's directory because a working directory went
        missing. A host with no uid/cgroup to apply (macOS, tests) runs
        unsandboxed; everything else is :func:`plan_sandbox`.
        """
        config = self._config
        if not config.fenced:
            return None
        if config.working_dir is None:
            raise self._refuse(
                SandboxRefusedError(
                    "a bounded session has no working directory to sandbox; it is refused "
                    "rather than run in the daemon's own directory"
                )
            )
        cap = current_capability()
        settings = SandboxSettings.from_env()
        self._state.sandbox_host = cap.reason
        if settings.mode == "none" and not cap.controls:
            # Nothing to apply — a single-tenant box with no chat uid (not
            # root, or no setpriv), macOS, or the tests. Spawn exactly as an
            # unsandboxed session, and say so once per start.
            logger.info("chat %s runs unsandboxed on this box: %s", config.session_id, cap.reason)
            return None
        try:
            return plan_sandbox(
                config=config,
                folder=config.working_dir,
                cap=cap,
                settings=settings,
                binary=self._binary,
                harness_dir=self._harness_dir,
                agent_config_root=self._agent_config_root,
            )
        except SandboxRefusedError as exc:
            raise self._refuse(exc) from exc

    def _sandbox_launch(self, argv: Sequence[str], env: Mapping[str, str]) -> SandboxLaunch:
        """The launch for this session's agent server: the plan's spec with the
        agent's environment, composed by the plan's runtime. The plan is made
        here when the start did not make it first (a caller composing the
        launch on its own)."""
        if not self._state.sandbox_planned:
            self._state.sandbox_plan = self._sandbox_plan()
            self._state.sandbox_planned = True
        plan = self._state.sandbox_plan
        if plan is None:
            return NO_SANDBOX
        spec = replace(plan.spec, agent_env=dict(env))
        try:
            return plan.runtime.compose_launch(spec, argv)
        except SandboxRefusedError as exc:
            raise self._refuse(exc) from exc

    def _sandbox_explanation(self) -> str:
        """The box's account of this session's sandbox, for a refusal or a
        failed start: the mode it is set to, what the probe found, and — once
        a launch exists — the cgroup driver, the uid, the agent's home alias
        and the wrapper argv in front of the agent binary. Never the
        environment or a secret."""
        plan = self._state.sandbox_plan
        return explain_sandbox(
            mode=SandboxSettings.from_env().mode,
            host=self._state.sandbox_host or "not probed",
            spec=plan.spec if plan is not None else None,
            launch=self._state.sandbox,
            wrapper=self._state.sandbox_wrapper,
        )

    def _refuse(self, exc: SandboxRefusedError) -> HarnessSandboxRefusedError:
        """The refusal the reader sees: the sandbox's own sentence, then the
        box's account of itself, so the operator reads on the console what
        the box found and what it was about to run."""
        return HarnessSandboxRefusedError(f"{exc} ({self._sandbox_explanation()})")

    def _agent_address(self, listen_url: str) -> str:
        """Where the daemon reaches the agent server: the address it reported,
        except under gVisor, where what it bound is inside its own namespace and
        the daemon reaches it at the container's end of the veth pair."""
        plan = self._state.sandbox_plan
        network = plan.network if plan is not None else None
        if network is None:
            return listen_url
        return at_host(listen_url, network.container_ip)

    async def _sandbox_after_exit(self) -> None:
        """Run the launch's after-exit steps once the agent server is gone
        (the cgroup goes, and anything still in it). Never raises."""
        launch = self._state.sandbox
        self._state.sandbox = None
        if launch is None or not launch.after_exit:
            return
        try:
            await asyncio.to_thread(run_steps, launch.after_exit)
        except Exception:
            logger.warning("sandbox cleanup failed for %s", self._config.session_id, exc_info=True)

    async def _await_listen_url(self, proc: asyncio.subprocess.Process) -> str:
        """Resolve opencode's listen URL once the server binds.

        PRIMARY signal: opencode writes the URL to ``<sandbox>/listen-url`` (a
        synchronous, fully-flushed write — immune to stdout block-buffering,
        which is how a ``bun build --compile`` standalone behaves on a Windows
        pipe and silently never delivers the stdout banner). We poll that file.

        FALLBACK: the ``server listening on `` stdout banner is still honored if
        it arrives first (dev / older agents). stdout AND stderr are captured in
        the background — kept draining for the process's life so a full pipe
        can't wedge the agent — and on failure the error names the exit code,
        the resolved binary, and both stream tails (a bun startup error,
        missing DLL, etc. surfaces only on stderr).
        """
        listen_file = self._listen_file
        found: dict[str, str] = {}
        stdout_tail: deque[str] = deque(maxlen=STARTUP_TAIL_LINES)
        stderr_tail: deque[str] = deque(maxlen=STARTUP_TAIL_LINES)

        async def _capture(stream: asyncio.StreamReader, tail: deque[str], *, banner: bool) -> None:
            async for raw in stream:
                line = raw.decode("utf-8", errors="replace").rstrip()
                tail.append(line)
                logger.debug("agent %s: %s", "stdout" if banner else "stderr", line)
                if banner and "url" not in found:
                    idx = line.find(_LISTEN_MARKER)
                    if idx >= 0:
                        found["url"] = line[idx + len(_LISTEN_MARKER) :].strip()

        tasks: list[asyncio.Task[None]] = []
        if proc.stdout is not None:
            tasks.append(asyncio.create_task(_capture(proc.stdout, stdout_tail, banner=True)))
        if proc.stderr is not None:
            tasks.append(asyncio.create_task(_capture(proc.stderr, stderr_tail, banner=False)))

        def _detail() -> str:
            return (
                f"binary: {self._binary.path} (source={self._binary.source}); "
                f"{self._sandbox_explanation()}"
            )

        def _streams() -> str:
            out = "\n".join(stdout_tail)
            err = "\n".join(stderr_tail)
            return f"--- stdout ---\n{out}\n--- stderr ---\n{err}\n"

        def _read_file_url() -> str:
            try:
                text = ChatTree(listen_file.parent).read_bytes(listen_file.name).decode().strip()
            except (OSError, ValueError):
                return ""
            return text if text.startswith("http://") else ""

        async def _fail(message: str) -> HarnessStartError:
            # Let the captures flush a final beat so the tails are complete.
            with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=2.0)
            for t in tasks:
                t.cancel()
            return HarnessStartError(f"{message} ({_detail()}).\n{_streams()}")

        loop = asyncio.get_running_loop()
        deadline = loop.time() + LISTEN_TIMEOUT_S
        while True:
            # 1. Primary: the listen-url file (buffering-immune).
            url = _read_file_url()
            if url:
                return url  # captures keep draining for the process's life
            # 2. Fallback: the stdout banner.
            if "url" in found:
                return found["url"]
            # 3. The process died before reporting — name the cause. Drain the
            #    captures first, then re-check (a print-then-exit could have
            #    delivered the URL during that final flush).
            if proc.returncode is not None:
                err = await _fail(
                    f"harness exited before reporting its listen URL (exit code {proc.returncode})"
                )
                if url := _read_file_url():
                    return url
                if "url" in found:
                    return found["url"]
                raise err
            if loop.time() >= deadline:
                raise await _fail(f"harness didn't report a listen URL within {LISTEN_TIMEOUT_S}s")
            await asyncio.sleep(0.05)

    async def _wait_for_ready(self) -> None:
        """Ping opencode until it answers. The server already BOUND (we hold its
        listen URL), so it exists — give it a real window to answer its first
        request on a machine that's busy right after the spawn, instead of the
        old 1s budget that flaked under load."""
        assert self._state.http_client is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (READY_TIMEOUT_SECONDS or math.inf)
        while True:
            try:
                r = await self._state.http_client.get("/doc")
                if r.status_code < 500:
                    return
            except httpx.HTTPError:
                pass
            if loop.time() >= deadline:
                raise HarnessStartError("harness never reached ready state")
            await asyncio.sleep(READY_POLL_SECONDS)

    async def _ensure_opencode_session(self) -> str:
        """Find an existing opencode session in this chat's isolated
        state dir (`.runtime/`) and attach, or create a fresh
        one on first run.

        Resume precedence (deterministic):

        1. If the manifest pinned an opencode session id from a prior
           run (via ``SessionConfig.harness_native["agent_session_id"]``,
           written by ``native_state()`` after the first start), attach
           to THAT exact id. If the id is gone (chat dir wiped, opencode
           storage corrupt, etc.) raise ``HarnessUnavailableError`` for a
           repair hint, never re-targeting; a cloud box's store is a cache,
           so there a fresh session opens (``choose_session``).
        2. Otherwise (brand-new chat), pick the latest by
           ``time.updated`` / ``time.created`` if any exist. Defensive
           fallback for chats that predate the pinning hook.
        3. Otherwise, POST a fresh one and return its id.

        Each chat owns its own XDG_DATA_HOME so opencode's SQLite is
        partitioned per chat. Pinning the id is what makes "resume
        this chat 6 months later" deterministic.
        """
        assert self._state.http_client is not None
        try:
            resp = await self._state.http_client.get("/session")
            resp.raise_for_status()
            sessions = resp.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code >= 500:
                # The agent is up and answering; what failed is its own store
                # — a database that came with the folder from another box.
                raise HarnessStoreUnreadableError(f"agent /session list failed: {exc}") from exc
            raise HarnessStartRefusedError(f"agent /session list failed: {exc}") from exc
        except httpx.HTTPError as exc:
            raise HarnessStartRefusedError(f"agent /session list failed: {exc}") from exc

        chosen = choose_session(
            sessions,
            self._config.harness_native.get("agent_session_id"),
            store_is_cache=self._config.store_is_cache,
            chat_id=self._config.session_id,
        )
        if chosen is not None:
            return chosen
        self._config.harness_native.pop("agent_session_id", None)
        return await self._create_opencode_session()

    async def _create_opencode_session(self) -> str:
        """POST a fresh, empty opencode session and return its id. Shared
        by first-run session creation and `clear()` (which mints a brand-new
        session to reset the conversation context)."""
        assert self._state.http_client is not None
        try:
            resp = await self._state.http_client.post("/session", json={})
            resp.raise_for_status()
            data = resp.json()
            sid = data.get("id") or data.get("session_id")
            if not sid:
                raise HarnessStartRefusedError(f"agent /session returned no id: {data!r}")
            logger.info("created fresh agent session %s", sid)
            return str(sid)
        except httpx.HTTPError as exc:
            raise HarnessStartRefusedError(f"agent /session create failed: {exc}") from exc

    # ------------------------------------------------------------------
    # Internal: process watcher (detect crash)
    # ------------------------------------------------------------------

    async def _watch_process(self) -> None:
        """Wait for the subprocess to exit. If it exits while `started`
        is True and we're not actively stopping, declare a crash."""
        proc = self._state.proc
        if proc is None:
            return
        try:
            rc = await proc.wait()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("subprocess.wait() raised", exc_info=True)
            rc = -1
        if self._state.stopping:
            # Expected exit on shutdown.
            return
        # Crash. Surface synthetic events + flip state. The reason is read
        # off the cgroup BEFORE the after-exit steps remove it.
        logger.warning("agent subprocess exited (rc=%s) — declaring crash", rc)
        detail = await self._exit_detail(rc)
        await self._sandbox_after_exit()
        await self._declare_crash(detail)

    async def _exit_detail(self, rc: int | None) -> str:
        """The crash detail for an agent process that exited with ``rc``: the
        bare exit, plus — when the chat's cgroup says the kernel killed for
        memory — the chat's limit, so the reader is told the limit rather
        than that the workspace stopped answering. Never raises."""
        detail = f"agent exited unexpectedly (rc={rc})"
        launch = self._state.sandbox
        if launch is None or launch.memory_events is None or launch.memory_mb is None:
            return detail
        kills = await self._note_oom_observed()
        if self._state.oom_kills_seen > self._state.oom_kills_at_spawn:
            return f"{detail}: {memory_limit_detail(launch.memory_mb)}"
        # The tree could not be read at all: systemd or the container runtime
        # took the cgroup down before this ran, so the counter is lost. A
        # bounded agent nobody here stopped that ended on SIGKILL was ended by
        # the kernel — the only sender of that signal into a chat's cgroup on
        # a box the daemon runs — and the limit is the reason. A tree that IS
        # readable and says no kill is believed over the exit status.
        if kills is None and rc in SIGKILL_EXIT_STATUSES:
            return f"{detail}: {memory_limit_detail(launch.memory_mb)}"
        return detail

    async def _note_oom_observed(self) -> int | None:
        """Read the chat cgroup's ``oom_kill`` count now and keep the highest
        seen; ``None`` when nothing under the launch's path could be read.
        Never raises."""
        launch = self._state.sandbox
        if launch is None or launch.memory_events is None:
            return None
        try:
            kills = await asyncio.to_thread(read_oom_kills_under, launch.memory_events)
        except Exception:
            logger.debug("memory.events unreadable for %s", self._config.session_id, exc_info=True)
            return None
        if kills is not None:
            self._state.oom_kills_seen = max(self._state.oom_kills_seen, kills)
        return kills

    async def _note_oom_baseline(self) -> None:
        """Remember the chat cgroup's ``oom_kill`` count at spawn, so a kill
        from an earlier life of the same cgroup (a slice a crashed daemon
        never stopped) is not read as this agent's. Never raises."""
        self._state.oom_kills_at_spawn = 0
        self._state.oom_kills_seen = 0
        launch = self._state.sandbox
        if launch is None or launch.memory_events is None:
            return
        try:
            self._state.oom_kills_at_spawn = await asyncio.to_thread(
                read_oom_kills, launch.memory_events
            )
        except Exception:
            logger.debug("memory.events unreadable for %s", self._config.session_id, exc_info=True)

    async def _declare_crash(self, detail: str) -> None:
        """Flip to crashed and close the live attempt — at most once.

        Two watchers can reach the same dead process (the subprocess waiter and
        the event stream noticing it has nothing left to talk to); the reader
        must see one terminal, not two."""
        if self._state.crashed:
            return
        self._state.crashed = True
        await self._synthesize_close_events(
            self._translator_ctx.end_attempt(),
            stop_reason="error",
            error_detail=detail,
        )

    # ------------------------------------------------------------------
    # Internal: SSE consumer
    # ------------------------------------------------------------------

    async def _consume_sse(self) -> None:
        """Connect to /event and translate native events to IR.

        Reconnect with exponential backoff on transport errors, for as long as
        opencode is alive. A transient read error on this stream says nothing
        about the turn: the agent keeps working through it and replays nothing,
        so the only honest reason to declare a crash is the agent being gone —
        its process exited, or it has refused its health endpoint for longer
        than ``SSE_UNHEALTHY_GIVE_UP_SECONDS``. Counting errors instead ended
        live turns: the errors accumulated over a session that may last days,
        and the count never came back down.
        """
        retry_idx = 0
        unhealthy_since: float | None = None
        loop = asyncio.get_running_loop()
        while not self._state.stopping:
            try:
                await self._sse_loop_once()
                # A clean return means opencode closed the stream — not fatal;
                # reconnect from the shortest delay.
                retry_idx = 0
                unhealthy_since = None
            except asyncio.CancelledError:
                raise
            except Exception:
                if self._state.stopping or self._state.crashed:
                    return
                delivered = self._state.sse_frames_this_connect > 0
                # The stream breaking is the earliest sign of a kill for
                # memory; the counter is read here, while the cgroup is
                # still there to be read, and kept for the exit's account.
                await self._note_oom_observed()
                if self._process_exited():
                    logger.warning("SSE stream failed and the agent process is gone")
                    proc = self._state.proc
                    rc = proc.returncode if proc is not None else None
                    await self._declare_crash(await self._exit_detail(rc))
                    return
                if delivered:
                    # This connection carried frames before it broke, so the
                    # agent was serving as recently as a moment ago.
                    unhealthy_since = None
                elif await self._agent_health_ok():
                    unhealthy_since = None
                else:
                    unhealthy_since = (
                        unhealthy_since if unhealthy_since is not None else loop.time()
                    )
                    if _unhealthy_too_long(loop.time() - unhealthy_since):
                        logger.warning("agent stopped answering /event and /doc; declaring crash")
                        await self._declare_crash("agent stopped answering")
                        return
                if delivered:
                    retry_idx = 0
                delay_ms = SSE_RETRY_DELAYS_MS[min(retry_idx, len(SSE_RETRY_DELAYS_MS) - 1)]
                retry_idx += 1
                logger.warning(
                    "SSE disconnected; reconnecting in %dms (attempt %d)",
                    delay_ms,
                    retry_idx,
                    exc_info=True,
                )
                await _sse_backoff(delay_ms)

    def _process_exited(self) -> bool:
        """Whether the agent child is known to be gone. Unknown (no child of our
        own, as when the adapter is driven over an injected transport) is not gone."""
        proc = self._state.proc
        return proc is not None and proc.returncode is not None

    async def _agent_health_ok(self) -> bool:
        """Whether opencode answers its doc endpoint right now."""
        client = self._state.http_client
        if client is None:
            return False
        try:
            resp = await client.get("/doc", timeout=_HEALTH_PROBE_TIMEOUT_SECONDS)
        except httpx.HTTPError:
            return False
        return resp.status_code < 500

    async def _sse_loop_once(self) -> None:
        """One iteration of the SSE connection loop. Returns when the server
        closes the stream cleanly; raises on transport error. Records how many
        frames this connection carried, which is how the caller tells a stream
        that broke mid-work from one that never served."""
        assert self._state.http_client is not None
        self._state.sse_frames_this_connect = 0
        async with aconnect_sse(
            self._state.http_client,
            "GET",
            "/event",
            timeout=httpx.Timeout(connect=5.0, read=SSE_READ_TIMEOUT_SECONDS, write=30.0, pool=5.0),
        ) as source:
            await self._iter_sse(source)

    async def _iter_sse(self, source: EventSource) -> None:
        async for sse in source.aiter_sse():
            self._state.sse_frames_this_connect += 1
            if not sse.data:
                continue
            try:
                payload = json.loads(sse.data)
            except json.JSONDecodeError:
                logger.warning("SSE payload not JSON: %r", sse.data[:200])
                continue
            await self._handle_native(payload)

    #: Native frames whose `sessionID` partitions them across a `clear`.
    _SESSION_STATUS_FRAMES = frozenset({"session.status", "session.idle", "session.error"})

    async def _handle_native(self, payload: dict[str, Any]) -> None:
        """Translate one native event and publish the resulting IR events.

        A single SSE event may translate to multiple IR events (e.g.
        session.status retry → SessionStatusChanged + Retrying); they
        publish in translation order.

        After a `clear` the shared `/event` stream still carries the OLD native
        session's tail; a status frame naming another session is dropped
        (one that names none, a plugin or global error, is attributed to the
        current attempt).
        """
        ours = self._state.opencode_session_id
        if ours is not None and payload.get("type") in self._SESSION_STATUS_FRAMES:
            native_sid = (payload.get("properties") or {}).get("sessionID")
            if native_sid and native_sid != ours:
                return
        ir = self._translate(payload)
        await self._answer_parent_gated_asks()
        if ir is None:
            return
        for ev in ir if isinstance(ir, list) else [ir]:
            await self._publish_ordered(ev)

    async def _answer_parent_gated_asks(self) -> None:
        """Answer the vendor asks raised for tools this process gates itself.

        The vendor asks before it calls a tool's body; for a parent-hosted tool
        that body is where the command is known and where Alkera's own ask is
        raised. Holding this one open would park the call short of that gate, so
        it is allowed once — the effect the vendor ruleset already has for every
        other parent-hosted mount — and never shown. A reply that cannot be
        delivered is logged and dropped: the vendor times the ask out on its own,
        and failing the SSE reader here would take the whole turn down.
        """
        pending = self._translator_ctx.parent_gated_requests
        while pending:
            request_id = pending.pop(0)
            try:
                await self.resolve_permission(request_id, "allow_once")
            except Exception:
                logger.warning(
                    "could not answer the vendor ask for a parent-gated tool (%s)",
                    request_id,
                )

    async def _publish_ordered(self, ev: Event) -> None:
        """Publish, enforcing the tool-events-before-idle ordering the adapter
        contract guarantees (README: "Event ordering guarantees").

        opencode publishes ``message.part.updated`` from a fiber forked
        after the DB commit (sync/index.ts `process`), but ``session.status``
        inline in the session's own fiber (status.ts `set`) — so under
        scheduler load an `idle` can reach the SSE stream while a tool part
        is still non-final. Consumers that stop reading at idle (every drain
        loop does) would then miss the tool's completed update. Hold such an
        idle and release it right after the real closing update lands; the
        watchdog covers the frame-lost case.
        """
        if isinstance(ev, SessionStatusChanged):
            if ev.status == "idle" and self._open_tool_parts():
                if self._state.held_idle is None:
                    self._state.held_idle = []
                self._state.held_idle.append(ev)
                if self._state.idle_watchdog_task is None:
                    self._arm_idle_watchdog()
                return
            # Any other status closes the hold window. With every tool call
            # closed the held idle goes first, keeping transitions in stream
            # order. With one still open the held idle drops unpublished — a
            # newer status supersedes it (an error after the race is the truth
            # of the turn), and idle ahead of the tool's result would read as
            # end-of-turn. The real idle arrives when the turn truly ends.
            if self._open_tool_parts():
                self._drop_held_idle()
            else:
                await self._flush_held_idle()
            await self._bus.publish(ev)
            return
        await self._bus.publish(ev)
        if isinstance(ev, ToolCallUpdate):
            self._translator_ctx.unpublished_tool_closures.discard(ev.tool_call_id)
        if self._state.held_idle is not None and not self._open_tool_parts():
            await self._flush_held_idle()

    def _open_tool_parts(self) -> list[_OpenPart]:
        return [p for p in self._translator_ctx.open_parts.values() if p.part_type == "tool_call"]

    def _arm_idle_watchdog(self) -> None:
        self._cancel_idle_watchdog()
        self._state.idle_watchdog_task = asyncio.create_task(
            self._idle_watchdog(), name="opencode-idle-hold"
        )

    def _cancel_idle_watchdog(self) -> None:
        task = self._state.idle_watchdog_task
        if task is not None and not task.done():
            task.cancel()
        self._state.idle_watchdog_task = None

    async def _flush_held_idle(self) -> None:
        held = self._state.held_idle
        if held is None:
            return
        self._state.held_idle = None
        self._cancel_idle_watchdog()
        for ev in held:
            await self._bus.publish(ev)

    def _drop_held_idle(self) -> None:
        """Discard a held idle unpublished: the caller emits (or the bus is
        closing on) a terminal status that supersedes it."""
        self._cancel_idle_watchdog()
        self._state.held_idle = None

    async def _idle_watchdog(self) -> None:
        """The tool's closing frame never arrived (an SSE reconnect drops
        in-flight frames and the stream has no replay): close the still-open
        tool parts synthetically — the turn DID finish (opencode declared
        idle after feeding the tool result to the model), only the status
        frame was lost — then release the held idle."""
        while True:
            grace = (
                self._idle_hold_grace
                if self._idle_hold_grace is not None
                else IDLE_TOOL_CLOSE_GRACE_SECONDS
            )
            if grace is None:
                # No grace at all: the held idle is released only by the real
                # closing frame (or the chat ending), never by this sweep.
                return
            await asyncio.sleep(grace)
            if self._state.held_idle is None:
                return
            if self._open_tool_parts():
                break
            if not self._translator_ctx.unpublished_tool_closures:
                # Nothing open and nothing in flight: the ordering this hold
                # protects is already satisfied, and no future publish is
                # coming to release the idle. Release it here — a hold left
                # standing is a session that never reports idle at all.
                # Detach from our own task handle before publishing, so the
                # release can't be cancelled by a flush cancelling us.
                pending = self._state.held_idle
                self._state.held_idle = None
                self._state.idle_watchdog_task = None
                for ev in pending or []:
                    await self._bus.publish(ev)
                return
            # A closure IS in flight: translate popped the part, but its
            # update is still a few awaits behind us in the publish loop.
            # Releasing now would put idle ahead of that update — the exact
            # inversion this hold exists to prevent. That publish releases
            # the idle itself; keep waiting in case it is slow.
        held = self._state.held_idle
        self._state.held_idle = None
        self._state.idle_watchdog_task = None
        if held is None:
            return
        now = self._now()
        sid = self._our_session_id()
        for part in self._open_tool_parts():
            self._translator_ctx.open_parts.pop(part.part_id, None)
            self._translator_ctx.synthetically_closed_tool_parts.add(part.part_id)
            await self._bus.publish(
                ToolCallUpdate(
                    event_id=self._new_event_id(),
                    time=now,
                    session_id=sid,
                    tool_call_id=part.part_id,
                    status="completed",
                    input=None,
                    output=None,
                    error_text=None,
                    metadata={"synthetic": True},
                )
            )
        for ev in held:
            await self._bus.publish(ev)

    # ------------------------------------------------------------------
    # Internal: opencode → IR translation (delegated)
    # ------------------------------------------------------------------

    def _translate(self, native: dict[str, Any]) -> Event | list[Event] | None:
        """Slim delegate for back-compat with the existing translator
        regression tests. The actual mapping logic lives on
        :class:`OpencodeEventTranslator` so it's testable without a live
        adapter — see :mod:`alkera_cli.harness.adapters.opencode_translate`."""
        return self._translator.translate(native)

    # ------------------------------------------------------------------
    # Synthetic close events (cancellation, crash, error)
    # ------------------------------------------------------------------

    async def _synthesize_close_events(
        self,
        stamp: str | None,
        *,
        stop_reason: str = "cancelled",
        error_detail: str | None = None,
    ) -> None:
        """Emit closing events for any open parts and one terminal status stamped
        ``stamp`` (what ``end_attempt`` returned when the caller closed the
        attempt). Idempotent, since it clears `open_parts` after firing."""
        # The synthesized terminal status supersedes any idle still held for
        # in-flight tool calls (those parts are force-closed below), and the
        # watchdog must not resurface the stale idle after it.
        self._drop_held_idle()
        now = self._now()
        sid = self._our_session_id()

        for part in list(self._translator_ctx.open_parts.values()):
            buffered = "".join(part.buffer)
            # Skip parts that buffered nothing: a turn aborted mid-step leaves
            # text/reasoning parts that opened but never received a token. A
            # finalized empty part renders as a content-less guardrail line (a
            # stray `│`) in the transcript and persists a blank reply — so the
            # only honest close for an empty part is to drop it.
            if not buffered:
                continue
            # Close each open part as its OWN type — a reasoning part that
            # was still streaming on cancel/crash must close as a
            # ReasoningPart, not a TextPart (otherwise the buffered
            # thinking text renders/persists as a normal assistant reply).
            closing: ReasoningPart | TextPart
            if part.part_type == "reasoning":
                closing = ReasoningPart(
                    part_id=part.part_id,
                    message_id=part.message_id,
                    text=buffered,
                )
            else:
                closing = TextPart(
                    part_id=part.part_id,
                    message_id=part.message_id,
                    text=buffered,
                )
            await self._bus.publish(
                PartCreated(
                    event_id=self._new_event_id(),
                    time=now,
                    session_id=sid,
                    part=closing,
                )
            )
        self._translator_ctx.open_parts.clear()

        # A cancel at idle publishes no terminal (the `HarnessAdapter.cancel`
        # contract); a crash publishes its error either way.
        if stamp is None and stop_reason != "error":
            return
        await self._bus.publish(
            SessionStatusChanged(
                event_id=self._new_event_id(),
                time=now,
                session_id=sid,
                status="error" if stop_reason == "error" else "aborted",
                phase="error" if stop_reason == "error" else "idle",
                detail=error_detail,
                turn_id=stamp,
            )
        )

    # ------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------

    def _require_ready(self) -> None:
        if not self._state.started:
            raise HarnessNotReadyError("agent adapter start() not yet called/finished")
        if self._state.crashed:
            raise HarnessGoneError("agent adapter is in a crashed state")
        if self._state.stopping:
            raise HarnessCrashError("agent adapter is stopping")

    def _our_session_id(self) -> str:
        """The session_id we use in our IR events — our chat session id.
        Distinct from opencode's session id (which is internal to opencode)."""
        return self._config.session_id

    def _now(self) -> datetime:
        return datetime.now(UTC)

    def _new_event_id(self) -> str:
        return secrets.token_hex(10)


# Touch the import to silence "unused import" — `Path` is used in type
# annotations only at present (stringized by `from __future__ import annotations`).
_USED_FOR_TYPE_ONLY: tuple[Any, ...] = (Path,)


__all__ = ["OpencodeHttpAdapter"]
