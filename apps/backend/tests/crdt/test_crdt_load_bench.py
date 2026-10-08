"""Load benchmark for the Loro lane: many people typing at once.

Opt-in twice over: the ``bench`` marker keeps it out of the default suite and
CI, and it skips without ``CRDT_BENCH=1``. It measures; it does not gate:

    CRDT_BENCH=1 pytest -m bench -s apps/backend/tests/crdt/test_crdt_load_bench.py

``CRDT_BENCH_DB_DELAY_MS`` puts a proxy between the replicas and Postgres that
delays each direction, so a laptop's sub-millisecond database can stand in
for production's (another availability zone, a synchronous standby). Two backend
replicas run as real ``uvicorn`` processes, the way production runs one per
task, against this run's database. ``CRDT_BENCH_CHATS`` chats are each open in
two tabs by two different people, the tabs spread across both replicas, and
every tab types a short token at ``CRDT_BENCH_RATE`` per second (Poisson) for
``CRDT_BENCH_SECONDS``. Reported: how long a typed token takes to be
confirmed by the server (p50 / p95 / p99 / max), updates acknowledged per
second, Postgres transactions per second, each replica's CPU and its sandbox
workers' CPU, and the client loop's own lag (so a slow client cannot pass for
a slow server). At the end every chat must have converged with every token
exactly once.
"""

from __future__ import annotations

import asyncio
import os
import random
import socket
import statistics
import subprocess
import sys
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import psutil
import pytest
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.files.authz.grants import Principal
from alkera_core.files.authz.ladder import ROLE_WRITER
from alkera_core.models import User
from backend.services.chats import chat_service
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession
from tests.chat_shares import files_on, share_chat_with  # noqa: F401
from tests.conftest import OrgWithAdmin, make_member, served_client
from tests.crdt.crdt_client import LaneClient
from tests.test_ws_gateway import login

pytestmark = [
    pytest.mark.bench,
    # Minutes of typing plus two replicas started and drained, not the
    # suite's per-test budget.
    pytest.mark.timeout(900),
    pytest.mark.asyncio,
    pytest.mark.usefixtures("files_on"),
    pytest.mark.skipif(os.environ.get("CRDT_BENCH") != "1", reason="set CRDT_BENCH=1"),
]

CHATS = int(os.environ.get("CRDT_BENCH_CHATS", "100"))
RATE = float(os.environ.get("CRDT_BENCH_RATE", "4"))
SECONDS = float(os.environ.get("CRDT_BENCH_SECONDS", "20"))
REPLICAS = int(os.environ.get("CRDT_BENCH_REPLICAS", "2"))
WRITERS = max(2, CHATS // 2)
#: Settings read .env files relative to the working directory: the repo root.
REPO_ROOT = Path(__file__).resolve().parents[4]
REPORT = Path(os.environ.get("CRDT_BENCH_REPORT", "/dev/null"))


@dataclass
class TimedTab(LaneClient):
    typed_at: dict[str, float] = field(default_factory=dict)
    latencies: list[float] = field(default_factory=list)
    acks: int = 0

    async def _frame(self, frame: dict[str, Any]) -> None:
        await super()._frame(frame)
        env = frame.get("envelope") if frame.get("t") == "doc" else None
        if not env or env.get("kind") not in ("ack", "snapshot"):
            return
        if env["kind"] == "ack":
            self.acks += 1
        pending = set(self.unconfirmed())
        now = time.monotonic()
        for token, at in list(self.typed_at.items()):
            if token not in pending:
                self.latencies.append(now - at)
                del self.typed_at[token]

    async def type_timed(self, token: str) -> None:
        self.typed_at[token] = time.monotonic()
        await self.type_token(token)


DB_DELAY = float(os.environ.get("CRDT_BENCH_DB_DELAY_MS", "0")) / 1000


@dataclass
class DbProxy:
    """A TCP proxy in front of Postgres that delays every chunk by ``delay``
    each way (a network round trip like production's, where the database is
    in another AZ) and counts the statements the replicas execute (protocol
    ``Execute`` and simple ``Query`` messages)."""

    upstream: tuple[str, int]
    delay: float
    statements: int = 0
    server: asyncio.Server | None = None

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        return int(self.server.sockets[0].getsockname()[1])

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        up_reader, up_writer = await asyncio.open_connection(*self.upstream)
        await asyncio.gather(
            self._pump(reader, up_writer, count=True),
            self._pump(up_reader, writer, count=False),
            return_exceptions=True,
        )
        for w in (writer, up_writer):
            w.close()

    async def _pump(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *, count: bool
    ) -> None:
        queue: asyncio.Queue[tuple[float, bytes]] = asyncio.Queue()
        buffer = b""
        started = not count

        async def deliver() -> None:
            while True:
                due, data = await queue.get()
                if not data:
                    writer.close()
                    return
                wait = due - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                writer.write(data)
                await writer.drain()

        sender = asyncio.create_task(deliver())
        try:
            while True:
                data = await reader.read(65536)
                await queue.put((time.monotonic() + self.delay, data))
                if not data:
                    break
                if count:
                    buffer += data
                    while True:
                        if not started:
                            if len(buffer) < 4:
                                break
                            size = int.from_bytes(buffer[:4], "big")
                            if len(buffer) < size:
                                break
                            code = int.from_bytes(buffer[4:8], "big") if size >= 8 else 0
                            # An SSL / GSS negotiation request precedes the
                            # startup message and is untyped too.
                            buffer = buffer[size:]
                            started = code not in (80877103, 80877104)
                            continue
                        if len(buffer) < 5:
                            break
                        size = int.from_bytes(buffer[1:5], "big")
                        if len(buffer) < size + 1:
                            break
                        if buffer[:1] in (b"E", b"Q"):
                            self.statements += 1
                        buffer = buffer[size + 1 :]
        finally:
            await sender


def _at_port(url: str, port: int) -> str:
    return make_url(url).set(host="127.0.0.1", port=port).render_as_string(hide_password=False)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _replica(stack: AsyncExitStack, root: Path, db_port: int) -> tuple[str, psutil.Process]:
    port = _free_port()
    env = {
        **os.environ,
        "DATABASE_URL": _at_port(os.environ["DATABASE_URL"], db_port),
        "DATABASE_URL_SYNC": _at_port(os.environ["DATABASE_URL_SYNC"], db_port),
        "FILES_ENABLED": "true",
        "FILES_STORE_PROVIDER": "filesystem",
        "FILES_STORE_ROOT": str(root),
        "LOG_LEVEL": "WARNING",
        "RATE_LIMIT_ENABLED": "false",
    }
    probe = os.environ.get("CRDT_BENCH_PROBE")
    command = (
        # A wrapper that starts uvicorn itself on the port it is given, for
        # timing probes the benchmark's operator brings along.
        [sys.executable, probe, str(port)]
        if probe
        else [
            sys.executable,
            "-m",
            "uvicorn",
            "--factory",
            "backend.app_factory:create_app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--ws-max-size",
            "2162688",
            "--no-access-log",
            "--log-level",
            "warning",
        ]
    )
    proc = subprocess.Popen(  # noqa: ASYNC220 - started once, waited on below
        command,
        env={**env, "PROBE_OUT": f"{os.environ.get('CRDT_BENCH_REPORT', '/dev/null')}.{port}"},
        cwd=REPO_ROOT,
    )

    def stop() -> None:
        proc.terminate()
        try:
            proc.wait(15)
        except subprocess.TimeoutExpired:
            proc.kill()

    stack.callback(stop)
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
        for _ in range(300):
            try:
                if (await http.get("/health/ready")).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            await asyncio.sleep(0.1)
        else:
            raise AssertionError("replica never became ready")
    return f"127.0.0.1:{port}", psutil.Process(proc.pid)


def _tree_cpu(process: psutil.Process) -> tuple[float, float]:
    """(this process's CPU seconds, its children's — the sandbox workers)."""
    own = sum(process.cpu_times()[:2])
    kids = 0.0
    for child in process.children(recursive=True):
        try:
            kids += sum(child.cpu_times()[:2])
        except psutil.NoSuchProcess:
            pass
    return own, kids


async def _commits() -> int:
    async with AsyncSessionLocal() as db:
        value = await db.scalar(
            text("SELECT xact_commit FROM pg_stat_database WHERE datname = current_database()")
        )
        return int(value or 0)


def _pct(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


async def test_many_people_typing_at_once(
    real_session: AsyncSession, org_admin: OrgWithAdmin, tmp_path: Path
) -> None:
    owner = await real_session.get(User, org_admin.admin_id)
    assert owner is not None
    writers: list[tuple[User, str]] = []
    for _ in range(WRITERS):
        member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
        assert password is not None
        writers.append((member, password))
    chats = []
    for index in range(CHATS):
        chat, _ = await chat_service.create_chat(
            real_session,
            owner=owner,
            org_id=owner.home_org_team_id,
            title=f"Bench {index}",
            client_id=None,
            machine_id=None,
            machine_status="none",
        )
        await real_session.commit()
        pair = (index % WRITERS, (index + WRITERS // 2) % WRITERS)
        for w in pair:
            await share_chat_with(
                real_session,
                chat=chat,
                owner=owner,
                principal=Principal(kind="user", id=writers[w][0].id),
                role=ROLE_WRITER,
            )
        chats.append((str(chat.id), pair))
    await real_session.commit()

    async with AsyncExitStack() as stack:
        root = tmp_path / "files-store-bench"
        root.mkdir(exist_ok=True)
        db_url = make_url(os.environ["DATABASE_URL"])
        proxy = DbProxy((db_url.host or "127.0.0.1", db_url.port or 5432), DB_DELAY)
        db_port = await proxy.start()
        replicas = [await _replica(stack, root, db_port) for _ in range(REPLICAS)]
        clients: dict[int, httpx.AsyncClient] = {}
        for index, (member, password) in enumerate(writers):
            client = served_client(replicas[0][0], timeout=30)
            await login(client, member.email, password)
            stack.push_async_callback(client.aclose)
            clients[index] = client
        tabs: list[list[TimedTab]] = []
        n = 0
        for chat_id, pair in chats:
            pair_tabs = []
            for w in pair:
                tab = TimedTab(clients[w], f"doc:chat_draft:{chat_id}", f"t{n}")
                await tab.connect(replicas[n % REPLICAS][0])
                stack.push_async_callback(tab.disconnect)
                pair_tabs.append(tab)
                n += 1
            tabs.append(pair_tabs)
        for pair_tabs in tabs:
            for tab in pair_tabs:
                await tab.wait_synced(60)

        lag: list[float] = []
        stop = asyncio.Event()

        async def watch_loop() -> None:
            while not stop.is_set():
                before = time.monotonic()
                await asyncio.sleep(0.05)
                lag.append(time.monotonic() - before - 0.05)

        typed: dict[str, list[str]] = {}

        async def type_on(tab: TimedTab, chat_id: str) -> None:
            rng = random.Random(tab.name)
            end = time.monotonic() + SECONDS
            count = 0
            while time.monotonic() < end:
                await asyncio.sleep(rng.expovariate(RATE))
                token = f"<{tab.name}.{count}>"
                count += 1
                typed.setdefault(chat_id, []).append(token)
                await tab.type_timed(token)

        cpu_before = [_tree_cpu(proc) for _, proc in replicas]
        commits_before = await _commits()
        statements_before = proxy.statements
        watcher = asyncio.create_task(watch_loop())
        started = time.monotonic()
        await asyncio.gather(
            *(
                type_on(tab, chat_id)
                for (chat_id, _), pair_tabs in zip(chats, tabs, strict=True)
                for tab in pair_tabs
            )
        )
        typing_seconds = time.monotonic() - started
        for pair_tabs in tabs:
            for tab in pair_tabs:
                await tab.settle(60)
        elapsed = time.monotonic() - started
        stop.set()
        await watcher
        cpu_after = [_tree_cpu(proc) for _, proc in replicas]
        commits = await _commits() - commits_before
        statements = proxy.statements - statements_before

        latencies = [x for pair_tabs in tabs for tab in pair_tabs for x in tab.latencies]
        acks = sum(tab.acks for pair_tabs in tabs for tab in pair_tabs)
        tokens = sum(len(v) for v in typed.values())
        report = [
            f"chats={CHATS} tabs={n} replicas={REPLICAS} rate/tab={RATE}/s "
            f"typing={typing_seconds:.1f}s",
            f"tokens typed={tokens} ({tokens / typing_seconds:.0f}/s)  acks={acks} "
            f"({acks / elapsed:.0f}/s)  commits={commits} ({commits / elapsed:.0f}/s)  "
            f"statements={statements} ({statements / max(acks, 1):.1f} per ack)  "
            f"db delay each way={DB_DELAY * 1000:.1f}ms",
            "confirm latency ms: "
            f"p50={_pct(latencies, 0.5) * 1000:.0f} p95={_pct(latencies, 0.95) * 1000:.0f} "
            f"p99={_pct(latencies, 0.99) * 1000:.0f} max={max(latencies) * 1000:.0f} "
            f"mean={statistics.fmean(latencies) * 1000:.0f}",
            f"client loop lag ms: p99={_pct(lag, 0.99) * 1000:.0f} max={max(lag) * 1000:.0f}",
        ]
        for index, ((own0, kids0), (own1, kids1)) in enumerate(
            zip(cpu_before, cpu_after, strict=True)
        ):
            report.append(
                f"replica {index}: backend cpu={(own1 - own0) / elapsed * 100:.0f}% "
                f"sandbox cpu={(kids1 - kids0) / elapsed * 100:.0f}% (of one core)"
            )
        print("\n" + "\n".join(report))
        await asyncio.to_thread(REPORT.write_text, "\n".join(report))

        # A writer is acknowledged before its update is announced, so the
        # last broadcasts may still be on their way: wait for each chat's tabs
        # to agree, without asking the server again. These clients never
        # resync on their own, so a broadcast that was lost fails here.
        for _ in range(150):
            if all(len({tab.text for tab in pair_tabs}) == 1 for pair_tabs in tabs):
                break
            await asyncio.sleep(0.1)
        for (chat_id, _), pair_tabs in zip(chats, tabs, strict=True):
            final = pair_tabs[0].text
            assert all(tab.text == final for tab in pair_tabs), chat_id
            for token in typed.get(chat_id, []):
                assert final.count(token) == 1, (chat_id, token)
