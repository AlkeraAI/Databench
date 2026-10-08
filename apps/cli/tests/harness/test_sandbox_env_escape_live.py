"""A real launch's host steps, run twice over an environment the agent tampered with.

The steps that make a chat's default environment run on the host as the
chat's uid, outside any gVisor container. Between two spawns the agent can
replace the environment's ``bin/python`` and drop a ``.pth`` into its
``site-packages``; this runs a composed launch's host steps for real (the uid
drop, the ownership, ``uv venv``, pip's seed, the relocation), plants both,
runs them again, and proves neither ran: the marker they write is never
written on the host. It needs Linux, root (the uid drop), ``setpriv``,
``setfacl``, ``useradd``, ``uv`` and the box's managed interpreter
(``ALKERA_SANDBOX_PYTHON``), so it runs in the ``live`` tier and skips
everywhere else.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alkera_cli.harness import sandbox as sb

pytestmark = [pytest.mark.live]


def _skip_unless_root_linux() -> sb.SandboxSettings:
    if sys.platform != "linux":
        pytest.skip("a real uid drop needs a Linux host")
    if os.geteuid() != 0:
        pytest.skip("a real uid drop needs root (the daemon runs as root on a box)")
    for tool in ("setpriv", "setfacl", "useradd", "userdel", "uv"):
        if shutil.which(tool) is None:
            pytest.skip(f"no {tool} on this host")
    settings = sb.SandboxSettings.from_env()
    if not Path(settings.python_home, "bin", "python3").is_file():
        pytest.skip(f"no managed interpreter at {settings.python_home}")
    return settings


@pytest.fixture
def chat() -> Iterator[tuple[str, int, sb.SandboxSettings]]:
    settings = _skip_unless_root_linux()
    chat_id = f"live{uuid.uuid4().hex[:12]}"
    uid = sb.ensure_chat_uid(chat_id)
    try:
        yield chat_id, uid, settings
    finally:
        subprocess.run(["userdel", sb.chat_user(chat_id)], capture_output=True, check=False)


def test_a_planted_python_and_pth_never_run_on_the_host(
    tmp_path: Path, chat: tuple[str, int, sb.SandboxSettings]
) -> None:
    chat_id, uid, settings = chat
    # The ownership steps lend the uid a way through every ancestor, as on a box.
    root = tmp_path / "work"
    try:
        chat_dir = root / ".alkera" / "chats" / chat_id
        runtime = chat_dir / sb.RUNTIME_STATE_SUBDIR
        config = root / "home" / "harness" / chat_id
        for path in (chat_dir / "sandbox", runtime, config / "config", config / "state"):
            path.mkdir(parents=True)
        env = sb.default_env_path(runtime)
        spec = sb.SandboxSpec(
            chat_id=chat_id,
            folder=chat_dir / "sandbox",
            uid=uid,
            vcpu=1,
            memory_mb=512,
            home="/home/alkera",
            mode="none",
            cgroup="none",
            binds=sb.chat_binds(runtime_dir=runtime, agent_config_root=config),
            private_dirs=(chat_dir, config),
            default_env=env,
            python_home=settings.python_home,
            uv=shutil.which("uv") or "uv",
            setpriv=shutil.which("setpriv") or "setpriv",
        )
        launch = sb.NoneRuntime().compose_launch(spec, ("/bin/true",))
        sb.run_steps(launch.before)
        assert (env / "bin" / "pip").is_file(), "the first spawn seeds pip"

        marker = runtime / "envs" / "escaped"
        version = subprocess.run(
            [
                f"{settings.python_home}/bin/python3",
                "-I",
                "-S",
                "-c",
                "import sys; print('python%d.%d' % sys.version_info[:2])",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        site = env / "lib" / version / "site-packages"
        (site / "zz_planted.pth").write_text(
            f"import os; open({str(marker)!r}, 'a').write('pth %d ' % os.getuid())\n"
        )
        python = env / "bin" / "python"
        python.unlink()
        python.write_text(f"#!/bin/sh\nprintf 'python ' >> {marker}\n")
        python.chmod(0o755)
        for dist in [*site.glob("pip-*.dist-info"), site / "pip"]:
            shutil.rmtree(dist)
        os.chown(site / "zz_planted.pth", uid, uid)
        os.chown(python, uid, uid)

        sb.run_steps(launch.before)
        sb.run_steps(launch.after_exit)

        assert not marker.exists(), marker.read_text()
        assert (env / "bin" / "pip").is_file(), "pip is seeded again"
        assert list(site.glob("pip-*.dist-info"))
        assert (env / "bin" / "pip").stat().st_uid == uid, "seeded as the chat's uid"
    finally:
        shutil.rmtree(root, ignore_errors=True)
