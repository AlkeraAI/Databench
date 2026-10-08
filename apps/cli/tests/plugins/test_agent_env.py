"""A bounded session's shell sees an environment worth nothing to steal.

The box's own bearer, its ``ALKERA_HOME``, the leased database and the cloud
provider it came from all sit in the daemon's environment, and the agent's
shell inherited every one of them. The table in ``agent_env`` is the one place
those names live; these cases prove it through a real shell — ``env``,
``printenv`` and, on Linux, ``/proc/self/environ`` — not by reading the table
back.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from alkera_cli.files.chat_fs import ChatTree
from alkera_cli.harness.adapter import SessionConfig
from alkera_cli.harness.adapters.opencode_http import (
    SHELL_ENV_RESTORE_VAR,
    OpencodeHttpAdapter,
    build_agent_env,
)
from alkera_cli.harness.event_bus import EventBus
from alkera_cli.harness.opencode_binary import ResolvedOpencodeBinary
from alkera_cli.plugins.plugin_base.agent_env import (
    FORBIDDEN_ENV_NAMES,
    is_forbidden_env_name,
)
from alkera_cli.plugins.plugin_base.agent_tree import SpillTarget
from alkera_cli.plugins.plugin_base.bash_exec import build_child_env, run_command, select_shell

#: A daemon's environment as a box would have it: one value per forbidden
#: family, each carrying a marker that a leak would print.
BOX_ENV = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "LANG": "C.UTF-8",
    "HOME": "/root",
    "ALKERA_HOME": "/opt/alkera-home",
    "ALKERA_SERVER_PASSWORD": "LEAK-server-password",
    "ALKERA_CONFIG_CONTENT": "LEAK-config",
    "ALKERA_MACHINE_TOKEN": "LEAK-machine",
    "ALKERA_CLOUD_REST_TIMEOUT_SECONDS": "30",
    "DATABASE_URL": "postgresql://u:LEAK-db@h/db",
    "PGPASSWORD": "LEAK-pg",
    "JWT_SECRET": "LEAK-jwt",
    "SMTP_PASSWORD": "LEAK-smtp",
    "FILES_SECRET_KEY": "LEAK-files",
    "ANTHROPIC_API_KEY": "LEAK-anthropic",
    "AWS_SECRET_ACCESS_KEY": "LEAK-aws",
    "AWS_REGION": "us-east-1",
    "RUNPOD_API_KEY": "LEAK-runpod",
    "GITHUB_TOKEN": "LEAK-gh",
    "SOME_VENDOR_ACCESS_TOKEN": "LEAK-vendor",
    "MY_TOOL_PRIVATE_KEY_PATH": "LEAK-key",
    "EDITOR": "vim",
    "PAGER": "less",
    "COLORTERM": "truecolor",
    "TZ": "UTC",
    # The box's account of itself: how it sandboxes, what it serves, where it
    # keeps its binaries. Not secrets, but a map of the box the agent sits in.
    "ALKERA_SANDBOX_ROOTFS": "LEAK-rootfs",
    "ALKERA_SANDBOX_MODE": "LEAK-gvisor",
    "SANDBOX_POOL_VCPU": "LEAK-vcpu",
    "ALKERA_LOCALDEV_CONTAINER": "LEAK-box-name",
    "ALKERA_DAEMON_SUPERVISED": "LEAK-supervised",
    "ALKERA_ALLOCATION_ID": "LEAK-allocation",
    "ALKERA_RELEASE_VERSION": "LEAK-release",
    "ALKERA_OPENCODE_BIN": "LEAK-opencode-bin",
    "ALKERA_RIPGREP_BIN": "LEAK-rg-bin",
}


def _leaks(text: str) -> list[str]:
    return sorted({word for word in text.replace("\0", "\n").split() if "LEAK-" in word})


def test_the_table_names_every_name_the_probes_read() -> None:
    for name in ("ALKERA_SERVER_PASSWORD", "ALKERA_CONFIG_CONTENT", "ALKERA_HOME", "DATABASE_URL"):
        assert name in FORBIDDEN_ENV_NAMES


@pytest.mark.parametrize(
    ("name", "forbidden"),
    [
        pytest.param("ALKERA_SERVER_PASSWORD", True, id="exact"),
        pytest.param("ALKERA_CLOUD_ANYTHING", True, id="prefix"),
        pytest.param("SOME_VENDOR_ACCESS_TOKEN", True, id="marker-token"),
        pytest.param("x_apikey", True, id="marker-lowercase"),
        pytest.param("PATH", False, id="path-kept"),
        pytest.param("COLORTERM", False, id="a-plain-var"),
        pytest.param("EDITOR", True, id="a-program-the-tools-run"),
        pytest.param("LESSOPEN", True, id="a-preprocessor-a-pager-runs"),
        pytest.param("AWS_REGION", True, id="the-whole-aws-prefix"),
        pytest.param("ALKERA_API_URL", False, id="the-api-url-is-not-a-secret"),
        pytest.param("TOKENIZER_PARALLELISM", True, id="token-inside-a-word-errs-closed"),
    ],
)
def test_is_forbidden_env_name(name: str, forbidden: bool) -> None:
    assert is_forbidden_env_name(name) is forbidden


def test_a_bounded_child_env_carries_none_of_the_names(tmp_path: Path) -> None:
    box = {**BOX_ENV, "HOSTNAME": "ip-10-0-0-7", "OLDPWD": "/opt/alkera-home", "SHLVL": "2"}
    env = build_child_env(box, cwd=str(tmp_path), home=str(tmp_path))
    assert _leaks(" ".join(f"{k}={v}" for k, v in env.items())) == []
    assert "ALKERA_HOME" not in env
    assert env["HOME"] == str(tmp_path)
    assert env["TMPDIR"] == str(tmp_path)
    assert env["PWD"] == str(tmp_path)
    # What only describes the box goes too, as it does for the agent server.
    assert "HOSTNAME" not in env and "OLDPWD" not in env and "SHLVL" not in env
    # The control: a local session keeps its own shell's account of itself.
    local = build_child_env(box, cwd=str(tmp_path))
    assert local["HOSTNAME"] == "ip-10-0-0-7" and local["SHLVL"] == "2"


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("WAREHOUSE_DSN", id="a-dsn-no-table-names"),
        pytest.param("OPS_WEBHOOK_URL", id="a-webhook-url"),
        pytest.param("PYTHONPATH", id="an-import-path-of-the-daemon"),
        pytest.param("ALKERA_API_URL", id="the-boxs-api-url"),
        pytest.param("HTTPS_PROXY", id="the-boxs-proxy"),
    ],
)
def test_a_bounded_shell_inherits_no_name_it_was_not_given(tmp_path: Path, name: str) -> None:
    """The tables name what is known to be secret. A box can hold one under a name
    nobody listed, so the bounded shell is built from what a command needs, and a
    name outside that is withheld whatever it is called. A local shell keeps it."""
    box = {**BOX_ENV, name: "LEAK-unlisted"}
    bounded = build_child_env(box, cwd=str(tmp_path), home=str(tmp_path))
    assert name not in bounded
    assert build_child_env(box, cwd=str(tmp_path))[name] == "LEAK-unlisted"


def test_a_bounded_shell_keeps_what_a_command_needs(tmp_path: Path) -> None:
    box = {**BOX_ENV, "LC_ALL": "C.UTF-8", "TERM": "xterm-256color", "USER": "alkera"}
    env = build_child_env(box, cwd=str(tmp_path), home=str(tmp_path))
    assert {k: env[k] for k in ("PATH", "LANG", "LC_ALL", "TERM", "TZ", "USER")} == {
        "PATH": BOX_ENV["PATH"],
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TERM": "xterm-256color",
        "TZ": "UTC",
        "USER": "alkera",
    }


def test_a_local_child_env_still_passes_the_users_own_vars(tmp_path: Path) -> None:
    """The control: a local session IS the user's shell, so their ``GITHUB_TOKEN``
    and ``AWS_*`` survive as before; only the harness's own names go."""
    env = build_child_env(BOX_ENV, cwd=str(tmp_path))
    assert env["GITHUB_TOKEN"] == "LEAK-gh"
    assert env["AWS_SECRET_ACCESS_KEY"] == "LEAK-aws"
    assert "ALKERA_SERVER_PASSWORD" not in env
    assert env["ALKERA_HOME"] == "/opt/alkera-home"
    # A person's own binary overrides are theirs to keep.
    assert env["ALKERA_OPENCODE_BIN"] == "LEAK-opencode-bin"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell")
@pytest.mark.parametrize(
    "command",
    [
        pytest.param("env", id="env"),
        pytest.param("printenv", id="printenv"),
        pytest.param(
            "cat /proc/self/environ",
            id="proc-self-environ",
            marks=pytest.mark.skipif(not Path("/proc/self/environ").exists(), reason="no procfs"),
        ),
    ],
)
async def test_the_shell_itself_prints_none_of_the_names(tmp_path: Path, command: str) -> None:
    env = build_child_env(BOX_ENV, cwd=str(tmp_path), home=str(tmp_path))
    result = await run_command(
        command,
        cwd=str(tmp_path),
        env=env,
        shell=select_shell(),
        timeout_ms=10_000,
        make_spill=lambda: SpillTarget(ChatTree(tmp_path), "spill.txt"),
    )
    assert result.exit_code == 0
    assert _leaks(result.output) == []
    assert "COLORTERM=truecolor" in result.output
    assert "PAGER=" not in result.output and "EDITOR=" not in result.output


def test_a_fenced_harness_process_inherits_none_of_the_boxs_secrets(tmp_path: Path) -> None:
    """opencode is told where its own secrets file is; everything the BOX
    holds — the bearer home, the database, the providers — is taken out first."""
    env = build_agent_env(
        data_home=str(tmp_path / "data"),
        config_home=str(tmp_path / "config"),
        listen_file=str(tmp_path / "listen"),
        secrets_file=str(tmp_path / "launch.json"),
        base_env=BOX_ENV,
        fenced=True,
    )
    assert env["ALKERA_SECRETS_FILE"] == str(tmp_path / "launch.json")
    assert env["ALKERA_LISTEN_FILE"] == str(tmp_path / "listen")
    assert "ALKERA_HOME" not in env
    assert _leaks(" ".join(env.values())) == []


def test_an_unfenced_harness_process_keeps_the_users_environment(tmp_path: Path) -> None:
    env = build_agent_env(
        data_home=str(tmp_path / "data"),
        config_home=str(tmp_path / "config"),
        listen_file=str(tmp_path / "listen"),
        secrets_file=str(tmp_path / "launch.json"),
        base_env=BOX_ENV,
    )
    assert env["GITHUB_TOKEN"] == "LEAK-gh"


def _fenced_agent_env(tmp_path: Path, box: dict[str, str]) -> dict[str, str]:
    return build_agent_env(
        data_home=str(tmp_path / "data"),
        config_home=str(tmp_path / "config"),
        listen_file=str(tmp_path / "listen"),
        secrets_file=str(tmp_path / "launch.json"),
        base_env=box,
        fenced=True,
    )


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("WAREHOUSE_DSN", id="a-dsn-no-table-names"),
        pytest.param("OPS_WEBHOOK_URL", id="a-webhook-url"),
        pytest.param("PYTHONPATH", id="an-import-path-of-the-daemon"),
        pytest.param("ALKERA_API_URL", id="the-boxs-api-url"),
        pytest.param("NODE_OPTIONS", id="a-runtime-option"),
    ],
)
def test_a_fenced_harness_process_inherits_no_name_it_was_not_given(
    tmp_path: Path, name: str
) -> None:
    """The agent server's environment is readable by the model's shell
    (``/proc/<pid>/environ``), so it starts from what the server needs, and a
    name a box carries that nobody listed is withheld whatever it is called. An
    unfenced (local) process keeps it."""
    box = {**BOX_ENV, name: "LEAK-unlisted"}
    assert name not in _fenced_agent_env(tmp_path, box)
    unfenced = build_agent_env(
        data_home=str(tmp_path / "data"),
        config_home=str(tmp_path / "config"),
        listen_file=str(tmp_path / "listen"),
        secrets_file=str(tmp_path / "launch.json"),
        base_env=box,
    )
    assert unfenced[name] == "LEAK-unlisted"


def test_a_fenced_harness_process_keeps_what_the_server_needs(tmp_path: Path) -> None:
    """Its home, temp directory and locale, and what it needs to reach the model
    gateway itself: a proxy and a certificate bundle."""
    box = {
        **BOX_ENV,
        "TMPDIR": "/var/tmp",
        "LC_ALL": "C.UTF-8",
        "HTTPS_PROXY": "http://proxy.example.com:3128",
        "no_proxy": "localhost",
        "SSL_CERT_FILE": "/etc/ssl/certs/ca.pem",
    }
    env = _fenced_agent_env(tmp_path, box)
    kept = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "TZ", "HTTPS_PROXY", "no_proxy")
    assert {k: env[k] for k in kept} == {
        "PATH": BOX_ENV["PATH"],
        "HOME": "/root",
        "TMPDIR": "/var/tmp",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
        "HTTPS_PROXY": "http://proxy.example.com:3128",
        "no_proxy": "localhost",
    }
    assert env["SSL_CERT_FILE"] == "/etc/ssl/certs/ca.pem"


def _adapter(tmp_path: Path, *, fenced: bool) -> OpencodeHttpAdapter:
    config = SessionConfig(
        session_id="our-sid",
        project_dir=tmp_path,
        chat_dir=tmp_path / "chat",
        fenced=fenced,
    )
    binary = ResolvedOpencodeBinary(path=Path("/usr/bin/true"), prefix_args=(), source="staged")
    return OpencodeHttpAdapter(config, binary=binary, event_bus=EventBus())


def test_a_fenced_agents_restore_map_spells_no_value_of_the_box(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shell-restore map is the reverse diff from the agent's environment to
    its original. Taken against the daemon's own environment it named, by value,
    every name the fence took out; a ``none`` box hands it to the agent as is."""
    for name, value in {**BOX_ENV, "WAREHOUSE_DSN": "LEAK-unlisted"}.items():
        monkeypatch.setenv(name, value)
    env = _adapter(tmp_path, fenced=True)._build_env("pw").env
    restore = json.loads(env[SHELL_ENV_RESTORE_VAR])
    assert _leaks(" ".join(v for v in restore.values() if v is not None)) == []
    assert _leaks(" ".join(env.values())) == []


def test_an_unfenced_agents_restore_map_gives_the_users_environment_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: a local agent's commands get the person's own environment
    back, their tokens included."""
    monkeypatch.setenv("GITHUB_TOKEN", "LEAK-gh")
    monkeypatch.setenv("ALKERA_SERVER_PASSWORD", "LEAK-server-password")
    env = _adapter(tmp_path, fenced=False)._build_env("pw").env
    restore = json.loads(env[SHELL_ENV_RESTORE_VAR])
    assert env["GITHUB_TOKEN"] == "LEAK-gh"
    assert restore["ALKERA_SERVER_PASSWORD"] == "LEAK-server-password"
