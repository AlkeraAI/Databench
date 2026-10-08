"""The sandboxed chats share one memory ceiling below the box's, so a global
OOM kills a chat and never the daemon.

Driven against a stand-in ``memory.max`` under ``tmp_path``: the file the
kernel reads is the contract, so the test reads it back.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from alkera_cli.cloud.chat_slice import (
    MIN_CHAT_SLICE_BYTES,
    bound_chat_slice,
    chat_slice_memory_bytes,
)
from alkera_cli.cloud.service import memory_limit_from_env

MIB = 1024 * 1024
GIB = 1024 * MIB
RESERVE = 1024 * MIB


def _slice(tmp_path: Path, content: str = "max\n") -> Path:
    path = tmp_path / "alkera.slice" / "memory.max"
    path.parent.mkdir()
    path.write_text(content)
    return path


@pytest.mark.parametrize(
    ("limit", "expected"),
    [
        pytest.param(8 * GIB, 7 * GIB, id="8g-box-keeps-1g-for-the-daemon"),
        pytest.param(64 * GIB, 63 * GIB, id="dedicated-box"),
        pytest.param(RESERVE + MIN_CHAT_SLICE_BYTES, MIN_CHAT_SLICE_BYTES, id="at-the-floor"),
        pytest.param(RESERVE + MIN_CHAT_SLICE_BYTES - 1, None, id="below-the-floor"),
        pytest.param(RESERVE // 2, None, id="smaller-than-the-reserve"),
        pytest.param(None, None, id="memory-unknown"),
    ],
)
def test_the_share_is_the_box_less_the_daemons_reserve(
    limit: int | None, expected: int | None
) -> None:
    assert chat_slice_memory_bytes(limit, reserve_bytes=RESERVE) == expected


def test_the_ceiling_is_written_onto_the_slice(tmp_path: Path) -> None:
    path = _slice(tmp_path)
    assert bound_chat_slice(8 * GIB, reserve_bytes=RESERVE, path=path) == 7 * GIB
    assert path.read_text() == f"{7 * GIB}\n"


def test_a_re_run_rewrites_the_same_figure(tmp_path: Path) -> None:
    path = _slice(tmp_path, f"{3 * GIB}\n")
    bound_chat_slice(8 * GIB, reserve_bytes=RESERVE, path=path)
    bound_chat_slice(8 * GIB, reserve_bytes=RESERVE, path=path)
    assert path.read_text() == f"{7 * GIB}\n"


def test_a_box_without_the_slice_is_left_alone(tmp_path: Path) -> None:
    path = tmp_path / "alkera.slice" / "memory.max"
    assert bound_chat_slice(8 * GIB, reserve_bytes=RESERVE, path=path) is None
    assert not path.parent.exists(), "no slice is created where the sandbox has none"


def test_an_unknown_or_tiny_memory_leaves_the_slice_unbounded(tmp_path: Path) -> None:
    path = _slice(tmp_path)
    assert bound_chat_slice(None, reserve_bytes=RESERVE, path=path) is None
    assert bound_chat_slice(RESERVE, reserve_bytes=RESERVE, path=path) is None
    assert path.read_text() == "max\n"


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root writes a read-only file"
)
def test_a_cgroupfs_it_may_not_write_is_a_warning_not_a_crash(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _slice(tmp_path)
    path.chmod(0o444)
    try:
        assert bound_chat_slice(8 * GIB, reserve_bytes=RESERVE, path=path) is None
    finally:
        path.chmod(0o644)
    assert path.read_text() == "max\n"
    assert any("was not set" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    ("env", "host", "expected"),
    [
        pytest.param({"ALKERA_CLOUD_MEMORY_LIMIT_MB": "4096"}, 16 * GIB, 4 * GIB, id="stated-wins"),
        pytest.param({}, 16 * GIB, 16 * GIB, id="host-when-unstated"),
        pytest.param({"ALKERA_CLOUD_MEMORY_LIMIT_MB": "junk"}, 8 * GIB, 8 * GIB, id="junk-ignored"),
    ],
)
def test_the_box_memory_is_read_as_the_mirror_cap_reads_it(
    env: dict[str, str], host: int, expected: int
) -> None:
    assert memory_limit_from_env(env, limit_bytes=host) == expected
