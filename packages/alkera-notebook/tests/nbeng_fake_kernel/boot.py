"""A stand-in kernel for the engine's tests: a real process speaking RPC v1.

Standard library only. It does what the engine needs to be tested without
the real kernel runtime: the token on stdin, the ``hello``
handshake, ``run.execute`` on the main thread with ``KeyboardInterrupt`` on
SIGINT, outputs and streams, ``cell.variables``, ``names.delete``,
``inspect.value``, ``comm.deliver``, ``kernel.shutdown`` and
``sql.execute`` requests back to the engine. Cells see helpers ``sql``,
``bind`` and ``widget_value``. It exits when the connection drops.
"""

from __future__ import annotations

import io
import json
import os
import queue
import signal
import socket
import struct
import sys
import threading
import time
import traceback
from typing import Any

HEAD = struct.Struct(">II")


def encode(msg: dict[str, Any], segments: list[bytes] | None = None) -> bytes:
    segments = segments or []
    header = dict(msg)
    header["seg"] = [len(s) for s in segments]
    raw = json.dumps(header).encode()
    return HEAD.pack(4 + len(raw) + sum(map(len, segments)), len(raw)) + raw + b"".join(segments)


class Conn:
    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.lock = threading.Lock()
        self.next_id = 0
        self.waiting: dict[int, queue.Queue[tuple[dict[str, Any], list[bytes]]]] = {}

    def send(self, msg: dict[str, Any], segments: list[bytes] | None = None) -> None:
        data = encode(msg, segments)
        with self.lock:
            self.sock.sendall(data)

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self.send({"jsonrpc": "2.0", "method": method, "params": params})

    def request(
        self, method: str, params: dict[str, Any], ctx: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any], list[bytes]]:
        with self.lock:
            self.next_id += 1
            rid = self.next_id
        box: queue.Queue[tuple[dict[str, Any], list[bytes]]] = queue.Queue()
        self.waiting[rid] = box
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        if ctx:
            msg["ctx"] = ctx
        self.send(msg)
        reply, segs = box.get()
        if "error" in reply:
            raise RuntimeError(reply["error"].get("message", "error"))
        return reply.get("result") or {}, segs

    def recv_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise EOFError
            buf += chunk
        return buf

    def recv(self) -> tuple[dict[str, Any], list[bytes]]:
        _total, hlen = HEAD.unpack(self.recv_exact(8))
        header = json.loads(self.recv_exact(hlen))
        segs = [self.recv_exact(n) for n in header.pop("seg", [])]
        return header, segs


NS: dict[str, Any] = {"__name__": "__main__"}
WORK: queue.Queue[tuple[str, dict[str, Any], Any]] = queue.Queue()
STATE: dict[str, Any] = {"run": None, "cell": None, "interrupt_for": None}
WIDGETS: dict[str, dict[str, Any]] = {}
conn: Conn


def main_reader() -> None:
    try:
        while True:
            msg, segs = conn.recv()
            if "method" in msg and "id" in msg:
                WORK.put(("request", msg, segs)) if msg["method"] in MAIN_THREAD else side_request(
                    msg
                )
            elif "method" in msg:
                if msg["method"] == "$/cancelRequest":
                    continue
            else:
                box = conn.waiting.pop(msg.get("id"), None)
                if box is not None:
                    box.put((msg, segs))
    except (EOFError, OSError):
        pass
    # The connection is gone: the kernel exits (unless a test keeps it alive
    # to prove the engine ends a kernel whose connection dropped).
    if STATE.get("keep_alive"):
        while True:
            time.sleep(60)
    os._exit(0)


MAIN_THREAD = {"run.execute", "names.delete", "comm.deliver"}


def reply(msg: dict[str, Any], result: dict[str, Any]) -> None:
    conn.send({"jsonrpc": "2.0", "id": msg["id"], "result": result})


def side_request(msg: dict[str, Any]) -> None:
    method = msg["method"]
    params = msg.get("params") or {}
    if method == "run.interrupt":
        STATE["interrupt_for"] = params.get("run_id")
        reply(msg, {"noted": True})
    elif method == "kernel.shutdown":
        reply(msg, {})
        os._exit(0)
    elif method == "inspect.value":
        name = params.get("name")
        if name not in NS:
            conn.send(
                {
                    "jsonrpc": "2.0",
                    "id": msg["id"],
                    "error": {"code": -32602, "message": "no such name"},
                }
            )
        else:
            reply(msg, {"summary": {"type": type(NS[name]).__name__, "repr": repr(NS[name])[:200]}})
    elif method == "inspect.frame":
        name = params.get("name")
        rows = NS.get(name)
        if not isinstance(rows, list):
            conn.send(
                {
                    "jsonrpc": "2.0",
                    "id": msg["id"],
                    "error": {"code": -32602, "message": "not a frame"},
                }
            )
            return
        off, lim = int(params.get("offset", 0)), int(params.get("limit", 50))
        table = {
            "schema": [{"name": "value", "type": "string"}],
            "rows": [[repr(r)] for r in rows[off : off + lim]],
            "total_rows": len(rows),
            "offset": off,
        }
        reply(msg, {"table": table, "total_rows": len(rows)})
    elif method == "complete":
        reply(msg, {"matches": sorted(n for n in NS if n.startswith(params.get("code", "")))})
    else:
        conn.send(
            {
                "jsonrpc": "2.0",
                "id": msg["id"],
                "error": {"code": -32601, "message": f"no method {method}"},
            }
        )


def sql(query: str, connection: str = "Warehouse") -> Any:
    """Rows of the result: the engine answers in ``rows.json`` (this kernel
    lists no Arrow codec)."""
    ctx = {"run_id": STATE["run"], "cell_id": STATE["cell"]}
    result, segs = conn.request("sql.execute", {"sql": query, "connection": connection}, ctx)
    table = result.get("table")
    if isinstance(table, dict) and "$seg" in table:
        table = json.loads(segs[table["$seg"]])
    if isinstance(table, dict) and "rows" in table:
        return table["rows"]
    return table


class Widget:
    """What a cell holds for a bound widget: ``.value`` reads the live state."""

    def __init__(self, model_id: str) -> None:
        self.model_id = model_id

    @property
    def value(self) -> Any:
        return WIDGETS.get(self.model_id, {}).get("value")


def bind(model_id: str, names: list[str], value: Any) -> Widget:
    WIDGETS[model_id] = {"value": value}
    conn.notify(
        "comm.open",
        {
            "comm_id": model_id,
            "content": {"data": {"state": {"value": value, "_model_name": "SliderModel"}}},
            "parent_msg_id": None,
        },
    )
    conn.notify("ui.bindings", {"cell_id": STATE["cell"], "bindings": {model_id: names}})
    return Widget(model_id)


def widget_value(model_id: str) -> Any:
    return WIDGETS.get(model_id, {}).get("value")


def display(value: Any) -> None:
    conn.notify(
        "cell.output",
        {
            "run_id": STATE["run"],
            "cell_id": STATE["cell"],
            "output": {"text/plain": repr(value)},
            "mode": "append",
        },
    )


class Stream(io.TextIOBase):
    def __init__(self, name: str) -> None:
        self.name_ = name

    def write(self, text: str) -> int:
        if text and STATE["cell"]:
            conn.notify(
                "cell.stream",
                {
                    "run_id": STATE["run"],
                    "cell_id": STATE["cell"],
                    "name": self.name_,
                    "text": text,
                },
            )
        return len(text)


class StopCellError(Exception):
    pass


HELD: list[bytearray] = []


def alloc(mb: int, step_mb: int = 50, pause: float = 0.02) -> int:
    """Allocate and touch ``mb`` MiB in steps (so RSS really grows)."""
    done = 0
    while done < mb:
        n = min(step_mb, mb - done)
        block = bytearray(n * 1024 * 1024)
        for i in range(0, len(block), 4096):
            block[i] = 1
        HELD.append(block)
        done += n
        time.sleep(pause)
    return done


def drop_connection() -> None:
    """Close the RPC socket but keep the process alive."""
    STATE["keep_alive"] = True
    conn.sock.shutdown(socket.SHUT_RDWR)
    conn.sock.close()


def stop(predicate: bool = True) -> None:
    if predicate:
        raise StopCellError


def run_execute(params: dict[str, Any]) -> None:
    run_id = params["run_id"]
    STATE["run"] = run_id
    for name in params.get("clear", []):
        NS.pop(name, None)
    conn.notify("run.started", {"run_id": run_id})
    failed: set[str] = set()
    status = "ok"
    for step in params.get("steps", []):
        if set(step.get("refs", [])) & failed:
            failed.update(step.get("defs", []))
            continue
        cid = step["cell_id"]
        STATE["cell"] = cid
        conn.notify("cell.started", {"run_id": run_id, "cell_id": cid})
        started = time.monotonic()
        error = None
        cell_status = "ok"
        try:
            exec(compile(step["body"] or "pass", step.get("filename", cid), "exec"), NS)
            value = eval(
                compile(step.get("last_expr") or "None", step.get("filename", cid), "eval"), NS
            )
            if value is not None:
                if hasattr(value, "_repr_mimebundle_"):
                    bundle = dict(value._repr_mimebundle_())
                    bundle.setdefault("text/plain", repr(value))
                else:
                    bundle = {"text/plain": repr(value)}
                conn.notify(
                    "cell.output",
                    {"run_id": run_id, "cell_id": cid, "output": bundle, "mode": "replace"},
                )
        except KeyboardInterrupt:
            cell_status = "interrupted"
        except StopCellError:
            cell_status = "stopped"
        except BaseException as exc:
            cell_status = "error"
            error = {
                "ename": type(exc).__name__,
                "evalue": str(exc),
                "traceback": traceback.format_exception(exc),
            }
        defs = [d for d in step.get("defs", []) if d in NS]
        if cell_status != "ok":
            failed.update(step.get("defs", []))
            status = "error" if cell_status == "error" else cell_status
        conn.notify(
            "cell.finished",
            {
                "run_id": run_id,
                "cell_id": cid,
                "status": cell_status,
                "error": error,
                "defs": defs,
                "duration_ms": int((time.monotonic() - started) * 1000),
            },
        )
        if cell_status == "ok" and defs:
            conn.notify(
                "cell.variables",
                {
                    "run_id": run_id,
                    "cell_id": cid,
                    "variables": [
                        {"name": d, "type": type(NS[d]).__name__, "repr": repr(NS[d])[:200]}
                        for d in defs
                    ],
                },
            )
        if cell_status == "interrupted":
            break
    STATE["cell"] = None
    STATE["run"] = None
    STATE["interrupt_for"] = None
    conn.notify("run.finished", {"run_id": run_id, "status": status})


def comm_deliver(params: dict[str, Any]) -> None:
    msg = params.get("msg") or {}
    content = msg.get("content") or {}
    comm_id = content.get("comm_id")
    data = content.get("data") or {}
    state = data.get("state") or {}
    if comm_id in WIDGETS and "value" in state:
        WIDGETS[comm_id]["value"] = state["value"]
        conn.notify(
            "comm.msg",
            {
                "comm_id": comm_id,
                "content": {"data": {"method": "echo_update", "state": state}},
                "parent_msg_id": params.get("msg_id"),
            },
        )
    conn.notify("comm.idle", {"msg_id": params.get("msg_id")})


def main() -> None:
    global conn
    token = sys.stdin.readline().strip()
    sys.stdin = open(os.devnull)
    import site

    site.main()  # the environment's site-packages, after the token is held
    endpoint = os.environ["ALKERA_RPC_ENDPOINT"]
    path = endpoint[len("unix:") :]
    nb_dir = os.environ.get("ALKERA_NOTEBOOK_DIR", ".")
    sys.path.insert(0, nb_dir)
    NS.update(
        {
            "display": display,
            "sql": sql,
            "bind": bind,
            "widget_value": widget_value,
            "stop": stop,
            "alloc": alloc,
            "drop_connection": drop_connection,
        }
    )
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    if os.environ.get("NBENG_FAKE_DELAY_CONNECT"):
        time.sleep(float(os.environ["NBENG_FAKE_DELAY_CONNECT"]))
    sock.connect(path)
    conn = Conn(sock)
    conn.send(
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "hello",
            "params": {
                "token": token,
                "client": {"name": "alkera-kernel", "version": "0.0.0-fake", "protocols": [1]},
                "runtime": {
                    "version": sys.version.split()[0],
                    "implementation": sys.implementation.name,
                },
                "codecs": ["json", "rows.json"],
                "libs": {},
                "env": sorted(os.environ),
                # A self-report the engine must never use for decisions.
                "rss_bytes": 1,
            },
        }
    )
    hello, _ = conn.recv()
    if "error" in hello:
        sys.exit(3)
    # What the real runtime serves as ``alkera.args()``.
    NS["alkera_args"] = ((hello.get("result") or {}).get("settings") or {}).get("args") or {}
    threading.Thread(target=main_reader, daemon=True).start()
    sys.stdout = Stream("stdout")  # type: ignore[assignment]
    sys.stderr = Stream("stderr")  # type: ignore[assignment]
    signal.signal(signal.SIGINT, signal.default_int_handler)
    while True:
        try:
            _kind, msg, _segs = WORK.get()
        except KeyboardInterrupt:
            continue  # an interrupt while idle does nothing
        method = msg["method"]
        try:
            if method == "run.execute":
                reply(msg, {"accepted": True})
                run_execute(msg.get("params") or {})
            elif method == "names.delete":
                for n in (msg.get("params") or {}).get("names", []):
                    NS.pop(n, None)
                reply(msg, {})
            elif method == "comm.deliver":
                reply(msg, {})
                comm_deliver(msg.get("params") or {})
        except KeyboardInterrupt:
            STATE["cell"] = None
            run_id = STATE["run"]
            STATE["run"] = None
            if run_id:
                conn.notify("run.finished", {"run_id": run_id, "status": "interrupted"})


if __name__ == "__main__":
    main()
