"""Spawn-failure diagnostics for the opencode adapter.

A harness that dies before its listen banner usually says why only on
stderr; the field failure that motivated this ("harness exited before
reporting its listen URL" with an empty stdout section, from the compiled
Windows binary) was undebuggable because the error carried neither stderr
nor the exit code nor which binary was spawned. These pin that the error
now names all three.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from alkera_cli.harness.adapter import (
    HarnessStartError,
    HarnessUnavailableError,
    SessionConfig,
)
from alkera_cli.harness.adapters.opencode_http import OpencodeHttpAdapter
from alkera_cli.harness.event_bus import EventBus
from alkera_cli.harness.opencode_binary import ResolvedOpencodeBinary


def _make_adapter(tmp_path: Path) -> OpencodeHttpAdapter:
    config = SessionConfig(
        session_id="diag-sid",
        project_dir=tmp_path,
        chat_dir=tmp_path / "chat",
    )
    binary = ResolvedOpencodeBinary(
        path=Path("/fake/alkera-agent"), prefix_args=(), source="bundled"
    )
    return OpencodeHttpAdapter(config, binary=binary, event_bus=EventBus())


async def _spawn(code: str) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        code,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


@pytest.mark.asyncio
async def test_exit_before_banner_reports_exit_code_stderr_and_binary(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    proc = await _spawn("import sys; print('dying horribly', file=sys.stderr); sys.exit(7)")

    with pytest.raises(HarnessStartError) as exc:
        await adapter._await_listen_url(proc)

    msg = str(exc.value)
    assert "exit code 7" in msg
    assert "dying horribly" in msg  # stderr made it into the error
    assert "alkera-agent" in msg and "source=bundled" in msg  # which binary was spawned


@pytest.mark.asyncio
async def test_exit_before_banner_still_includes_stdout(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    proc = await _spawn("print('partial output'); raise SystemExit(1)")

    with pytest.raises(HarnessStartError) as exc:
        await adapter._await_listen_url(proc)

    msg = str(exc.value)
    assert "partial output" in msg
    assert "exit code 1" in msg


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "banner",
    [
        pytest.param("opencode server listening on", id="upstream-banner"),
        pytest.param("server listening on", id="bare-banner"),
    ],
)
async def test_banner_still_resolves_url(tmp_path: Path, banner: str) -> None:
    """The banner line yields the URL even with stderr chatter, and the process
    keeps running (drained, not killed). opencode prints upstream's banner."""
    adapter = _make_adapter(tmp_path)
    proc = await _spawn(
        "import sys, time; print('noise', file=sys.stderr); "
        f"print('{banner} http://127.0.0.1:4567', flush=True); time.sleep(5)"
    )
    try:
        url = await adapter._await_listen_url(proc)
        assert url == "http://127.0.0.1:4567"
        assert proc.returncode is None  # still running — banner doesn't kill it
    finally:
        proc.kill()
        await proc.wait()


@pytest.mark.asyncio
async def test_listen_file_resolves_url_without_any_stdout(tmp_path: Path) -> None:
    """The buffering-immune path: a process that writes the listen-url FILE but
    prints NOTHING to stdout (mimicking a bun standalone whose stdout banner is
    stuck in a pipe buffer on Windows) still resolves — proving readiness no
    longer depends on stdout reaching us."""
    adapter = _make_adapter(tmp_path)
    adapter._listen_file.parent.mkdir(parents=True, exist_ok=True)
    listen_file = adapter._listen_file
    # Process writes the file, prints nothing, then idles like a real server.
    code = f"import time; open(r'{listen_file}', 'w').write('http://127.0.0.1:9876'); time.sleep(5)"
    proc = await _spawn(code)
    try:
        url = await adapter._await_listen_url(proc)
        assert url == "http://127.0.0.1:9876"
        assert proc.returncode is None
    finally:
        proc.kill()
        await proc.wait()


@pytest.mark.asyncio
async def test_live_but_unbound_child_rides_the_full_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child that stays ALIVE without ever binding hits the deadline branch —
    with the 'didn't report a listen URL within' message, not the exit-code one.
    (Deadline shrunk via monkeypatch so the test doesn't wait minutes.)"""
    from alkera_cli.harness.adapters import opencode_http

    monkeypatch.setattr(opencode_http, "LISTEN_TIMEOUT_S", 0.3)
    adapter = _make_adapter(tmp_path)
    proc = await _spawn("import time; time.sleep(60)")
    try:
        with pytest.raises(HarnessStartError) as exc:
            await adapter._await_listen_url(proc)
        msg = str(exc.value)
        assert "didn't report a listen URL within 0.3s" in msg
        assert "alkera-agent" in msg and "source=bundled" in msg
    finally:
        proc.kill()
        await proc.wait()


def test_listen_deadline_covers_a_first_run_migration() -> None:
    """opencode's first launch runs a one-time SQLite migration ('may take a few
    minutes' on stderr) BEFORE binding; a 15s deadline killed exactly that on a
    fresh Windows host while the harness was alive and healthy. The deadline must
    stay generous enough for that first run — a dead child never waits it out
    (returncode fails fast), so only a live-but-unbound harness rides this."""
    from alkera_cli.harness.adapters.opencode_http import LISTEN_TIMEOUT_S

    assert LISTEN_TIMEOUT_S >= 300.0


@pytest.mark.asyncio
async def test_listen_file_beats_a_stale_value(tmp_path: Path) -> None:
    """A leftover non-URL listen-file is ignored (start() clears it, but be
    defensive): only an http:// value resolves."""
    adapter = _make_adapter(tmp_path)
    adapter._listen_file.parent.mkdir(parents=True, exist_ok=True)
    adapter._listen_file.write_text("garbage-not-a-url")
    proc = await _spawn("import sys; print('x', file=sys.stderr); sys.exit(3)")
    try:
        with pytest.raises(HarnessStartError) as exc:
            await adapter._await_listen_url(proc)
        assert "exit code 3" in str(exc.value)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


# --- start(): bounded retries around the whole spawn → bind → ready attempt ---


@pytest.mark.asyncio
async def test_start_retries_transient_failures_then_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two transient start failures are absorbed (with a cleanup between
    attempts, so retries never stack children); the third attempt wins."""
    from alkera_cli.harness.adapters import opencode_http

    monkeypatch.setattr(opencode_http, "_START_RETRY_DELAYS", (0.0, 0.0))
    adapter = _make_adapter(tmp_path)
    attempts = {"n": 0}
    cleanups = {"n": 0}

    async def flaky() -> None:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise HarnessStartError("transient spawn weather")
        adapter._state.started = True

    async def record_cleanup() -> None:
        cleanups["n"] += 1

    monkeypatch.setattr(adapter, "_start_once", flaky)
    monkeypatch.setattr(adapter, "_cleanup_failed_start", record_cleanup)

    await adapter.start()
    assert attempts["n"] == 3
    assert cleanups["n"] == 2, "every failed attempt cleans up before the retry"
    assert adapter._state.started is True


@pytest.mark.asyncio
async def test_start_raises_the_last_error_when_every_attempt_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from alkera_cli.harness.adapters import opencode_http

    monkeypatch.setattr(opencode_http, "_START_RETRY_DELAYS", (0.0, 0.0))
    adapter = _make_adapter(tmp_path)
    attempts = {"n": 0}

    async def always_fails() -> None:
        attempts["n"] += 1
        raise HarnessStartError(f"attempt {attempts['n']} died")

    async def noop_cleanup() -> None:
        return None

    monkeypatch.setattr(adapter, "_start_once", always_fails)
    monkeypatch.setattr(adapter, "_cleanup_failed_start", noop_cleanup)

    with pytest.raises(HarnessStartError) as exc:
        await adapter.start()
    assert attempts["n"] == 3, "three tries total, then give up"
    assert "attempt 3 died" in str(exc.value), "the LAST failure is what surfaces"


@pytest.mark.asyncio
async def test_start_does_not_retry_a_non_start_error_but_still_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing pinned session (Unavailable) describes state a relaunch can't
    heal — no retry — but the half-started child still gets torn down."""
    adapter = _make_adapter(tmp_path)
    attempts = {"n": 0}
    cleanups = {"n": 0}

    async def unavailable() -> None:
        attempts["n"] += 1
        raise HarnessUnavailableError("pinned opencode session is gone")

    async def record_cleanup() -> None:
        cleanups["n"] += 1

    monkeypatch.setattr(adapter, "_start_once", unavailable)
    monkeypatch.setattr(adapter, "_cleanup_failed_start", record_cleanup)

    with pytest.raises(HarnessUnavailableError):
        await adapter.start()
    assert attempts["n"] == 1
    assert cleanups["n"] == 1


@pytest.mark.asyncio
async def test_cleanup_failed_start_reaps_the_child_and_closes_the_client(
    tmp_path: Path,
) -> None:
    """The between-attempts cleanup kills a still-running child and closes the
    HTTP client, so a retry can never stack a second agent on the first."""
    import httpx

    adapter = _make_adapter(tmp_path)
    proc = await _spawn("import time; time.sleep(60)")
    adapter._state.proc = proc
    adapter._state.http_client = httpx.AsyncClient(base_url="http://127.0.0.1:9")

    await adapter._cleanup_failed_start()

    assert proc.returncode is not None, "the half-started child was reaped"
    assert adapter._state.proc is None
    assert adapter._state.http_client is None
