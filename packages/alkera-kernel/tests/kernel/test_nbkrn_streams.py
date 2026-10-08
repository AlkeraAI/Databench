"""stdout and stderr captured at the descriptor level, collapsed and bounded."""

from __future__ import annotations

import pytest
from nbkrn_harness import KernelFactory, step


def _streams():  # the pure parts, loaded without starting a kernel
    import _alkera_kernel.streams as streams

    return streams


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        pytest.param("import os\nos.write(1, b'raw fd\\n')\n", "raw fd\n", id="os-write"),
        pytest.param(
            "import subprocess\nsubprocess.run(['echo', 'from child'])\n",
            "from child\n",
            id="subprocess",
        ),
        pytest.param(
            "import os\npid = os.fork()\nif pid == 0:\n    print('from fork', flush=True)\n    os"
            "._exit(0)\nos.waitpid(pid, 0)\n",
            "from fork\n",
            id="fork-child",
        ),
        pytest.param(
            "print('a')\nimport os\nos.write(1, b'b\\n')\nprint('c')\n", "a\nb\nc\n", id="ordering"
        ),
    ],
)
async def test_nbkrn_fd_level_stdout_is_attributed_to_the_step(
    start_kernel: KernelFactory, code: str, expected: str
) -> None:
    ks = await start_kernel()
    result = await ks.run(step("s", code), step("t", "print('next cell')"))
    assert result.status == "ok", result.events
    assert result.stdout("s") == expected
    assert result.stdout("t") == "next cell\n"


async def test_nbkrn_c_level_stderr_is_captured(start_kernel: KernelFactory) -> None:
    """ctypes calls libc's write(2, ...) directly, as a C extension would."""
    ks = await start_kernel()
    code = (
        "import ctypes, ctypes.util\nlibc = ctypes.CDLL(ctypes.util.find_library('c'))\nlibc.writ"
        "e(2, b'from C\\n', 7)\n"
    )
    result = await ks.run(step("s", code))
    assert result.stdout("s", "stderr") == "from C\n"


async def test_nbkrn_progress_bar_collapses(start_kernel: KernelFactory) -> None:
    ks = await start_kernel()
    code = "import sys\nfor i in range(10000):\n    sys.stdout.write(f'\\r{i:5d}/10000')\nprint()\n"
    result = await ks.run(step("s", code))
    chunks = [p["text"] for p in result.of("cell.stream", "s")]
    text = "".join(chunks)
    assert len(text) < 2000, len(text)
    assert _render(text) == " 9999/10000\n"


async def test_nbkrn_tqdm_collapses(start_kernel: KernelFactory) -> None:
    pytest.importorskip("tqdm")
    ks = await start_kernel()
    code = (
        "from tqdm import tqdm\nfor _ in tqdm(range(10000), mininterval=0, miniters=1):\n    pass\n"
    )
    result = await ks.run(step("s", code))
    text = result.stdout("s", "stderr")
    assert len(text) < 20000, len(text)
    assert "10000/10000" in _render(text)


def _render(text: str) -> str:
    """JupyterLab's overwrite semantics, written independently here."""
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = ""
        for i, piece in enumerate(raw.split("\r")):
            line = piece + line[len(piece) :] if i else piece
        lines.append(line)
    return "\n".join(lines)


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("abc\rxy", id="partial-overwrite"),
        pytest.param("\r 1%\r 2%\r 3%", id="progress"),
        pytest.param("one\ntwo\rTWO\nthree\r", id="lines"),
        pytest.param("abcd\rxy\rz", id="cursor-inside"),
        pytest.param("plain\ntext\n", id="no-cr"),
        pytest.param("\x1b[Aup\rdown", id="ansi-untouched"),
    ],
)
@pytest.mark.parametrize("before", ["", "PREVIOUS-LONG-LINE", "x\n"])
@pytest.mark.parametrize("after", ["", "Q", "\nnext"])
def test_nbkrn_carriage_return_reduction_is_lossless(text: str, before: str, after: str) -> None:
    reduce = _streams().reduce_carriage_returns
    assert _render(before + reduce(text) + after) == _render(before + text + after)


def test_nbkrn_carriage_return_reduction_shrinks_a_bar() -> None:
    reduce = _streams().reduce_carriage_returns
    text = "".join(f"\r{i:4d}" for i in range(1000))
    assert reduce(text) == "\r 999"


async def test_nbkrn_large_output_keeps_head_and_tail(start_kernel: KernelFactory) -> None:
    ks = await start_kernel()
    code = (
        "import sys\nfor i in range(40000):\n    sys.stdout.write(f'line {i:06d} ' + 'x' * 60 + '"
        "\\n')\n"
    )
    result = await ks.run(step("s", code))
    text = result.stdout("s")
    streams = _streams()
    assert text.startswith("line 000000 ")
    assert text.rstrip("\n").endswith("line 039999 " + "x" * 60)
    assert "characters of output omitted" in text
    assert len(text) <= streams.LIVE_LIMIT + streams.TAIL_LIMIT + 200


async def test_nbkrn_thread_output_after_the_cell_ends_goes_to_that_cell(
    start_kernel: KernelFactory,
) -> None:
    ks = await start_kernel()
    code = (
        "import threading, time\ndef later():\n    time.sleep(0.5)\n    print('late hello')\nthre"
        "ading.Thread(target=later).start()\n"
    )
    first = await ks.run(step("bg", code))
    assert first.status == "ok"
    await ks.run(step("other", "import time\ntime.sleep(1.0)\nprint('other')"))
    late = await ks.wait_for(lambda: [p for m, p in ks.events if m == "thread.output"])
    assert late[0]["cell_id"] == "bg"
    assert late[0]["text"] == "late hello\n"
    others = "".join(
        p["text"] for m, p in ks.events if m == "cell.stream" and p["cell_id"] == "other"
    )
    assert others == "other\n"


async def test_nbkrn_replace_output_is_rate_limited_last_wins(start_kernel: KernelFactory) -> None:
    ks = await start_kernel()
    code = (
        "import sys, time\nhost = sys.modules['_alkera_runtime'].host\n"
        "for i in range(100):\n    host.replace(i)\n    time.sleep(0.005)\n"
    )
    result = await ks.run(step("s", code))
    replaces = [p for p in result.of("cell.output", "s") if p["mode"] == "replace"]
    # The policy itself is pinned with an injected clock in the throttle
    # tests; end to end, most replaces are dropped and the last one wins.
    assert len(replaces) < 50


async def test_nbkrn_kernel_stdio_streams_survive_print_flush(start_kernel: KernelFactory) -> None:
    ks = await start_kernel()
    result = await ks.run(
        step("s", "import sys\nprint('x', file=sys.stderr, flush=True)\nsys.stdout.isatty()")
    )
    assert result.stdout("s", "stderr") == "x\n"
    assert result.outputs("s")[0]["text/plain"] == "False"
