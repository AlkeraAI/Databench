"""The CRDT sandbox workers on their own interpreter.

The notebook format code runs in the workers, so a deployment runs them on a
Python at least as new as the newest a notebook's environment may use
(``realtime_crdt_worker_python``, built by ``make nb-doc-sandbox-python``).
These cases start real workers on that interpreter and have them read a
notebook written in syntax only it parses, then render it back.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import psutil
import pytest
import pytest_asyncio
from backend.services.crdt.sandbox.pool import PoolConfig, SandboxPool, worker_command
from backend.services.crdt.sandbox.protocol import decode_projection
from tests.crdt.test_nbdoc_real_format import HEAD, SETUP, TAIL

pytestmark = [pytest.mark.asyncio]

REPO = Path(__file__).resolve().parents[4]
#: Where ``make nb-doc-sandbox-python`` builds the interpreter, unless the
#: environment names another.
INTERPRETER = os.environ.get("REALTIME_CRDT_WORKER_PYTHON") or str(
    REPO / ".venv-crdt-sandbox" / "bin" / "python"
)
NEWEST = (3, 14)


def _version(python: str) -> tuple[int, int] | None:
    try:
        found = subprocess.run(
            [python, "-c", "import sys; print(sys.version_info[0], sys.version_info[1])"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    major, minor = found.stdout.split()
    return int(major), int(minor)


def _executable(pid: int) -> str:
    """The executable a running process was started from."""
    return str(psutil.Process(pid).exe())


def _same_file(one: str, other: str) -> bool:
    return os.path.realpath(one) == os.path.realpath(other)


VERSION = _version(INTERPRETER)
requires_interpreter = pytest.mark.skipif(
    VERSION is None or VERSION < NEWEST,
    reason=f"no Python {NEWEST[0]}.{NEWEST[1]} sandbox interpreter at {INTERPRETER} "
    "(make nb-doc-sandbox-python)",
)

#: A cell in syntax Python 3.14 reads and 3.13 does not: a template string.
TSTRING = """@app.cell(alkera_id="s7t8v9w0x1")
def _():
    name = "world"
    greeting = t"hello {name}"
    return
"""


@pytest_asyncio.fixture
async def pool() -> AsyncIterator[SandboxPool]:
    started = SandboxPool(
        PoolConfig(
            workers=1,
            command=worker_command(
                memory_mb=1024,
                cache_docs=8,
                cache_bytes=8 * 1024 * 1024,
                python=INTERPRETER,
            ),
        )
    )
    try:
        yield started
    finally:
        await started.close()


@requires_interpreter
async def test_a_worker_runs_on_the_configured_interpreter(pool: SandboxPool) -> None:
    reply = await pool.request("k", {"op": "ping"}, [], budget_seconds=30)
    assert reply.header["ok"] is True
    assert _same_file(_executable(int(reply.header["pid"])), INTERPRETER)


@requires_interpreter
async def test_a_notebook_in_the_newest_syntax_is_read_and_rendered_by_the_workers(
    pool: SandboxPool,
) -> None:
    text = HEAD + SETUP + "\n\n" + TSTRING + TAIL
    seeded = await pool.request(
        "org:notebook:t",
        {"op": "seed", "key": "org:notebook:t@1", "epoch": 1, "rules": "notebook", "peer": 5000},
        [text.encode()],
        budget_seconds=60,
    )
    assert seeded.header["ok"] is True, seeded.header
    projection = decode_projection(seeded.blobs[2])
    assert projection["kinds"] == {"setup": 1, "python": 1}
    content = await pool.request(
        "org:notebook:t",
        {
            "op": "content",
            "key": "org:notebook:t@1",
            "epoch": 1,
            "log_seq": 0,
            "rules": "notebook",
        },
        [],
        budget_seconds=60,
    )
    rendered = content.blobs[0].decode()
    assert 'greeting = t"hello {name}"' in rendered
    assert 'alkera_id="s7t8v9w0x1"' in rendered
