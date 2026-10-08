"""The agent config root under `ALKERA_HOME/harness/<session>` has a bounded life.

Moving opencode's config/cache/state roots out of `<project>/.alkera/chats/<sid>/
.runtime` closed a real hole (the agent's own `write` tool could drop an
`agent/*.md` whose `permission:` frontmatter disabled our permission gate), but it
also moved them somewhere nothing ever deletes: the user's home. These pin that a
root dies with the harness process that used it —

* on the normal path, `stop()` removes it;
* on a start that never succeeds, the failing `start()` removes it;
* on a SIGKILLed `alkera` that reached neither, the next spawn's sweep removes it —
  and ONLY when the owning process is provably gone, on the machine that owns it.

The chat's own `.runtime` data dir (the agent DB a resume attaches to) must survive
all of it.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from alkera_cli.harness.adapter import (
    HarnessStartError,
    HarnessStartRefusedError,
    HarnessUnavailableError,
    SessionConfig,
)
from alkera_cli.harness.adapters import opencode_http
from alkera_cli.harness.adapters.opencode_http import (
    OpencodeHttpAdapter,
    sweep_stale_agent_config_roots,
)
from alkera_cli.harness.agent_root import agent_config_root, agent_config_roots_dir
from alkera_cli.harness.event_bus import EventBus
from alkera_cli.harness.opencode_binary import ResolvedOpencodeBinary
from alkera_cli.host import paths

_OWNER = ".owner"


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """Never let these tests create — or delete — anything in the developer's real
    `~/.alkera`."""
    home = tmp_path / "alkera-home"
    monkeypatch.setattr(paths, "ALKERA_HOME", home)
    yield home


def _adapter(tmp_path: Path, *, session_id: str = "sid") -> OpencodeHttpAdapter:
    config = SessionConfig(
        session_id=session_id,
        project_dir=tmp_path,
        chat_dir=tmp_path / "chat",
    )
    binary = ResolvedOpencodeBinary(
        path=Path("/usr/bin/true"), prefix_args=(), source="staged", ripgrep_path=None
    )
    return OpencodeHttpAdapter(config, binary=binary, event_bus=EventBus())


def _dead_pid() -> int:
    """A PID that is certainly not running right now."""
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    return proc.pid


# ---------------------------------------------------------------------------
# Normal teardown
# ---------------------------------------------------------------------------


async def test_stop_removes_the_config_root_but_keeps_the_chats_runtime_data(
    tmp_path: Path,
) -> None:
    adapter = _adapter(tmp_path)
    adapter._harness_dir.mkdir(parents=True, exist_ok=True)
    adapter._ensure_agent_config_root()
    root = adapter._agent_config_root
    # Something opencode would have written into its cache tree.
    (root / "agent" / "bin").mkdir(parents=True)
    (root / "agent" / "bin" / "rg").write_bytes(b"binary")
    # …and the chat's own data, which must NOT be collateral.
    (adapter._harness_dir / "agent").mkdir(parents=True, exist_ok=True)
    (adapter._harness_dir / "agent" / "agent.db").write_bytes(b"sqlite")

    await adapter.stop()

    assert not root.exists(), "the agent config root outlived the harness process"
    assert (adapter._harness_dir / "agent" / "agent.db").is_file()


async def test_stop_only_removes_its_own_chats_root(tmp_path: Path) -> None:
    """A second chat open in the same process keeps its root when the first closes."""
    other = _adapter(tmp_path, session_id="other-sid")
    other._ensure_agent_config_root()
    adapter = _adapter(tmp_path)
    adapter._ensure_agent_config_root()

    await adapter.stop()

    assert not adapter._agent_config_root.exists()
    assert other._agent_config_root.is_dir()


async def test_stop_without_a_start_is_still_clean(tmp_path: Path) -> None:
    """`stop()` runs on paths where the root was never created — no raise."""
    adapter = _adapter(tmp_path)
    assert not adapter._agent_config_root.exists()
    await adapter.stop()


# ---------------------------------------------------------------------------
# Start that never succeeds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(HarnessStartError("spawn failed"), id="start-error-retried"),
        pytest.param(HarnessUnavailableError("pinned session gone"), id="unavailable-not-retried"),
    ],
)
async def test_a_start_that_never_succeeds_leaves_no_config_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    adapter = _adapter(tmp_path)
    monkeypatch.setattr(opencode_http, "_START_RETRY_DELAYS", ())

    async def failing_start() -> None:
        adapter._ensure_agent_config_root()  # as the real one does, first thing
        raise error

    monkeypatch.setattr(adapter, "_start_once", failing_start)

    with pytest.raises(type(error)):
        await adapter.start()

    assert not adapter._agent_config_root.exists()


async def test_a_start_that_succeeds_on_retry_keeps_its_config_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The asymmetric case: the between-attempts cleanup must not delete the root
    the NEXT attempt is about to use."""
    adapter = _adapter(tmp_path)
    monkeypatch.setattr(opencode_http, "_START_RETRY_DELAYS", (0.0,))
    attempts: list[int] = []

    async def flaky_start() -> None:
        adapter._ensure_agent_config_root()
        attempts.append(1)
        if len(attempts) == 1:
            raise HarnessStartError("transient")

    monkeypatch.setattr(adapter, "_start_once", flaky_start)

    await adapter.start()

    assert len(attempts) == 2
    assert adapter._agent_config_root.is_dir()


async def test_a_refused_start_is_not_respawned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agent that bound, answered, and THEN refused the session call is not
    relaunched.

    The retry above exists for machine weather on the way up, which a second
    spawn wins. A refusal is the opposite: the process is already serving, so
    two more spawns meet the same state and the caller is handed the third
    attempt's copy of the error instead of the first one's — and on a loaded
    machine those spawns are what the wait is made of. The root still goes: no
    agent will run from it."""
    adapter = _adapter(tmp_path)
    monkeypatch.setattr(opencode_http, "_START_RETRY_DELAYS", (0.0, 0.0))
    attempts: list[int] = []

    async def refusing_start() -> None:
        adapter._ensure_agent_config_root()
        attempts.append(1)
        raise HarnessStartRefusedError("agent /session list failed: 500")

    monkeypatch.setattr(adapter, "_start_once", refusing_start)

    with pytest.raises(HarnessStartRefusedError):
        await adapter.start()

    assert attempts == [1], f"the refusal was respawned {len(attempts) - 1} more time(s)"
    assert not adapter._agent_config_root.exists()


# ---------------------------------------------------------------------------
# Crash sweep
# ---------------------------------------------------------------------------


def _owner(pid: int, *, host: str | None = None) -> dict[str, object]:
    """An `.owner` payload for a process that is NOT running (create_time 1.0 can
    match nothing live), recorded — unless told otherwise — by THIS machine."""
    return {
        "pid": pid,
        "create_time": 1.0,
        "host": socket.gethostname() if host is None else host,
    }


def _root_with_owner(session_id: str, payload: object | None) -> Path:
    root = agent_config_root(session_id)
    root.mkdir(parents=True, exist_ok=True)
    (root / "agent").mkdir(exist_ok=True)
    if payload is not None:
        (root / _OWNER).write_text(json.dumps(payload), encoding="utf-8")
    return root


def test_sweep_removes_a_root_whose_owner_process_is_gone() -> None:
    root = _root_with_owner("dead-sid", _owner(_dead_pid()))

    assert sweep_stale_agent_config_roots() == 1
    assert not root.exists()


def test_sweep_removes_a_root_whose_owner_pid_was_recycled() -> None:
    """Alive PID, but not the process we recorded — the reason a bare PID is never
    enough to claim ownership."""
    root = _root_with_owner("recycled-sid", _owner(os.getpid()))

    assert sweep_stale_agent_config_roots() == 1
    assert not root.exists()


def test_sweep_leaves_another_machines_root_alone_and_still_reaps_this_ones() -> None:
    """`ALKERA_HOME` sits in the user's home, which in an enterprise deployment is
    routinely shared over NFS — so another machine's LIVE chat root lands right here,
    with a pid that means nothing locally. Deleting it would pull opencode's cache
    (`agent/bin/rg`, whose absence hard-kills glob/grep) and the user's
    global-instructions.md out from under a running session. The same payload written
    by THIS host is still reaped: the host check must not turn the sweep into a
    no-op."""
    dead_here = _dead_pid()
    foreign = _root_with_owner("nfs-sid", _owner(dead_here, host="some-other-machine"))
    local = _root_with_owner("local-sid", _owner(dead_here))

    assert sweep_stale_agent_config_roots() == 1
    assert foreign.is_dir(), "deleted a root this machine cannot judge"
    assert not local.exists()


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(None, id="no-breadcrumb"),
        pytest.param("not-json-at-all", id="unreadable-breadcrumb"),
        pytest.param(4242, id="legacy-bare-pid-no-create-time"),
        pytest.param({"pid": os.getpid(), "create_time": 1.0}, id="no-host-recorded"),
    ],
)
def test_sweep_leaves_a_root_it_cannot_prove_is_abandoned(payload: object | None) -> None:
    """Fail-safe: deleting a LIVE chat's cache/state root under it is worse than
    leaving scratch behind, so anything unidentifiable is left alone."""
    root = agent_config_root("unknown-sid")
    root.mkdir(parents=True, exist_ok=True)
    if payload is not None:
        (root / _OWNER).write_text(
            payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8"
        )

    assert sweep_stale_agent_config_roots() == 0
    assert root.is_dir()


def test_sweep_leaves_a_live_owners_root_alone(tmp_path: Path) -> None:
    """The interleaving that matters: one alkera sweeps while another chat — here,
    this very process — is holding a root open."""
    live = _adapter(tmp_path, session_id="live-sid")
    live._ensure_agent_config_root()
    stale = _root_with_owner("stale-sid", _owner(_dead_pid()))

    assert sweep_stale_agent_config_roots() == 1
    assert live._agent_config_root.is_dir()
    assert not stale.exists()


def test_sweep_never_touches_the_root_it_is_told_to_keep() -> None:
    """`keep` guards the caller's own root even if its breadcrumb looks abandoned."""
    root = _root_with_owner("keep-sid", _owner(_dead_pid()))

    assert sweep_stale_agent_config_roots(keep=root) == 0
    assert root.is_dir()


def test_sweep_is_a_noop_when_no_root_has_ever_been_created() -> None:
    assert not agent_config_roots_dir().exists()
    assert sweep_stale_agent_config_roots() == 0


async def test_a_spawn_sweeps_roots_abandoned_by_a_killed_alkera(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wiring: the sweep runs as part of a real spawn attempt, before the child is
    launched — so an `alkera` that was SIGKILLed before `stop()` is cleaned up by
    the next chat that opens, not by nothing."""
    stale = _root_with_owner("killed-sid", _owner(_dead_pid()))
    adapter = _adapter(tmp_path)
    monkeypatch.setattr(opencode_http, "_START_RETRY_DELAYS", ())

    async def no_binary(_env: dict[str, str]) -> None:
        raise OSError("no such binary")

    monkeypatch.setattr(adapter, "_spawn_opencode", no_binary)

    with pytest.raises(HarnessStartError):
        await adapter.start()

    assert not stale.exists()
    # …and this attempt's own root went with the failed start.
    assert not adapter._agent_config_root.exists()


def test_ensure_config_root_stamps_a_live_owner_breadcrumb(tmp_path: Path) -> None:
    """What makes the sweep safe: an in-use root always carries a breadcrumb that
    identifies THIS process on THIS machine, creation time included. It is read by
    other `alkera` processes, so its shape is a cross-process contract."""
    adapter = _adapter(tmp_path)
    adapter._ensure_agent_config_root()

    data = json.loads((adapter._agent_config_root / _OWNER).read_text(encoding="utf-8"))
    assert data["pid"] == os.getpid()
    assert isinstance(data["create_time"], float)
    assert data["host"] == socket.gethostname()


def test_a_root_stamped_here_becomes_sweepable_once_its_owner_dies(tmp_path: Path) -> None:
    """Writer/reader round trip: take the breadcrumb the adapter actually stamps and
    age it into the SIGKILLed-owner state — the sweep must still recognize it as
    ours. Pins the two halves of the host rule against drifting apart (a writer that
    stopped recording a host would silently make every root unsweepable)."""
    adapter = _adapter(tmp_path, session_id="stamped-sid")
    adapter._ensure_agent_config_root()
    root = adapter._agent_config_root
    stamped = json.loads((root / _OWNER).read_text(encoding="utf-8"))
    stamped["pid"], stamped["create_time"] = _dead_pid(), 1.0
    (root / _OWNER).write_text(json.dumps(stamped), encoding="utf-8")

    assert sweep_stale_agent_config_roots() == 1
    assert not root.exists()
