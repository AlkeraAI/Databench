"""A Loro channel client for the chaos and two-replica suites.

It behaves the way the browser's channel does, which is what lets a test
throw a socket, a worker, a replica or the whole history at the server and
still expect convergence:

* one update in flight; what is typed meanwhile goes in the next;
* an ack settles exactly the tokens its update carried;
* a refusal about load (``crdt_busy``, a chunk error) or a lost ack is
  retried; missing history is resynced by version vector;
* a dropped socket reconnects — to either replica — and says ``hello`` with
  the vector it holds and the Loro peer it wrote as;
* a new epoch rebuilds the copy from the server's and types again only the
  tokens the new document does not already hold.

Every token a tab types is unique, so a test can check the end state for
lost and duplicated text exactly.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

import websockets
from alkera_core.schemas.realtime import (
    CRDT_CHUNK_BYTES,
    CRDT_PROTOCOL,
    WS_PATH,
    WS_SUBPROTOCOL,
    WS_TICKET_SUBPROTOCOL_PREFIX,
    CrdtChunk,
    decode_b64,
    encode_b64,
)
from backend.services.crdt.chunks import ChunkAssembler, split
from httpx import AsyncClient
from loro import ID, ExportMode, LoroDoc, VersionVector


@dataclass
class _Token:
    text: str
    peer: int
    counter_end: int
    epoch: int


@dataclass
class LaneClient:
    """One tab on one live text channel: a chat's draft by default, or (with
    ``text_name="content"``) a co-edited file."""

    http: AsyncClient
    channel: str
    name: str
    #: The root text the document keeps its content in (its type's).
    text_name: str = "draft"
    doc: LoroDoc | None = None
    epoch: int = 0
    peer: int | None = None
    server_vv: bytes | None = None
    tokens: list[_Token] = field(default_factory=list)
    inflight: tuple[str, list[_Token]] | None = None
    errors: list[str] = field(default_factory=list)
    #: Every frame this tab sent and received, newest last (for a failure's message).
    trace: list[str] = field(default_factory=list)
    #: Every frame this tab received, as it arrived.
    received: list[dict[str, Any]] = field(default_factory=list)
    _ws: Any = None
    _reader: asyncio.Task[None] | None = None
    _assembler: ChunkAssembler = field(
        default_factory=lambda: ChunkAssembler(max_total_bytes=1 << 23)
    )
    _synced: asyncio.Event = field(default_factory=asyncio.Event)
    _settled: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def doc_id(self) -> str:
        return self.channel.split(":", 2)[2]

    # -- the socket ---------------------------------------------------------

    async def connect(self, addr: str) -> None:
        resp = await self.http.post("/api/v1/ws/tickets")
        resp.raise_for_status()
        ticket = resp.json()["ticket"]
        try:
            self._ws = await websockets.connect(
                f"ws://{addr}{WS_PATH}",
                subprotocols=[WS_SUBPROTOCOL, f"{WS_TICKET_SUBPROTOCOL_PREFIX}{ticket}"],
                max_size=4 * 1024 * 1024,
            )
        except TimeoutError as exc:
            raise AssertionError(
                f"{self.name} could not open a socket; database={await _waiting()}; "
                f"pool={_pool_status()}; tasks={_stuck_tasks()}"
            ) from exc
        welcome = json.loads(await asyncio.wait_for(self._ws.recv(), 10))
        assert welcome["t"] == "welcome", welcome
        self._synced.clear()
        self.inflight = None
        self._reader = asyncio.create_task(self._read(), name=f"lane-{self.name}")
        await self._send({"t": "subscribe", "channel": self.channel})

    async def disconnect(self) -> None:
        reader, self._reader = self._reader, None
        ws, self._ws = self._ws, None
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
        if reader is not None:
            reader.cancel()
            with contextlib.suppress(BaseException):
                await reader
        self.inflight = None

    async def _send(self, frame: dict[str, Any]) -> None:
        self.trace.append(f"-> {_describe(frame)}")
        if self._ws is None:
            self.trace.append("   (no socket)")
            return
        with contextlib.suppress(Exception):
            await self._ws.send(json.dumps(frame))

    def _envelope(
        self, kind: str, payload: dict[str, Any], epoch: int | None = None
    ) -> dict[str, Any]:
        return {
            "t": "doc",
            "envelope": {
                "doc_id": self.doc_id,
                "doc_type": self.channel.split(":", 2)[1],
                "epoch": self.epoch if epoch is None else epoch,
                "peer_id": "p:client",
                "seq": 0,
                "kind": kind,
                "payload": payload,
            },
        }

    async def hello(self) -> None:
        payload: dict[str, Any] = {"proto": CRDT_PROTOCOL, "loro": "1.16.4", "doc_schema": 1}
        if self.doc is not None and self.epoch > 0:
            payload["vv_b64"] = encode_b64(bytes(self.doc.oplog_vv.encode()))
            payload["epoch_seen"] = self.epoch
            if self.peer is not None:
                payload["loro_peer"] = self.peer
        await self._send(
            self._envelope("hello", payload, self.epoch if self.doc is not None else 0)
        )

    # -- typing -------------------------------------------------------------

    @property
    def text(self) -> str:
        return "" if self.doc is None else str(self.doc.get_text(self.text_name).to_string())

    async def type_token(self, token: str) -> None:
        """Type ``token`` at the end of the draft."""
        await self.wait_synced()
        assert self.doc is not None and self.peer is not None
        text = self.doc.get_text(self.text_name)
        text.insert(len(self.text), token)
        self.doc.commit()
        self.tokens.append(
            _Token(token, self.peer, self.doc.oplog_vv.get_last(self.peer) or 0, self.epoch)
        )
        await self.push()

    def _confirmed(self, token: _Token, vv: VersionVector) -> bool:
        return token.epoch == self.epoch and vv.includes_id(ID(token.peer, token.counter_end))

    def unconfirmed(self) -> list[str]:
        if self.server_vv is None:
            return [t.text for t in self.tokens]
        vv = VersionVector.decode(self.server_vv)
        return [t.text for t in self.tokens if not self._confirmed(t, vv)]

    async def push(self) -> None:
        if self.doc is None or self.server_vv is None or self.inflight is not None:
            return
        server = VersionVector.decode(self.server_vv)
        update = bytes(self.doc.export(ExportMode.Updates(server)))
        pending = [t for t in self.tokens if not self._confirmed(t, server)]
        if not pending and len(update) < 30:
            self._settled.set()
            return
        self._settled.clear()
        update_id = f"{self.name}-{uuid.uuid4().hex[:10]}"
        self.inflight = (update_id, pending)
        if len(update) <= CRDT_CHUNK_BYTES:
            await self._send(
                self._envelope(
                    "crdt", {"t": "update", "update_id": update_id, "data_b64": encode_b64(update)}
                )
            )
        else:
            for piece in split(update, update_id):
                await self._send(
                    self._envelope(
                        "crdt",
                        {
                            "t": "update",
                            "update_id": update_id,
                            "chunk": piece.model_dump(mode="json"),
                        },
                    )
                )

    async def wait_synced(self, seconds: float = 20) -> None:
        try:
            await asyncio.wait_for(self._synced.wait(), seconds)
        except TimeoutError as exc:
            raise AssertionError(
                f"{self.name} never synced; errors={self.errors[-10:]}; trace={self.trace[-25:]}; "
                f"database={await _waiting()}; pool={_pool_status()}; tasks={_stuck_tasks()}"
            ) from exc

    async def settle(self, seconds: float = 30) -> None:
        """Until everything this tab typed is acknowledged."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        while loop.time() < deadline:
            if self._synced.is_set() and not self.unconfirmed() and self.inflight is None:
                return
            if self.inflight is None:
                await self.push()
            await asyncio.sleep(0.05)
        raise AssertionError(
            f"{self.name} never settled: {self.unconfirmed()} errors={self.errors[-5:]}"
        )

    # -- frames -------------------------------------------------------------

    async def _read(self) -> None:
        assert self._ws is not None
        async for raw in self._ws:
            frame = json.loads(raw)
            self.trace.append(f"<- {_describe(frame)}")
            self.received.append(frame)
            try:
                await self._frame(frame)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                # Keep reading; the test asserts on outcomes. A panic in this
                # process's own Loro (pyo3's PanicException) is a BaseException:
                # caught only as Exception, it silently ended this reader and a
                # tab looked like a server that never answered.
                import traceback

                self.errors.append(
                    f"client: {type(exc).__name__}: {exc} {traceback.format_exc()[-1500:]}"
                )

    async def _frame(self, frame: dict[str, Any]) -> None:
        if frame.get("t") == "subscribed" and frame.get("channel") == self.channel:
            await self.hello()
            return
        if frame.get("t") == "reset":
            # The server dropped this socket's queue (it overflowed): ask for
            # whatever it held, as the browser does.
            if self.doc is not None:
                await self.hello()
            return
        if frame.get("t") == "error" and frame.get("channel") == self.channel:
            self.errors.append(f"subscribe: {frame.get('code')}")
            return
        if frame.get("t") != "doc":
            return
        env = frame["envelope"]
        if f"doc:{env['doc_type']}:{env['doc_id']}" != self.channel:
            return
        kind, payload = env["kind"], env["payload"]
        if kind == "snapshot":
            await self._sync(env["epoch"], payload)
        elif kind in ("ack", "crdt") and env["epoch"] != self.epoch:
            # Another epoch's history never mixes into this document (as in
            # the browser): a newer one means the reload was missed.
            if kind == "crdt" and env["epoch"] > self.epoch and self.doc is not None:
                await self.hello()
        elif kind == "ack":
            if self.inflight is not None and payload.get("update_id") == self.inflight[0]:
                self.server_vv = decode_b64(payload["vv_b64"])
                self.inflight = None
                await self.push()
        elif kind == "crdt" and payload.get("t") == "update":
            data: bytes | None
            if payload.get("chunk"):
                data = self._assembler.add(CrdtChunk.model_validate(payload["chunk"]))
                if data is None:
                    return
            else:
                data = decode_b64(payload["data_b64"])
            if self.doc is not None:
                status = self.doc.import_(data)
                if status.pending is not None and not status.pending.is_empty:
                    await self.hello()
        elif kind == "reload":
            self.inflight = None
            await self.hello()
        elif kind == "error":
            code = payload.get("code")
            self.errors.append(str(code))
            named = payload.get("update_id")
            if self.inflight is not None and named == self.inflight[0]:
                self.inflight = None
            if code in ("crdt_resync", "not_synced"):
                await self.hello()
            elif code in (
                "crdt_busy",
                "internal",
                "chunk_invalid",
                "chunk_timeout",
                "chunk_limit",
                "doc_full",
            ):
                await asyncio.sleep((payload.get("retry_after_ms") or 200) / 1000)
                # A refused hello (no update named) is asked again; a refused
                # update is sent again.
                if named is None and not self._synced.is_set():
                    await self.hello()
                else:
                    await self.push()
            elif code == "stale_epoch":
                pass  # the reload that follows re-hellos

    async def _sync(self, epoch: int, payload: dict[str, Any]) -> None:
        if payload.get("chunk"):
            data = self._assembler.add(CrdtChunk.model_validate(payload["chunk"]))
            if data is None:
                return
        else:
            data = decode_b64(payload["data_b64"])
        peer = int(payload["loro_peer"])
        if self.doc is None or epoch != self.epoch or payload["mode"] == "snapshot":
            fresh = LoroDoc()  # type: ignore[no-untyped-call]
            fresh.peer_id = peer
            fresh.import_(data)
            if self.doc is not None and epoch == self.epoch:
                fresh.import_(bytes(self.doc.export(ExportMode.Snapshot())))
            moved = self.doc is not None and epoch != self.epoch
            carried = self.unconfirmed() if moved else []
            self.doc, self.epoch, self.peer = fresh, epoch, peer
            if moved:
                # Confirmed tokens are in the new document already; the rest are
                # typed again below and tracked from this epoch on.
                self.tokens = []
                for token in carried:
                    # Typed before the history restarted and never confirmed:
                    # type it again unless the new document already holds it.
                    if token not in self.text:
                        await self._retype(token)
        else:
            if self.peer != peer:
                assert self.doc is not None
                self.doc.peer_id = peer
                self.peer = peer
            assert self.doc is not None
            self.doc.import_(data)
        self.server_vv = decode_b64(payload["vv_b64"])
        self.inflight = None
        self._synced.set()
        await self.push()

    async def _retype(self, token: str) -> None:
        assert self.doc is not None and self.peer is not None
        self.doc.get_text(self.text_name).insert(len(self.text), token)
        self.doc.commit()
        self.tokens.append(
            _Token(token, self.peer, self.doc.oplog_vv.get_last(self.peer) or 0, self.epoch)
        )


def _describe(frame: dict[str, Any]) -> str:
    if frame.get("t") != "doc":
        return f"{frame.get('t')} {frame.get('code', '')}".strip()
    env = frame["envelope"]
    payload = env.get("payload", {})
    detail = (
        payload.get("code")
        or payload.get("reason")
        or payload.get("mode")
        or payload.get("t")
        or ""
    )
    return (
        f"{env['kind']} e{env['epoch']} {detail} {str(payload.get('update_id', ''))[-6:]}".strip()
    )


def _pool_status() -> str:
    """The shared engine's connection pool (both in-process servers and the
    test draw on it)."""
    from alkera_core.db.session import engine

    return str(engine.pool.status())


def _stuck_tasks() -> list[str]:
    """Where every server task touching a socket or the lane is parked (the
    servers run on the test's loop): the other half of a request that never
    answers."""
    import io

    found = []
    for task in asyncio.all_tasks():
        buffer = io.StringIO()
        task.print_stack(file=buffer)
        stack = buffer.getvalue()
        if "/backend/" in stack and "site-packages/uvicorn/server.py" not in stack[-400:]:
            found.append(f"[{task.get_name()}]\n{stack[-2500:]}")
    return found


async def _waiting() -> list[str]:
    """What every other session in the database is doing and waiting on: the
    first thing to look at when a request never answers."""
    from alkera_core.db.session import AsyncSessionLocal
    from sqlalchemy import text

    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            text(
                "SELECT pid, state, wait_event_type, wait_event, "
                "now() - xact_start AS age, pg_blocking_pids(pid) AS blockers, "
                "left(query, 160) AS query FROM pg_stat_activity "
                "WHERE datname = current_database() AND pid <> pg_backend_pid() "
                "AND state <> 'idle'"
            )
        )
        return [str(tuple(r)) for r in rows]
