"""JSON-RPC 2.0 server over stdio.

Loops on stdin, dispatches each incoming envelope through the
``METHODS`` registry, writes responses to stdout. Also supports
**server-initiated** requests (daemon → extension) and notifications.

JSON-RPC envelope shape we handle:

    Request  { "jsonrpc": "2.0", "id": N|"s", "method": "name", "params": {...} }
    Response { "jsonrpc": "2.0", "id": N|"s", "result": {...} | "error": {...} }
    Notif    { "jsonrpc": "2.0",               "method": "name", "params": {...} }

Dispatch strips LSP framing, parses the envelope, looks up ``method``, validates
``params`` against its request model, awaits the handler, then validates + frames the
response (parse -> -32700, unknown method -> -32601, bad params -> -32602, handler
raise -> -32603).

For server-initiated requests, we keep an ``_outbound`` map of
``request_id → asyncio.Future`` and resolve when the client replies.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

if TYPE_CHECKING:
    LifecycleHook = Callable[["JsonRpcServer"], Awaitable[None]]

from alkera_core.extensions import ExtensionError
from alkera_core.json_safe import json_safe
from alkera_core.observability.sentry import capture_exception

from alkera_cli.account.auth_file import ProfileResolutionError
from alkera_cli.account.binding import BoundProfileGoneError
from alkera_cli.account.memberships import MembershipsError
from alkera_cli.daemon.extension_points import DAEMON_METHODS, DaemonMethods, RpcErrorAnswer
from alkera_cli.daemon.framing import (
    FramingError,
    read_frame_async,
    write_frame_async,
)
from alkera_cli.daemon.logging_setup import configure as _configure_logging
from alkera_cli.daemon.logging_setup import get_logger
from alkera_cli.daemon.paths import daemon_working_dir
from alkera_cli.daemon.pipes import PipeWriter, connect_pipe_reader, connect_pipe_writer
from alkera_cli.daemon.protocol import METHODS, MethodSpec
from alkera_cli.host.version_info import daemon_version

log = get_logger("alkera.daemon.server")


# --- JSON-RPC error codes (spec) -------------------------------------------

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
AUTH_REQUIRED = -32001  # custom: handler needs a logged-in user
SESSION_NOT_OPEN = -32002  # custom: the named chat session is not open here
MODEL_SWITCH_REFUSED = -32010  # custom: the chat may not move to that model; data says why
#: custom: no stored sign-in may act here (the project is pinned to another org,
#: or an asked-for org has no sign-in). The message is the line to show.
PROFILE_REFUSED = -32006

#: The codes this server answers with on its own account. A contributed error
#: answer may not borrow one: a client keys on them (it shows the sign-in panel
#: on AUTH_REQUIRED, reopens a chat on SESSION_NOT_OPEN), so a distribution's
#: error must never read as one of them. INTERNAL_ERROR is not reserved: an
#: answered error under it is an expected failure whose message is the sentence.
RESERVED_CODES = frozenset(
    {
        PARSE_ERROR,
        INVALID_REQUEST,
        METHOD_NOT_FOUND,
        INVALID_PARAMS,
        AUTH_REQUIRED,
        SESSION_NOT_OPEN,
        PROFILE_REFUSED,
        MODEL_SWITCH_REFUSED,
    }
)


# --- Startup / shutdown hooks ---------------------------------------------
#
# Method modules use these to attach per-server background work (auth file
# watcher, etc.) without the server needing direct knowledge of them.
# Registered at import time; fired once the event loop is running.

_STARTUP_HOOKS: list[LifecycleHook] = []
_SHUTDOWN_HOOKS: list[LifecycleHook] = []


def register_startup_hook(hook: LifecycleHook) -> LifecycleHook:
    """Run ``hook(server)`` after the server is constructed and the loop
    is live but before the first frame is read. Idempotent re-registration
    is up to the caller (we don't dedupe)."""
    _STARTUP_HOOKS.append(hook)
    return hook


def register_shutdown_hook(hook: LifecycleHook) -> LifecycleHook:
    """Run ``hook(server)`` once the read loop has exited and inflight
    handlers have drained. Exceptions are logged, not raised."""
    _SHUTDOWN_HOOKS.append(hook)
    return hook


class AuthRequiredError(Exception):
    """Raised by method handlers when the call needs cloud auth.

    The server's ``_invoke`` catches this and responds with JSON-RPC
    error code -32001 + ``data: {reason}`` so the editor extension can
    flip its auth-state machine without parsing prose.
    """

    def __init__(self, reason: str = "missing") -> None:
        super().__init__(f"auth required ({reason})")
        self.reason = reason


class ModelSwitchRefusedRpcError(Exception):
    """A model switch the chat may not make. Answered with ``-32010`` and
    ``data: {code, message, blocking_formats?, escape_new_chat_model?}`` so the
    editor renders the same refusal the web does."""

    def __init__(self, message: str, data: dict[str, Any]) -> None:
        super().__init__(message)
        self.data = {**data, "message": message}


class SessionNotOpenError(Exception):
    """Raised when a handler names a chat session this daemon does not hold.

    Session ids do not survive a daemon restart, so a stale id is an expected
    client condition. Answering with its own code keeps it out of the traceback
    log and Sentry.
    """

    def __init__(self, session_id: str) -> None:
        super().__init__("That chat is no longer open. Open it again to continue.")
        self.session_id = session_id


def contributed_error_answers(
    contributed: tuple[DaemonMethods, ...],
) -> tuple[RpcErrorAnswer, ...]:
    """Every contribution's error answers, in installation order.

    Refuses an answer under a reserved code, and an exception type two
    contributions both answer: which answer won would depend on the order the
    product happened to install them in."""
    answers: list[RpcErrorAnswer] = []
    owner: dict[type[Exception], str] = {}
    for contribution in contributed:
        for answer in contribution.errors:
            if answer.code in RESERVED_CODES:
                raise ExtensionError(
                    f"{contribution.name!r} answers {answer.error.__name__} with "
                    f"{answer.code}, a code the daemon reserves"
                )
            if answer.error in owner:
                raise ExtensionError(
                    f"{answer.error.__name__} is answered by both {owner[answer.error]!r} "
                    f"and {contribution.name!r}"
                )
            owner[answer.error] = contribution.name
            answers.append(answer)
    return tuple(answers)


class JsonRpcServer:
    """Owns the read loop, dispatch, and the outbound-request bookkeeping.

    One ``JsonRpcServer`` instance per daemon process. The instance is
    passed as the first argument to every method handler so handlers can
    issue notifications (``server.notify(...)``) and server→client
    requests (``await server.request(...)``).
    """

    def __init__(
        self,
        *,
        reader: asyncio.StreamReader,
        writer: PipeWriter,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._started_at = time.monotonic()
        self._shutdown_requested = asyncio.Event()
        self._outbound: dict[int, asyncio.Future[Any]] = {}
        self._outbound_seq = 0
        self._write_lock = asyncio.Lock()
        self._inflight: set[asyncio.Task[None]] = set()
        # Cache: per-process scratch dir exists for the life of the daemon.
        self._working_dir = daemon_working_dir()
        # Per-server slot owned by the auth methods. Holds the active
        # device-login session (device_code + background poll task) while a
        # login is in flight; None otherwise. Stored here (instead of as module
        # state) so tests can spin up multiple JsonRpcServer instances cleanly.
        self.pending_login: Any = None
        self._error_answers = contributed_error_answers(DAEMON_METHODS.items())

    # ------------------------------------------------------------------
    # Public surface for handlers
    # ------------------------------------------------------------------

    @property
    def version(self) -> str:
        return daemon_version()

    @property
    def uptime_seconds(self) -> float:
        return time.monotonic() - self._started_at

    @property
    def working_dir(self) -> os.PathLike[str]:
        return self._working_dir

    async def notify(self, method: str, params: BaseModel | None = None) -> None:
        """Fire-and-forget server→client notification."""
        envelope: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            envelope["params"] = params.model_dump(mode="json")
        await self._write(envelope)

    async def request(
        self,
        method: str,
        params: BaseModel | None,
        *,
        timeout_seconds: float | None = 30.0,
    ) -> Any:
        """Issue a server→client request and await the response.

        Returns the raw ``result`` JSON object; the caller is responsible
        for shape validation (Pydantic in the handler module).
        Raises ``TimeoutError`` if the client doesn't reply in time, and
        ``RuntimeError`` on a client-side JSON-RPC error.
        """
        self._outbound_seq += 1
        request_id = self._outbound_seq
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._outbound[request_id] = fut
        envelope: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }
        if params is not None:
            envelope["params"] = params.model_dump(mode="json")
        try:
            await self._write(envelope)
            if timeout_seconds is None:
                return await fut
            return await asyncio.wait_for(fut, timeout=timeout_seconds)
        finally:
            self._outbound.pop(request_id, None)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def serve(self) -> None:
        """Run until stdin closes, ``shutdown`` is invoked, or a signal hits."""
        log.info("daemon.start", version=self.version, pid=os.getpid())
        for hook in _STARTUP_HOOKS:
            try:
                await hook(self)
            except Exception:
                log.exception("daemon.startup_hook.failed", hook=hook.__name__)
        try:
            while not self._shutdown_requested.is_set():
                try:
                    frame = await read_frame_async(self._reader)
                except EOFError:
                    log.info("daemon.stdin.eof")
                    break
                except FramingError as exc:
                    log.warning("daemon.framing.error", error=str(exc))
                    continue

                envelope = self._parse(frame)
                if envelope is None:
                    continue

                # Dispatch in a task so a slow handler doesn't block the read loop.
                task = asyncio.create_task(self._handle(envelope))
                self._inflight.add(task)
                task.add_done_callback(self._inflight.discard)
        finally:
            # Connection is gone — wake up every in-flight server→client
            # request so resolvers stop hanging forever and can fast-reject.
            # Without this, a mid-prompt disconnect (editor crashed, user
            # closed the window, etc.) leaves the harness's prompt path
            # blocked permanently, holding the chat lock with no recourse.
            self._fail_outbound_on_disconnect()
            await self._drain_inflight()
            for hook in _SHUTDOWN_HOOKS:
                try:
                    await hook(self)
                except Exception:
                    log.exception("daemon.shutdown_hook.failed", hook=hook.__name__)
            log.info("daemon.stop", uptime_seconds=round(self.uptime_seconds, 2))

    def request_shutdown(self) -> None:
        """Signal the loop to stop after the current iteration."""
        self._shutdown_requested.set()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _parse(self, frame: bytes) -> dict[str, Any] | None:
        try:
            envelope = json.loads(frame.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            log.warning("daemon.parse.error", error=str(exc))
            # Per JSON-RPC spec, parse error responses use id=null since we
            # couldn't determine the request id.
            asyncio.get_running_loop().create_task(
                self._write_error(None, PARSE_ERROR, f"parse error: {exc}")
            )
            return None
        if not isinstance(envelope, dict):
            asyncio.get_running_loop().create_task(
                self._write_error(None, INVALID_REQUEST, "envelope must be a JSON object")
            )
            return None
        return envelope

    async def _handle(self, envelope: dict[str, Any]) -> None:
        # Server→client response coming back in?
        if "method" not in envelope and "id" in envelope:
            self._dispatch_outbound_response(envelope)
            return

        method_name = envelope.get("method")
        if not isinstance(method_name, str):
            await self._write_error(envelope.get("id"), INVALID_REQUEST, "missing method")
            return

        spec = METHODS.get(method_name)
        if spec is None:
            await self._write_error(
                envelope.get("id"), METHOD_NOT_FOUND, f"unknown method {method_name!r}"
            )
            return

        await self._invoke(spec, envelope)

    def _dispatch_outbound_response(self, envelope: dict[str, Any]) -> None:
        """Resolve an outstanding server→client request future."""
        rid = envelope.get("id")
        if not isinstance(rid, int):
            log.warning("daemon.outbound.bad_id", envelope=envelope)
            return
        fut = self._outbound.get(rid)
        if fut is None or fut.done():
            log.warning("daemon.outbound.stale", id=rid)
            return
        if "error" in envelope:
            err = envelope["error"] or {}
            fut.set_exception(RuntimeError(f"client error {err.get('code')}: {err.get('message')}"))
        else:
            fut.set_result(envelope.get("result"))

    async def _invoke(self, spec: MethodSpec, envelope: dict[str, Any]) -> None:
        rid = envelope.get("id")
        params_raw = envelope.get("params") or {}
        try:
            params = spec.request_type.model_validate(params_raw)
        except ValidationError as exc:
            await self._write_error(rid, INVALID_PARAMS, f"invalid params for {spec.name!r}: {exc}")
            return

        try:
            result = await spec.handler(self, params)
        except AuthRequiredError as exc:
            await self._write_error(rid, AUTH_REQUIRED, str(exc), data={"reason": exc.reason})
            return
        except BoundProfileGoneError as exc:
            # A chat's own sign-in was logged out: auth-required, never another one.
            await self._write_error(rid, AUTH_REQUIRED, str(exc), data={"reason": "missing"})
            return
        except ProfileResolutionError as exc:
            await self._write_error(rid, PROFILE_REFUSED, str(exc))
            return
        except SessionNotOpenError as exc:
            await self._write_error(
                rid, SESSION_NOT_OPEN, str(exc), data={"session_id": exc.session_id}
            )
            return
        except ModelSwitchRefusedRpcError as exc:
            await self._write_error(rid, MODEL_SWITCH_REFUSED, str(exc), data=exc.data)
            return
        except MembershipsError as exc:
            # The memberships read failed in a way already said in the sentence the
            # person reads, not a fault to page anyone over.
            await self._write_error(rid, INTERNAL_ERROR, str(exc))
            return
        except Exception as exc:
            answer = self._answer_for(exc)
            if answer is not None:
                data = answer.data(exc) if answer.data is not None else None
                await self._write_error(rid, answer.code, str(exc), data=data)
                return
            log.exception("daemon.handler.exception", method=spec.name)
            capture_exception(exc, method=spec.name)
            await self._write_error(rid, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
            return

        if not isinstance(result, spec.response_type):
            # Defensive: a handler may return the wrong shape; treat as internal error.
            log.error(
                "daemon.handler.bad_response",
                method=spec.name,
                got=type(result).__name__,
                want=spec.response_type.__name__,
            )
            await self._write_error(
                rid, INTERNAL_ERROR, f"handler returned wrong type for {spec.name!r}"
            )
            return

        # Notifications carry no id and expect no response.
        if rid is None:
            return
        await self._write({"jsonrpc": "2.0", "id": rid, "result": result.model_dump(mode="json")})

    def _answer_for(self, exc: Exception) -> RpcErrorAnswer | None:
        """The first contributed answer whose exception type ``exc`` is."""
        return next((a for a in self._error_answers if isinstance(exc, a.error)), None)

    async def _write(self, envelope: dict[str, Any]) -> None:
        # json_safe wraps any non-finite float ($nonfinite) so the JSON-RPC wire
        # is always strict-parseable by the vscode-jsonrpc TS client — the single
        # egress for EVERY daemon response, so nothing non-finite leaks regardless
        # of source (tool results are already wrapped upstream; this is the net).
        body = json.dumps(json_safe(envelope), separators=(",", ":")).encode("utf-8")
        async with self._write_lock:
            await write_frame_async(self._writer, body)

    async def _write_error(
        self,
        rid: Any,
        code: int,
        message: str,
        *,
        data: Any = None,
    ) -> None:
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        await self._write({"jsonrpc": "2.0", "id": rid, "error": error})

    async def _drain_inflight(self) -> None:
        if not self._inflight:
            return
        log.info("daemon.draining", inflight=len(self._inflight))
        await asyncio.gather(*self._inflight, return_exceptions=True)

    def _fail_outbound_on_disconnect(self) -> None:
        """Cancel every in-flight server→client request with a
        ``ConnectionError`` so resolvers waiting on the editor's
        response unblock immediately instead of hanging until the
        timeout (which is intentionally ``None`` for the harness
        prompt path)."""
        for rid, fut in list(self._outbound.items()):
            if not fut.done():
                fut.set_exception(ConnectionError(f"daemon client disconnected (request {rid})"))


# ---------------------------------------------------------------------------
# Entry point used by `alkera serve`
# ---------------------------------------------------------------------------


async def _make_stdio_streams() -> tuple[asyncio.StreamReader, PipeWriter]:
    """Wire asyncio streams to stdin/stdout.

    We re-open the stdio fds via ``os.fdopen`` instead of passing
    ``sys.stdin.buffer`` / ``sys.stdout.buffer`` directly. The Nuitka onefile
    bootstrap wraps those buffers in ways that make asyncio's pipe-transport
    constructors fail with ``"Pipe transport is only for pipes, sockets and
    character devices"``. Reopening from the raw fd dodges that and works
    identically under source-mode `uv run`.

    The pipe wiring itself is platform-split inside ``daemon.pipes``: POSIX
    uses the loop's pipe transports; Windows uses thread bridges (the
    ProactorEventLoop has no transport for anonymous-pipe/console handles).
    """
    import os

    stdin = os.fdopen(os.dup(sys.stdin.fileno()), "rb", buffering=0)
    stdout = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)

    reader = await connect_pipe_reader(stdin)
    writer = await connect_pipe_writer(stdout)
    return reader, writer


async def run_stdio(*, log_level: str = "info") -> None:
    """Configure logging, load every method, and run the server.

    Loading the methods (the open modules, then each distribution's
    contribution) populates ``METHODS`` via the ``@method`` decorators.
    """
    _configure_logging(log_level)

    # Sentry (gated on the CLI's DSN config + the unified telemetry opt-out;
    # no-op until a DSN is set, and skipped entirely when the user opted out via
    # ALKERA_TELEMETRY / the `telemetry_enabled` preference). Captures daemon
    # handler exceptions + harness crashes.
    from alkera_cli.observability.telemetry import reconcile_sentry

    reconcile_sentry("daemon")

    import contextlib as _contextlib

    from alkera_cli.daemon.methods import load_methods
    from alkera_cli.harness.orphan_sweep import (
        sweep_orphaned_agents,
        sweep_orphaned_seed_workers,
    )

    # Every open handler registers on importing `methods/`; each installed
    # distribution's contribution registers when it is loaded here.
    load_methods()
    # Reap any alkera-agent stranded by a prior daemon that died ungracefully
    # (the macOS no-orphan net; Win/Linux also bind children to parent death).
    with _contextlib.suppress(Exception):
        sweep_orphaned_agents()
    # Also reap lineage/KB seed workers a prior daemon orphaned (DevWatcher restart /
    # crash) so they don't pile up + chew CPU across restarts.
    with _contextlib.suppress(Exception):
        sweep_orphaned_seed_workers()

    # Front-load the harness's one-time first-run costs (staging the bundled
    # agent binary + opencode's one-time database migration) in the background,
    # so the user's FIRST chat starts as fast as every later one instead of
    # paying them inside its own open. Fire-and-forget; compiled builds only
    # (ALKERA_HARNESS_PREWARM overrides); the adapter awaits an in-flight
    # pre-warm before spawning so the two never race the migration.
    from alkera_cli.harness.prewarm import start_prewarm_in_background

    with _contextlib.suppress(Exception):
        start_prewarm_in_background()

    reader, writer = await _make_stdio_streams()
    server = JsonRpcServer(reader=reader, writer=writer)

    # Graceful shutdown on signals (Unix only — Windows lacks add_signal_handler).
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, server.request_shutdown)
        except (NotImplementedError, RuntimeError):
            # Windows / non-main-thread cases — fall back to KeyboardInterrupt.
            pass

    await server.serve()


__all__ = ["RESERVED_CODES", "JsonRpcServer", "contributed_error_answers", "run_stdio"]
