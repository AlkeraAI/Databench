"""A person with a file open live, as the browser's editor is: a Loro
document on the file's channel of the realtime socket, for the tests that
drive a real backend (the chaos run, the box's text peer end to end).

A keystroke counts as typed once the server acknowledged it, as the editor
counts it saved; one the socket lost before its ack is typed again. Every
agent line (``agent<n>``) the tab's copy takes in is timed on arrival.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
import uuid
from typing import Any, Protocol

import httpx
import websockets
from alkera_core.schemas.realtime import (
    CRDT_CHUNK_BYTES,
    CRDT_PROTOCOL,
    WS_SUBPROTOCOL,
    WS_TICKET_SUBPROTOCOL_PREFIX,
    decode_b64,
    encode_b64,
)
from alkera_core.schemas.realtime.tickets import WS_PATH
from loro import ExportMode, LoroDoc

#: The refusals of a hello the editor's channel asks again after (a loaded
#: database's lock timeout is ``internal``), and how many times this run does.
RETRYABLE_HELLO = frozenset({"crdt_busy", "internal"})
HELLO_TRIES = 20
#: An agent's line in the file.
AGENT_TOKEN = re.compile(r"\bagent\d+\b")
TICKET_TRIES = 20


class Server(Protocol):
    """Where the person's browser is pointed, and as whom."""

    @property
    def base_url(self) -> str: ...

    @property
    def token(self) -> str: ...


class Typist:
    """One browser tab: a Loro document on the file's channel. A keystroke
    counts as typed once the server acknowledged it, as the editor counts it
    saved; one the socket lost before its ack is typed again."""

    def __init__(self, backend: Server, node_id: str, line: int, mark: str) -> None:
        self.backend = backend
        self.channel = f"doc:file:{node_id}"
        self.node_id = node_id
        self.line = line
        self.mark = mark
        self.acked: list[str] = []
        self.doc: LoroDoc | None = None
        self.epoch = 0
        self.peer_id = ""
        self.ws: Any = None
        self.hellos_refused = 0
        #: When each agent line first appeared in this tab's copy (monotonic).
        self.seen_at: dict[str, float] = {}

    async def _connect(self) -> None:
        async with httpx.AsyncClient(
            base_url=self.backend.base_url,
            headers={"Authorization": f"Bearer {self.backend.token}"},
        ) as http:
            # Tickets are rate limited per person, and a ten-minute run with
            # the backend restarting reconnects often: a refusal is waited
            # out for as long as the server says, as the portal's client does.
            for _ in range(TICKET_TRIES):
                answer = await http.post("/api/v1/ws/tickets")
                if answer.status_code != 429:
                    break
                await asyncio.sleep(min(float(answer.headers.get("retry-after") or 1), 10))
            answer.raise_for_status()
            ticket = answer.json()["ticket"]
        host = self.backend.base_url.removeprefix("http://")
        self.ws = await websockets.connect(
            f"ws://{host}{WS_PATH}",
            subprotocols=[WS_SUBPROTOCOL, f"{WS_TICKET_SUBPROTOCOL_PREFIX}{ticket}"],
            max_size=8 * 1024 * 1024,
        )
        welcome = json.loads(await self.ws.recv())
        self.peer_id = welcome["peer_id"]
        await self._send({"t": "subscribe", "channel": self.channel})
        told = await self._until(lambda f: f["t"] in ("subscribed", "error"))
        assert told["t"] == "subscribed" and told["can_write"], told
        await self._hello()

    async def _send(self, frame: dict[str, Any]) -> None:
        await self.ws.send(json.dumps(frame))

    def _envelope(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "t": "doc",
            "envelope": {
                "doc_id": self.node_id,
                "doc_type": "file",
                "epoch": self.epoch,
                "peer_id": self.peer_id,
                "seq": 0,
                "kind": kind,
                "payload": payload,
            },
        }

    async def _until(self, matches: Any, seconds: float = 30.0) -> dict[str, Any]:
        deadline = time.monotonic() + seconds
        while True:
            raw = await asyncio.wait_for(self.ws.recv(), max(0.05, deadline - time.monotonic()))
            frame = json.loads(raw)
            if matches(frame):
                return dict(frame)
            self._absorb(frame)

    def _absorb(self, frame: dict[str, Any]) -> None:
        """Take somebody else's update into this tab's copy."""
        if frame.get("t") != "doc" or self.doc is None:
            return
        envelope = frame["envelope"]
        payload = envelope.get("payload") or {}
        if envelope["kind"] == "crdt" and payload.get("t") == "update":
            if envelope["epoch"] == self.epoch and payload.get("data_b64"):
                self.doc.import_(decode_b64(payload["data_b64"]))
                self._note_agent_lines()

    def _note_agent_lines(self) -> None:
        assert self.doc is not None
        now = time.monotonic()
        for token in AGENT_TOKEN.findall(self.doc.get_text("content").to_string()):
            self.seen_at.setdefault(token, now)

    async def _hello(self) -> None:
        """Take the document afresh (a new tab, or one whose epoch moved). A
        hello refused as busy is asked again after the hint, as the editor's
        channel does; any other refusal fails the run."""
        self.epoch = 0
        for _ in range(HELLO_TRIES):
            await self._send(
                self._envelope("hello", {"proto": CRDT_PROTOCOL, "loro": "1.16.4", "doc_schema": 1})
            )
            sync = await self._until(
                lambda f: f["t"] == "doc" and f["envelope"]["kind"] in ("snapshot", "error")
            )
            envelope = sync["envelope"]
            refused = envelope.get("payload") or {}
            if envelope["kind"] != "error" or refused.get("code") not in RETRYABLE_HELLO:
                break
            self.hellos_refused += 1
            await asyncio.sleep(min(float(refused.get("retry_after_ms") or 500), 5000) / 1000)
        assert envelope["kind"] == "snapshot", envelope
        body = envelope["payload"]
        # A file the run has grown past one frame arrives in pieces, in order.
        pieces = [body]
        while (chunk := pieces[-1].get("chunk")) and chunk["index"] < chunk["count"] - 1:
            more = await self._until(
                lambda f: f["t"] == "doc" and f["envelope"]["kind"] == "snapshot"
            )
            pieces.append(more["envelope"]["payload"])
        data = b"".join(
            decode_b64(one["chunk"]["data_b64"] if one.get("chunk") else one["data_b64"])
            for one in pieces
        )
        assert data, "the snapshot carried the document"
        doc = LoroDoc()
        doc.peer_id = body["loro_peer"]
        doc.import_(data)
        self.doc = doc
        self.epoch = envelope["epoch"]
        self._note_agent_lines()

    def _type(self, piece: str) -> bytes:
        assert self.doc is not None
        text = self.doc.get_text("content")
        lines = text.to_string().split("\n")
        line = min(self.line, len(lines) - 1)
        at = sum(len(one) + 1 for one in lines[: line + 1]) - 1
        before = self.doc.oplog_vv
        text.insert(max(at, 0), piece)
        self.doc.commit()
        return bytes(self.doc.export(ExportMode.Updates(before)))

    async def run(self, stop: asyncio.Event) -> None:
        count = 0
        # A keystroke sent and not yet acknowledged when the socket dropped:
        # the browser sends the same change again on its return, so it lands
        # once. Here, the server's document after the return either holds it
        # (it was taken, the ack was what got lost) or it is typed again.
        unacked: tuple[str, Any] | None = None
        while not stop.is_set():
            try:
                if self.ws is None:
                    await self._connect()
                    assert self.doc is not None
                    if unacked is not None and self.doc.oplog_vv.includes_vv(unacked[1]):
                        self.acked.append(unacked[0])
                        count += 1
                    unacked = None
                piece = f" {self.mark}{count}"
                data = self._type(piece)
                assert self.doc is not None
                unacked = (piece.strip(), self.doc.oplog_vv)
                assert len(data) <= CRDT_CHUNK_BYTES
                update_id = f"{self.mark}-{uuid.uuid4().hex[:10]}"
                await self._send(
                    self._envelope(
                        "crdt",
                        {"t": "update", "update_id": update_id, "data_b64": encode_b64(data)},
                    )
                )
                answer = await self._until(
                    lambda f: (
                        f["t"] == "doc" and f["envelope"]["kind"] in ("ack", "error", "reload")
                    )
                )
                unacked = None
                if answer["envelope"]["kind"] == "ack":
                    self.acked.append(piece.strip())
                    count += 1
                else:
                    # Refused or reloaded: the edit never became the
                    # document's. The tab takes the document afresh and types
                    # the same keystroke again.
                    await self._hello()
            except (OSError, websockets.WebSocketException, TimeoutError, httpx.HTTPError):
                with contextlib.suppress(Exception):
                    await self.ws.close()
                self.ws = None
                await asyncio.sleep(0.2)
            await asyncio.sleep(0.08)
        with contextlib.suppress(Exception):
            await self.ws.close()
