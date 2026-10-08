"""The widget walkthrough's server: two origins, a real kernel, and the
engine's own widget hub and asset store in between.

App origin (127.0.0.1:APP_PORT): the parent page standing in for the notebook
tab, and a WebSocket carrying frame traffic to the hub.
Content origin (localhost:CONTENT_PORT): the output frame's bootstrap page,
served under the content app's HTML CSP, unchanged.

The kernel is ipykernel driven through jupyter_client, standing in for the
Alkera kernel runtime: kernel-to-frontend comm traffic goes into
``alkera_notebook.widgets.WidgetHub`` exactly as the runtime's
``comm.open|msg|close`` events will, frontend messages leave through
``WidgetHub.frontend_send`` with the frame's ``msg_id``, and the kernel's
idle for that id goes back through ``WidgetHub.kernel_idle``. Two things the
Alkera runtime does inside the kernel are done here instead: routing output
under ``with out:`` into the Output model, and the host comm ``alkera.ui``
elements open (installed by ``RIG_HOST`` at start).

Run: uv run --no-project --with-requirements requirements.txt python server.py
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import sysconfig
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web
from jupyter_client.manager import AsyncKernelManager

HERE = Path(__file__).resolve().parent
PACKAGES = HERE.parents[2]
sys.path[:0] = [str(PACKAGES / "alkera-notebook"), str(PACKAGES / "alkera-py")]

from alkera_notebook.widgets import (  # noqa: E402
    FileBlobStore,
    Frame,
    WidgetAssets,
    WidgetHub,
    WidgetRefusedError,
)

APP_PORT = int(os.environ.get("ALK_WIDGETS_APP_PORT", "8711"))
CONTENT_PORT = int(os.environ.get("ALK_WIDGETS_CONTENT_PORT", "8712"))
APP_ORIGIN = f"http://127.0.0.1:{APP_PORT}"
CONTENT_ORIGIN = f"http://localhost:{CONTENT_PORT}"
SCOPE = "walkthrough-notebook"


def html_csp() -> str:
    """content_app._html_csp(page=False), with frame-ancestors = the app origin."""
    return (
        "default-src 'none'; script-src 'unsafe-inline'; object-src 'none'; "
        "style-src 'unsafe-inline'; img-src data:; font-src data:; "
        f"media-src data:; frame-ancestors {APP_ORIGIN}; "
        "base-uri 'none'; form-action 'none'; sandbox allow-scripts"
    )


CONTENT_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "private, no-store",
}

# A host for alkera.ui inside ipykernel, published where the Alkera runtime
# publishes its own: comms through ipykernel's comm package.
RIG_HOST = r"""
import sys, types, comm as _comm
class _RigComm:
    def __init__(self, target, data, metadata, on_msg):
        self._c = _comm.create_comm(target_name=target, data=data, metadata=metadata)
        self._c.on_msg(on_msg)
        self.comm_id = self._c.comm_id
    def send(self, data, buffers=None):
        self._c.send(data, buffers=buffers or None)
    def close(self):
        self._c.close()
class _RigHost:
    protocol_version = 1
    name = "runtime"
    def open_comm(self, target, data, metadata, on_msg):
        return _RigComm(target, data, metadata, on_msg)
    def register_reactive(self, obj):
        pass
    def display(self, obj):
        from IPython.display import display
        display(obj)
_m = types.ModuleType("_alkera_runtime"); _m.host = _RigHost(); sys.modules["_alkera_runtime"] = _m
sys.path.insert(0, %r)
"""


def b64(items: list[bytes]) -> list[str]:
    return [base64.b64encode(bytes(b)).decode() for b in items]


class Rig:
    def __init__(self, dist: Path) -> None:
        self.km = AsyncKernelManager(kernel_name="python3")
        self.kc: Any = None
        self.assets = WidgetAssets(FileBlobStore(HERE / ".assets"))
        self.assets.load_platform_dist(dist)
        self.hub = WidgetHub(scope=SCOPE, assets=self.assets, move_threshold=64 * 1024)
        self.env_prefix = Path(sysconfig.get_path("data"))
        self.sockets: dict[str, web.WebSocketResponse] = {}
        self.frame_socket: dict[str, str] = {}
        self.frontend_msgs: set[str] = set()
        self.exec_waiters: dict[str, dict[str, Any]] = {}
        self.stats = {"frontend_in": 0, "refused": 0}

    async def start(self) -> None:
        await self.km.start_kernel()
        self.kc = self.km.client()
        self.kc.start_channels()
        await self.kc.wait_for_ready(timeout=60)
        self._iopub_task = asyncio.create_task(self._iopub())
        await self.execute(RIG_HOST % str(PACKAGES / "alkera-py"))

    # ------------------------------------------------------------------ kernel -> hub

    async def _iopub(self) -> None:
        while True:
            msg = await self.kc.get_iopub_msg()
            try:
                await self._on_iopub(msg)
            except Exception as exc:
                print("iopub handler error", repr(exc), file=sys.stderr)

    async def _on_iopub(self, msg: dict[str, Any]) -> None:
        kind = msg["msg_type"]
        content = msg["content"]
        parent = msg.get("parent_header", {}).get("msg_id")
        buffers = [bytes(b) for b in msg.get("buffers", [])]
        if kind == "comm_open":
            self.hub.kernel_open(content["comm_id"], content, buffers, msg.get("metadata", {}))
        elif kind == "comm_msg":
            self.hub.kernel_msg(content["comm_id"], content, buffers, parent)
        elif kind == "comm_close":
            self.hub.kernel_close(content["comm_id"])
        elif (
            kind == "status"
            and content["execution_state"] == "idle"
            and parent in self.frontend_msgs
        ):
            self.frontend_msgs.discard(parent)
            self.hub.kernel_idle(parent)
        if kind in ("stream", "display_data", "error", "clear_output") and self._capture(
            kind, content, parent
        ):
            pass
        else:
            waiter = self.exec_waiters.get(parent or "")
            if waiter is not None:
                waiter["outputs"].append({"output_type": kind, **content})
                view = (
                    content.get("data", {}).get("application/vnd.jupyter.widget-view+json")
                    if kind == "display_data"
                    else None
                )
                if view:
                    await self.broadcast({"type": "display", "model_id": view["model_id"]})
        if kind == "status" and content["execution_state"] == "idle":
            waiter = self.exec_waiters.get(parent or "")
            if waiter is not None:
                waiter["done"].set()
        await self.flush()

    def _capture(self, kind: str, content: dict[str, Any], parent: str | None) -> bool:
        """Output produced under an Output model's msg_id goes to that model
        (the Alkera runtime does this in the kernel)."""
        if not parent:
            return False
        for model in self.hub.models():
            if (
                model.state.get("_model_name") == "OutputModel"
                and model.state.get("msg_id") == parent
            ):
                outputs = list(model.state.get("outputs", []))
                if kind == "clear_output":
                    outputs = []
                elif kind == "stream":
                    outputs.append(
                        {"output_type": "stream", "name": content["name"], "text": content["text"]}
                    )
                else:
                    outputs.append({"output_type": kind, **content})
                data = {"method": "update", "state": {"outputs": outputs}, "buffer_paths": []}
                msg = self.kc.session.msg("comm_msg", {"comm_id": model.comm_id, "data": data})
                self.kc.session.send(self.kc.shell_channel.socket, msg)
                self.hub.kernel_msg(
                    model.comm_id, {"comm_id": model.comm_id, "data": data}, [], None
                )
                return True
        return False

    # ------------------------------------------------------------------ hub -> frames

    async def flush(self) -> None:
        for out in self.hub.drain():
            await self.send_frame(
                out.frame_id,
                {
                    "type": "out",
                    "frame_id": out.frame_id,
                    "message": out.message,
                    "buffers": b64(out.buffers),
                },
            )

    async def send_frame(self, frame_id: str, payload: dict[str, Any]) -> None:
        socket_id = self.frame_socket.get(frame_id)
        ws = self.sockets.get(socket_id or "")
        if ws is not None and not ws.closed:
            await ws.send_str(json.dumps(payload))

    async def broadcast(self, payload: dict[str, Any]) -> None:
        for ws in list(self.sockets.values()):
            if not ws.closed:
                await ws.send_str(json.dumps(payload))

    # ------------------------------------------------------------------ frames -> hub

    async def from_parent(self, socket_id: str, m: dict[str, Any]) -> None:
        kind = m["type"]
        if kind == "attach":
            frame = Frame(
                m["frame_id"],
                m.get("client_id", socket_id),
                tuple(m.get("model_ids") or [m["model_id"]]),
                bool(m.get("readonly")),
            )
            self.frame_socket[frame.frame_id] = socket_id
            opens = self.hub.attach(frame)
            await self.send_frame(
                frame.frame_id,
                {
                    "type": "opens",
                    "frame_id": frame.frame_id,
                    "opens": [{**o.message, "buffers": b64(o.buffers)} for o in opens],
                },
            )
        elif kind == "comm.send":
            self.stats["frontend_in"] += 1
            try:
                delivery = self.hub.frontend_send(
                    m["frame_id"],
                    m["comm_id"],
                    m["msg_id"],
                    m["content"],
                    [base64.b64decode(b) for b in m.get("buffers", [])],
                    client_id=m.get("client_id", socket_id),
                )
            except WidgetRefusedError as exc:
                self.stats["refused"] += 1
                await self.send_frame(m["frame_id"], {"type": "refused", "message": str(exc)})
                return
            self.frontend_msgs.add(delivery.msg_id)
            msg = self.kc.session.msg("comm_msg", delivery.msg["content"])
            msg["header"]["msg_id"] = delivery.msg_id
            msg["msg_id"] = delivery.msg_id
            self.kc.session.send(self.kc.shell_channel.socket, msg, buffers=delivery.buffers)
        elif kind == "need_module":
            found = self.assets.resolve_module(SCOPE, m["name"], self.env_prefix)
            if found is None:
                await self.send_frame(m["frame_id"], {"type": "missing", "name": m["name"]})
                return
            entry, data = found
            await self.send_frame(
                m["frame_id"],
                {
                    "type": "module",
                    "frame_id": m["frame_id"],
                    "name": m["name"],
                    "version": entry.version,
                    "code": data.decode("utf-8"),
                },
            )
        elif kind == "detach":
            self.hub.detach(m["frame_id"])

    async def execute(self, code: str) -> dict[str, Any]:
        msg_id = self.kc.execute(code)
        waiter: dict[str, Any] = {"outputs": [], "done": asyncio.Event()}
        self.exec_waiters[msg_id] = waiter
        await asyncio.wait_for(waiter["done"].wait(), timeout=120)
        del self.exec_waiters[msg_id]
        return {
            "stdout": "".join(
                o.get("text", "") for o in waiter["outputs"] if o["output_type"] == "stream"
            ),
            "errors": [
                f"{o['ename']}: {o['evalue']}"
                for o in waiter["outputs"]
                if o["output_type"] == "error"
            ],
        }


def manager_bundle() -> tuple[Path, str]:
    dist = PACKAGES / "widgets" / "dist"
    manifest = json.loads((dist / "manifest.json").read_text())
    return dist, (dist / manifest["file"]).read_text()


async def main() -> None:
    dist, bundle = manager_bundle()
    rig = Rig(dist)
    await rig.start()
    counter = 0

    async def parent_page(_: web.Request) -> web.Response:
        html = (HERE / "parent.html").read_text().replace("__CONTENT_ORIGIN__", CONTENT_ORIGIN)
        return web.Response(text=html, content_type="text/html")

    async def parent_js(_: web.Request) -> web.Response:
        return web.Response(
            text=(HERE / "parent.js").read_text(), content_type="application/javascript"
        )

    async def manager_js(_: web.Request) -> web.Response:
        return web.Response(text=bundle, content_type="application/javascript")

    async def ws_route(request: web.Request) -> web.WebSocketResponse:
        nonlocal counter
        counter += 1
        socket_id = f"s{counter}"
        ws = web.WebSocketResponse(max_msg_size=64 * 1024 * 1024)
        await ws.prepare(request)
        rig.sockets[socket_id] = ws
        await ws.send_str(json.dumps({"type": "hello", "socket": socket_id}))
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                await rig.from_parent(socket_id, json.loads(msg.data))
                await rig.flush()
        rig.sockets.pop(socket_id, None)
        return ws

    async def exec_route(request: web.Request) -> web.Response:
        body = await request.json()
        result = await rig.execute(body["code"])
        await rig.flush()
        return web.json_response(result)

    async def stats_route(_: web.Request) -> web.Response:
        return web.json_response(rig.stats)

    async def frame_page(_: web.Request) -> web.Response:
        html = (
            (HERE / "frame.html").read_text().replace("__APP_ORIGINS__", json.dumps([APP_ORIGIN]))
        )
        return web.Response(
            text=html,
            content_type="text/html",
            headers={**CONTENT_HEADERS, "Content-Security-Policy": html_csp()},
        )

    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.add_routes(
        [
            web.get("/", parent_page),
            web.get("/parent.js", parent_js),
            web.get("/manager.js", manager_js),
            web.get("/kernel", ws_route),
            web.post("/exec", exec_route),
            web.get("/stats", stats_route),
        ]
    )
    content = web.Application()
    content.add_routes([web.get("/c/nb-output/frame.html", frame_page)])
    for application, port in ((app, APP_PORT), (content, CONTENT_PORT)):
        runner = web.AppRunner(application)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", port).start()
    print(f"ready app={APP_ORIGIN} content={CONTENT_ORIGIN}", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
