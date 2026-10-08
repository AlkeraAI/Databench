"""The engine's widget hub: one per kernel.

It keeps the state of every widget model as the kernel last described it,
and routes comm traffic between the kernel and the output frames that show
widgets.

- The cache changes only on kernel-to-frontend messages (``comm_open``,
  ``update`` and ``echo_update``, ``comm_close``), merging partial state and
  binary buffers by path. What a frontend sends never touches it: the kernel
  may clamp or refuse a value, and its answer is the truth.
- A frame receives only the models it displays: the closure of its model over
  ``IPY_MODEL_`` references, extended when an update adds one. A frame that
  joins is brought up to date with comm-open replays of that closure from
  the cache, in creation order.
- Each frontend message names its sender: only the client that attached a
  frame may send on it, and only on the models that frame was given.
- Each frontend message is authorized (``can_run``, asked per message),
  carries the ``msg_id`` the frame chose, and is delivered to the kernel as a
  run; when the kernel reports it handled the message, the idle status goes
  back to the frame that sent it, which releases that frame's buffered
  changes.
- Values of sensitive models (passwords) reach only the frontend that typed
  them; everywhere else, in the cache and in snapshots, they are redacted.
- Large anywidget ``_esm`` and ``_css`` values are moved into the asset store
  and replaced by a reference the frame resolves with ``need_module``.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from alkera_notebook.widgets.assets import (
    AssetEntry,
    AssetRefusedError,
    KernelAssetFile,
    WidgetAssets,
    asset_ref,
)

REDACTED = "<redacted>"
MOVE_TO_STORE_BYTES = 64 * 1024
MOVABLE_KEYS = ("_esm", "_css")
SENSITIVE_MODEL_NAMES = frozenset({"PasswordModel"})
MODEL_REF = "IPY_MODEL_"

Path_ = tuple[str | int, ...]


class WidgetRefusedError(Exception):
    """A frontend message the hub will not deliver."""


@dataclass
class Model:
    comm_id: str
    target_name: str
    state: dict[str, Any]
    buffers: dict[Path_, bytes]
    metadata: dict[str, Any]
    seq: int
    #: The kernel said so (``comm.open`` with ``sensitive: true``).
    flagged: bool = False

    @property
    def sensitive(self) -> bool:
        return (
            self.flagged
            or bool(self.state.get("_sensitive"))
            or self.state.get("_model_name") in SENSITIVE_MODEL_NAMES
        )


@dataclass(frozen=True)
class Frame:
    """An output frame attached at the hub (``POST .../frames``): the widgets
    one output shows, in one person's (or agent's) client. Everything the hub
    sends it, ``comm.status`` included, is addressed by ``frame_id``."""

    frame_id: str
    client_id: str
    model_ids: tuple[str, ...]
    readonly: bool = False


@dataclass(frozen=True)
class Outbound:
    """A message for one frame, in the frame contract's shape."""

    frame_id: str
    message: dict[str, Any]
    buffers: list[bytes] = field(default_factory=list)


@dataclass(frozen=True)
class Delivery:
    """A frontend message for the kernel (``comm.deliver``)."""

    msg_id: str
    client_id: str
    msg: dict[str, Any]
    buffers: list[bytes]


# ---------------------------------------------------------------- state helpers


def _set_path(state: dict[str, Any], path: Path_, value: Any) -> None:
    target: Any = state
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value


def _references(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        if value.startswith(MODEL_REF):
            yield value[len(MODEL_REF) :]
    elif isinstance(value, dict):
        for v in value.values():
            yield from _references(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _references(v)


def _paths(raw: Any) -> list[Path_]:
    return [tuple(p) for p in raw or []]


class WidgetHub:
    def __init__(
        self,
        *,
        scope: str,
        assets: WidgetAssets | None = None,
        asset_scope: str | None = None,
        can_run: Callable[[str], bool] = lambda _client: True,
        move_threshold: int = MOVE_TO_STORE_BYTES,
    ) -> None:
        self.scope = scope
        self._assets = assets
        self._asset_scope = asset_scope
        self._can_run = can_run
        self._move_threshold = move_threshold
        self._models: dict[str, Model] = {}
        self._seq = 0
        self._frames: dict[str, Frame] = {}
        self._visible: dict[str, set[str]] = {}
        # msg_id -> (client_id, frame_id) of the frontend message it names
        self._origins: dict[str, tuple[str, str]] = {}
        # pending outbound messages, coalesced by `drain`
        self._queue: list[Outbound] = []

    # ---------------------------------------------------------------- reads

    def model(self, comm_id: str) -> Model | None:
        return self._models.get(comm_id)

    def models(self) -> list[Model]:
        return sorted(self._models.values(), key=lambda m: m.seq)

    def frame_closure(self, frame: Frame) -> set[str]:
        """The models a frame may hold: the union of its widgets' closures."""
        out: set[str] = set()
        for model_id in frame.model_ids:
            out |= self.closure(model_id)
        return out

    def closure(self, model_id: str) -> set[str]:
        """``model_id`` and every model its state reaches through references,
        plus the view-less models that only tie shown models together (a
        ``jslink``'s LinkModel points at both ends and nothing points at it)."""
        seen: set[str] = set()

        def reach(start: str) -> None:
            stack = [start]
            while stack:
                current = stack.pop()
                if current in seen or current not in self._models:
                    continue
                seen.add(current)
                stack.extend(_references(self._models[current].state))

        reach(model_id)
        grew = True
        while grew:
            grew = False
            for model in self._models.values():
                if model.comm_id in seen or model.state.get("_view_name"):
                    continue
                refs = set(_references(model.state))
                if refs and refs <= seen:
                    reach(model.comm_id)
                    grew = True
        return seen

    def snapshot(self) -> dict[str, Any]:
        """``application/vnd.jupyter.widget-state+json`` for a kernel-less
        render; sensitive values are redacted and buffers inlined as base64."""
        import base64

        state: dict[str, Any] = {}
        for m in self.models():
            data = self._redacted_state(m)
            buffers = [
                {"path": list(p), "encoding": "base64", "data": base64.b64encode(b).decode()}
                for p, b in m.buffers.items()
            ]
            entry: dict[str, Any] = {
                "model_name": data.get("_model_name"),
                "model_module": data.get("_model_module"),
                "model_module_version": data.get("_model_module_version"),
                "state": data,
            }
            if buffers:
                entry["buffers"] = buffers
            state[m.comm_id] = entry
        return {"version_major": 2, "version_minor": 0, "state": state}

    # ---------------------------------------------------------------- kernel side

    def kernel_open(
        self,
        comm_id: str,
        content: dict[str, Any],
        buffers: list[bytes],
        metadata: dict[str, Any] | None = None,
        *,
        sensitive: bool = False,
    ) -> None:
        data = content.get("data") or {}
        state = copy.deepcopy(data.get("state") or {})
        model = Model(
            comm_id=comm_id,
            target_name=str(content.get("target_name") or "jupyter.widget"),
            state={},
            buffers={},
            metadata=dict(metadata or {}),
            seq=self._seq,
            flagged=sensitive,
        )
        self._seq += 1
        self._models[comm_id] = model
        self._merge(model, state, _paths(data.get("buffer_paths")), buffers)
        # Frames whose closure now reaches this model hear about it.
        for frame in self._frames.values():
            if (
                comm_id in self.frame_closure(frame)
                and comm_id not in self._visible[frame.frame_id]
            ):
                self._send_open(frame, model)

    def kernel_msg(
        self, comm_id: str, content: dict[str, Any], buffers: list[bytes], parent_msg_id: str | None
    ) -> None:
        model = self._models.get(comm_id)
        if model is None:
            return
        data = content.get("data") or {}
        method = data.get("method")
        if method in ("update", "echo_update"):
            delta = copy.deepcopy(data.get("state") or {})
            paths = _paths(data.get("buffer_paths"))
            self._merge(model, delta, paths, buffers)
            self._grow_closures()
            self._broadcast_update(model, delta, paths, buffers, method, parent_msg_id)
            return
        # `custom` and anything else pass through unchanged, to the frames
        # showing the model.
        for frame in self._frames_seeing(comm_id):
            self._queue.append(
                Outbound(
                    frame.frame_id,
                    {
                        "type": "comm.msg",
                        "comm_id": comm_id,
                        "content": content,
                        "parent_msg_id": parent_msg_id,
                    },
                    list(buffers),
                )
            )

    def kernel_asset(
        self, module: str, version: str, files: list[dict[str, Any]], segments: list[bytes]
    ) -> AssetEntry:
        """The kernel's ``widget.asset`` notification: ``files`` name each
        file's path and hash, in the order of the segments that carry the
        bytes. Offered to this hub's notebook; stored under ``asset_scope``,
        the (org, workspace) key, when the engine gave one."""
        if self._assets is None:
            raise AssetRefusedError("this engine keeps no widget assets")
        if len(files) != len(segments):
            raise AssetRefusedError("widget.asset files and segments differ in number")
        parsed = [
            KernelAssetFile(str(f.get("path", "")), str(f.get("sha256", "")), data)
            for f, data in zip(files, segments, strict=True)
        ]
        return self._assets.offer_kernel_asset(
            self.scope, module, version, parsed, store_scope=self._asset_scope
        )

    def kernel_close(self, comm_id: str) -> None:
        frames = self._frames_seeing(comm_id)
        self._models.pop(comm_id, None)
        for frame in frames:
            self._visible[frame.frame_id].discard(comm_id)
            self._queue.append(Outbound(frame.frame_id, {"type": "comm.close", "comm_id": comm_id}))

    def kernel_idle(self, msg_id: str) -> None:
        """The kernel finished handling a frontend message."""
        origin = self._origins.pop(msg_id, None)
        if origin is None:
            return
        _client, frame_id = origin
        if frame_id in self._frames:
            self._queue.append(
                Outbound(
                    frame_id, {"type": "comm.status", "msg_id": msg_id, "execution_state": "idle"}
                )
            )

    # ---------------------------------------------------------------- frames

    def attach(self, frame: Frame) -> list[Outbound]:
        """A frame joins: its comm-open replays, in creation order."""
        self._frames[frame.frame_id] = frame
        self._visible[frame.frame_id] = set()
        replays: list[Outbound] = []
        for model in self.models():
            if model.comm_id in self.frame_closure(frame):
                replays.append(self._open_message(frame, model))
                self._visible[frame.frame_id].add(model.comm_id)
        return replays

    def detach(self, frame_id: str) -> None:
        self._frames.pop(frame_id, None)
        self._visible.pop(frame_id, None)
        self._origins = {k: v for k, v in self._origins.items() if v[1] != frame_id}

    def frontend_send(
        self,
        frame_id: str,
        comm_id: str,
        msg_id: str,
        content: dict[str, Any],
        buffers: list[bytes],
        *,
        client_id: str,
    ) -> Delivery:
        """A frame's ``comm.send`` from ``client_id``. The hub is the one
        place that decides frame ownership: a client sends only on a frame it
        attached, only on the comms that frame was given, never on a read-only
        frame, and only while it may run (asked per message). Never changes
        the cache. Returns what the engine delivers to the kernel as a run."""
        frame = self._frames.get(frame_id)
        if frame is None:
            raise WidgetRefusedError("unknown frame")
        if frame.client_id != client_id:
            raise WidgetRefusedError("that frame is not yours")
        if frame.readonly:
            raise WidgetRefusedError("this frame is read-only")
        if comm_id not in self._visible.get(frame_id, set()):
            raise WidgetRefusedError("this frame was not given that widget")
        if not msg_id or msg_id in self._origins:
            raise WidgetRefusedError("each message needs a new msg_id")
        if not self._can_run(frame.client_id):
            raise WidgetRefusedError("you cannot run this notebook")
        self._origins[msg_id] = (frame.client_id, frame_id)
        data = content.get("data", content) if isinstance(content, dict) else {}
        msg = {"msg_type": "comm_msg", "content": {"comm_id": comm_id, "data": data}}
        return Delivery(msg_id=msg_id, client_id=frame.client_id, msg=msg, buffers=list(buffers))

    def drain(self) -> list[Outbound]:
        """Pending messages per frame, with consecutive updates of one model
        (same parent message) merged into one."""
        out: list[Outbound] = []
        for item in self._queue:
            prev = out[-1] if out else None
            if prev is not None and _mergeable(prev, item):
                out[-1] = _merge_updates(prev, item)
            else:
                out.append(item)
        self._queue = []
        return out

    # ---------------------------------------------------------------- internals

    def _frames_seeing(self, comm_id: str) -> list[Frame]:
        return [f for f in self._frames.values() if comm_id in self._visible.get(f.frame_id, set())]

    def _merge(
        self, model: Model, delta: dict[str, Any], paths: list[Path_], buffers: list[bytes]
    ) -> None:
        for key in list(delta):
            # A key replaced by JSON drops buffers that lived under it.
            for path in [p for p in model.buffers if p and p[0] == key]:
                del model.buffers[path]
        if self._assets is not None:
            for key in MOVABLE_KEYS:
                value = delta.get(key)
                if isinstance(value, str) and len(value.encode()) > self._move_threshold:
                    entry = self._assets.offer_value(
                        self.scope, f"{model.comm_id}:{key}", value.encode()
                    )
                    delta[key] = asset_ref(entry.sha256)
        sensitive = (
            bool(delta.get("_sensitive"))
            or model.sensitive
            or delta.get("_model_name") in SENSITIVE_MODEL_NAMES
        )
        cached = dict(delta)
        if sensitive and "value" in cached:
            cached["value"] = REDACTED
        model.state.update(cached)
        for path, buf in zip(paths, buffers, strict=False):
            if path:
                model.buffers[path] = bytes(buf)

    def _redacted_state(self, model: Model) -> dict[str, Any]:
        state = copy.deepcopy(model.state)
        if model.sensitive and "value" in state:
            state["value"] = REDACTED
        return state

    def _open_message(self, frame: Frame, model: Model) -> Outbound:
        state = self._redacted_state(model)
        paths = list(model.buffers)
        for path in paths:
            # Buffers travel beside the state; their slots hold nothing.
            try:
                _set_path(state, path, None)
            except (KeyError, IndexError, TypeError):
                continue
        message = {
            "type": "comm.open",
            "comm_id": model.comm_id,
            "target_name": model.target_name,
            "data": {"state": state, "buffer_paths": [list(p) for p in paths]},
            "metadata": model.metadata,
        }
        return Outbound(frame.frame_id, message, [model.buffers[p] for p in paths])

    def _send_open(self, frame: Frame, model: Model) -> None:
        self._queue.append(self._open_message(frame, model))
        self._visible[frame.frame_id].add(model.comm_id)

    def _grow_closures(self) -> None:
        for frame in self._frames.values():
            visible = self._visible[frame.frame_id]
            for model in self.models():
                if model.comm_id not in visible and model.comm_id in self.frame_closure(frame):
                    self._send_open(frame, model)

    def _broadcast_update(
        self,
        model: Model,
        delta: dict[str, Any],
        paths: list[Path_],
        buffers: list[bytes],
        method: str,
        parent_msg_id: str | None,
    ) -> None:
        origin_client = self._origins.get(parent_msg_id or "", (None, None))[0]
        for frame in self._frames_seeing(model.comm_id):
            state = dict(delta)
            if model.sensitive and "value" in state and frame.client_id != origin_client:
                state["value"] = REDACTED
            content = {
                "comm_id": model.comm_id,
                "data": {
                    "method": method,
                    "state": state,
                    "buffer_paths": [list(p) for p in paths],
                },
            }
            self._queue.append(
                Outbound(
                    frame.frame_id,
                    {
                        "type": "comm.msg",
                        "comm_id": model.comm_id,
                        "content": content,
                        "parent_msg_id": parent_msg_id,
                    },
                    list(buffers),
                )
            )


def _update_parts(item: Outbound) -> tuple[str, str | None, dict[str, Any]] | None:
    m = item.message
    if m.get("type") != "comm.msg":
        return None
    data = m["content"].get("data") or {}
    if data.get("method") not in ("update", "echo_update"):
        return None
    return m["comm_id"], m.get("parent_msg_id"), data


def _mergeable(a: Outbound, b: Outbound) -> bool:
    pa_, pb = _update_parts(a), _update_parts(b)
    return (
        a.frame_id == b.frame_id
        and pa_ is not None
        and pb is not None
        and pa_[0] == pb[0]
        and pa_[1] == pb[1]
        and pa_[2].get("method") == pb[2].get("method")
    )


def _merge_updates(a: Outbound, b: Outbound) -> Outbound:
    da = a.message["content"]["data"]
    db = b.message["content"]["data"]
    buffers: dict[str, bytes] = {}
    for data, bufs in ((da, a.buffers), (db, b.buffers)):
        for key in data.get("state", {}):
            buffers = {k: v for k, v in buffers.items() if json.loads(k)[0] != key}
        for path, buf in zip(data.get("buffer_paths", []), bufs, strict=False):
            buffers[json.dumps(path)] = buf
    state = {**da.get("state", {}), **db.get("state", {})}
    message = copy.deepcopy(b.message)
    message["content"]["data"] = {
        "method": db.get("method"),
        "state": state,
        "buffer_paths": [json.loads(k) for k in buffers],
    }
    return Outbound(b.frame_id, message, list(buffers.values()))


def frames_for(outbound: Iterable[Outbound], frame_id: str) -> list[Outbound]:
    return [o for o in outbound if o.frame_id == frame_id]
