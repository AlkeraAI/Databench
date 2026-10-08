"""An org worker's children never read its control channel.

The supervisor hands each worker its end of a socketpair on standard input.
A child the worker starts that reads its stdin (``runsc ps`` did) would take
the supervisor's frames off that socket, and the worker, cut off, was
restarted every few minutes. Here a real worker process is spawned the way
the supervisor spawns one (``worker_spec``), deliberately WITHOUT moving the
channel off fd 0, so the only protection left is the spawn seam's: the
worker runs a fake ``runsc`` that reads stdin to its end, then answers the
supervisor's frame.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
import textwrap
from pathlib import Path

import pytest
from alkera_cli.org_worker_channel import worker_spec
from alkera_core.process import spawn_async

needs_posix = pytest.mark.skipif(
    sys.platform == "win32", reason="a socketpair on a child's fd 0 is POSIX"
)

FAKE_RUNSC = textwrap.dedent(
    """
    import sys
    # Reads its standard input to the end, as a probe that inherited it would.
    sys.stdout.write(repr(sys.stdin.buffer.read()))
    """
)

FAKE_WORKER = textwrap.dedent(
    """
    import os, sys
    from alkera_core.process import SpawnSpec, run
    # A worker that left its channel on fd 0, so only the seam protects it.
    probe = run(
        SpawnSpec(argv=[sys.executable, sys.argv[1]], env=dict(os.environ), stdout="pipe"),
        timeout=20,
    )
    frame = os.read(0, 64)
    os.write(1, b"probe read " + probe.stdout + b"; worker got " + frame + b"\\n")
    """
)


@needs_posix
async def test_org_worker_survives_child_reading_stdin(tmp_path: Path) -> None:
    runsc = tmp_path / "runsc.py"
    runsc.write_text(FAKE_RUNSC, encoding="utf-8")
    worker = tmp_path / "worker.py"
    worker.write_text(FAKE_WORKER, encoding="utf-8")
    ours, theirs = socket.socketpair()
    try:
        ours.sendall(b"drain")
        process = await spawn_async(
            worker_spec(
                [sys.executable, str(worker), str(runsc)],
                env=os.environ,
                channel=theirs.fileno(),
            )
        )
        theirs.close()
        assert process.stdout is not None
        line = await asyncio.wait_for(process.stdout.readline(), 30)
        await asyncio.wait_for(process.wait(), 30)
    finally:
        ours.close()
    assert line.decode().strip() == "probe read b''; worker got drain"
    assert process.returncode == 0
