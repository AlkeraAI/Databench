"""A test that changes ``SIGCHLD`` gives back the kernel's disposition it found.

The suite's Temporal dev server is watched by a ``SIGCHLD`` handler the SDK's
Rust runtime installs in C, which Python's signal table never hears of. A test
that restored the table's answer wrote ``SIG_DFL`` over it, and the session's
last teardown then waited forever for a dev server nothing would reap.
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import shutil
import signal
import sys
from collections.abc import Iterator

import pytest
from _helpers.sigchld import kernel_handler, sigchld_preserved
from alkera_core.process import reclaim_children

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Windows has no SIGCHLD")

SIGCHLD = getattr(signal, "SIGCHLD", 0)


@pytest.fixture
def handler_set_in_c() -> Iterator[int]:
    """``SIGCHLD`` handled by a C function the table has never heard of, the
    way the Rust runtime leaves it. ``getpid`` stands in: async-signal-safe and
    indifferent to its argument. Yields that handler's address."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.signal.restype = ctypes.c_void_p
    libc.signal.argtypes = (ctypes.c_int, ctypes.c_void_p)
    with sigchld_preserved():
        handler = ctypes.cast(libc.getpid, ctypes.c_void_p).value
        assert handler is not None
        libc.signal(int(SIGCHLD), handler)
        assert kernel_handler(SIGCHLD) == handler
        assert signal.getsignal(SIGCHLD) is signal.SIG_DFL, "the table cannot see it"
        yield handler


def test_restoring_the_tables_answer_drops_a_handler_set_in_c(handler_set_in_c: int) -> None:
    """The premise: Python's own save-and-restore is not a restore here."""
    signal.signal(SIGCHLD, signal.getsignal(SIGCHLD))
    assert kernel_handler(SIGCHLD) != handler_set_in_c


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda: signal.signal(SIGCHLD, signal.SIG_IGN), id="ignored"),
        pytest.param(
            lambda: (signal.signal(SIGCHLD, signal.SIG_IGN), reclaim_children()),
            id="ignored-then-reclaimed",
        ),
        pytest.param(lambda: signal.signal(SIGCHLD, lambda *_: None), id="python-handler"),
    ],
)
def test_a_handler_set_in_c_survives_whatever_the_block_does(
    handler_set_in_c: int, change: object
) -> None:
    with sigchld_preserved():
        change()  # type: ignore[operator]
        assert kernel_handler(SIGCHLD) != handler_set_in_c
    assert kernel_handler(SIGCHLD) == handler_set_in_c
    assert signal.getsignal(SIGCHLD) is signal.SIG_DFL


@pytest.mark.temporal
async def test_a_dev_server_started_before_the_change_still_shuts_down() -> None:
    """The failure itself: a dev server whose exit tokio must hear of, a test
    that ignores and restores ``SIGCHLD`` in between, then the shutdown the
    session ends with. Bounded so the hang it guards against names itself."""
    from temporalio.testing import WorkflowEnvironment

    explicit = os.environ.get("ALKERA_TEMPORAL_BIN")
    env = await WorkflowEnvironment.start_local(
        dev_server_existing_path=explicit or shutil.which("temporal"),
        dev_server_log_level="warn",
    )
    try:
        with sigchld_preserved():
            signal.signal(SIGCHLD, signal.SIG_IGN)
            reclaim_children()
    finally:
        await asyncio.wait_for(env.shutdown(), timeout=30)
