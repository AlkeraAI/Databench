"""The shell default the spawned agent runs under, on the platform where the
shell is not ours.

On POSIX the native shell is dropped and the model calls Alkera's parent-hosted
one, which reads `DEFAULT_TIMEOUT_MS` directly. On Windows there is no parent
shell to swap in, so the agent runs opencode's own — whose default is two
minutes unless the environment raises it. Ours is no wall clock at all, which
the vendor flag can only spell as the largest count it accepts (about 24.8
days); without it the same command is killed at two minutes on Windows and runs
to the end everywhere else.
"""

from __future__ import annotations

from pathlib import Path

from alkera_cli.harness.adapter import SessionConfig
from alkera_cli.harness.adapters.opencode_http import OpencodeHttpAdapter
from alkera_cli.harness.event_bus import EventBus
from alkera_cli.harness.opencode_binary import ResolvedOpencodeBinary
from alkera_cli.plugins.plugin_base.bash_ids import DEFAULT_TIMEOUT_MS

_FLAG = "ALKERA_BASH_DEFAULT_TIMEOUT_MS"


def _adapter(tmp_path: Path) -> OpencodeHttpAdapter:
    config = SessionConfig(
        session_id="sid",
        project_dir=tmp_path,
        chat_dir=tmp_path / "chat",
    )
    binary = ResolvedOpencodeBinary(
        path=Path("/usr/bin/true"), prefix_args=(), source="path", ripgrep_path=None
    )
    adapter = OpencodeHttpAdapter(config, binary=binary, event_bus=EventBus())
    adapter._harness_dir.mkdir(parents=True, exist_ok=True)
    return adapter


def test_the_native_shell_is_given_no_wall_clock(tmp_path: Path) -> None:
    assert DEFAULT_TIMEOUT_MS is None
    env = _adapter(tmp_path)._build_env("pw").env
    assert env[_FLAG] == str(2_147_483_647)


def test_the_agents_own_commands_do_not_inherit_the_flag(tmp_path: Path) -> None:
    """The shell hands agent-run commands the user's original environment back;
    a harness knob leaking into it would follow every command the agent runs."""
    import json

    from alkera_cli.harness.adapters.opencode_http import SHELL_ENV_RESTORE_VAR

    env = _adapter(tmp_path)._build_env("pw").env
    restore = json.loads(env[SHELL_ENV_RESTORE_VAR])
    assert restore[_FLAG] is None
