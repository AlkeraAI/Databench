"""Loro runs only in the sandbox worker.

Python's ``loro`` lags the JavaScript build's fix for an abort on malformed
operations, so a process that imports it can be taken down by one hostile
update. The process serving sockets must therefore never load it: these
import the backend (and the CRDT lane's own modules, which the gateway uses)
in a fresh interpreter and look.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "load",
    [
        pytest.param("import backend.app_factory as f; f.create_app()", id="app"),
        pytest.param("import backend.services.crdt.sandbox.pool", id="pool"),
        pytest.param("import backend.services.crdt.sandbox.protocol", id="protocol"),
    ],
)
def test_the_backend_never_loads_loro(load: str) -> None:
    probe = f"import sys; {load}; print('loro' in sys.modules)"
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().splitlines()[-1] == "False"


def test_the_worker_is_what_loads_it() -> None:
    probe = "import sys, backend.services.crdt.sandbox.worker; print('loro' in sys.modules)"
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().splitlines()[-1] == "True"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
def test_the_worker_caps_its_memory_and_never_dumps_core() -> None:
    import asyncio

    from backend.services.crdt.sandbox.pool import PoolConfig, SandboxPool, worker_command

    async def limits() -> str:
        pool = SandboxPool(
            PoolConfig(
                workers=1,
                command=worker_command(memory_mb=768, cache_docs=1, cache_bytes=1024 * 1024),
            )
        )
        try:
            reply = await pool.request("doc", {"op": "ping"}, budget_seconds=10)
            limits_file = Path(f"/proc/{reply.header['pid']}/limits")
            return await asyncio.to_thread(limits_file.read_text, encoding="utf-8")
        finally:
            await pool.close()

    text = asyncio.run(limits())
    rows = {line[:26].strip(): line[26:].split() for line in text.splitlines()[1:]}
    assert rows["Max core file size"][:2] == ["0", "0"]
    assert rows["Max address space"][:2] == [str(768 * 1024 * 1024)] * 2


async def test_a_worker_starts_with_none_of_the_backends_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker parses bytes a stranger sent; whatever a bug in Loro could
    reach, the backend's credentials are not among it. The stand-in worker
    writes down the environment it was started with."""
    from backend.services.crdt.sandbox.pool import PoolConfig, SandboxPool

    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:secret@db/prod")
    monkeypatch.setenv("JWT_SECRET", "not-for-the-worker")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "not-for-the-worker")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    seen = tmp_path / "env.json"
    dump = f"import json, os; open({str(seen)!r}, 'w').write(json.dumps(dict(os.environ)))"
    pool = SandboxPool(PoolConfig(workers=1, command=[sys.executable, "-c", dump]))
    try:
        with pytest.raises(Exception):  # noqa: B017 - the stand-in never answers; only its environment matters
            await pool.request("org:chat_draft:x", {"op": "ping"}, budget_seconds=5)
    finally:
        await pool.close()
    env = json.loads(seen.read_text())
    assert not {"DATABASE_URL", "JWT_SECRET", "AWS_SECRET_ACCESS_KEY"} & set(env)
    assert env["LANG"] == "en_US.UTF-8"
    assert "PATH" in env
