"""The startup pre-warm: it stages + probes the agent ONCE in the background so
the user's first chat doesn't pay the one-time costs (binary staging,
opencode's one-time database migration) inside its own open — and the
adapter's `wait_for_prewarm` gate can't race a second migrator against it.

The "agent" here is a tiny python script (real subprocess, real listen-file
contract); only the binary resolver is monkeypatched.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from alkera_cli.harness import prewarm
from alkera_cli.harness.opencode_binary import ResolvedOpencodeBinary
from alkera_cli.harness.orphan_sweep import process_create_time
from alkera_cli.host import paths
from alkera_core.process import process_alive


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    # One-shot module state must not leak between tests, and the probe's
    # breadcrumb/registry writes must land in a throwaway home.
    monkeypatch.setattr(paths, "ALKERA_HOME", tmp_path / "alkera-home")
    prewarm._reset_for_tests()
    yield
    prewarm._reset_for_tests()


# --- gating ------------------------------------------------------------------


def test_disabled_by_default_in_a_source_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALKERA_HARNESS_PREWARM", raising=False)
    assert prewarm.prewarm_enabled() is False  # this suite runs from source


def test_enabled_by_default_in_a_compiled_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALKERA_HARNESS_PREWARM", raising=False)
    monkeypatch.setattr(prewarm, "_running_compiled", lambda: True)
    assert prewarm.prewarm_enabled() is True


@pytest.mark.parametrize("token", ["1", "true", "YES", " on "])
def test_env_force_enables(monkeypatch: pytest.MonkeyPatch, token: str) -> None:
    monkeypatch.setenv("ALKERA_HARNESS_PREWARM", token)
    assert prewarm.prewarm_enabled() is True


@pytest.mark.parametrize("token", ["0", "false", "No", " off "])
def test_env_force_disables_even_when_compiled(monkeypatch: pytest.MonkeyPatch, token: str) -> None:
    monkeypatch.setenv("ALKERA_HARNESS_PREWARM", token)
    monkeypatch.setattr(prewarm, "_running_compiled", lambda: True)
    assert prewarm.prewarm_enabled() is False


def test_a_caller_that_asks_pre_warms_from_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """A workspace box runs from a source checkout on the demo stack, and the
    costs it would otherwise pay inside a reader's first question are the same
    ones a compiled daemon pays at boot."""
    monkeypatch.delenv("ALKERA_HARNESS_PREWARM", raising=False)
    assert prewarm.prewarm_enabled() is False
    assert prewarm.prewarm_enabled(force=True) is True


@pytest.mark.parametrize("token", ["0", "false", "No", " off "])
def test_the_field_escape_hatch_still_beats_a_caller_that_asks(
    monkeypatch: pytest.MonkeyPatch, token: str
) -> None:
    monkeypatch.setenv("ALKERA_HARNESS_PREWARM", token)
    assert prewarm.prewarm_enabled(force=True) is False


# --- the run + the adapter's wait ---------------------------------------------

#: Tighter than `same_process`'s 1.0s, which is biased against falsely killing.
#: Here a recycled pid must read as a stranger rather than stall the poll.
_PROBE_IDENTITY_SLACK_S = 0.05


def _read_probe_identity(path: str) -> tuple[int, float]:
    """Sync read of the pid + creation time the fake agent reported for itself
    (pathlib methods are linted out of async test bodies)."""
    reported = json.loads(Path(path).read_text(encoding="utf-8"))
    return int(reported["pid"]), float(reported["create_time"])


def _read_json(path: str) -> dict[str, str]:
    """Sync read of a JSON blob the fake agent dumped (see `_read_probe_identity`)."""
    loaded: dict[str, str] = json.loads(Path(path).read_text(encoding="utf-8"))
    return loaded


def _probe_alive(pid: int, create_time: float) -> bool:
    """Whether that exact process is up. A Windows process someone still holds a
    handle to keeps a readable creation time after exit, so liveness comes first."""
    if not process_alive(pid):
        return False
    current = process_create_time(pid)
    return current is not None and abs(current - create_time) < _PROBE_IDENTITY_SLACK_S


def _outlived_prewarm(pid: int, create_time: float, timeout_s: float = 10.0) -> bool:
    """The OS owns the reap (a job object kills its members once the last handle
    closes), so death can land after the pre-warm returns. Bound as in test_spawn.py."""
    deadline = time.monotonic() + timeout_s
    while _probe_alive(pid, create_time):
        if time.monotonic() >= deadline:
            return True
        time.sleep(0.05)
    return False


#: A fake agent's dump of what it was launched with: its environment, the
#: secrets file it was named, and that file's mode.
_DUMP = (
    "{'env': dict(os.environ), "
    "'secrets': json.load(open(os.environ['ALKERA_SECRETS_FILE'])), "
    "'secrets_mode': os.stat(os.environ['ALKERA_SECRETS_FILE']).st_mode & 0o777}"
)


def _fake_agent(monkeypatch: pytest.MonkeyPatch, script: str) -> None:
    """Route the resolver at a python 'agent' whose behavior is `script` (it
    receives the real `serve --hostname … --port 0` args and the
    ALKERA_LISTEN_FILE env, like the true binary)."""
    binary = ResolvedOpencodeBinary(
        path=Path(sys.executable), prefix_args=("-c", script), source="bundled"
    )
    monkeypatch.setattr(
        "alkera_cli.harness.opencode_binary.resolve_opencode_binary",
        lambda **_: binary,
    )


@pytest.mark.asyncio
async def test_prewarm_probes_to_ready_then_tears_the_agent_down(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    identity_file = (tmp_path / "agent-identity.json").as_posix()
    monkeypatch.setenv("ALKERA_HARNESS_PREWARM", "1")
    # The agent stamps its own creation time because reading it back here would
    # race the teardown. It then "migrates" for a beat, binds, and idles.
    _fake_agent(
        monkeypatch,
        "import json, os, time, psutil\n"
        "me = {'pid': os.getpid(), 'create_time': psutil.Process().create_time()}\n"
        f"open(r'{identity_file}', 'w').write(json.dumps(me))\n"
        "time.sleep(0.1)\n"
        "open(os.environ['ALKERA_LISTEN_FILE'], 'w').write('http://127.0.0.1:1')\n"
        "time.sleep(60)\n",
    )

    prewarm.start_prewarm_in_background()
    await prewarm.wait_for_prewarm(timeout_seconds=30)

    assert prewarm._settled is not None and prewarm._settled.is_set()
    agent_pid, agent_create_time = _read_probe_identity(identity_file)
    assert not _outlived_prewarm(agent_pid, agent_create_time), (
        "the probe agent must not outlive the pre-warm"
    )


@pytest.mark.asyncio
async def test_the_probe_leaves_no_entry_in_the_agent_registry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The sweep kills an agent whose spawning ``alkera`` is gone, and it
    decides that from ``<ALKERA_HOME>/agents/<pid>.json``. The probe registers
    itself there so a daemon that dies mid-pre-warm doesn't strand it — but the
    probe is dead by the time the pre-warm returns, and its tmp dir (which holds
    only the pid breadcrumb) goes with it. An entry left behind names a pid
    nothing owns any more, forever; the day the OS recycles that pid onto a live
    process started within ``same_process``'s tolerance, the next sweep confirms
    it as "ours" and terminates a stranger.
    """
    identity_file = (tmp_path / "agent-identity.json").as_posix()
    monkeypatch.setenv("ALKERA_HARNESS_PREWARM", "1")
    _fake_agent(
        monkeypatch,
        "import json, os, psutil\n"
        "me = {'pid': os.getpid(), 'create_time': psutil.Process().create_time()}\n"
        f"open(r'{identity_file}', 'w').write(json.dumps(me))\n"
        "open(os.environ['ALKERA_LISTEN_FILE'], 'w').write('http://127.0.0.1:1')\n"
        "import time; time.sleep(60)\n",
    )

    prewarm.start_prewarm_in_background()
    await prewarm.wait_for_prewarm(timeout_seconds=30)

    agent_pid, _create_time = _read_probe_identity(identity_file)
    assert not (paths.agents_dir() / f"{agent_pid}.json").exists(), (
        "the probe's registry entry outlived it — a later sweep still reads it"
    )


@pytest.mark.asyncio
async def test_wait_blocks_until_a_slow_probe_settles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chat opened mid-pre-warm waits for it (never two migrators at once):
    the wait returns only once the probe hits its (shrunk) deadline."""
    from alkera_cli.harness.adapters import opencode_http

    monkeypatch.setenv("ALKERA_HARNESS_PREWARM", "1")
    monkeypatch.setattr(opencode_http, "LISTEN_TIMEOUT_S", 0.5)
    # Never binds, never exits — rides the full (shrunk) deadline.
    _fake_agent(monkeypatch, "import time; time.sleep(60)")

    loop = asyncio.get_running_loop()
    prewarm.start_prewarm_in_background()
    t0 = loop.time()
    await prewarm.wait_for_prewarm(timeout_seconds=30)
    assert loop.time() - t0 >= 0.4, "the wait must ride the probe, not return early"
    assert prewarm._settled is not None and prewarm._settled.is_set()


@pytest.mark.asyncio
async def test_a_failing_resolver_still_settles_and_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALKERA_HARNESS_PREWARM", "1")

    def _boom(**_: object) -> ResolvedOpencodeBinary:
        raise RuntimeError("no binary anywhere")

    monkeypatch.setattr("alkera_cli.harness.opencode_binary.resolve_opencode_binary", _boom)

    prewarm.start_prewarm_in_background()
    await prewarm.wait_for_prewarm(timeout_seconds=10)
    assert prewarm._settled is not None and prewarm._settled.is_set()


@pytest.mark.asyncio
async def test_wait_is_a_noop_when_no_prewarm_ran(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default (gated-off) path: the adapter's await costs nothing."""
    monkeypatch.setenv("ALKERA_HARNESS_PREWARM", "0")
    prewarm.start_prewarm_in_background()  # gated off → no task, no latch
    assert prewarm._task is None
    await asyncio.wait_for(prewarm.wait_for_prewarm(), timeout=0.5)


@pytest.mark.asyncio
async def test_start_is_idempotent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ALKERA_HARNESS_PREWARM", "1")
    marker_dir = tmp_path / "spawns"
    marker_dir.mkdir()
    # Each spawn drops a uniquely-named marker; a double start would drop two.
    _fake_agent(
        monkeypatch,
        "import os, uuid\n"
        f"open(os.path.join(r'{marker_dir.as_posix()}', uuid.uuid4().hex), 'w').write('x')\n"
        "open(os.environ['ALKERA_LISTEN_FILE'], 'w').write('http://127.0.0.1:1')\n",
    )
    prewarm.start_prewarm_in_background()
    first_task = prewarm._task
    prewarm.start_prewarm_in_background()
    assert prewarm._task is first_task
    await prewarm.wait_for_prewarm(timeout_seconds=30)
    assert len(list(marker_dir.iterdir())) == 1


# --- the probe's spawn env is as locked down as a real chat spawn -------------


@pytest.mark.asyncio
async def test_probe_server_is_password_protected_and_sandboxed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The probe binds a REAL loopback HTTP server whose API can spawn a PTY and
    run shell commands, and the agent's authorization middleware short-circuits
    to a no-op when no server password is configured. So the probe's env must
    carry one — plus the same sandbox/lockdown the chat spawn applies. Without
    this, every daemon start opened an unauthenticated local RCE surface."""
    monkeypatch.setenv("ALKERA_HARNESS_PREWARM", "1")
    # A user-shell var that would inject external opencode config/auth.
    monkeypatch.setenv("OPENCODE_CONFIG", "/Users/someone/.config/opencode/config.json")
    env_file = (tmp_path / "probe-env.json").as_posix()
    _fake_agent(
        monkeypatch,
        "import json, os\n"
        f"open(r'{env_file}', 'w').write(json.dumps({_DUMP}))\n"
        "open(os.environ['ALKERA_LISTEN_FILE'], 'w').write('http://127.0.0.1:1')\n",
    )

    prewarm.start_prewarm_in_background()
    await prewarm.wait_for_prewarm(timeout_seconds=30)

    dumped = _read_json(env_file)
    spawned = dumped["env"]
    # The load-bearing one: a real, non-trivial secret, handed over by an
    # owner-only file and never by the environment.
    assert len(dumped["secrets"]["ALKERA_SERVER_PASSWORD"]) >= 32
    assert "ALKERA_SERVER_PASSWORD" not in spawned
    if os.name == "posix":
        assert dumped["secrets_mode"] == 0o600
    # Config isolation: the probe reads none of the user's opencode state.
    sandbox = spawned["XDG_CONFIG_HOME"]
    for var in ("XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "ALKERA_TEST_HOME"):
        assert spawned[var] == sandbox, var
    assert spawned["ALKERA_DISABLE_PROJECT_CONFIG"] == "true"
    assert "OPENCODE_CONFIG" not in spawned
    # Full permission passthrough + no phone-home, exactly like a chat spawn.
    assert json.loads(spawned["ALKERA_PERMISSION"])["*"] == "ask"
    for flag in (
        "ALKERA_DISABLE_MODELS_FETCH",
        "ALKERA_DISABLE_SHARE",
        "ALKERA_DISABLE_AUTOUPDATE",
        "ALKERA_DISABLE_LSP_DOWNLOAD",
        "ALKERA_DISABLE_CHANNEL_DB",
        "ALKERA_DISABLE_NPM_INSTALL",
        "ALKERA_PURE",
    ):
        assert spawned[flag] == "true", flag


@pytest.mark.asyncio
async def test_probe_password_is_fresh_per_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two probes must never share a secret (a leaked one would then unlock a
    later daemon's probe as well)."""
    seen: list[str] = []
    for run in range(2):
        prewarm._reset_for_tests()
        monkeypatch.setenv("ALKERA_HARNESS_PREWARM", "1")
        env_file = (tmp_path / f"env-{run}.json").as_posix()
        _fake_agent(
            monkeypatch,
            "import json, os\n"
            f"open(r'{env_file}', 'w').write(json.dumps({_DUMP}))\n"
            "open(os.environ['ALKERA_LISTEN_FILE'], 'w').write('http://127.0.0.1:1')\n",
        )
        prewarm.start_prewarm_in_background()
        await prewarm.wait_for_prewarm(timeout_seconds=30)
        seen.append(_read_json(env_file)["secrets"]["ALKERA_SERVER_PASSWORD"])

    assert seen[0] != seen[1]


def test_prewarm_env_pass_through_keeps_listen_file_private() -> None:
    """The probe's ALKERA_LISTEN_FILE must not leak into THIS process env
    (regression guard: the run builds a copied env, never mutates os.environ)."""
    assert "ALKERA_LISTEN_FILE" not in os.environ
