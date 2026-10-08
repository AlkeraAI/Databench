"""``LoroDocumentStore``: the engine's document store over the platform.

Inside Alkera a notebook's document is a Loro document on the backend's CRDT
lane, co-edited by people in the browser. The engine running beside the
kernel (on the box) never holds Loro: it reads the document through the
notebook view route, edits it through the notebook operations route (applied
on a Loro peer at the token it read, so it merges with people's typing), and
hears that the document moved on the document's realtime channel.

Every collaborator is injected, so the store is the same code on a box, in a
test against a live backend, and anywhere else:

* ``http``: an ``httpx.AsyncClient`` already pointed at the backend and
  carrying the caller's credential (the box's org-bound worker credential);
* ``resolve``: a notebook path (as the engine names it) to the drive and file
  node the backend knows it by. The box's Files mirror supplies it; a test
  supplies a dict;
* ``signals``: the document channel, as :class:`DocSignals`. The platform's
  implementation is :class:`RealtimeDocSignals` (a socket subscribed to
  ``doc:notebook:<item_id>``); a change it reports is read back through the
  view route, so the store needs no Loro.

``editing(path)`` comes from the view's presence: the server keeps who holds
a caret in which cell (from the channel) and which actor with no caret edited
which cell lately (from the edit record). Each entry is a claim, and the
answer is the shared rule over them (``alkera_notebook.document.editing``),
the same one the file store answers with.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Literal, Protocol, cast

import httpx
from alkera_core.schemas.realtime import WS_SUBPROTOCOL, WS_TICKET_SUBPROTOCOL_PREFIX
from alkera_notebook import format as notebook_format
from alkera_notebook.document.editing import FocusClaim, editing_now
from alkera_notebook.document.model import DocCell, DocMeta, Document
from alkera_notebook.document.ops import (
    InsertCell,
    NotebookOp,
    NotebookOpError,
    NotebookOpsResult,
)
from alkera_notebook.document.store import (
    CodeSnapshot,
    DocumentChange,
    EditingInfo,
    StoredNotebook,
)
from alkera_notebook.engine.errors import ForbiddenError, NotFoundError
from alkera_notebook.engine.models import Actor
from alkera_notebook.format.settings import stored_settings
from pydantic import TypeAdapter
from websockets.typing import Subprotocol

from alkera_cli.host.backoff import doubled

logger = logging.getLogger(__name__)

#: The most change signals held for a slow consumer; past it they collapse
#: into one (each signal only says "read again").
SIGNALS_KEPT: Final = 64
#: The route prefix.
NOTEBOOKS: Final = "/api/v1/notebooks"
#: How a chat's agent is named as a notebook actor: ``agent:<chat id>``.
AGENT_ACTOR_PREFIX: Final = "agent:"
#: How long a view read while the document's channel listens answers later
#: reads, unless a change signal (or the store's own edit) ends it sooner.
VIEW_FRESH_SECONDS: Final = 1.0


def agent_actor_id(chat_id: str) -> str:
    """The actor id a chat's agent edits and runs notebooks as."""
    return f"{AGENT_ACTOR_PREFIX}{chat_id}"


class _Acting(Protocol):
    @property
    def kind(self) -> str: ...

    @property
    def id(self) -> str: ...


def chat_of_agent(actor: _Acting) -> str | None:
    """The chat whose agent ``actor`` is, or ``None`` for anyone else."""
    if actor.kind != "agent" or not actor.id.startswith(AGENT_ACTOR_PREFIX):
        return None
    return actor.id[len(AGENT_ACTOR_PREFIX) :] or None


_OPS: Final = TypeAdapter(list[NotebookOp])


class NotebookRef(Protocol):
    """Where the backend keeps a notebook: its drive and its file node."""

    @property
    def drive_id(self) -> str: ...

    @property
    def item_id(self) -> str: ...


@dataclass(frozen=True, slots=True)
class Located:
    drive_id: str
    item_id: str
    #: Headers every request about this notebook carries: on a box, the
    #: fence of the lease it holds the notebook's folder under (only the
    #: holder's batches land, and only under its current epoch).
    headers: Mapping[str, str] = field(default_factory=dict)


def _headers_of(where: NotebookRef) -> dict[str, str]:
    return dict(getattr(where, "headers", None) or {})


Resolver = Callable[[str], Awaitable[NotebookRef]]
#: Drops what a resolver remembers of a path (it answered not found there).
Forget = Callable[[str], None]
#: Writes a new notebook file at a path and says where the backend keeps it
#: (the box's Files mirror creates it where the agent named it).
Creator = Callable[[str, str], Awaitable[NotebookRef]]
#: Removes the file a create just wrote, from the folder and the drive.
Remover = Callable[[str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class DocSignal:
    """What the channel says: it is now listening (``ready``: every change
    after this one is signalled), an update committed (``update``), or the
    history restarted and everything must be read again (``reload``).
    ``actor_id`` is who wrote an update, when the channel said."""

    kind: Literal["ready", "update", "reload"]
    actor_id: str | None = None


class DocSignals(Protocol):
    """The document channel of one notebook, as change signals."""

    def watch(self, item_id: str) -> AsyncIterator[DocSignal]: ...


class StoreError(Exception):
    """The backend refused or could not serve a request (``code`` is its)."""

    def __init__(self, code: str, message: str, status: int) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.code = code
        self.message = message
        self.status = status


#: Refusals the backend answers with a wait because the same request is
#: taken later: a busy lane, and a file whose bytes are still on their way to
#: the drive (a notebook this box has just written). Nothing was applied.
WAIT_CODES: Final = frozenset({"crdt_busy", "content_landing"})
#: How long one request keeps asking again on such a refusal.
WAIT_WITHIN_SECONDS: Final = 15.0
#: The wait when the backend names none.
WAIT_DEFAULT_SECONDS: Final = 0.25


def _wait_for(response: httpx.Response) -> float | None:
    """How long to wait before asking again, when ``response`` is a refusal the
    same request clears later; ``None`` for any other answer."""
    if response.status_code != 503:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict) or body.get("code") not in WAIT_CODES:
        return None
    try:
        return max(0.0, float(response.headers.get("Retry-After", WAIT_DEFAULT_SECONDS)))
    except ValueError:
        return WAIT_DEFAULT_SECONDS


def _refusal(response: httpx.Response) -> Exception:
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    # The error envelope, else the flat body a server older than it answered.
    error = body.get("error")
    layer: Mapping[str, Any] = error if isinstance(error, Mapping) else body
    details = layer.get("details") if layer is error else body
    code = str(layer.get("code") or "error")
    message = str(layer.get("message") or response.text[:200])
    op_index = details.get("op_index") if isinstance(details, Mapping) else None
    refused: Exception
    if response.status_code == 404:
        refused = NotFoundError(message or "no such notebook")
    elif response.status_code == 403:
        refused = ForbiddenError(message or "not allowed")
    elif response.status_code == 422 and isinstance(op_index, int):
        refused = NotebookOpError(op_index, code, message)
    else:
        refused = StoreError(code, message, response.status_code)
    return refused


def _document(view: Mapping[str, Any]) -> Document:
    """A view's live cells as the engine's document (deleted cells are not in
    a view: the store reads what is live)."""
    cells: dict[str, DocCell] = {}
    order: list[str] = []
    for found in view.get("cells") or []:
        kind, source, meta = (
            str(found["kind"]),
            str(found.get("source") or ""),
            found.get("meta") or {},
        )
        cells[found["id"]] = DocCell(
            id=str(found["id"]),
            kind=kind,
            name=str(found.get("name") or "_"),
            source=source,
            code=notebook_format.render_cell(kind, source, meta),
            config=dict(found.get("config") or {}),
            meta=dict(meta),
            extra=dict(found.get("extra") or {}),
        )
        order.append(str(found["id"]))
    # The view answers a settings read (effective values and their sources);
    # the document holds only what the file sets.
    read = dict(view.get("settings") or {})
    settings: dict[str, Any] = stored_settings(read)
    file_format = str(read.get("format") or "1.0")
    settings["format"] = file_format
    return Document(
        meta=DocMeta(format=file_format),
        settings=settings,
        order=order,
        cells=cells,
        read_only_reason=view.get("read_only_reason"),
    )


def _result(body: Mapping[str, Any]) -> NotebookOpsResult:
    """The route answers the engine's own result shape."""
    return NotebookOpsResult.model_validate(body)


@dataclass(slots=True)
class _CachedView:
    view: dict[str, Any]
    read_at: float


@dataclass
class LoroDocumentStore:
    """See the module docstring.

    The engine reads the document several times for one run (when the run is
    asked for, and again when it starts: the cells, then who is editing them;
    and once per poll while it waits for the request's frontier). Each read
    was a path lookup and a view over the network, so a run on a busy box sat
    in the queue for seconds. A view is therefore remembered for
    :data:`VIEW_FRESH_SECONDS`, but only while the notebook's follower
    (:meth:`changes`) listens on its channel: every change signal and every
    edit made through this store forgets it at once, so a read never answers
    a document the channel has said moved on. With no follower listening,
    every read goes to the backend."""

    http: httpx.AsyncClient
    resolve: Resolver
    signals: DocSignals
    #: How a new notebook's file is made; ``None`` for a store that only
    #: edits notebooks that exist.
    create_file: Creator | None = None
    #: How a file :meth:`create` wrote is taken back when its cells are
    #: refused; ``None`` for a store that creates nothing.
    remove_file: Remover | None = None
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    #: Whether a chat's agent's batch names its chat (``agent_chat_id``): what
    #: the box holding the folder does, so the backend writes the batch as
    #: that agent for the chat's person. Only the holder may name one.
    names_agent_chat: bool = False
    #: How long :meth:`changes` waits before following a document again after
    #: its channel failed, doubling up to the most.
    follow_retry_initial: float = 1.0
    follow_retry_max: float = 60.0
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    #: Drops a path the resolver remembers, when the backend answers it is
    #: not there (the path may name a new file now); ``None`` for a resolver
    #: that remembers nothing.
    forget: Forget | None = None
    view_fresh_for: float = VIEW_FRESH_SECONDS
    #: How long one request keeps asking again on a refusal that asks for a wait.
    wait_within: float = WAIT_WITHIN_SECONDS
    monotonic: Callable[[], float] = time.monotonic
    _views: dict[str, _CachedView] = field(default_factory=dict, init=False, repr=False)
    #: Per path, how many followers listen on its channel now.
    _listening: dict[str, int] = field(default_factory=dict, init=False, repr=False)

    async def _url(self, path: str, tail: str = "") -> tuple[str, dict[str, str]]:
        where = await self.resolve(path)
        return f"{NOTEBOOKS}/{where.drive_id}/{where.item_id}{tail}", _headers_of(where)

    async def _view(self, path: str) -> dict[str, Any]:
        """The view read from the backend now (and remembered for the next
        reads while the channel listens)."""
        url, headers = await self._url(path)
        response = await self._asked(lambda: self.http.get(url, headers=headers))
        if response.status_code == 404 and self.forget is not None:
            # The resolver may remember a file that is no longer at the path.
            self.forget(path)
            url, headers = await self._url(path)
            response = await self._asked(lambda: self.http.get(url, headers=headers))
        if response.status_code != 200:
            self._views.pop(path, None)
            raise _refusal(response)
        found = response.json()
        if not isinstance(found, dict):  # pragma: no cover - the route answers an object
            raise StoreError("bad_view", "the view is not an object", response.status_code)
        self._views[path] = _CachedView(found, self.monotonic())
        return found

    async def _asked(self, send: Callable[[], Awaitable[httpx.Response]]) -> httpx.Response:
        """``send``'s answer, asked again while the backend answers that the
        same request is taken later (:data:`WAIT_CODES`), for at most
        :data:`WAIT_WITHIN_SECONDS`. A notebook this box has just written is
        on the drive as a row before its bytes; its first edit waits for the
        bytes instead of failing."""
        waited = 0.0
        while True:
            response = await send()
            wait = _wait_for(response)
            if wait is None or waited + wait > self.wait_within:
                return response
            await self.sleep(wait)
            waited += wait

    async def _fresh_view(self, path: str) -> dict[str, Any]:
        """The remembered view while it is fresh, else :meth:`_view`."""
        cached = self._views.get(path)
        if (
            cached is not None
            and self._listening.get(path, 0) > 0
            and self.monotonic() - cached.read_at < self.view_fresh_for
        ):
            return cached.view
        return await self._view(path)

    def _moved(self, path: str) -> None:
        """The document at ``path`` changed: its remembered view is stale."""
        self._views.pop(path, None)

    async def load(self, path: str) -> StoredNotebook:
        view = await self._fresh_view(path)
        return StoredNotebook(path=path, token=str(view["token"]), document=_document(view))

    async def create(
        self,
        path: str,
        cells: Document | Sequence[InsertCell],
        settings: dict[str, Any],
        actor: Actor,
    ) -> StoredNotebook:
        """A new notebook at ``path``: its file written with no cells (the
        format writer's text for the settings), then its cells inserted as
        operations, so the new document's first cells carry ids the document
        minted and every insert is checked as any other.

        A create is whole or nothing: when the cells are refused (the backend
        will not take the agent's batch, the network fails), the file written
        for them is taken back before the refusal is raised, so a refused
        create leaves no empty notebook behind."""
        if not actor.can_edit:
            raise ForbiddenError("creating a notebook needs edit rights")
        if self.create_file is None:
            raise ForbiddenError("this store cannot create notebooks")
        if isinstance(cells, Document):
            inserts: list[InsertCell] = [
                InsertCell(
                    op="insert",
                    kind=c.kind,
                    source=c.source,
                    name=c.name,
                    config=dict(c.config),
                    meta=dict(c.meta),
                )
                for c in cells.live_cells()
            ]
        else:
            inserts = list(cells)
        known = {k: v for k, v in settings.items() if k != "header"}
        text = notebook_format.write(
            notebook_format.NotebookIR(
                format=str(known.pop("format", "1.0")),
                header_text=str(settings.get("header") or ""),
                settings=known,
                unknown_settings="",
                app_config={},
                generated_with="",
                cells=(),
                violations=(),
                read_only_reason=None,
            )
        )
        await self.create_file(path, text)
        if inserts:
            try:
                await self.apply(path, list(inserts), None, actor, None)
            except BaseException:
                await self._take_back(path)
                raise
        return await self.load(path)

    async def _take_back(self, path: str) -> None:
        """Remove the file a refused :meth:`create` wrote. A failure here is
        logged, never raised over the refusal that caused it."""
        self._moved(path)
        if self.remove_file is None:
            logger.warning("notebook %s: a refused create left its file behind", path)
            return
        try:
            await self.remove_file(path)
        except Exception:
            logger.warning(
                "notebook %s: the refused create's file not removed", path, exc_info=True
            )

    async def apply(
        self,
        path: str,
        ops: Sequence[NotebookOp],
        base_token: str | None,
        actor: Actor,
        submit_id: str | None,
    ) -> NotebookOpsResult:
        if not actor.can_edit:
            raise ForbiddenError("editing a notebook needs edit rights")
        body: dict[str, Any] = {
            "ops": _OPS.dump_python(list(ops), mode="json"),
            "base_token": base_token,
            "submit_id": submit_id,
        }
        chat = chat_of_agent(actor) if self.names_agent_chat else None
        if chat is not None:
            # A chat's agent edits for the chat's person: the backend checks
            # the chat is one this box serves and writes the batch as its agent.
            body["agent_chat_id"] = chat
        url, headers = await self._url(path, "/ops")
        self._moved(path)
        response = await self._asked(lambda: self.http.post(url, json=body, headers=headers))
        self._moved(path)
        if response.status_code != 200:
            raise _refusal(response)
        return _result(response.json())

    async def snapshot(self, path: str, cell_ids: Sequence[str] | None = None) -> CodeSnapshot:
        view = await self._fresh_view(path)
        wanted = None if cell_ids is None else set(cell_ids)
        document = _document(view)
        return CodeSnapshot(
            token=str(view["token"]),
            cells=[
                (cell.id, cell.code)
                for cell in document.live_cells()
                if wanted is None or cell.id in wanted
            ],
        )

    async def editing(self, path: str) -> dict[str, list[EditingInfo]]:
        view = await self._fresh_view(path)
        claims: list[FocusClaim] = []
        for entry in view.get("presence") or []:
            at = datetime.fromisoformat(str(entry["at"]))
            if at.tzinfo is None:
                at = at.replace(tzinfo=UTC)
            who = str(entry.get("who") or "")
            claims.append(
                FocusClaim(
                    # A backend that predates the rule names nobody as an
                    # actor and marks no caret: its entry reads as an edit by
                    # whoever it names.
                    actor_id=str(entry.get("actor_id") or who),
                    display_name=who,
                    kind=entry.get("kind") or "person",
                    cell_id=str(entry["cell_id"]),
                    at=at,
                    caret=entry.get("caret") is True,
                )
            )
        return editing_now(claims, now=self.clock())

    async def changes(self, path: str) -> AsyncIterator[DocumentChange]:
        """Every change to the document after iteration starts, as the cells
        it touched. The document is read first; once the channel says it is
        listening it is read again (a change made while the channel was being
        joined is reported then), and again at each signal, each reading
        compared with the last, so a burst of signals is one read and nothing
        is missed between them.

        The channel ending or failing (a refused subscribe, a credential that
        was replaced, a dropped socket) is not the end: the document is read
        again after a growing pause, what moved meanwhile is reported, and the
        channel is joined again. It ends only once the notebook is gone (its
        view answers not found)."""
        where = await self.resolve(path)
        before = await self._view(path)
        delay = self.follow_retry_initial
        while True:
            listening = False
            try:
                try:
                    async for signal in self.signals.watch(where.item_id):
                        delay = self.follow_retry_initial
                        # Whatever was remembered predates this signal.
                        self._moved(path)
                        after = await self._view(path)
                        if not listening:
                            # From the channel's ``ready`` on, every change is
                            # signalled, so a remembered view can be trusted.
                            listening = True
                            self._listening[path] = self._listening.get(path, 0) + 1
                        touched = _changed_cells(before, after)
                        if touched or signal.kind == "reload":
                            yield DocumentChange(
                                path=path,
                                token=str(after["token"]),
                                actor_id=signal.actor_id,
                                cell_ids=touched,
                                origin="ops",
                            )
                        before = after
                    logger.info("notebook %s: its document channel ended; joining again", path)
                finally:
                    if listening:
                        self._listening[path] -= 1
            except NotFoundError:
                yield DocumentChange(path=path, token="", actor_id=None, origin="deleted")
                return
            except Exception as exc:
                logger.warning(
                    "notebook %s: following its document failed (%s); again in %.0f s",
                    path,
                    exc,
                    delay,
                )
            await self.sleep(delay)
            delay = doubled(delay, floor=self.follow_retry_initial, cap=self.follow_retry_max)
            try:
                where = await self.resolve(path)
                after = await self._view(path)
            except NotFoundError:
                yield DocumentChange(path=path, token="", actor_id=None, origin="deleted")
                return
            except Exception as exc:
                logger.warning("notebook %s: its document cannot be read yet (%s)", path, exc)
                continue
            touched = _changed_cells(before, after)
            if touched:
                yield DocumentChange(
                    path=path, token=str(after["token"]), actor_id=None, cell_ids=touched
                )
            before = after


def _changed_cells(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    """Ids of cells added, removed, moved or changed between two views."""
    old = {c["id"]: (i, c) for i, c in enumerate(before.get("cells") or [])}
    new = {c["id"]: (i, c) for i, c in enumerate(after.get("cells") or [])}
    touched = [cid for cid in new if cid not in old or old[cid] != new[cid]]
    touched.extend(cid for cid in old if cid not in new)
    return touched


# ---------------------------------------------------------------------------
# The document channel over the realtime socket
# ---------------------------------------------------------------------------

Connect = Callable[..., Any]


@dataclass
class RealtimeDocSignals:
    """``doc:notebook:<item_id>`` on the realtime socket, as change signals.

    Mints a single-use ticket (``POST /api/v1/ws/tickets`` on ``http``), opens
    the socket offering it as a subprotocol (the credential never rides the
    URL), subscribes to the channel, and turns every committed update (a
    ``crdt`` envelope carrying an update) and every ``reload`` into a
    :class:`DocSignal`. Carets and other frames are ignored. Signals are held
    in a bounded queue: a consumer that falls behind loses nothing, because a
    signal only says "read again"."""

    http: httpx.AsyncClient
    ws_url: str
    connect: Connect | None = None

    async def watch(self, item_id: str) -> AsyncIterator[DocSignal]:
        connect = self.connect
        if connect is None:
            from websockets.asyncio.client import connect as ws_connect

            connect = ws_connect
        minted = await self.http.post("/api/v1/ws/tickets")
        if minted.status_code not in (200, 201):
            raise _refusal(minted)
        ticket = str(minted.json()["ticket"])
        channel = f"doc:notebook:{item_id}"
        queue: asyncio.Queue[DocSignal] = asyncio.Queue(maxsize=SIGNALS_KEPT)
        async with connect(
            self.ws_url,
            subprotocols=[
                cast(Subprotocol, WS_SUBPROTOCOL),
                cast(Subprotocol, f"{WS_TICKET_SUBPROTOCOL_PREFIX}{ticket}"),
            ],
        ) as ws:
            await ws.recv()  # welcome
            await ws.send(json.dumps({"t": "subscribe", "channel": channel}))
            while True:
                answer = json.loads(await ws.recv())
                if answer.get("t") == "subscribed" and answer.get("channel") == channel:
                    break
                if answer.get("t") == "error":
                    raise StoreError(
                        str(answer.get("code") or "error"), str(answer.get("message") or ""), 403
                    )
            yield DocSignal(kind="ready")

            async def pump() -> None:
                async for raw in ws:
                    signal = _signal(raw, channel)
                    if signal is None:
                        continue
                    if queue.full():
                        # Collapse: the consumer reads the whole view anyway.
                        with contextlib.suppress(asyncio.QueueEmpty):
                            queue.get_nowait()
                    queue.put_nowait(signal)

            reader = asyncio.create_task(pump())
            try:
                while True:
                    getter = asyncio.create_task(queue.get())
                    done, _ = await asyncio.wait(
                        {getter, reader}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if getter in done:
                        yield getter.result()
                        continue
                    getter.cancel()
                    reader.result()  # a closed socket ends the watch (or raises)
                    return
            finally:
                reader.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await reader


def _signal(raw: str | bytes, channel: str) -> DocSignal | None:
    try:
        frame = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(frame, dict) or frame.get("t") != "doc":
        return None
    envelope = frame.get("envelope")
    if not isinstance(envelope, dict):
        return None
    if f"doc:{envelope.get('doc_type')}:{envelope.get('doc_id')}" != channel:
        return None
    if envelope.get("kind") == "reload":
        return DocSignal(kind="reload")
    payload = envelope.get("payload")
    if envelope.get("kind") != "crdt" or not isinstance(payload, dict):
        return None
    if payload.get("t") != "update" or not payload.get("update_id"):
        return None
    user = payload.get("user_id")
    return DocSignal(kind="update", actor_id=str(user) if user else None)


__all__ = [
    "AGENT_ACTOR_PREFIX",
    "DocSignal",
    "DocSignals",
    "Located",
    "LoroDocumentStore",
    "NotebookRef",
    "RealtimeDocSignals",
    "StoreError",
    "agent_actor_id",
    "chat_of_agent",
]
