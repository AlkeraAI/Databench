"""Node boot profiles: every way a node differs by provider, declared once.

The renderer and the provisioning read ``BOOT_PROFILES`` and never branch on a
provider's name. These tests pin what a profile decides (the sandbox a node
runs, the firewall it must load, that its script is valid bash) and that the
API's provision vocabulary is exactly the providers a node can be booted for.
"""

from __future__ import annotations

import os
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, get_args

import pytest
from alkera_core.compute import bootstrap
from alkera_core.compute.bootstrap import (
    BOOT_PROFILES,
    BOOTSTRAP_PROVIDERS,
    BootstrapError,
    BootstrapSpec,
    boot_profile,
    host_firewall_policy,
    render_bootstrap,
    work_root_lines,
)
from alkera_core.compute.box_contract import START_COMMAND, BootstrapPlan, StartMode
from alkera_core.compute.box_layout import ORGS_DIR, ORGS_ROOT_MODE, WORK_ROOT_MODE
from alkera_core.compute.provider import COMPUTE_PROVIDER_KINDS, provider_traits
from alkera_core.schemas.compute_machines import ProvisionProvider

#: What every render here installs and starts, unless a test says otherwise.
PLAN = BootstrapPlan(version="1.4.2", start_mode=StartMode.SUPERVISE)

#: A node boots on Linux and every check here runs its script through bash; a
#: Windows runner has neither bash semantics nor POSIX signals.
needs_bash = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None, reason="needs a POSIX bash"
)


def _spec(provider: str, **overrides: Any) -> BootstrapSpec:
    base: dict[str, Any] = {
        "provider": provider,
        "allocation_id": "11111111-2222-3333-4444-555555555555",
        "machine_name": "box-1",
        "type_code": "local",
        "tenancy": "pool",
        "api_url": "http://host.docker.internal:38320",
        "release_base_url": "https://releases.example.test",
        "credential_secret": "alkera/test/node/x",
        "region": "us-east-1",
        "sandbox_mode": boot_profile(provider).sandbox_mode("gvisor"),
    }
    base.update(overrides)
    return BootstrapSpec(**base)


def _boot_steps(script: str) -> str:
    """The rendered script without the sandbox prerequisites it embeds, which
    name the metadata address (to block it) and probe for systemd."""
    begin = script.index(f"<<'{bootstrap.SANDBOX_PREREQS_DELIMITER}'")
    end = script.index(f"\n{bootstrap.SANDBOX_PREREQS_DELIMITER}\n", begin)
    return script[:begin] + script[end:]


def test_the_console_provisions_exactly_the_providers_a_node_can_boot_for() -> None:
    """A provider the console offers with no boot profile would fail at the
    provision; one with a profile the console cannot pick is dead code, unless
    its machines are not provisioned from the catalog (a host an org attaches
    boots through its own add flow)."""
    provisioned = {k for k in BOOTSTRAP_PROVIDERS if provider_traits(k).catalog_provisioned}
    assert set(get_args(ProvisionProvider)) == provisioned
    assert set(BOOTSTRAP_PROVIDERS) <= set(COMPUTE_PROVIDER_KINDS)


@pytest.mark.parametrize("provider", sorted(BOOT_PROFILES))
@needs_bash
def test_every_provider_renders_a_valid_bash_script(provider: str) -> None:
    script = render_bootstrap(_spec(provider), PLAN)
    checked = subprocess.run(
        ["bash", "-n"], input=script, text=True, capture_output=True, check=False
    )
    assert checked.returncode == 0, checked.stderr


def test_an_unknown_provider_has_no_bootstrap() -> None:
    spec = _spec("ec2")
    with pytest.raises(BootstrapError, match="no bootstrap for provider 'nimbus'"):
        render_bootstrap(BootstrapSpec(**{**spec.__dict__, "provider": "nimbus"}), PLAN)


@pytest.mark.parametrize(
    ("provider", "requested", "runs"),
    [
        pytest.param("ec2", "gvisor", "gvisor", id="ec2-follows-the-deployment-gvisor"),
        pytest.param("ec2", "none", "none", id="ec2-follows-the-deployment-none"),
        pytest.param("runpod", "gvisor", "none", id="runpod-cannot-nest-a-kernel"),
        pytest.param("runpod", "none", "none", id="runpod-none"),
        pytest.param("localdev", "none", "gvisor", id="localdev-always-gvisor"),
        pytest.param("localdev", "gvisor", "gvisor", id="localdev-gvisor"),
    ],
)
def test_each_provider_runs_the_sandbox_it_can_enforce(
    provider: str, requested: str, runs: str
) -> None:
    assert boot_profile(provider).sandbox_mode(requested) == runs


def test_a_local_box_will_not_render_to_run_chats_open() -> None:
    with pytest.raises(BootstrapError, match="under gVisor"):
        render_bootstrap(_spec("localdev", sandbox_mode="none"), PLAN)


@pytest.mark.parametrize(
    ("provider", "mode", "policy"),
    [
        pytest.param("runpod", "none", "optional", id="runpod-container-without-cap-net-admin"),
        pytest.param("ec2", "none", "required", id="ec2-firewall-is-the-metadata-boundary"),
        pytest.param("ec2", "gvisor", "required", id="ec2-gvisor"),
        pytest.param("localdev", "gvisor", "required", id="localdev-gvisor-needs-it"),
    ],
)
def test_the_host_firewall_is_optional_only_where_there_is_nothing_to_fence(
    provider: str, mode: str, policy: str
) -> None:
    assert host_firewall_policy(provider, mode) == policy


def test_a_secrets_manager_provider_without_a_named_secret_is_refused() -> None:
    with pytest.raises(BootstrapError, match="named secret"):
        render_bootstrap(_spec("ec2", credential_secret=""), PLAN)


@pytest.mark.parametrize(
    "provider", sorted(p for p, prof in BOOT_PROFILES.items() if prof.secrets == "environment")
)
def test_an_environment_secrets_node_never_asks_a_cloud_service(provider: str) -> None:
    """A RunPod pod and a local box have no metadata service or Secrets Manager;
    a boot that asked one would hang on a dead address."""
    steps = _boot_steps(render_bootstrap(_spec(provider), PLAN))
    assert "169.254.169.254" not in steps
    assert "secretsmanager" not in steps


@pytest.mark.parametrize("provider", ["ssh", "ec2", "runpod"])
def test_an_installed_build_is_linked_onto_path(provider: str) -> None:
    """For a person on the host; what the node spawns never relies on it."""
    steps = _boot_steps(render_bootstrap(_spec(provider), PLAN))
    assert 'ln -sfn "$BOX_ROOT/alkera.dist/alkera" /usr/local/bin/alkera;' in steps


needs_posix_shell = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None, reason="needs a POSIX bash and modes"
)


@needs_posix_shell
def test_the_work_root_and_orgs_root_let_every_org_through_whatever_the_umask(
    tmp_path: Path,
) -> None:
    """An attached host's launch runs the bootstrap under umask 077, which made
    the work root 0700 and stranded every org's worker (its own uid) at it."""
    work = tmp_path / "alkera-work"
    preamble = ["set -euo pipefail", "umask 077", f"DATA_MOUNT={work}", 'mkdir -p "$DATA_MOUNT"']
    script = "\n".join([*preamble, *work_root_lines()])
    subprocess.run(["bash", "-c", script], check=True, timeout=30)
    assert (WORK_ROOT_MODE, ORGS_ROOT_MODE) == (0o711, 0o711)
    assert stat.S_IMODE(work.stat().st_mode) == 0o711
    assert stat.S_IMODE((work / ORGS_DIR).stat().st_mode) == 0o711


@pytest.mark.parametrize("provider", BOOTSTRAP_PROVIDERS)
def test_every_bootstrap_sets_the_work_root_modes_after_any_mount(provider: str) -> None:
    steps = _boot_steps(render_bootstrap(_spec(provider), PLAN))
    lines = work_root_lines()
    at = steps.index("\n".join(lines))
    mounted = steps.find('mount "$DATA_MOUNT"')
    assert mounted < at
    assert 'chmod 0711 "$DATA_MOUNT"' in lines


def test_a_local_box_runs_the_daemon_from_the_mounted_source_not_a_release() -> None:
    steps = _boot_steps(render_bootstrap(_spec("localdev"), PLAN))
    assert "uv sync --frozen" in steps
    assert "release.tar.gz" not in steps
    assert "systemctl" not in steps


# -- the local box's supervisor, actually run -------------------------------------


@pytest.fixture
def supervised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, dict[str, str], Path]:
    """The rendered supervisor block, with the daemon replaced by a script that
    records how it was started and stopped."""
    daemon = tmp_path / "alkera"
    log = tmp_path / "daemon.log"
    daemon.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "started supervised=$ALKERA_DAEMON_SUPERVISED umask=$(umask)" >>{log}\n'
        'final() { [ -f "$ALKERA_DAEMON_FINAL_STOP_FILE" ] && echo yes || echo no; }\n'
        f"trap 'echo \"got TERM final=$(final)\" >>{log}; exit 0' TERM\n"
        "while true; do sleep 0.1; done\n"
    )
    daemon.chmod(0o755)
    final = tmp_path / "final-stop"
    monkeypatch.setattr(bootstrap, "LOCALDEV_DAEMON", str(daemon))
    monkeypatch.setattr(bootstrap, "FINAL_STOP_FILE", str(final))
    ready = tmp_path / "sandbox.ready"
    ready.touch()
    box_root = tmp_path / "box"
    box_root.mkdir()
    (box_root / "node.env").write_text("ALKERA_API_URL=http://x\n")
    # The build brings nothing newer to apply before the start.
    (box_root / bootstrap.RESTAGE_PREREQS).write_text("#!/bin/sh\nexit 0\n")
    # The boot script reaches the supervisor under umask 077.
    script = "umask 077\n" + "\n".join(bootstrap._container_supervisor_lines()) + "\n"
    env = {
        **os.environ,
        "SANDBOX_READY": str(ready),
        "BOX_ROOT": str(box_root),
        "DATA_MOUNT": str(tmp_path),
    }
    return script, env, log


def _wait_for(path: Path, text: str, deadline_s: float = 10.0) -> None:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        if path.exists() and text in path.read_text():
            return
        time.sleep(0.05)
    raise AssertionError(
        f"{text!r} never appeared in {path}: {path.read_text() if path.exists() else ''}"
    )


@needs_bash
def test_docker_stop_hands_the_chats_on_before_the_daemon_is_signalled(
    supervised: tuple[str, dict[str, str], Path],
) -> None:
    script, env, log = supervised
    proc = subprocess.Popen(["bash", "-c", script], env=env)
    try:
        _wait_for(log, "started supervised=1")
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
    # The daemon saw the final-stop file when its TERM arrived: a full hand-back.
    assert "got TERM final=yes" in log.read_text()


@needs_bash
def test_the_supervisor_refuses_to_start_the_daemon_without_a_ready_sandbox(
    supervised: tuple[str, dict[str, str], Path], tmp_path: Path
) -> None:
    script, env, log = supervised
    env["SANDBOX_READY"] = str(tmp_path / "never-written")
    done = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True, timeout=10
    )
    assert done.returncode == 1
    assert "sandbox is not ready" in done.stdout
    assert not log.exists()


@needs_bash
def test_the_daemon_runs_under_an_owner_only_umask(
    supervised: tuple[str, dict[str, str], Path],
) -> None:
    """The daemon holds every org's state beside agents that run as other
    uids, so what it writes is its own unless it names a mode. runsc, whose
    mountpoints take its umask, is started under 022 by the launch itself
    (``RUNSC_UMASK``), never by loosening the daemon's."""
    script, env, log = supervised
    proc = subprocess.Popen(["bash", "-c", script], env=env)
    try:
        _wait_for(log, "started supervised=1")
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
    assert "umask=0077" in log.read_text()


def test_a_source_installed_daemon_is_readable_by_an_org_worker() -> None:
    """The boot runs under umask 077; a venv made under it is root's alone, and
    an org worker (another uid) could not start the daemon it runs."""
    script = render_bootstrap(_spec("localdev"), PLAN)
    synced = script.index(
        '(umask 022 && uv sync --frozen --no-dev --package alkera-cli --project "$SOURCE_ROOT")'
    )
    assert script.index('chmod -R go+rX "$UV_PROJECT_ENVIRONMENT"') > synced
    assert script.index('chmod -R go+rX "$UV_PYTHON_INSTALL_DIR"') > synced


def test_a_developer_box_runs_the_supervisor_with_a_worker_per_org() -> None:
    script = render_bootstrap(_spec("localdev"), PLAN)
    assert f"{bootstrap.LOCALDEV_DAEMON} cloud-mirror run &" in script
    assert "ALKERA_BOX_START_MODE=supervise" in script.splitlines()
    assert "cloud-mirror supervise" not in script


_RECORDING_BOX = '#!/bin/sh\necho "$*" >>"$BOX_ROOT/started"\nkill -TERM $PPID\n'
"""An installed box build that records the command it was started with and
ends the loop that started it."""


@needs_bash
@pytest.mark.parametrize("provider", ["ec2", "runpod"])
@pytest.mark.parametrize("mode", list(StartMode))
def test_a_provisioned_box_starts_with_the_one_command_and_its_mode_in_the_env(
    provider: str, mode: StartMode, tmp_path: Path
) -> None:
    """The unit and the foreground loop of a host with no init both start the
    one command every build has, whatever the mode, and nothing else: the
    mode rides in the node's environment, so a build swapped in later (or
    rolled back to) is never asked for a command it lacks. No probe of the
    installed build is left in the script."""
    command = START_COMMAND
    script = render_bootstrap(_spec(provider), BootstrapPlan(version="1.4.2", start_mode=mode))
    assert f"ALKERA_BOX_START_MODE={mode.value}" in script.splitlines()
    binary = tmp_path / "alkera.dist" / "alkera"
    binary.parent.mkdir()
    (tmp_path / bootstrap.RESTAGE_PREREQS).write_text("#!/bin/sh\nexit 0\n")
    binary.write_text(_RECORDING_BOX)
    binary.chmod(0o755)
    lines = script.splitlines()
    exec_start = next(line for line in lines if line.startswith("ExecStart="))
    loop = next(line for line in lines if "while true; do" in line and "alkera.dist" in line)
    env = {"PATH": os.environ["PATH"], "BOX_ROOT": str(tmp_path)}

    # The unit is written through an expanding heredoc, exactly as here.
    unit = subprocess.run(
        ["bash", "-c", f"cat <<UNIT\n{exec_start}\nUNIT\n"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert f"ExecStart={binary} {command}" in unit.stdout.splitlines()

    # The foreground loop of a host with no init, run as written.
    subprocess.run(["bash", "-c", loop], env=env, timeout=30, check=False)
    assert (tmp_path / "started").read_text().splitlines() == [command]
    assert "--help" not in script


def test_the_data_volume_carries_project_quotas() -> None:
    """Each org's root is a quota project, so the volume is formatted with the
    feature and mounted with it enforced."""
    script = render_bootstrap(_spec("ec2"), PLAN)
    assert 'mkfs.ext4 -q -O quota,project "$DATA_DEV"' in script
    assert "ext4 defaults,nofail,prjquota 0 2" in script


@pytest.mark.parametrize("provider", ["ec2", "runpod"])
def test_the_released_build_is_installed_read_only_and_its_harness_runs_in_place(
    provider: str,
) -> None:
    """Every org's worker runs the one build; none may change it, and none
    extracts the harness into a cache of its own at run time."""
    script = render_bootstrap(_spec(provider), PLAN)
    installed = script.index('mv "$work/alkera.dist" "$BOX_ROOT/alkera.dist"')
    assert script.index('chown -R root:root "$BOX_ROOT/alkera.dist"') > installed
    assert script.index('chmod -R u=rwX,go=rX "$BOX_ROOT/alkera.dist"') > installed
    env = script[script.index("cat >\"$BOX_ROOT/node.env\" <<'ENV'") :]
    env = env[: env.index("\nENV\n")]
    assert f"ALKERA_OPENCODE_BIN={bootstrap.INSTALLED_HARNESS}" in env
    assert f"ALKERA_RIPGREP_BIN={bootstrap.INSTALLED_RIPGREP}" in env
    assert bootstrap.INSTALLED_HARNESS.startswith("/opt/alkera/alkera.dist/runtime/")


def test_a_source_installed_box_names_no_released_harness() -> None:
    script = render_bootstrap(_spec("localdev"), PLAN)
    assert bootstrap.INSTALLED_HARNESS not in script
