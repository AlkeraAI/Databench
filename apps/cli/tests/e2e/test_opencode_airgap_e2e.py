"""End-to-end AIRGAP proofs for the spawned opencode harness.

Two real-bun-driven guarantees (marked ``opencode_e2e`` → CI's required
``e2e`` job + ``make e2e``):

1. ``test_bundled_ripgrep_used_and_no_download`` — opencode resolves OUR
   bundled ``rg`` (via PATH injection) and never downloads one from github.com.

2. ``test_full_turn_makes_no_external_network_egress`` — the headline airgap
   proof: across a REAL model turn (LLM round-trip + a ripgrep tool call), the
   spawned opencode reaches NOTHING but the gateway. We verify this empirically
   instead of by reasoning about flags: every non-loopback connection bun makes
   is forced through a deny-all recording proxy (bun honors
   ``HTTP_PROXY``/``HTTPS_PROXY``), while ``NO_PROXY`` lets the gateway (the mock
   on 127.0.0.1) through. After the turn the proxy must have recorded NOTHING —
   proving every phone-home gate (models.dev, opncd.ai share, autoupdate, LSP
   download, ripgrep download, and the runtime npm installer behind
   ALKERA_DISABLE_NPM_INSTALL) stayed shut while the turn still completed.

Both skip if no real ``rg`` is available to wrap (the deterministic
resolver/adapter unit tests carry the guarantee everywhere else).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import stat
import sys
from pathlib import Path

import pytest
from _helpers.opencode_runner import opencode_e2e_adapter
from _mocks.mock_openai_server import text_chunks
from alkera_cli.harness.adapter import PromptInput

pytestmark = pytest.mark.opencode_e2e

_MODEL = {"provider_id": "mock", "model_id": "mock-model"}

# The spawned opencode must reach NOTHING but the gateway (loopback, which
# bypasses the proxy via NO_PROXY and so never appears here). Every phone-home is
# now gated by a harness flag: models.dev / opncd.ai share / github autoupdate /
# LSP download / ripgrep download / and the runtime npm installer
# (ALKERA_DISABLE_NPM_INSTALL — @opencode-ai/plugin config-dep, external
# plugins, non-bundled providers, edit/write formatters). Any host recorded by
# the deny-all proxy is therefore a regression.
_KNOWN_OPEN_HOSTS: set[str] = set()


def _connect_target(request_line: str) -> str:
    """Pull the `host:port` out of a recorded proxy request line — either
    `CONNECT host:443 HTTP/1.1` (HTTPS) or `GET http://host/p HTTP/1.1` (HTTP)."""
    parts = request_line.split()
    if len(parts) >= 2 and parts[0].upper() == "CONNECT":
        return parts[1]
    if len(parts) >= 2 and "://" in parts[1]:
        rest = parts[1].split("://", 1)[1]
        return rest.split("/", 1)[0]
    return request_line


def _rg_filename() -> str:
    """opencode resolves `which("rg.exe")` on Windows, `which("rg")` elsewhere —
    the staged/wrapper file must match (the binary build bundles a real
    `rg.exe`)."""
    return "rg.exe" if sys.platform == "win32" else "rg"


def _find_real_rg() -> Path | None:
    """A real ripgrep to wrap: ALKERA_RIPGREP_BIN → staged → system PATH."""
    env = os.environ.get("ALKERA_RIPGREP_BIN", "").strip()
    if env and Path(env).is_file() and os.access(env, os.X_OK):
        return Path(env)
    staged = (
        Path(__file__).resolve().parents[4] / "apps" / "cli" / "dist" / "opencode" / _rg_filename()
    )
    if staged.is_file() and os.access(staged, os.X_OK):
        return staged
    found = shutil.which("rg")
    return Path(found) if found else None


def _make_wrapper_rg(dir_: Path, real_rg: Path, sentinel: Path) -> Path:
    """An `rg` the adapter will resolve FIRST on PATH.

    POSIX: a shell shim that records each invocation in `sentinel` then execs the
    real rg — proving OUR rg ran. Windows: opencode does `which("rg.exe")` and
    spawns the result as a real PE, so a recording shim would have to be a
    compiled executable; instead we drop a real `rg.exe` here and rely on
    PATH-ordering (`which` returns the first match — our injected dir) plus the
    no-download assertion to prove our rg was the one used (`sentinel` stays
    unwritten there — see `_assert_our_rg_ran`)."""
    dir_.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        wrapper = dir_ / "rg.exe"
        shutil.copy2(real_rg, wrapper)
        return wrapper
    wrapper = dir_ / "rg"
    wrapper.write_text(f"#!/bin/sh\nprintf 'x' >> '{sentinel}'\nexec '{real_rg}' \"$@\"\n")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return wrapper


def _assert_our_rg_ran(sentinel: Path) -> None:
    """POSIX: the recording shim stamped the sentinel on exec. Windows: there's
    no shell shim — `which("rg.exe")` returns the first PATH match (our injected
    dir), so a successful search (asserted by the caller) already means OUR rg
    ran; the no-download assertion rules out a fetched one."""
    if sys.platform == "win32":
        return
    assert sentinel.is_file() and sentinel.read_text(), (
        "the bundled rg wrapper was never invoked — opencode used a different rg"
    )


class _DenyAllProxy:
    """A loopback HTTP/HTTPS proxy that records the target of every connection
    and refuses it. Combined with ``NO_PROXY=127.0.0.1,...`` (so loopback — the
    gateway — bypasses it), anything that lands here is by definition an
    EXTERNAL egress attempt. ``seen`` being empty after a turn == airgapped."""

    def __init__(self) -> None:
        self.seen: list[str] = []
        self._server: asyncio.AbstractServer | None = None
        self.port = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if line:
                # First request line is `CONNECT host:443 HTTP/1.1` (HTTPS) or
                # `GET http://host/path HTTP/1.1` (proxied HTTP) — record the target.
                self.seen.append(line.decode("latin-1", "replace").strip())
            writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
        except (TimeoutError, OSError):
            pass
        finally:
            with contextlib.suppress(OSError):
                writer.close()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()


async def _await_gateway_request(
    server: object, marker: str, *, budget_seconds: float = 60.0
) -> None:
    """Poll the mock until a model request carrying ``marker`` lands — proof the
    turn actually reached the gateway (over loopback), not just that it didn't error."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget_seconds
    while loop.time() < deadline:
        requests = getattr(server, "requests", [])
        if any(marker in json.dumps(r, default=str) for r in requests):
            return
        await asyncio.sleep(0.1)
    raise AssertionError(
        f"no model request containing {marker!r} reached the gateway in {budget_seconds}s"
    )


async def test_bundled_ripgrep_used_and_no_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_rg = _find_real_rg()
    if real_rg is None:
        pytest.skip("no real `rg` available to wrap (system/staged/env all absent)")

    sentinel = tmp_path / "rg-was-invoked"
    wrapper = _make_wrapper_rg(tmp_path / "bundled-bin", real_rg, sentinel)

    # A file in the search root (the project dir = tmp_path) for rg to match.
    needle = "ZZ_RIPGREP_BUNDLE_NEEDLE_ZZ"
    (tmp_path / "haystack.txt").write_text(f"first line\n{needle} here\nlast line\n")

    async with opencode_e2e_adapter(
        tmp_path,
        monkeypatch,
        mock_script={"unused": text_chunks("noop")},
        ripgrep_path=wrapper,
    ) as (adapter, _server):
        # Sanity: the adapter wired our rg into the spawn env.
        env = adapter._build_env("pw").env
        assert env["PATH"].split(os.pathsep)[0] == str(wrapper.parent)
        assert env["ALKERA_DISABLE_RIPGREP_DOWNLOAD"] == "true"

        # Drive ripgrep through opencode's HTTP surface — no LLM turn needed.
        client = adapter._state.http_client
        assert client is not None
        resp = await client.get("/find", params={"pattern": needle})
        resp.raise_for_status()
        matches = resp.json()

    # 1. The search actually worked → our wrapper exec'd a functioning rg.
    assert isinstance(matches, list) and len(matches) >= 1, matches
    assert any(needle in m["lines"]["text"] for m in matches), matches

    # 2. Our rg ran (PATH injection beat any system rg).
    _assert_our_rg_ran(sentinel)

    # 3. The gate held: opencode never downloaded rg into the sandbox.
    downloaded = adapter._harness_dir / "agent" / "bin" / _rg_filename()
    assert not downloaded.exists(), (
        f"opencode downloaded rg to {downloaded} despite the disable gate"
    )


async def test_full_turn_makes_no_external_network_egress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real turn (model round-trip + ripgrep tool) reaches NOTHING but the
    gateway. Every non-loopback connection is funnelled to a deny-all proxy;
    after the turn it must be empty."""
    real_rg = _find_real_rg()
    if real_rg is None:
        pytest.skip("no real `rg` available to wrap (system/staged/env all absent)")

    proxy = _DenyAllProxy()
    await proxy.start()
    proxy_url = f"http://127.0.0.1:{proxy.port}"
    # Force every non-loopback connection through the deny-all proxy; let the
    # gateway (mock on 127.0.0.1) bypass it. Both cases bun + httpx honor.
    no_proxy = "127.0.0.1,localhost,::1,0.0.0.0"
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(key, proxy_url)
    for key in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(key, no_proxy)

    sentinel = tmp_path / "rg-was-invoked"
    wrapper = _make_wrapper_rg(tmp_path / "bundled-bin", real_rg, sentinel)
    marker = "ZZ_AIRGAP_TURN_MARKER_ZZ"
    (tmp_path / "haystack.txt").write_text(f"{marker} lives here\n")

    try:
        async with opencode_e2e_adapter(
            tmp_path,
            monkeypatch,
            mock_script={"*": text_chunks("airgapped-ok")},
            ripgrep_path=wrapper,
        ) as (adapter, server):
            # A real model turn — proves the gateway path works while everything
            # else is firewalled.
            await adapter.send_prompt(PromptInput(text=f"{marker} say hi", model=_MODEL))
            await _await_gateway_request(server, marker)

            # Exercise the ripgrep tool path too (historically a github download).
            client = adapter._state.http_client
            assert client is not None
            resp = await client.get("/find", params={"pattern": marker})
            resp.raise_for_status()
            assert resp.json(), "ripgrep search returned nothing — rg path is broken"
    finally:
        await proxy.stop()

    # POSITIVE assertion — the gateway WAS reached. The mock listens only on
    # 127.0.0.1, so a recorded request carrying our marker is, by construction, a
    # real model round-trip over loopback. This is what makes the empty proxy log
    # below mean "gateway-only" rather than "nothing happened" (a turn that never
    # ran would have zero egress AND zero gateway calls — this catches that).
    gateway_calls = [r for r in server.requests if marker in json.dumps(r, default=str)]
    assert gateway_calls, "the gateway received no model request — the turn never ran"

    # NEGATIVE assertion — the gateway (loopback) bypassed the proxy entirely, so
    # the deny-all proxy must have recorded ZERO external egress. Any host here —
    # github, models.dev, opncd.ai, registry.npmjs.org, an LSP CDN — means a
    # phone-home gate regressed.
    hosts = {_connect_target(line) for line in proxy.seen}
    unexpected = hosts - _KNOWN_OPEN_HOSTS
    assert not unexpected, (
        f"opencode made external network calls (no host is allowed besides the "
        f"gateway): {sorted(unexpected)} (full egress log: {proxy.seen})"
    )
    # And our bundled rg ran rather than a download being attempted.
    _assert_our_rg_ran(sentinel)
