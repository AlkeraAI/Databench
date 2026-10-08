"""Parent-hosted loopback MCP server — the SINGLE Alkera tool transport shared
by BOTH harness backends.

Claude (``McpHttpServerConfig``) and OpenCode (``type:"remote"`` MCP) both
connect to this Streamable-HTTP server over ``127.0.0.1``. The decisive property:
dispatch runs HERE, in the parent process that owns the ``ChatSession`` — so the
session's ``PermissionBroker``, its live permission mode, and the loaded
``.alkera/permissions.yml`` are all reachable. A write/destroy/egress is gated
(Layer-A prompt) IDENTICALLY on both backends, instead of being unreachable from
an OpenCode-spawned child. The unbypassable connector chokepoint (Layer B) still
applies underneath.

One server per ``ChatSession``: a chat runs exactly one harness at a time, so the
handlers close over a single :class:`SessionToolBinding` (no per-request session
routing). The bearer token authenticates the loopback connection — any local
process could otherwise reach the ephemeral port.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from alkera_cli.cloud_sync.lease_scope import LEASE_CHAT
from alkera_cli.cloud_sync.shared_lease import invalidate_shared_lease
from alkera_cli.harness.sandbox import SandboxSettings
from alkera_cli.harness.web_flags import WebToolFlags

if TYPE_CHECKING:
    from alkera_cli.harness.permission_broker import PermissionBroker
    from alkera_cli.plugins.plugin_base.permissions.config import PermissionsConfig
    from alkera_cli.plugins.plugin_base.tool import ToolRegistry, ToolScope


#: The name of the dedicated web mount in a backend's MCP server map. Spelled
#: once: the runtime writes the entry under it, and the opencode adapter reads it
#: back to decide whether the vendor's own fetch tool should still be advertised.
WEB_MCP_MOUNT = "web"

_INCOMPLETE_RESPONSE_MSG = "ASGI callable returned without completing response"


class _DropIncompleteResponse(logging.Filter):
    """Drops uvicorn's benign ERROR for a streaming response that was cut — the
    agent's MCP client opens a short-lived stream that the STATELESS server closes
    each request, so uvicorn logs this once per request with nothing actionable."""

    def filter(self, record: logging.LogRecord) -> bool:
        return _INCOMPLETE_RESPONSE_MSG not in record.getMessage()


_transport_logs_quieted = False


def _quiet_transport_logs() -> None:
    """Silence the per-request loopback MCP transport noise (idempotent,
    process-wide — every per-chat server shares these loggers). The agent's MCP
    client streams against a stateless server, so each request the SDK logs an
    INFO ``Terminating session: None`` and uvicorn an ERROR ``ASGI callable
    returned without completing response``. Both are EXPECTED for stateless-HTTP +
    a streaming client and say nothing about tool calls (which succeed); without
    this they spam the daemon log roughly once per second per open chat."""
    global _transport_logs_quieted
    if _transport_logs_quieted:
        return
    _transport_logs_quieted = True
    logging.getLogger("mcp.server.streamable_http").setLevel(logging.WARNING)
    _filter = _DropIncompleteResponse()
    for name in ("uvicorn.error", "uvicorn"):
        logging.getLogger(name).addFilter(_filter)


#: How old a chat's connection scope may be when a tool call uses it.
CONNECTION_SCOPE_RECHECK_SECONDS = 60.0


@dataclass
class SessionToolBinding:
    """The per-session dispatch context the server threads into every tool call.

    ``registry`` is this session's view of the per-project registry (the
    project's tools less what this chat's org withholds); ``broker`` /
    ``permission_mode`` / ``permissions`` are this session's. ``permission_mode``
    is mutated in place by ``ChatSession.set_permission_mode`` so the gate
    always sees the live mode.
    """

    registry: ToolRegistry
    web_tools: WebToolFlags = field(default_factory=WebToolFlags)
    """This chat's org web-tool flags as last resolved — at open, then at the
    start of every turn. ``registry`` is derived from them, and re-derived from
    them when the project's registry is rebuilt, so a rebuild can never hand
    this chat another org's answer."""
    connection_ids: frozenset[str] | None = None
    """This chat's connection scope as last resolved — the team-record ids the
    server answered for it — resolved beside ``web_tools`` at open and at each
    turn start, and re-applied on a rebuild. ``None`` on a runtime with no scope
    (a person's own daemon: every connection in the store is theirs); a set on a
    shared box, where the store is the union over every chat it holds."""
    scope_chat_id: str = ""
    """The chat the scope is resolved for: the root chat for a subagent, so a
    child asks as the chat the server knows rather than as its own local id."""
    knowledge_owner: str = ""
    """The chat owner's user id, the principal a scoped view files and reads
    unattributed knowledge (notes, learned facts) for on a shared machine. Empty
    on a person's own daemon, and for a chat the box was not told the owner of,
    which then reads no note rather than everyone's."""
    broker: PermissionBroker | None = None
    permissions: PermissionsConfig | None = None
    session_id: str = ""
    owner_session_id: str = ""
    """The session whose sandbox identity owns the working tree: the session,
    or the parent of a subagent child, which shares its parent's root."""
    permission_mode: str = "default"
    spawn: Any = None
    """``ChatSession.spawn_subagent`` for a ROOT session; ``None`` for a subagent
    (recursion-off). The spawn tool calls it via ``ctx.spawn``."""
    tool_scope: ToolScope = None
    """A subagent's tool restriction (``"read_only"`` / an allowlist). ``None`` for
    a root session = the full toolset. Filters the advertised list AND binds the
    dispatch refusal so an ``explore`` agent can't reach a write tool."""
    decision_sink: Any = None
    """This chat's per-chat ``DecisionSink`` (``<chat>/decisions.jsonl``). The SQL
    gate audits THROUGH it so an in-chat tool decision lands in the same log as the
    harness permission loop's — overriding the registry's project-level fallback."""
    judge: Any = None
    """The session's auto-mode ``SafetyJudge`` — passed to the in-tool gate so a
    ``sql.query`` write is grounded in auto exactly like a bash/fs write."""
    task_goal: str = ""
    """The live current-turn user message (updated each ``send_prompt``) — the
    judge's context. Mutated in place like ``permission_mode``."""
    alkera_dir: Any = None
    """The ``.alkera/`` dir — lets an in-tool always-allow/reject persist."""
    credential: Any = None
    """The chat's credential (``account.binding.ChatCredential``), which every
    cloud call a tool makes for this chat acts as."""
    task_store: Any = None
    """This chat's ``TaskStore`` (the unified TODO system) — the ``manage_tasks``
    tool reads/mutates it. Set for a ROOT session; ``None`` for a subagent (the
    task tool is gated out, so it's never reached)."""
    sandbox_dir: Any = None
    """This chat's scratch dir (``<chat>/sandbox/``) — where blob.materialize /
    blob.transform write files and plan mode writes ``plan.md``."""
    background: Any = None
    """This chat's ``BackgroundJobRegistry`` — the ``background_status`` /
    ``background_cancel`` tools read + cancel through it. Set for a ROOT session;
    ``None`` for a subagent (the background tools are gated out, app=="background")."""
    abort: Any = None
    """The session's live per-turn abort ``asyncio.Event`` — re-pointed each turn by
    ``ChatSession._send_prompt_locked`` and SET by ``ChatSession.cancel()``. A
    long-running foreground ``bash`` races against it so a turn cancel reaps the
    process group (the loopback request itself isn't cancelled on a turn cancel)."""
    fence: Any = None
    """The session's bound (a cloud chat's ``SessionFence``); ``None`` for a local
    session. The shell gate judges every command through it."""
    scope_runtime: Any = None
    """The ``HarnessRuntime`` that resolves this chat's connection scope and builds
    its scoped view: set where the scope is read at all, so a tool call can read it
    again mid-turn. ``None`` on a binding with nothing to re-read."""
    scope_read_at: float = field(default_factory=time.monotonic)
    """When the connection scope was last read, on the monotonic clock."""

    async def rebind(self, connection_ids: frozenset[str] | None) -> None:
        """Bind the scope the server just answered and rebuild the view from the
        runtime's current build, the one place a scoped view is derived.

        A connection that left the scope leaves the credential cache too: a
        lease taken while the share stood is not served past the read that
        says it is gone, so the next use asks the server and is refused."""
        left = (self.connection_ids or frozenset()) - (connection_ids or frozenset())
        for record_id in left:
            invalidate_shared_lease(record_id)
        self.connection_ids = connection_ids
        self.scope_read_at = time.monotonic()
        self.registry = await self.scope_runtime.tool_registry_for(
            self.web_tools, connection_ids=connection_ids, knowledge_owner=self.knowledge_owner
        )

    async def current_registry(self) -> ToolRegistry:
        """The registry a tool call dispatches through.

        The turn start reads the chat's connection scope; a tool call later in
        the turn reads it again once that read is older than
        :data:`CONNECTION_SCOPE_RECHECK_SECONDS`, so a share revoked mid-turn
        (the workspace owner's connections reach a collaborator's chat only while
        they may write the workspace) stops resolving within that bound rather
        than at the next turn. A read that fails is an empty scope, as at turn
        start. Every transport (the loopback MCP server, the daemon's
        ``tool.call``, a cloud re-run) dispatches through this.
        """
        runtime = self.scope_runtime
        if runtime is None or self.connection_ids is None:
            return self.registry
        # A credential the call leases is leased for this chat (lease_scope).
        LEASE_CHAT.set(self.scope_chat_id)
        if time.monotonic() - self.scope_read_at < CONNECTION_SCOPE_RECHECK_SECONDS:
            return self.registry
        connection_ids = await runtime._resolve_connection_scope(self.scope_chat_id)
        if connection_ids == self.connection_ids:
            self.scope_read_at = time.monotonic()
        else:
            await self.rebind(connection_ids)
        return self.registry


def tool_server_bind_host(settings: SandboxSettings | None = None) -> str:
    """Where the tool server listens. The daemon's own loopback, except on a
    gVisor box: a chat's container has a network of its own and reaches the
    daemon at the host end of its veth pair, not at the host's loopback, so
    there the server binds every address — the box firewall admits a chat to
    exactly the (interface, port) pairs its launch granted and drops the rest,
    and the bearer token stands behind that. The URLs the daemon hands out
    still name the loopback; the adapter names the host end of the pair under
    gVisor."""
    box = settings if settings is not None else SandboxSettings.from_env()
    return "0.0.0.0" if box.mode == "gvisor" else "127.0.0.1"  # noqa: S104 -- see above


class AlkeraToolServer:
    """A loopback Streamable-HTTP MCP server bound to one :class:`SessionToolBinding`."""

    def __init__(self, binding: SessionToolBinding) -> None:
        self._binding = binding
        self._token = secrets.token_urlsafe(24)
        self._server: Any = None
        self._task: asyncio.Task[Any] | None = None
        self._actor_task: asyncio.Task[Any] | None = None
        self._port: int | None = None
        #: The tool names each mount last answered ``tools/list`` with. The
        #: agent lists a mount once, as it connects, and keeps that list — a
        #: stateless server has no channel to tell it the list changed — so
        #: this is what the agent believes it can call.
        self._listed: dict[str, frozenset[str]] = {}

    def _alkera_names(self) -> frozenset[str]:
        from alkera_cli.plugins.plugin_base.mcp_entry import alkera_tool_descriptors

        binding = self._binding
        # A child has no spawn wiring (``binding.spawn is None``) → withhold the
        # agent-spawning AND task/TODO tools from its advertised set (both are
        # root-only; a subagent cannot spawn another).
        is_root = binding.spawn is not None
        return frozenset(
            d.name
            for d in alkera_tool_descriptors(
                binding.registry,
                binding.tool_scope,
                allow_agent_tools=is_root,
                allow_task_tools=is_root,
                allow_background_tools=is_root,
                # A bounded (cloud) session is not offered the tools that run
                # code outside the fence; dispatch refuses them too.
                allow_code_tools=binding.fence is None,
            )
        )

    def _web_names(self) -> frozenset[str]:
        from alkera_cli.plugins.plugin_base.mcp_entry import web_tool_descriptors

        return frozenset(
            d.name for d in web_tool_descriptors(self._binding.registry, self._binding.tool_scope)
        )

    def stale_mounts(self) -> list[str]:
        """The mounts whose last answered listing no longer matches what the
        binding would list now: for each, the agent holds a tool set the
        session has since moved away from. A mount the agent never listed is
        not stale — there is no list at the agent to be behind."""
        current = {"alkera": self._alkera_names, WEB_MCP_MOUNT: self._web_names}
        return [
            mount
            for mount, names in current.items()
            if mount in self._listed and self._listed[mount] != names()
        ]

    @property
    def token(self) -> str:
        return self._token

    @property
    def url(self) -> str:
        if self._port is None:
            raise RuntimeError("AlkeraToolServer not started")
        return f"http://127.0.0.1:{self._port}/mcp"

    @property
    def web_url(self) -> str:
        """The dedicated ``web`` mount — serves ONLY the web tools, under their
        bare names, so the backend's ``<server>_<tool>`` composition yields
        ``web_search``/``web_fetch`` instead of an ``alkera_``-prefixed name."""
        if self._port is None:
            raise RuntimeError("AlkeraToolServer not started")
        return f"http://127.0.0.1:{self._port}/mcp-web"

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def start(self) -> None:
        if self._server is not None:
            return
        import uvicorn

        _quiet_transport_logs()

        app = self._build_app()
        config = uvicorn.Config(
            app, host=tool_server_bind_host(), port=0, log_level="warning", access_log=False
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())
        # Wait for the bound socket so we can read the ephemeral port. uvicorn
        # exposes no asyncio.Event for startup — polling `server.started` is the
        # upstream-documented way to await bind (uvicorn#742), so ASYNC110's
        # Event suggestion doesn't apply here.
        while not self._server.started:
            if self._task.done():
                # A serve that ended never binds: waiting on `started` would spin
                # forever. Its failure is the start's failure.
                task, self._task, self._server = self._task, None, None
                task.result()
                raise RuntimeError("the loopback tool server ended before it bound")
            await asyncio.sleep(0.02)
        sockets = self._server.servers[0].sockets
        assert sockets, "uvicorn started with no sockets"
        self._port = sockets[0].getsockname()[1]
        self._actor_task = asyncio.create_task(self._resolve_actor())

    async def _resolve_actor(self) -> None:
        """Who is acting, for the SQL gate's cross-team escalation. This
        server IS the surface that carries that gate, so the principal resolves
        when it comes up, but never on the bind path: the read is credentialed
        backend HTTP and the loopback socket must not wait on the network for it.
        A resolution that came back empty installs nothing, because a signed-out
        or unreachable read must not turn escalation off for a live session that
        already resolved one."""
        from alkera_cli.plugins.plugin_base.permissions import (
            resolve_acting_principal,
            set_acting_principal,
        )

        binding = self._binding
        try:
            actor = await resolve_acting_principal(
                alkera_dir=binding.alkera_dir, credential=binding.credential
            )
        except Exception:
            logging.getLogger(__name__).warning("gate.actor.resolve_failed", exc_info=True)
            return
        if actor is not None:
            set_acting_principal(
                actor, alkera_dir=binding.alkera_dir, credential=binding.credential
            )

    async def stop(self) -> None:
        if self._server is None:
            return
        if self._actor_task is not None:
            self._actor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._actor_task
            self._actor_task = None
        self._server.should_exit = True
        if self._task is not None:
            # Bounded: uvicorn normally exits promptly on should_exit, but never
            # let a wedged serve() hang teardown or leak the loopback port —
            # cancel if it doesn't stop in time.
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except Exception:  # incl. TimeoutError; teardown must not raise
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._task
        self._server = None
        self._task = None
        self._port = None

    # --- ASGI app ------------------------------------------------------

    def _build_app(self) -> Any:
        from contextlib import asynccontextmanager

        from mcp import types
        from mcp.server.lowlevel import Server
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
        from mcp.server.transport_security import TransportSecuritySettings
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Mount
        from starlette.types import Receive, Scope, Send

        from alkera_cli.plugins.plugin_base.mcp_entry import (
            alkera_tool_descriptors,
            web_registry_name,
            web_tool_descriptors,
        )
        from alkera_cli.plugins.plugin_base.wire import (
            HOST_SPILL_META_KEY,
            host_spill_pointer,
            is_tool_error_result,
            model_facing_text,
        )

        binding = self._binding
        token = self._token
        mcp_server: Any = Server("alkera")

        async def _dispatch(name: str, arguments: dict[str, Any]) -> Any:
            # Dispatch in-parent → the session's broker/mode/permissions gate writes.
            registry = await binding.current_registry()
            result = await registry.dispatch(
                name,
                arguments,
                broker=binding.broker,
                permission_mode=binding.permission_mode,
                permissions=binding.permissions,
                session_id=binding.session_id,
                owner_session_id=binding.owner_session_id,
                spawn=binding.spawn,
                tool_scope=binding.tool_scope,
                decision_sink=binding.decision_sink,
                judge=binding.judge,
                task_goal=binding.task_goal,
                alkera_dir=binding.alkera_dir,
                task_store=binding.task_store,
                sandbox_dir=binding.sandbox_dir,
                background=binding.background,
                # Read LIVE each call: the session re-points binding.abort at a fresh
                # Event every turn, so a long foreground tool sees THIS turn's signal.
                abort=binding.abort,
                fence=binding.fence,
                credential=binding.credential,
            )
            # A failed tool call carries the reserved error flag — surface it as MCP
            # ``isError`` so the model (Claude/OpenAI, via the harness) gets a proper
            # tool-error result it can act on, not a "successful" result whose text
            # happens to contain an error. The flag itself is transport-only → strip it.
            text = model_facing_text(result)
            call = types.CallToolResult(
                content=[types.TextContent(type="text", text=text)],
                isError=is_tool_error_result(result),
            )
            # A tool that outgrew the model's window kept the whole output in a file
            # of its own and returned a tail plus that path. Say so on the result:
            # a backend that bounds tool results would otherwise shorten the tail
            # AGAIN into a file of its own and stamp THAT path on the tool call,
            # leaving the row's pointer naming a copy of the preview while the
            # output it promises sits in a file the row never names.
            spilled = host_spill_pointer(result)
            if spilled is not None:
                call.meta = {HOST_SPILL_META_KEY: {"outputPath": spilled}}
            return call

        # The mcp SDK's low-level decorators are untyped upstream — suppress the
        # untyped-decorator propagation.
        @mcp_server.list_tools()  # type: ignore[untyped-decorator]
        async def _list() -> list[Any]:
            # A child has no spawn wiring (``binding.spawn is None``) → withhold the
            # agent-spawning AND task/TODO tools from its advertised set (both are
            # root-only; a subagent cannot spawn another).
            is_root = binding.spawn is not None
            listed = alkera_tool_descriptors(
                binding.registry,
                binding.tool_scope,
                allow_agent_tools=is_root,
                allow_task_tools=is_root,
                allow_background_tools=is_root,
                # A bounded (cloud) session is not offered the tools that run
                # code outside the fence; dispatch refuses them too.
                allow_code_tools=binding.fence is None,
            )
            self._listed["alkera"] = frozenset(d.name for d in listed)
            return [
                types.Tool(name=d.name, description=d.description, inputSchema=d.input_schema)
                for d in listed
            ]

        @mcp_server.call_tool()  # type: ignore[untyped-decorator]
        async def _call(name: str, arguments: dict[str, Any]) -> Any:
            return await _dispatch(name, arguments)

        # The dedicated `web` mount: ONLY the web tools, advertised under their
        # bare names ("search"/"fetch") so the backend's `<server>_<tool>`
        # composition reads `web_search`/`web_fetch` — a clean first-class name,
        # not an `alkera_`-prefixed one. Same registry, same binding, same gate:
        # only the advertised name differs (mapped back at call time).
        web_server: Any = Server("web")

        @web_server.list_tools()  # type: ignore[untyped-decorator]
        async def _web_list() -> list[Any]:
            listed = web_tool_descriptors(binding.registry, binding.tool_scope)
            self._listed[WEB_MCP_MOUNT] = frozenset(d.name for d in listed)
            return [
                types.Tool(name=d.name, description=d.description, inputSchema=d.input_schema)
                for d in listed
            ]

        @web_server.call_tool()  # type: ignore[untyped-decorator]
        async def _web_call(name: str, arguments: dict[str, Any]) -> Any:
            return await _dispatch(web_registry_name(name), arguments)

        # Loopback bind + a bearer token are the security boundary; the DNS
        # rebinding Host/Origin allowlist would otherwise reject 127.0.0.1.
        security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
        manager = StreamableHTTPSessionManager(
            app=mcp_server, json_response=True, stateless=True, security_settings=security
        )
        web_manager = StreamableHTTPSessionManager(
            app=web_server, json_response=True, stateless=True, security_settings=security
        )

        async def _handle(scope: Scope, receive: Receive, send: Send) -> None:
            await manager.handle_request(scope, receive, send)

        async def _handle_web(scope: Scope, receive: Receive, send: Send) -> None:
            await web_manager.handle_request(scope, receive, send)

        @asynccontextmanager
        async def _lifespan(_app: Starlette) -> Any:
            async with manager.run(), web_manager.run():
                yield

        inner = Starlette(
            routes=[Mount("/mcp-web", app=_handle_web), Mount("/mcp", app=_handle)],
            lifespan=_lifespan,
        )

        async def _auth_gate(scope: Scope, receive: Receive, send: Send) -> None:
            # Auth runs BEFORE routing, so even the bare-`/mcp`→`/mcp/` redirect is
            # gated — an unauthenticated probe gets a 401, never a redirect oracle.
            # The redirect itself still works for authenticated clients.
            if scope["type"] == "http":
                headers = dict(scope.get("headers") or [])
                auth = headers.get(b"authorization", b"").decode()
                if not secrets.compare_digest(auth, f"Bearer {token}"):
                    await JSONResponse({"error": "unauthorized"}, status_code=401)(
                        scope, receive, send
                    )
                    return
            await inner(scope, receive, send)

        return _auth_gate


__all__ = ["AlkeraToolServer", "SessionToolBinding"]
